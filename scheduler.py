"""APScheduler daily runner. Run with: uv run python scheduler.py

- discovery_cron: ingest (API boards + LinkedIn/Naukri via the persistent browser), dedupe, score, tailor.
- apply_cron: apply to *approved* jobs inside execution windows, pacing with human-like delays.
Stops the apply job for the day once every platform's cap is reached (quotas.stop_scheduler_when_caps_reached).

Controlled from the dashboard's Scheduler page through data/exports/scheduler_control.json (polled every 15s):
pause / resume per job or globally, run now, and cron edits (saved to data/settings_overrides.yaml).
"""

from __future__ import annotations

import json
import os
import signal
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from core import scheduler_control as ctl
from core.db import init_db, session_factory
from core.logging import log, setup_logging
from core.quota import QuotaManager
from core.settings import load_settings
from pipeline.runner import run_apply_batch, run_discovery

settings = load_settings()
setup_logging(settings.app.log_level, settings.path("data/exports"))
log.add(settings.path(ctl.LOG_FILE), level="INFO", rotation="2 MB", retention=3, enqueue=True)
init_db(settings.path(settings.app.db_path))
tz = ZoneInfo(settings.app.timezone)
quota = QuotaManager(settings.quotas, settings.app.timezone)
scheduler = BlockingScheduler(
    timezone=tz,
    job_defaults={
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": settings.scheduler.misfire_grace_seconds,
    },
)
STATE: dict = {"state": "idle", "config_version": None, "last_runs": {}, "capped_until": None}
_RUNNING = {job: threading.Lock() for job in ctl.JOBS}


