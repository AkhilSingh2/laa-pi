"""Run entrypoints shared by app.py (manual buttons) and scheduler.py (cron)."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from core import repository as repo
from core.db import session_factory
from core.llm import LLMClient
from core.logging import log
from core.models import JobStatus, platform_for
from core.quota import QuotaManager
from core.settings import Settings
from pipeline.browser_run import is_running, read_state, write_state
from pipeline.discovery import DiscoveryPipeline, DiscoveryStats


@dataclass
class RunResult:
    status: str
    stats: dict
    error: str | None = None


def _llm(settings: Settings) -> LLMClient:
    return LLMClient(settings.llm)


# --------------------------------------------------------------------------- discovery


def run_discovery(settings: Settings, *, trigger: str, include_browser: bool = False) -> RunResult:
    """API boards -> filter -> store -> score -> prepare.

    include_browser=True first runs the LinkedIn/Naukri collector to completion (blocking, used by the
    scheduler). The dashboard starts that collector in the background instead and uses process_pending_jobs.
    """
    factory = session_factory(settings.path(settings.app.db_path))
    llm = _llm(settings)
    with factory() as session:
        try:
            run = repo.start_run(session, "discovery", trigger)
            session.commit()
        except Exception as e:  # noqa: BLE001
            log.exception("discovery failed to start")
            return RunResult("failed", {}, str(e))
        try:
            browser_state: dict = {}
            if include_browser:
                proc = start_browser_collection(settings, ["linkedin", "naukri"], 0, trigger)
                if proc is None:
                    log.warning("browser collection already running; skipping it for this discovery run")
                else:
                    proc.wait()
                    browser_state = read_state(settings)
            pipeline = DiscoveryPipeline(settings, llm if llm.available else None)
            stats: DiscoveryStats = pipeline.run(session)
            stats_dict = stats.as_dict()
            if include_browser:
                stats_dict["browser"] = {
                    "state": browser_state.get("state"),
                    "counters": browser_state.get("counters", {}),
                    "errors": browser_state.get("errors", []),
                    "friction": browser_state.get("friction", []),
                }
            status = "partial" if stats.errors or stats.friction or browser_state.get("errors") else "success"
            repo.finish_run(session, run, status, stats_dict)
            session.commit()
            log.info("discovery {}: {}", status, stats_dict)
            return RunResult(status, stats_dict)
        except Exception as e:  # noqa: BLE001
            log.exception("discovery failed")
            session.rollback()
            repo.finish_run(session, run, "failed", error=str(e))
            session.commit()
            return RunResult("failed", {}, str(e))


def process_pending_jobs(settings: Settings, *, trigger: str) -> RunResult:
    """Score, dedupe and prepare everything saved but not yet processed (e.g. by the browser collector)."""
    factory = session_factory(settings.path(settings.app.db_path))
    llm = _llm(settings)
    with factory() as session:
        run = repo.start_run(session, "score_pending", trigger)
        session.commit()
        try:
            pipeline = DiscoveryPipeline(settings, llm if llm.available else None)
            stats = pipeline.process_pending(session)
            status = "partial" if stats.errors else "success"
            repo.finish_run(session, run, status, stats.as_dict())
            session.commit()
            return RunResult(status, stats.as_dict())
        except Exception as e:  # noqa: BLE001
            log.exception("score pending failed")
            session.rollback()
            repo.finish_run(session, run, "failed", error=str(e))
            session.commit()
            return RunResult("failed", {}, str(e))


# --------------------------------------------------------------------------- browser collection


def start_browser_collection(
    settings: Settings, platforms: list[str], max_postings: int, trigger: str = "manual"
) -> subprocess.Popen | None:
    """Start the background LinkedIn/Naukri collector. Returns None if one is already running."""
    if is_running(settings):
        return None
    cmd = [
        sys.executable,
        "-m",
        "pipeline.browser_run",
        "--platforms",
        ",".join(platforms),
        "--max-postings",
        str(max_postings),
        "--trigger",
        trigger,
    ]
    log_path: Path = settings.path("data/exports") / "browser_run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "ab")  # noqa: SIM115 - handed to the subprocess
    proc = subprocess.Popen(cmd, cwd=str(settings.root), stdout=fh, stderr=subprocess.STDOUT)
    write_state(
        settings,
        state="starting",
        pid=proc.pid,
        platforms=platforms,
        max_postings=max_postings,
        stop_requested=False,
        counters={},
        filter_reasons={},
        errors=[],
        friction=[],
        error=None,
        stopped_reason=None,
    )
    log.info("started browser collection pid {} platforms {}", proc.pid, platforms)
    return proc


def request_browser_stop(settings: Settings) -> bool:
    """Ask the running collector to stop after the current card. Postings already saved are kept."""
    if not is_running(settings):
        return False
    write_state(settings, stop_requested=True, state="stopping", stopped_reason="stopped by you")
    return True


def pending_job_count(settings: Settings) -> int:
    factory = session_factory(settings.path(settings.app.db_path))
    with factory() as session:
        return len(
            [r for r in repo.jobs_by_status(session, JobStatus.DISCOVERED) if r.canonical_job_id is None]
        )


# --------------------------------------------------------------------------- apply


def run_apply_batch(settings: Settings, *, trigger: str, max_jobs: int | None = None) -> RunResult:
    """Apply to approved jobs until caps are reached.

    Scheduled runs stay inside the execution windows. A manual run from the dashboard starts immediately.
    """
    from copilot.applier import Applier
    from copilot.browser import BrowserSession
    from core.browser_lock import browser_lock

    factory = session_factory(settings.path(settings.app.db_path))
    quota = QuotaManager(settings.quotas, settings.app.timezone)
    llm = _llm(settings)
    with factory() as session:
        run = repo.start_run(session, "apply", trigger)
        session.commit()
        stats = {
            "attempted": 0,
            "submitted": 0,
            "awaiting_review": 0,
            "paused": 0,
            "skipped_cap": 0,
            "skipped_window": 0,
        }
        try:
            manual = trigger == "manual"
            if not manual and not quota.in_execution_window():
                log.info(
                    "outside execution window; skipping scheduled apply (next {})", quota.next_window_start()
                )
                stats["skipped_window"] = 1
                repo.finish_run(session, run, "success", stats)
                session.commit()
                return RunResult("success", stats)
            if manual and not quota.in_execution_window():
                log.info("manual apply outside the execution window; running anyway")
            approved = repo.jobs_by_status(session, JobStatus.APPROVED, limit=max_jobs)
            if not approved:
                repo.finish_run(session, run, "success", stats)
                session.commit()
                return RunResult("success", stats)
            stats["needs_you"] = 0
            stats["platform_blocked"] = 0
            blocked: dict[str, str] = {}
            with browser_lock(settings, "scheduled apply"):
                browser = BrowserSession(settings).__enter__()
                try:
                    applier = Applier(settings, browser, llm if llm.available else None)
                    for job in approved:
                        platform = platform_for(job.source, job.ats_type)
                        if platform in blocked:
                            stats["platform_blocked"] += 1
                            continue
                        if not quota.can_submit(session, platform):
                            stats["skipped_cap"] += 1
                            continue
                        if quota.all_exhausted(session):
                            break
                        stats["attempted"] += 1
                        out = _apply_one(applier, session, job, enforce_window=not manual)
                        if out.status == "failed" and _browser_gone(out.message):
                            log.warning(
                                "browser closed while applying to {}; reopening and retrying", job.title
                            )
                            browser = _reopen_browser(browser, settings)
                            applier = Applier(settings, browser, llm if llm.available else None)
                            out = _apply_one(applier, session, job, enforce_window=not manual)
                        log.info("{} at {} -> {}: {}", job.title, job.company, out.status, out.message)
                        if out.status == "submitted":
                            stats["submitted"] += 1
                        elif out.status == "awaiting_review":
                            stats["awaiting_review"] += 1
                        elif out.status == "paused_friction" and _platform_wide(out.friction, platform):
                            # login wall / captcha / OTP on LinkedIn or Naukri affects every job there
                            stats["paused"] += 1
                            blocked[platform] = out.message
                            log.warning(
                                "{} needs you ({}); skipping its jobs this batch", platform, out.message
                            )
                        elif out.status in ("paused_friction", "failed", "awaiting_review"):
                            # a batch never waits on you: review-mode forms, missing forms and pauses go to your
                            # review list; Apply now on the job re-runs it with you watching
                            stats["needs_you"] += 1
                            repo.set_job_status(
                                session,
                                job,
                                JobStatus.READY_FOR_REVIEW,
                                f"copilot stopped: {out.message}"[:500],
                            )
                            session.commit()
                            log.info("{} moved back to review: {}", job.title, out.message)
                        if _browser_gone(out.message):
                            browser = _reopen_browser(browser, settings)
                            applier = Applier(settings, browser, llm if llm.available else None)
                        quota.human_delay("between applications")
                finally:
                    _close_browser(browser)
            status = "stopped_cap_reached" if quota.all_exhausted(session) else "success"
            repo.finish_run(session, run, status, stats)
            session.commit()
            return RunResult(status, stats)
        except Exception as e:  # noqa: BLE001
            log.exception("apply batch failed")
            repo.finish_run(session, run, "failed", stats, str(e))
            session.commit()
            return RunResult("failed", stats, str(e))


_SESSION_FRICTION = {"login_required", "captcha", "otp", "blocked"}


def _platform_wide(friction: str | None, platform: str) -> bool:
    return friction in _SESSION_FRICTION and platform in ("linkedin", "naukri")


def _browser_gone(message: str | None) -> bool:
    return "has been closed" in (message or "") or "Browser closed" in (message or "")


def _apply_one(applier, session, job, *, enforce_window: bool):  # noqa: ANN001, ANN202
    """One job; an unexpected error is reported as a failed outcome instead of ending the batch."""
    from copilot.applier import ApplyOutcome

    try:
        return applier.apply(session, job, enforce_window=enforce_window, review_wait_seconds=0)
    except Exception as e:  # noqa: BLE001
        log.exception("applier crashed on {}", job.title)
        session.rollback()
        return ApplyOutcome(job.id, "failed", f"error: {e}")


def _close_browser(browser) -> None:  # noqa: ANN001
    try:
        browser.__exit__(None, None, None)
    except Exception as e:  # noqa: BLE001
        log.debug("browser close: {}", e)


def _reopen_browser(browser, settings: Settings):  # noqa: ANN001, ANN202
    from copilot.browser import BrowserSession

    _close_browser(browser)
    return BrowserSession(settings).__enter__()


def spawn_applier(settings: Settings, job_id: str, mode: str | None = None) -> subprocess.Popen:
    """Launch the copilot for one job in a separate process (so the Streamlit UI stays responsive)."""
    cmd = [sys.executable, "-m", "copilot.applier", "--job-id", job_id, "--review-wait", "900"]
    if mode:
        cmd += ["--mode", mode]
    log_path: Path = settings.path("data/exports") / f"applier_{job_id[:8]}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "ab")  # noqa: SIM115 - handed to the subprocess
    return subprocess.Popen(cmd, cwd=str(settings.root), stdout=fh, stderr=subprocess.STDOUT)


def spawn_connection(settings: Settings, action: str, platforms: list[str]) -> subprocess.Popen:
    """Open the browser to log in (`action="login"`) or to check the saved sessions (`"check"`)."""
    cmd = [sys.executable, "-m", "copilot.connections", action]
    for p in platforms:
        cmd += ["--platform", p]
    log_path: Path = settings.path("data/exports") / "connections.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "ab")  # noqa: SIM115 - handed to the subprocess
    return subprocess.Popen(cmd, cwd=str(settings.root), stdout=fh, stderr=subprocess.STDOUT)


def browser_holder(settings: Settings) -> dict | None:
    """Which process (if any) has the browser open right now."""
    from core.browser_lock import holder

    return holder(settings)
