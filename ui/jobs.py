"""Jobs: card list to triage, and a detail view (fit, tailored resume, JD) with a sticky action bar."""

from __future__ import annotations

import html
import json
import re

import streamlit as st

from core import repository as repo
from core.models import AppStatus, JobStatus, platform_for
from pipeline.runner import browser_holder, spawn_applier
from ui.components import browser_view_url, get_session, get_settings, pdf_preview
from ui.theme import action_bar, card, chip, page_header, platform_label, score_tone

FILTERS = {
    "To review": (JobStatus.READY_FOR_REVIEW.value,),
    "Approved": (JobStatus.APPROVED.value,),
    "Scored": (JobStatus.SCORED.value,),
    "Low fit": (JobStatus.ARCHIVED_LOW_FIT.value,),
    "Applied": (JobStatus.APPLIED.value,),
    "Skipped": (JobStatus.SKIPPED.value,),
}
PAGE_SIZE = 20


def _highlight(jd: str, terms: list[str]) -> str:
    out = jd
    for t in sorted(set(terms), key=len, reverse=True):
        head = re.split(r"\s*\(", t)[0]
        if len(head) < 2:
            continue
        out = re.sub(
            rf"(?<![A-Za-z0-9])({re.escape(head)})(?![A-Za-z0-9])", r"**\1**", out, flags=re.IGNORECASE
        )
    return out


def _score_badge(score: int | None, threshold: int) -> str:
    tone = score_tone(score, threshold)
    value = "–" if score is None else str(score)
    label = "unscored" if score is None else "fit"
    return f'<div class="laa-score {tone}">{value}<small>{label}</small></div>'


def _meta_chips(job, _threshold: int) -> str:  # noqa: ANN001
    parts = [chip(platform_label(job.source), "info")]
    remote = (job.remote_type or "").lower()
    if remote and remote != "unknown" and remote not in (job.location or "").lower():
        parts.append(chip(remote.title(), "mute"))
    if job.posted_at:
        parts.append(chip(job.posted_at[:10], "mute"))
    return f'<div class="laa-chips">{"".join(parts)}</div>'


def _approve(session, job, mode: str) -> None:  # noqa: ANN001
    repo.set_job_status(session, job, JobStatus.APPROVED)
    app = repo.application_for_job(session, job.id)
    if app:
        app.mode = mode
    session.commit()


def _apply_now(session, settings, job, mode: str) -> None:  # noqa: ANN001
    busy = browser_holder(settings)
    if busy:
        st.toast(f"Browser is busy ({busy.get('owner')}). Try again when it finishes.")
        return
    _approve(session, job, mode)
    spawn_applier(settings, job.id, mode)
    view = browser_view_url()
    st.toast("Copilot started. " + ("Watch it in the Browser view." if view else "A browser window opens."))


def _remove(session, job) -> None:  # noqa: ANN001
    repo.set_job_status(session, job, JobStatus.SKIPPED, "removed by user")
    app = repo.application_for_job(session, job.id)
    if app:
        repo.set_application_status(session, app, AppStatus.REJECTED, notes="removed from the job list")
    session.commit()