def heartbeat() -> None:
    control = ctl.read_control(settings)
    jobs = {}
    for job in ctl.JOBS:
        nxt = getattr(scheduler.get_job(job), "next_run_time", None)
        jobs[job] = {
            "next_run": nxt.isoformat() if nxt else None,
            "paused": ctl.is_paused(control, job),
            "running": _RUNNING[job].locked(),
        }
    payload = {
        "at": datetime.now(tz).isoformat(),
        "pid": os.getpid(),
        "state": STATE["state"],
        "paused_all": bool(control.get("paused_all")),
        "paused_jobs": control.get("paused_jobs", []),
        "jobs": jobs,
        "next_discovery": jobs["discovery"]["next_run"],
        "next_apply": jobs["apply"]["next_run"],
        "discovery_cron": settings.scheduler.discovery_cron,
        "apply_cron": settings.scheduler.apply_cron,
        "capped_until": STATE["capped_until"],
        "last_runs": STATE["last_runs"],
    }
    p = settings.path(ctl.HEARTBEAT_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Unique name: the poller and a running job both write this file, and a shared .tmp gets renamed away.
    tmp = p.with_name(f".heartbeat-{os.getpid()}-{threading.get_ident()}.tmp")
    body = json.dumps(payload)
    for attempt in range(2):
        try:
            tmp.write_text(body)
            os.replace(tmp, p)
            return
        except OSError:
            if attempt:
                log.exception("heartbeat write failed")
            tmp.unlink(missing_ok=True)


def _record(job: str, trigger: str, status: str, stats: dict | None = None) -> None:
    STATE["last_runs"][job] = {
        "at": datetime.now(tz).isoformat(timespec="seconds"),
        "trigger": trigger,
        "status": status,
        "stats": {k: v for k, v in (stats or {}).items() if isinstance(v, int | float | str)},
    }


def _guarded(job: str, trigger: str, fn) -> None:  # noqa: ANN001
    """Run `fn` unless paused (scheduled runs only) or the same job is already running."""
    if trigger == "scheduled" and ctl.is_paused(ctl.read_control(settings), job):
        log.info("{} skipped: paused from the dashboard", job)
        _record(job, trigger, "skipped (paused)")
        heartbeat()
        return
    if not _RUNNING[job].acquire(blocking=False):
        log.info("{} skipped: already running", job)
        return
    try:
        STATE["state"] = job if job != "apply" else "applying"
        heartbeat()
        fn(trigger)
    except Exception as e:  # noqa: BLE001
        log.exception("{} failed", job)
        _record(job, trigger, f"failed: {e}")
    finally:
        _RUNNING[job].release()
        STATE["state"] = "idle" if not any(lock.locked() for lock in _RUNNING.values()) else STATE["state"]
        heartbeat()


def _discovery(trigger: str) -> None:
    res = run_discovery(settings, trigger=trigger, include_browser=True)
    log.info("{} discovery -> {} {}", trigger, res.status, res.stats)
    _record("discovery", trigger, res.status, res.stats)


def _apply(trigger: str) -> None:
    factory = session_factory(settings.path(settings.app.db_path))
    with factory() as session:
        if quota.all_exhausted(session):
            log.info("all daily caps reached; skipping apply batch")
            _record("apply", trigger, "skipped (all caps reached)")
            _pause_apply_until_tomorrow()
            return
    res = run_apply_batch(settings, trigger=trigger)
    log.info("{} apply -> {} {}", trigger, res.status, res.stats)
    _record("apply", trigger, res.status, res.stats)
    if res.status == "stopped_cap_reached" and settings.quotas.stop_scheduler_when_caps_reached:
        _pause_apply_until_tomorrow()


def discovery_job(trigger: str = "scheduled") -> None:
    _guarded("discovery", trigger, _discovery)


def apply_job(trigger: str = "scheduled") -> None:
    _guarded("apply", trigger, _apply)


JOB_FUNCS = {"discovery": discovery_job, "apply": apply_job}


def _pause_apply_until_tomorrow() -> None:
    job = scheduler.get_job("apply")
    if job is None:
        return
    tomorrow = (datetime.now(tz) + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    nxt = job.trigger.get_next_fire_time(None, tomorrow)
    job.modify(next_run_time=nxt)
    STATE["capped_until"] = nxt.isoformat() if nxt else None
    log.info("apply job paused until {} (caps reached)", nxt)


def _apply_cron(reload: bool) -> None:
    global settings
    if reload:
        settings = load_settings()
    for job in ctl.JOBS:
        cron = getattr(settings.scheduler, f"{job}_cron")
        trigger = CronTrigger.from_crontab(cron, timezone=tz)
        if scheduler.get_job(job) is None:
            scheduler.add_job(JOB_FUNCS[job], trigger, id=job, name=job)
        else:
            scheduler.reschedule_job(job, trigger=trigger)
    log.info(
        "schedule: discovery '{}' | apply '{}' | tz {}",
        settings.scheduler.discovery_cron,
        settings.scheduler.apply_cron,
        settings.app.timezone,
    )


def poll_control() -> None:
    control = ctl.read_control(settings)
    version = control.get("config_version", 0)
    if STATE["config_version"] is None:
        STATE["config_version"] = version
    elif version != STATE["config_version"]:
        STATE["config_version"] = version
        try:
            _apply_cron(reload=True)
        except Exception as e:  # noqa: BLE001
            log.error("could not apply new schedule: {}", e)
    for job in ctl.take_run_now(settings):
        log.info("{} requested from the dashboard", job)
        scheduler.add_job(
            JOB_FUNCS[job],
            kwargs={"trigger": "manual"},
            id=f"{job}_now",
            name=f"{job} (run now)",
            replace_existing=True,
            misfire_grace_time=None,
        )
    heartbeat()


def _listener(event) -> None:  # noqa: ANN001
    if event.exception:
        log.error("job {} failed: {}", event.job_id, event.exception)


def _shutdown(*_args) -> None:  # noqa: ANN002
    STATE["state"] = "stopped"
    heartbeat()
    scheduler.shutdown(wait=False)
    # sys.exit only ends the main thread; a running apply batch's worker thread would keep the process alive
    os._exit(0)


def main() -> None:
    if not settings.scheduler.enabled:
        log.warning("scheduler.enabled is false in config.yaml; exiting")
        return
    _apply_cron(reload=False)
    scheduler.add_job(heartbeat, "interval", seconds=60, id="heartbeat")
    scheduler.add_job(poll_control, "interval", seconds=15, id="control")
    scheduler.add_listener(_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    log.info("scheduler started (pid {})", os.getpid())
    poll_control()
    scheduler.start()


if __name__ == "__main__":
    main()
