"""Outreach: drafted recruiter / referral messages to copy and send yourself. Nothing is ever auto-sent."""

from __future__ import annotations

import streamlit as st

from core import repository as repo
from core.models import AppStatus
from ui.components import get_session
from ui.theme import card, chips, page_header, title_block

CHANNEL_LABEL = {
    "linkedin_connect": "LinkedIn connection note",
    "email": "Email / InMail",
    "inmail": "InMail",
    "peer_referral": "Referral request",
}
FILTERS = {
    "To send": ("drafted",),
    "Sent": ("sent", "replied"),
    "All": ("drafted", "sent", "replied", "skipped"),
}


def _mark_sent(session, m, job_id: str) -> None:  # noqa: ANN001
    repo.set_outreach_status(session, m, "sent")
    app = repo.application_for_job(session, job_id)
    if app and app.status in (
        AppStatus.READY_FOR_REVIEW.value,
        AppStatus.APPLIED.value,
        AppStatus.DISCOVERED.value,
    ):
        repo.set_application_status(
            session, app, AppStatus.OUTREACH_SENT, notes=f"outreach sent via {m.channel}"
        )


def render() -> None:
    session = get_session()
    try:
        page_header("Outreach", "Copy a message, send it yourself on LinkedIn or email, then mark it sent.")
        choice = (
            st.segmented_control(
                "Show", list(FILTERS), default="To send", key="outreach_filter", label_visibility="collapsed"
            )
            or "To send"
        )
        rows = repo.outreach_by_status(session, *FILTERS[choice])
        if not rows:
            st.info("Nothing here. Drafts are created for every job that passes the fit threshold.")
            return
        by_job: dict[str, list] = {}
        for m, j, c in rows:
            by_job.setdefault(j.id, []).append((m, j, c))
        st.caption(f"{len(rows)} message(s) for {len(by_job)} job(s)")
        for job_id, items in by_job.items():
            job = items[0][1]
            with card(f"out_{job_id}"):
                title_block(job.title, job.company)
                for c in repo.contacts_for_job(session, job_id):
                    who = f"**{c.contact_type.title()}:** {c.name}" + (f" - {c.role}" if c.role else "")
                    hint = (
                        c.email_guess
                        and f"email guess `{c.email_guess}`"
                        or (c.email_pattern and f"email pattern `{c.email_pattern}`")
                    )
                    with st.container(horizontal=True, vertical_alignment="center"):
                        st.markdown(who + (f"  \n{hint}" if hint else ""), width="stretch")
                        if c.profile_url:
                            st.link_button(
                                "Find", c.profile_url, icon=":material/person_search:", type="tertiary"
                            )
                for m, _j, _c in items:
                    chips(
                        [
                            (CHANNEL_LABEL.get(m.channel, m.channel), "info"),
                            (f"{m.char_count} chars / {m.word_count} words", "mute"),
                            (m.status, "ok" if m.status in ("sent", "replied") else "mute"),
                        ]
                    )
                    st.code(m.body, language=None, wrap_lines=True)
                    with st.container(horizontal=True, gap="small"):
                        if m.status == "drafted":
                            if st.button(
                                "Mark sent", key=f"sent_{m.id}", type="primary", icon=":material/send:"
                            ):
                                _mark_sent(session, m, job_id)
                                session.commit()
                                st.rerun()
                            if st.button("Skip", key=f"skip_{m.id}"):
                                repo.set_outreach_status(session, m, "skipped")
                                session.commit()
                                st.rerun()
                        elif m.status == "sent" and st.button(
                            "Undo sent", key=f"undo_{m.id}", type="tertiary"
                        ):
                            repo.set_outreach_status(session, m, "drafted")
                            session.commit()
                            st.rerun()
    finally:
        session.close()