def _job_card(session, settings, job, threshold: int) -> None:  # noqa: ANN001
    breakdown = repo.job_fit_breakdown(job)
    matched = breakdown.get("matched_skills") or []
    shown = ", ".join(s.split(" (")[0] for s in matched[:4])
    if len(matched) > 4:
        shown += f" +{len(matched) - 4}"
    where = " · ".join(p for p in (job.company, job.location or None) if p)
    why = (job.fit_rationale or "").strip()
    if why.lower().startswith("heuristic") or "ANTHROPIC_API_KEY" in why:
        why = ""
    with card(f"job_{job.id}"):
        st.markdown(
            f'<div class="laa-job">{_score_badge(job.fit_score, threshold)}'
            f'<div class="laa-job-main"><p class="laa-title">{html.escape(job.title)}</p>'
            f'<p class="where">{html.escape(where)}</p>{_meta_chips(job, threshold)}'
            + (f'<div class="laa-muted">{html.escape(shown)}</div>' if shown else "")
            + (f'<p class="why">{html.escape(why)}</p>' if why else "")
            + "</div></div>",
            unsafe_allow_html=True,
        )
        approved = job.status == JobStatus.APPROVED.value
        done = job.status == JobStatus.APPLIED.value
        platform = platform_for(job.source, job.ats_type)
        mode = settings.copilot.default_mode_by_platform.get(platform, "review_mode")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button(
                "Approve",
                key=f"approve_{job.id}",
                type="primary",
                disabled=approved or done,
                width="stretch",
            ):
                _approve(session, job, mode)
                st.toast("Approved. The next apply batch picks it up.")
                st.rerun()
        with c2:
            if st.button("Apply now", key=f"apply_{job.id}", disabled=done, width="stretch"):
                _apply_now(session, settings, job, mode)
                st.rerun()
        with c3:
            if st.button("Remove", key=f"remove_{job.id}", width="stretch"):
                _remove(session, job)
                st.rerun()
        if st.button("Open", key=f"open_{job.id}", icon=":material/arrow_forward:", width="stretch"):
            st.query_params["job"] = job.id
            st.rerun()


def _list(session, settings) -> None:  # noqa: ANN001
    page_header(
        "Jobs", "Approve, apply, or remove a job from its card. Open it for the fit breakdown and resume."
    )
    choice = (
        st.segmented_control(
            "Show", list(FILTERS), default="To review", key="jobs_filter", label_visibility="collapsed"
        )
        or "To review"
    )
    query = st.text_input(
        "Search title or company",
        key="jobs_q",
        placeholder="Search title or company",
        label_visibility="collapsed",
    )
    jobs = [j for j in repo.jobs_by_status(session, *FILTERS[choice]) if j.canonical_job_id is None]
    if query:
        q = query.lower()
        jobs = [j for j in jobs if q in j.title.lower() or q in j.company.lower()]
    jobs.sort(key=lambda j: (j.fit_score is None, -(j.fit_score or 0)))
    if not jobs:
        st.info(f"No jobs under '{choice}'. Collect more from the Command Center.")
        return
    limit = st.session_state.get("jobs_limit", PAGE_SIZE)
    st.caption(f"{len(jobs)} job(s), highest fit first")
    threshold = settings.matching.fit_threshold
    shown = jobs[:limit]
    for start in range(0, len(shown), 2):
        cols = st.columns(2)
        for col, job in zip(cols, shown[start : start + 2], strict=False):
            with col:
                _job_card(session, settings, job, threshold)
    if len(jobs) > limit and st.button(f"Show more ({len(jobs) - limit} left)", width="stretch"):
        st.session_state["jobs_limit"] = limit + PAGE_SIZE
        st.rerun()


def _fit(job) -> None:  # noqa: ANN001
    breakdown = repo.job_fit_breakdown(job)
    for name, v in (breakdown.get("criteria") or {}).items():
        st.progress(
            min(v["score"] / max(v["max"], 1), 1.0),
            text=f"{name.replace('_', ' ')}: {v['score']}/{v['max']}",
        )
        if v.get("evidence"):
            st.caption(v["evidence"][:220])
    if job.fit_rationale:
        st.info(job.fit_rationale)
    matched = breakdown.get("matched_skills", [])
    missing = breakdown.get("missing_skills", [])
    if matched:
        st.markdown("**Matched skills:** " + ", ".join(matched))
    if missing:
        st.markdown("**JD asks for (not in your profile):** " + ", ".join(missing))


