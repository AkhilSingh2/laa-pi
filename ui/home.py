"""Home: who you are, what is running, what needs you, today's quotas and the pipeline at a glance."""

from __future__ import annotations

import streamlit as st

from core import repository as repo
from core import scheduler_control as ctl
from core.models import AppStatus, JobStatus
from ui.components import (
    app_counts,
    fmt_when,
    follow_ups_due,
    get_profile,
    get_quota,
    get_session,
    get_settings,
    job_counts,
    page,
    profile_health,
    system_status,
)
from ui.theme import card, chips, platform_label, tiles, title_block


def _status_strip(settings, status: dict) -> None:  # noqa: ANN001
    hb = status["heartbeat"]
    items = []
    if status["scheduler_alive"]:
        if status["control"].get("paused_all"):
            items.append(("Scheduler paused", "warn"))
        else:
            items.append((f"Scheduler {hb.get('state', 'idle')}", "ok"))
            nxt = hb.get("next_discovery")
            if nxt:
                items.append((f"Next discovery {fmt_when(nxt)}", "mute"))
    else:
        items.append(("Scheduler not running", "bad"))
    if status["collecting"]:
        c = status["collection"].get("counters", {}) or {}
        items.append((f"Collecting: {c.get('saved', 0)} saved", "info"))
    elif status["browser_holder"]:
        items.append((f"Browser in use: {status['browser_holder'].get('owner')}", "info"))
    else:
        items.append(("Browser idle", "mute"))
    items.append(
        (
            "AI on" if status["ai"]["set"] else "AI off (heuristic scoring)",
            "ok" if status["ai"]["set"] else "warn",
        )
    )
    for platform in ("linkedin", "naukri"):
        conn = status["connections"].get(platform, {})
        if "logged_in" in conn:
            items.append(
                (
                    f"{platform_label(platform)} {'connected' if conn['logged_in'] else 'logged out'}",
                    "ok" if conn["logged_in"] else "bad",
                )
            )
        else:
            items.append((f"{platform_label(platform)} not checked", "mute"))
    chips(items, dot=True)


def _needs_action(session, settings, status: dict, counts: dict[str, int]) -> None:  # noqa: ANN001
    rows: list[tuple[str, str, str, str, dict | None]] = []  # icon, text, button label, page, query
    review = counts.get(JobStatus.READY_FOR_REVIEW.value, 0)
    if review:
        rows.append(
            (":material/rate_review:", f"{review} job(s) ready for your review", "Review", "jobs", None)
        )
    paused = repo.paused_sessions(session)
    friction = [fs for fs, _ in paused if fs.status == "paused_friction"]
    awaiting = [fs for fs, _ in paused if fs.status == "awaiting_review"]
    if awaiting:
        rows.append(
            (
                ":material/task_alt:",
                f"{len(awaiting)} application(s) filled and waiting for submit approval",
                "Open",
                "command",
                None,
            )
        )
    if friction:
        rows.append(
            (
                ":material/front_hand:",
                f"{len(friction)} copilot session(s) paused on OTP / CAPTCHA / a question",
                "Open",
                "command",
                None,
            )
        )
    questions = len(repo.unapproved_screening_answers(session))
    if questions:
        rows.append(
            (
                ":material/help:",
                f"{questions} screening question(s) need your answer",
                "Answer",
                "command",
                None,
            )
        )
    drafts = len(repo.outreach_by_status(session, "drafted"))
    if drafts:
        rows.append(
            (
                ":material/forum:",
                f"{drafts} outreach message(s) drafted, ready to copy and send",
                "Open",
                "outreach",
                None,
            )
        )
    due = follow_ups_due(session)
    if due:
        rows.append((":material/event:", f"{due} follow-up(s) due", "Open", "applications", None))
    pending = counts.get(JobStatus.DISCOVERED.value, 0)
    if pending and not status["collecting"]:
        rows.append(
            (":material/hourglass_top:", f"{pending} fetched job(s) not scored yet", "Score", "command", None)
        )
    for platform in ("linkedin", "naukri"):
        if status["connections"].get(platform, {}).get("logged_in") is False:
            rows.append(
                (
                    ":material/link_off:",
                    f"{platform_label(platform)} session expired - log in again",
                    "Fix",
                    "settings",
                    None,
                )
            )
    if not status["ai"]["set"]:
        rows.append(
            (
                ":material/key:",
                "No AI key: scoring and answers use the offline heuristic",
                "Add key",
                "settings",
                None,
            )
        )
    gaps = profile_health(get_profile())
    if gaps:
        rows.append(
            (
                ":material/manage_accounts:",
                f"{len(gaps)} profile gap(s) that affect application forms",
                "Review",
                "settings",
                None,
            )
        )
    if not status["scheduler_alive"]:
        rows.append(
            (
                ":material/schedule:",
                "Scheduler is not running, so nothing runs automatically",
                "Details",
                "scheduler",
                None,
            )
        )

    with card("needs_action"):
        title_block("Needs your action", None if rows else "All clear. Nothing is waiting on you.")
        for i, (icon, text, label, target, query) in enumerate(rows):
            with st.container(horizontal=True, vertical_alignment="center", wrap=False, key=f"na_{i}"):
                st.markdown(f"{icon} {text}", width="stretch")
                st.page_link(page(target), label=label, icon=":material/arrow_forward:", query_params=query)


