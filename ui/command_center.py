"""Command Center: run discovery, collect from LinkedIn / Naukri, approve copilot submits, answer questions."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from core import repository as repo
from core.models import FormSessionStatus
from pipeline.browser_run import ACTIVE_STATES, pid_alive, read_state
from pipeline.runner import (
    browser_holder,
    pending_job_count,
    process_pending_jobs,
    request_browser_stop,
    run_discovery,
    start_browser_collection,
)
from ui.components import browser_view_url, fmt_when, get_quota, get_session, get_settings
from ui.theme import card, chips, page_header, title_block

_LABELS = {
    "starting": "Starting",
    "running": "Running",
    "stopping": "Stopping after the current card",
    "stopped": "Stopped (fetched jobs kept)",
    "done": "Finished",
    "paused": "Paused on friction",
    "failed": "Failed",
}


def pending_job_count_safe(settings) -> int:  # noqa: ANN001
    try:
        return pending_job_count(settings)
    except Exception:  # noqa: BLE001
        return 0


def _result(res) -> None:  # noqa: ANN001
    stats = {k: v for k, v in res.stats.items() if isinstance(v, int) and v}
    (st.success if res.status == "success" else st.warning)(
        f"{res.status}: " + (", ".join(f"{k} {v}" for k, v in stats.items()) or "nothing new")
    )
    if res.error:
        st.error(res.error)


def _discovery(settings, session) -> None:  # noqa: ANN001
    with card("discovery"):
        title_block(
            "Job boards (Greenhouse / Lever / Ashby)",
            "Public APIs, no browser needed. Fetches, filters, scores and tailors resumes for good fits.",
        )
        pending = pending_job_count_safe(settings)
        with st.container(horizontal=True, gap="small"):
            if st.button("Fetch job boards now", type="primary", icon=":material/cloud_download:"):
                # SQLite allows one writer: release this page's transaction before discovery opens its own.
                session.commit()
                with st.spinner("Fetching, deduplicating, scoring, tailoring..."):
                    _result(run_discovery(settings, trigger="manual", include_browser=False))
            if st.button(
                f"Score fetched jobs ({pending})",
                icon=":material/calculate:",
                disabled=pending == 0,
                help="Dedupe, score and tailor jobs saved by browser collection that are not scored yet.",
            ):
                session.commit()
                with st.spinner("Scoring and tailoring saved jobs..."):
                    _result(process_pending_jobs(settings, trigger="manual"))


def _collection(settings) -> None:  # noqa: ANN001
    with card("collection"):
        title_block(
            "Browser collection (LinkedIn / Naukri)",
            "Opens the logged-in browser and saves each posting as it is read. Stop any time and keep what "
            "was fetched; start again to continue. Postings you already have are skipped without opening.",
        )
        now = read_state(settings)
        running = now.get("state") in ACTIVE_STATES and pid_alive(now.get("pid"))
        c1, c2 = st.columns([2, 1])
        platforms = c1.multiselect(
            "Platforms",
            ["linkedin", "naukri"],
            default=["linkedin", "naukri"],
            key="bc_platforms",
            disabled=running,
        )
        cap = c2.number_input(
            "Stop after N new (0 = no cap)",
            min_value=0,
            max_value=1000,
            value=40,
            step=10,
            key="bc_cap",
            disabled=running,
        )
        with st.container(horizontal=True, gap="small"):
            if st.button(
                "Start / continue",
                type="primary",
                icon=":material/play_arrow:",
                disabled=running or not platforms,
            ):
                busy = browser_holder(settings)
                if busy:
                    st.warning(f"Browser is in use by {busy.get('owner')}. Start again when it finishes.")
                elif start_browser_collection(settings, platforms, int(cap), trigger="manual") is None:
                    st.warning("A collection is already running.")
                else:
                    st.toast("Collection started")
            if st.button(
                "Stop, keep fetched", icon=":material/stop:", disabled=not running
            ) and request_browser_stop(settings):
                st.toast("Stopping after the current card")
            view = browser_view_url()
            if view:
                st.link_button("Browser view", view, icon=":material/desktop_windows:")
        _progress(settings, int(cap))


@st.fragment(run_every=3)
def _progress(settings, cap: int) -> None:  # noqa: ANN001
    state = read_state(settings)
    if not state:
        st.caption("No collection has run yet.")
        return
    raw = state.get("state", "")
    if raw in ACTIVE_STATES and not pid_alive(state.get("pid")):
        label, tone = "Ended unexpectedly (process is gone)", "bad"
    else:
        label = _LABELS.get(raw, raw or "unknown")
        if state.get("stopped_reason") and raw in ("stopped", "done"):
            label += f" - {state['stopped_reason']}"
        tone = {
            "running": "info",
            "starting": "info",
            "stopping": "warn",
            "done": "ok",
            "stopped": "ok",
            "paused": "warn",
            "failed": "bad",
        }.get(raw, "mute")
    c = state.get("counters", {}) or {}
    saved = c.get("saved", 0)
    chips(
        [
            (label, tone),
            (f"{saved} saved", "ok" if saved else "mute"),
            (f"{c.get('seen', 0)} seen", "mute"),
            (f"{c.get('known', 0)} already had", "mute"),
            (f"{c.get('prefiltered', 0) + c.get('rejected', 0)} filtered", "mute"),
            (f"on {state['platform']}", "mute") if state.get("platform") else ("", "mute"),
        ],
        dot=False,
    )
    if cap > 0 and raw in ACTIVE_STATES:
        st.progress(min(saved / cap, 1.0), text=f"{saved} of {cap} new postings")
    reasons = state.get("filter_reasons") or {}
    if reasons:
        st.caption(
            "Filtered before opening: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in reasons.items())
        )
    for msg in state.get("friction") or []:
        st.warning(f"Paused: {msg}")
    for msg in (state.get("errors") or [])[-3:]:
        st.error(msg)
    if state.get("error"):
        st.error(state["error"])
    st.caption(f"Updated {fmt_when(state.get('updated_at'))}")


def _copilot(settings, session) -> None:  # noqa: ANN001
    quota = get_quota()
    paused = repo.paused_sessions(session)
    with card("copilot"):
        title_block(
            "Copilot: waiting on you",
            None if paused else "No application is waiting. Approve jobs on the Jobs page and use Apply now.",
        )
        for fs, job in paused:
            label = f"{job.title} at {job.company}"
            kind = (
                "Ready to submit"
                if fs.status == FormSessionStatus.AWAITING_REVIEW.value
                else (f"Paused: {fs.friction_type or 'friction'}")
            )
            with st.expander(f"{kind} - {label}", expanded=True):
                payload = json.loads(fs.friction_payload) if fs.friction_payload else {}
                if payload.get("message") or payload.get("detail"):
                    st.caption(payload.get("message") or payload.get("detail"))
                if fs.screenshot_path:
                    st.image(fs.screenshot_path, caption="Form when it paused", width="stretch")
                if fs.field_map:
                    with st.popover("Filled fields"):
                        st.dataframe(pd.DataFrame(json.loads(fs.field_map)), hide_index=True, width="stretch")
                with st.container(horizontal=True, gap="small"):
                    if fs.status == FormSessionStatus.AWAITING_REVIEW.value and st.button(
                        "Approve submit", key=f"appr_{fs.id}", type="primary", icon=":material/send:"
                    ):
                        fs.status = "submit_approved"
                        session.commit()
                        st.toast("Approved. The copilot clicks Submit if its browser tab is still open.")
                    if st.button("I submitted it myself", key=f"man_{fs.id}"):
                        app = repo.application_for_job(session, job.id)
                        if app:
                            repo.set_application_status(
                                session,
                                app,
                                "Applied",
                                submitted_by="manual",
                                notes="marked applied from dashboard",
                            )
                        repo.set_job_status(session, job, "applied")
                        repo.finish_form_session(
                            session,
                            fs,
                            status=FormSessionStatus.SUBMITTED.value,
                            screenshot_path=fs.screenshot_path,
                        )
                        quota.record_submission(session, fs.platform)
                        session.commit()
                        st.rerun()
                    if st.button("Abandon", key=f"ab_{fs.id}", type="tertiary"):
                        fs.status = FormSessionStatus.ABANDONED.value
                        session.commit()
                        st.rerun()
                    st.link_button("Open job", job.apply_url, type="tertiary")


def _screening(session) -> None:  # noqa: ANN001
    if repo.merge_duplicate_questions(session):
        session.commit()
    pending = repo.unapproved_screening_answers(session)
    remembered = repo.approved_screening_answers(session)
    with card("screening"):
        title_block(
            "Questions only you can answer",
            "Answer once; the copilot reuses it on every similar question and retries the jobs that stopped on it."
            if pending
            else "Nothing to answer.",
        )
        for sa in pending:
            with st.form(key=f"sa_{sa.id}", border=False):
                st.markdown(f"**{sa.question_text}**")
                ans = st.text_area(
                    "Answer", value=sa.answer or "", key=f"sa_txt_{sa.id}", label_visibility="collapsed"
                )
                with st.container(horizontal=True, gap="small"):
                    if st.form_submit_button("Save answer", type="primary"):
                        if not ans.strip():
                            st.warning("Type an answer first.")
                        else:
                            sa.answer = ans.strip()
                            sa.approved = 1
                            requeued = repo.requeue_jobs_waiting_on(session, sa.question_text)
                            session.commit()
                            if requeued:
                                st.toast(f"Saved. {requeued} job(s) back in the apply queue.")
                            st.rerun()
                    if st.form_submit_button("Discard"):
                        session.delete(sa)
                        session.commit()
                        st.rerun()
        if remembered:
            with st.expander(f"Your remembered answers ({len(remembered)})"):
                for sa in remembered:
                    with st.form(key=f"sa_ok_{sa.id}", border=False):
                        st.markdown(f"**{sa.question_text}**")
                        ans = st.text_area(
                            "Answer",
                            value=sa.answer or "",
                            key=f"sa_ok_txt_{sa.id}",
                            label_visibility="collapsed",
                        )
                        with st.container(horizontal=True, gap="small"):
                            if st.form_submit_button("Update"):
                                sa.answer = ans.strip()
                                session.commit()
                                st.toast("Updated")
                            if st.form_submit_button("Forget", type="tertiary"):
                                session.delete(sa)
                                session.commit()
                                st.rerun()


def _runs(session) -> None:  # noqa: ANN001
    runs = repo.recent_runs(session, 20)
    if not runs:
        return
    with st.expander("Recent runs"):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "started": fmt_when(r.started_at),
                        "type": r.run_type,
                        "trigger": r.trigger,
                        "status": r.status,
                        "stats": ", ".join(
                            f"{k} {v}" for k, v in repo.run_stats(r).items() if isinstance(v, int) and v
                        ),
                        "error": r.error or "",
                    }
                    for r in runs
                ]
            ),
            width="stretch",
            hide_index=True,
        )


def render() -> None:
    settings = get_settings()
    session = get_session()
    try:
        page_header("Command Center", "Collect jobs, then handle anything the copilot paused on.")
        left, right = st.columns([1.15, 1], gap="medium")
        with left:
            _collection(settings)
            _discovery(settings, session)
        with right:
            _copilot(settings, session)
            _screening(session)
        _runs(session)
    finally:
        session.close()
