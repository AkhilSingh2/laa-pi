"""Dashboard -> scheduler control channel.

The dashboard and the scheduler are separate processes, so they talk through two small files:
- data/exports/scheduler_control.json (dashboard writes, scheduler polls every 15s): paused jobs, a global
  pause, queued "run now" requests and a config version that tells the scheduler to reload its cron times.
- data/exports/scheduler_heartbeat.json (scheduler writes): state, next runs, last results, pid.
Read-modify-write on the control file is serialized with an flock so neither side loses an update.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.settings import Settings

CONTROL_FILE = "data/exports/scheduler_control.json"
HEARTBEAT_FILE = "data/exports/scheduler_heartbeat.json"
LOG_FILE = "data/exports/scheduler.log"
JOBS = ("discovery", "apply")


def _default() -> dict[str, Any]:
    return {"paused_all": False, "paused_jobs": [], "run_now": [], "config_version": 0}


@contextmanager
def _locked(settings: Settings) -> Iterator[Path]:
    p = settings.path(CONTROL_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_suffix(".lock"), "a") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            yield p
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def _load(p: Path) -> dict[str, Any]:
    data = _default()
    if p.exists():
        try:
            data.update(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return data


def read_control(settings: Settings) -> dict[str, Any]:
    return _load(settings.path(CONTROL_FILE))


def update_control(settings: Settings, mutate: Callable[[dict[str, Any]], Any]) -> Any:
    with _locked(settings) as p:
        data = _load(p)
        result = mutate(data)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        return result


def set_paused(settings: Settings, job: str, paused: bool) -> None:
    """Pause / resume one job ("discovery", "apply") or everything ("all")."""
    if job not in (*JOBS, "all"):
        raise ValueError(job)

    def _m(d: dict[str, Any]) -> None:
        if job == "all":
            d["paused_all"] = paused
            return
        jobs = set(d["paused_jobs"])
        (jobs.add if paused else jobs.discard)(job)
        d["paused_jobs"] = sorted(jobs)

    update_control(settings, _m)


def request_run_now(settings: Settings, job: str) -> None:
    if job not in JOBS:
        raise ValueError(job)

    def _m(d: dict[str, Any]) -> None:
        if job not in [r["job"] for r in d["run_now"]]:
            d["run_now"].append({"job": job, "requested_at": datetime.now(UTC).isoformat(timespec="seconds")})

    update_control(settings, _m)


def take_run_now(settings: Settings) -> list[str]:
    """Scheduler side: pop queued run-now requests."""

    def _m(d: dict[str, Any]) -> list[str]:
        jobs = [r["job"] for r in d["run_now"]]
        d["run_now"] = []
        return jobs

    return update_control(settings, _m)


def bump_config_version(settings: Settings) -> None:
    update_control(settings, lambda d: d.__setitem__("config_version", int(d["config_version"]) + 1))


def is_paused(control: dict[str, Any], job: str) -> bool:
    return bool(control.get("paused_all")) or job in control.get("paused_jobs", [])


def read_heartbeat(settings: Settings) -> dict[str, Any] | None:
    p = settings.path(HEARTBEAT_FILE)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def scheduler_alive(settings: Settings, stale_after_s: int = 180) -> bool:
    """True when the scheduler wrote a heartbeat recently and has not reported itself stopped."""
    hb = read_heartbeat(settings)
    if not hb or hb.get("state") == "stopped":
        return False
    try:
        at = datetime.fromisoformat(hb["at"])
    except (KeyError, ValueError):
        return False
    return (datetime.now(at.tzinfo or UTC) - at).total_seconds() < stale_after_s


def tail(path: Path, lines: int = 80) -> str:
    if not path.exists():
        return ""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - 64_000))
        data = fh.read().decode("utf-8", errors="replace")
    return "\n".join(data.splitlines()[-lines:])
