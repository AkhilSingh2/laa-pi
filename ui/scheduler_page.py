"""Scheduler: is it alive, what runs next, pause / resume / run now, edit the schedule, read its log."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st
from apscheduler.triggers.cron import CronTrigger

from core import scheduler_control as ctl
from pipeline.browser_run import is_running
from pipeline.runner import request_browser_stop
from ui.components import fmt_when, get_settings, page, reload_settings
from ui.settings_page import _save
from ui.theme import card, chips, page_header, title_block

JOB_INFO = {
    "discovery": ("Discovery", "Fetch job boards and LinkedIn / Naukri, filter, score, tailor resumes."),
    "apply": ("Apply batch", "Run the copilot on approved jobs inside execution windows, within daily caps."),
}


def _next_fires(cron: str, tz: str, n: int = 3) -> list[datetime]:
    trig = CronTrigger.from_crontab(cron, timezone=ZoneInfo(tz))
    out, prev = [], None
    now = datetime.now(ZoneInfo(tz))
    for _ in range(n):
        nxt = trig.get_next_fire_time(prev, prev or now)
        if nxt is None:
            break
        out.append(nxt)
        prev = nxt
        now = nxt
    return out


@st.fragment(run_every=5)
def _status() -> None:
    settings = get_settings()
    hb = ctl.read_heartbeat(settings) or {}
    control = ctl.read_control(settings)
    alive = ctl.scheduler_alive(settings)
    with card("sched_status"):
        if alive:
            age = (datetime.now().astimezone() - datetime.fromisoformat(hb["at"])).total_seconds()
            title_block("Scheduler is running", f"pid {hb.get('pid', '-')} - heartbeat {int(age)}s ago")
            chips(
                [
                    (
                        f"State: {hb.get('state', 'idle')}",
                        "info" if hb.get("state") not in ("idle", None) else "ok",
                    ),
                    ("All jobs paused", "warn") if control.get("paused_all") else ("", "mute"),
                    (f"Apply capped until {fmt_when(hb.get('capped_until'))}", "warn")
                    if hb.get("capped_until")
                    else ("", "mute"),
                ],
                dot=True,
            )
        else:
            title_block(
                "Scheduler is not running",
                "In Docker it is started and restarted automatically. Locally run: uv run python scheduler.py. "
                "Pauses and run-now requests made here wait until it starts.",
            )
            chips([("Last seen " + fmt_when(hb.get("at")) if hb else "Never started", "bad")], dot=True)
        with st.container(horizontal=True, gap="small"):
            if control.get("paused_all"):
                if st.button("Resume everything", type="primary", icon=":material/play_arrow:"):
                    ctl.set_paused(settings, "all", False)
                    st.rerun()
            elif st.button("Pause everything", icon=":material/pause:"):
                ctl.set_paused(settings, "all", True)
                st.rerun()
            if is_running(settings) and st.button("Stop browser collection", icon=":material/stop:"):
                request_browser_stop(settings)
                st.toast("Stopping after the current card")

    for job, (name, desc) in JOB_INFO.items():
        info = (hb.get("jobs") or {}).get(job, {})
        last = (hb.get("last_runs") or {}).get(job)
        paused = ctl.is_paused(control, job)
        queued = job in [r["job"] for r in control.get("run_now", [])]
        with card(f"sched_{job}"):
            title_block(name, desc)
            chips(
                [
                    ("Running now", "info") if info.get("running") else ("", "mute"),
                    ("Paused", "warn") if paused else ("Active", "ok"),
                    ("Run-now queued", "info") if queued else ("", "mute"),
                    (f"Next {fmt_when(info.get('next_run'))}", "mute")
                    if alive and not paused
                    else ("", "mute"),
                    (f"Cron {getattr(settings.scheduler, job + '_cron')}", "mute"),
                ],
                dot=True,
            )
            if last:
                stats = ", ".join(f"{k} {v}" for k, v in (last.get("stats") or {}).items() if v)
                st.caption(
                    f"Last run {fmt_when(last.get('at'))} ({last.get('trigger')}): {last.get('status')}"
                    + (f" - {stats}" if stats else "")
                )
            with st.container(horizontal=True, gap="small"):
                if st.button(
                    "Resume" if paused else "Pause",
                    key=f"pause_{job}",
                    icon=":material/play_arrow:" if paused else ":material/pause:",
                ):
                    ctl.set_paused(settings, job, not paused)
                    st.rerun()
                if st.button(
                    "Run now",
                    key=f"run_{job}",
                    type="primary",
                    icon=":material/bolt:",
                    disabled=queued or bool(info.get("running")),
                ):
                    ctl.request_run_now(settings, job)
                    st.toast(f"{name} queued" + ("" if alive else " (runs when the scheduler starts)"))
                    st.rerun()


def _edit_schedule() -> None:
    settings = get_settings()
    with card("sched_edit"), st.form("cron_form", border=False):
        title_block(
            "Schedule",
            f"Cron format: minute hour day month weekday, in {settings.app.timezone}. "
            "Example: 0 9 * * mon-fri = 09:00 on weekdays.",
        )
        disc = st.text_input("Discovery", settings.scheduler.discovery_cron)
        appl = st.text_input("Apply batch", settings.scheduler.apply_cron)
        if st.form_submit_button("Save schedule", type="primary"):
            try:
                for c in (disc, appl):
                    CronTrigger.from_crontab(c.strip(), timezone=ZoneInfo(settings.app.timezone))
            except ValueError as e:
                st.error(f"Invalid cron: {e}")
            else:
                if _save({"scheduler": {"discovery_cron": disc.strip(), "apply_cron": appl.strip()}}):
                    reload_settings()
                    st.caption("The scheduler picks up the new times within 15 seconds.")
    for job in ctl.JOBS:
        try:
            fires = _next_fires(getattr(settings.scheduler, f"{job}_cron"), settings.app.timezone)
            st.caption(f"{JOB_INFO[job][0]} next: " + ", ".join(f"{f:%a %d %b %H:%M}" for f in fires))
        except ValueError:
            pass


@st.fragment(run_every=10)
def _log() -> None:
    settings = get_settings()
    text = ctl.tail(settings.path(ctl.LOG_FILE), 80)
    st.code(text or "No scheduler log yet.", language=None, wrap_lines=True, height=320)


def render() -> None:
    page_header("Scheduler", "Runs discovery and apply batches automatically. Control it here.")
    left, right = st.columns([1.1, 1], gap="medium")
    with left:
        _status()
    with right:
        _edit_schedule()
        st.page_link(
            page("settings"),
            label="Caps, windows and delays are in Settings > Guardrails",
            icon=":material/tune:",
        )
    with st.expander("Scheduler log"):
        _log()
