"""Background LinkedIn/Naukri collection in its own process.

Run with: uv run python -m pipeline.browser_run --platforms linkedin,naukri --max-postings 40

- Every accepted posting is written to the DB the moment it is read, so a crash or a closed window keeps
  everything collected so far.
- Postings already stored for that source are skipped before their detail page is opened, so starting again
  continues into new results instead of re-reading the same ones.
- Progress is written to data/exports/browser_run.json; the dashboard polls it.
- A stop request (stop_requested in that file) is honoured between cards, keeping what was fetched.

Scoring and tailoring are NOT done here; run "Score & prepare fetched" (or the normal discovery run) afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

STATE_FILE = "data/exports/browser_run.json"
ACTIVE_STATES = ("starting", "running", "stopping")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def state_path(settings) -> Path:  # noqa: ANN001
    return settings.path(STATE_FILE)


def read_state(settings) -> dict[str, Any]:  # noqa: ANN001
    p = state_path(settings)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_state(settings, **updates: Any) -> dict[str, Any]:  # noqa: ANN001
    """Merge updates into the state file atomically (write temp file, then rename)."""
    current = read_state(settings)
    current.update(updates)
    current["updated_at"] = _now()
    p = state_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return current


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_running(settings) -> bool:  # noqa: ANN001
    st = read_state(settings)
    return st.get("state") in ACTIVE_STATES and pid_alive(st.get("pid"))


def stop_requested(settings) -> bool:  # noqa: ANN001
    return bool(read_state(settings).get("stop_requested"))


# --------------------------------------------------------------------------- worker


def run_worker(settings, platforms: list[str], max_postings: int, trigger: str) -> int:  # noqa: ANN001
    from connectors.hooks import CollectHooks
    from connectors.linkedin import LinkedInCollector
    from connectors.naukri import NaukriCollector
    from copilot.browser import BrowserSession
    from core import repository as repo
    from core.browser_lock import BrowserBusy, browser_lock
    from core.db import session_factory
    from core.logging import log
    from core.models import Source
    from core.quota import QuotaManager
    from core.schema import Job, RunLog
    from pipeline.discovery import build_role_patterns, text_reject_reason

    factory = session_factory(settings.path(settings.app.db_path))
    quota = QuotaManager(settings.quotas, settings.app.timezone)
    patterns = build_role_patterns(settings.search)
    counters: dict[str, int] = {}
    filter_reasons: dict[str, int] = {}
    friction: list[str] = []
    errors: list[str] = []
    current = {"platform": None}

    with factory() as s:
        run = repo.start_run(s, "browser", trigger)
        run_id = run.id
        s.commit()

    write_state(
        settings,
        state="running",
        pid=os.getpid(),
        trigger=trigger,
        platforms=platforms,
        max_postings=max_postings,
        started_at=_now(),
        stop_requested=False,
        stopped_reason=None,
        error=None,
        platform=None,
        counters={},
        filter_reasons={},
        friction=[],
        errors=[],
    )
    log.info("browser collection started: {} cap={} trigger={}", platforms, max_postings, trigger)

    def bump(kind: str, detail: str = "") -> None:
        counters[kind] = counters.get(kind, 0) + 1
        if kind in ("prefiltered", "rejected") and detail:
            filter_reasons[detail] = filter_reasons.get(detail, 0) + 1
        write_state(settings, counters=counters, filter_reasons=filter_reasons, platform=current["platform"])

    def should_stop() -> bool:
        if max_postings and counters.get("saved", 0) >= max_postings:
            write_state(settings, stopped_reason="cap reached")
            return True
        return stop_requested(settings)

    final_error: str | None = None
    try:
        with browser_lock(settings, "collector"), BrowserSession(settings) as browser:
            for platform in platforms:
                if should_stop():
                    break
                current["platform"] = platform
                write_state(settings, platform=platform)
                source = Source.LINKEDIN if platform == "linkedin" else Source.NAUKRI
                with factory() as s:
                    known = set(s.scalars(select(Job.external_id).where(Job.source == source.value)))

                def on_posting(posting, known=known) -> None:  # noqa: ANN001
                    with factory() as s2:
                        _, created = repo.upsert_job(s2, posting)
                        s2.commit()
                    counters["created"] = counters.get("created", 0) + int(created)
                    known.add(posting.external_id)  # never open this one again in this run

                def pre_filter(title: str, company: str, location: str | None) -> str | None:
                    return text_reject_reason(
                        title, company, location, _looks_remote(location), settings.search, patterns
                    )

                hooks = CollectHooks(
                    on_posting=on_posting,
                    should_stop=should_stop,
                    skip_ids=known,
                    pre_filter=pre_filter,
                    on_event=bump,
                )
                if platform == "linkedin":
                    collector = LinkedInCollector(browser, settings.sources.linkedin, settings.search, quota)
                elif platform == "naukri":
                    collector = NaukriCollector(browser, settings.sources.naukri, settings.search, quota)
                else:
                    errors.append(f"unknown platform {platform}")
                    continue
                try:
                    collector.fetch(hooks)
                except Exception as e:  # noqa: BLE001
                    # Postings already saved stay saved. A closed browser will fail the next platform too.
                    log.exception("{} collection failed", platform)
                    errors.append(f"{platform}: {e}")
                    break
                if collector.friction:
                    friction.append(f"{platform}: {collector.friction}")
    except BrowserBusy as e:
        final_error = f"{e}. Wait for it to finish, then start again."
    except Exception as e:  # noqa: BLE001
        log.exception("browser collection failed")
        final_error = str(e)

    stopped = stop_requested(settings) or (max_postings and counters.get("saved", 0) >= max_postings)
    if final_error or errors:
        status = "failed" if final_error else "partial"
    else:
        status = "partial" if stopped or friction else "success"
    if final_error:
        final_state = "failed"
    elif stopped:
        final_state = "stopped"
    elif friction:
        final_state = "paused"  # a site blocked or challenged us; the user needs to look before retrying
    else:
        final_state = "done"
    stats = {
        "counters": counters,
        "filter_reasons": filter_reasons,
        "friction": friction,
        "errors": errors,
        "platforms": platforms,
    }
    write_state(
        settings,
        state=final_state,
        pid=None,
        stop_requested=False,
        platform=None,
        error=final_error,
        friction=friction,
        errors=errors,
        ended_at=_now(),
    )
    with factory() as s:
        repo.finish_run(s, s.get(RunLog, run_id), status, stats, final_error)
        s.commit()
    log.info("browser collection {}: {}", final_state, counters)
    return 0 if final_error is None else 1


def _looks_remote(location: str | None) -> bool:
    return "remote" in (location or "").lower()


def main(argv: list[str] | None = None) -> int:
    from core.db import init_db
    from core.logging import setup_logging
    from core.settings import load_settings

    ap = argparse.ArgumentParser(description="Collect LinkedIn/Naukri postings into the DB as they are read.")
    ap.add_argument("--platforms", default="linkedin,naukri")
    ap.add_argument(
        "--max-postings", type=int, default=0, help="stop after this many new postings (0 = no cap)"
    )
    ap.add_argument("--trigger", default="manual")
    args = ap.parse_args(argv)

    settings = load_settings()
    setup_logging(settings.app.log_level, settings.path("data/exports"))
    init_db(settings.path(settings.app.db_path))
    platforms = [p.strip().lower() for p in args.platforms.split(",") if p.strip()]
    return run_worker(settings, platforms, args.max_postings, args.trigger)


if __name__ == "__main__":
    sys.exit(main())