def _detail(session, settings, job_id: str) -> None:  # noqa: ANN001
    job = repo.get_job(session, job_id)
    if st.button("All jobs", icon=":material/arrow_back:", type="tertiary"):
        del st.query_params["job"]
        st.rerun()
    if job is None:
        st.warning("That job no longer exists.")
        return
    where = " · ".join(p for p in (job.company, job.location or "location n/a") if p)
    st.markdown(
        f'<div class="laa-job">{_score_badge(job.fit_score, settings.matching.fit_threshold)}'
        f'<div class="laa-job-main"><p class="laa-title">{html.escape(job.title)}</p>'
        f'<p class="where">{html.escape(where)}</p>{_meta_chips(job, settings.matching.fit_threshold)}</div></div>',
        unsafe_allow_html=True,
    )
    st.link_button("Open posting", job.apply_url, icon=":material/open_in_new:")

    fit_tab, resume_tab, jd_tab = st.tabs(["Fit", "Tailored resume", "Job description"])
    with fit_tab:
        _fit(job)
    with resume_tab:
        resume = repo.latest_resume(session, job.id)
        if resume is None:
            st.caption("No tailored resume (below the fit threshold, or not prepared yet).")
        else:
            st.markdown(
                f"Summary variant `{resume.summary_variant}` - ATS keyword match **{resume.ats_keyword_score}%**"
            )
            missing_kw = json.loads(resume.missing_keywords or "[]")
            if missing_kw:
                st.caption("Profile skills in the JD that did not fit on the page: " + ", ".join(missing_kw))
            pdf_preview(resume.pdf_path, height=560)
            st.caption(f"Every line is verified against {settings.app.profile_path} (no invented facts).")
    with jd_tab:
        st.markdown(_highlight(job.jd_text, repo.job_fit_breakdown(job).get("matched_skills", [])))

    app = repo.application_for_job(session, job.id)
    platform = platform_for(job.source, job.ats_type)
    mode = "review_mode"
    if platform == "direct_ats":
        default_mode = settings.copilot.default_mode_by_platform.get(platform, "review_mode")
        with st.expander("Copilot mode"):
            mode = st.radio(
                "Mode",
                ["review_mode", "auto_submit_ats"],
                index=0 if default_mode == "review_mode" else 1,
                format_func=lambda m: (
                    "Review before submit" if m == "review_mode" else "Auto-submit verified ATS forms"
                ),
                help="Auto-submit only acts on Greenhouse / Lever / Ashby forms where every field mapped with high confidence.",
            )
    else:
        st.caption(
            "After you approve, Naukri and LinkedIn apply on their own, including the Naukri question chat "
            "and a company website. It stops for OTP, CAPTCHA, or a question that is not in your profile."
        )

    with action_bar(f"job_{job.id}"):
        approved = job.status == JobStatus.APPROVED.value
        if st.button("Approve", type="primary", disabled=approved, icon=":material/check:"):
            _approve(session, job, mode)
            st.toast("Approved. The next scheduled apply batch picks it up.")
            st.rerun()
        if st.button("Apply now", icon=":material/rocket_launch:"):
            _apply_now(session, settings, job, mode)
        if st.button("Skip", icon=":material/close:"):
            _remove(session, job)
            del st.query_params["job"]
            st.rerun()
        if st.button(
            "Mark applied", icon=":material/done_all:", help="You applied yourself, outside the copilot"
        ):
            repo.set_job_status(session, job, JobStatus.APPLIED)
            if app is None:
                app = repo.ensure_application(session, job, mode=mode)
            repo.set_application_status(
                session, app, AppStatus.APPLIED, submitted_by="manual", notes="applied manually"
            )
            session.commit()
            st.rerun()
    view = browser_view_url()
    if view:
        st.link_button("Browser view", view, icon=":material/desktop_windows:", type="tertiary")


def render() -> None:
    settings = get_settings()
    session = get_session()
    try:
        job_id = st.query_params.get("job")
        if job_id:
            _detail(session, settings, job_id)
        else:
            _list(session, settings)
    finally:
        session.close()