def _quotas(session, settings) -> None:  # noqa: ANN001
    quota = get_quota()
    with card("quotas"):
        in_window = quota.in_execution_window()
        nxt = quota.next_window_start()
        title_block(
            "Today's apply quotas",
            ("Execution window open now" if in_window else "Outside execution window")
            + ("" if in_window or nxt is None else f" - opens {nxt:%a %H:%M}"),
        )
        for s in quota.all_status(session):
            label = platform_label(s.platform)
            st.progress(
                min(s.used / max(s.cap, 1), 1.0),
                text=f"{label}: {s.used} / {s.cap}"
                + (" - cap reached" if s.exhausted else f" - {s.remaining} left"),
            )


def _pipeline(counts: dict[str, int], apps: dict[str, int]) -> None:
    with card("pipeline"):
        title_block("Pipeline")
        tiles(
            [
                ("Fetched, unscored", counts.get(JobStatus.DISCOVERED.value, 0), False),
                ("To review", counts.get(JobStatus.READY_FOR_REVIEW.value, 0), True),
                ("Approved", counts.get(JobStatus.APPROVED.value, 0), False),
                (
                    "Applied",
                    apps.get(AppStatus.APPLIED.value, 0) + apps.get(AppStatus.OUTREACH_SENT.value, 0),
                    False,
                ),
                ("Interviews", apps.get(AppStatus.INTERVIEW.value, 0), True),
                ("Low fit (archived)", counts.get(JobStatus.ARCHIVED_LOW_FIT.value, 0), False),
            ]
        )


def _quick_actions(settings, status: dict) -> None:  # noqa: ANN001
    with card("quick"):
        title_block("Quick actions")
        with st.container(horizontal=True, gap="small"):
            if st.button("Review jobs", icon=":material/rate_review:", type="primary"):
                st.switch_page(page("jobs"))
            if st.button("Collect jobs", icon=":material/travel_explore:"):
                st.switch_page(page("command"))
            if status["scheduler_alive"] and st.button("Run discovery now", icon=":material/play_arrow:"):
                ctl.request_run_now(settings, "discovery")
                st.toast("Discovery queued. The scheduler picks it up within 15 seconds.")
            if st.button("Applications", icon=":material/fact_check:"):
                st.switch_page(page("applications"))


def render() -> None:
    settings = get_settings()
    profile = get_profile()
    status = system_status(settings)
    session = get_session()
    try:
        counts = job_counts(session)
        apps = app_counts(session)
        b, ad = profile.basics, profile.application_defaults
        with card("profile"):
            title_block(
                f"Hi {b.first_name}",
                f"{ad.current_title} at {ad.current_company} - {ad.total_experience_years:g} yrs - "
                f"{b.location.city}, notice {ad.notice_period_days} days",
            )
            _status_strip(settings, status)
        left, right = st.columns([1.25, 1], gap="medium")
        with left:
            _needs_action(session, settings, status, counts)
            _pipeline(counts, apps)
        with right:
            _quotas(session, settings)
            _quick_actions(settings, status)
            runs = repo.recent_runs(session, 5)
            if runs:
                with card("recent"):
                    title_block("Recent runs")
                    for r in runs:
                        tone = "ok" if r.status == "success" else ("bad" if r.status == "failed" else "warn")
                        st.markdown(
                            f"`{fmt_when(r.started_at)}` {r.run_type} ({r.trigger}) "
                            f":{'green' if tone == 'ok' else 'red' if tone == 'bad' else 'orange'}[{r.status}]"
                        )
    finally:
        session.close()
