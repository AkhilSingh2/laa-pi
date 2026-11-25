"""Applications: status tiles, cards on phones, editable table on desktop, CSV export."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from core import repository as repo
from core.models import utcnow_iso
from ui.components import get_session, get_settings
from ui.theme import card, chip, page_header, platform_label, tiles, title_block

_TONE = {
    "Applied": "info",
    "Outreach Sent": "info",
    "Interview": "ok",
    "Rejected": "bad",
    "Ghosted": "mute",
    "Ready for Review": "warn",
    "Discovered": "mute",
}


def _frame(rows) -> pd.DataFrame:  # noqa: ANN001
    return pd.DataFrame(
        [
            {
                "id": a.id,
                "status": a.status,
                "company": j.company,
                "title": j.title,
                "fit": j.fit_score,
                "platform": a.platform,
                "applied_at": (a.applied_at or "")[:10],
                "follow_up_at": a.follow_up_at or "",
                "notes": a.notes or "",
                "url": j.apply_url,
            }
            for a, j in rows
        ]
    )


def _save_one(session, app, status: str, notes: str, follow_up: str) -> int:  # noqa: ANN001
    changed = 0
    if status != app.status:
        repo.set_application_status(session, app, status, notes="status edited in dashboard")
        changed += 1
    if (notes or "") != (app.notes or ""):
        app.notes = notes or None
        app.last_activity_at = utcnow_iso()
        changed += 1
    if (follow_up or "") != (app.follow_up_at or ""):
        app.follow_up_at = follow_up or None
        changed += 1
    return changed


_SOURCE = {
    "llm_guideline": "written by AI",
    "approved_answer": "your saved answer",
    "qa_bank": "your defaults",
    "naukri_chat": "chat reply",
    "resume_pdf": "resume",
    "unresolved": "left blank",
}


def _answers(session, job_id: str) -> None:  # noqa: ANN001
    sent = repo.last_answers_sent(session, job_id)
    if not sent:
        st.caption("No form answers recorded for this application.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "question": m.get("label") or "",
                    "answer": "" if m.get("value") is None else str(m["value"]),
                    "from": _SOURCE.get(m.get("value_source", ""), "profile")
                    if not str(m.get("value_source", "")).startswith("profile:")
                    else "profile",
                }
                for m in sent
                if m.get("value") not in (None, "") or m.get("required")
            ]
        ),
        hide_index=True,
        width="stretch",
        column_config={"answer": st.column_config.TextColumn("answer", width="large")},
    )


def _cards(session, settings, rows) -> None:  # noqa: ANN001
    statuses = settings.tracker.statuses
    for a, j in rows:
        with card(f"app_{a.id}"):
            title_block(j.title, f"{j.company} - {platform_label(a.platform)}")
            parts = [chip(a.status, _TONE.get(a.status, "mute"))]
            if a.applied_at:
                parts.append(chip(f"Applied {a.applied_at[:10]}", "mute"))
            if a.follow_up_at:
                due = a.follow_up_at <= date.today().isoformat()
                parts.append(chip(f"Follow up {a.follow_up_at}", "warn" if due else "mute"))
            st.markdown(f'<div class="laa-chips">{"".join(parts)}</div>', unsafe_allow_html=True)
            with st.expander("Update"), st.form(f"appf_{a.id}", border=False):
                status = st.selectbox(
                    "Status", statuses, index=statuses.index(a.status) if a.status in statuses else 0
                )
                follow = st.date_input(
                    "Follow up on",
                    value=date.fromisoformat(a.follow_up_at) if a.follow_up_at else None,
                    format="YYYY-MM-DD",
                )
                notes = st.text_area("Notes", value=a.notes or "", height=80)
                if st.form_submit_button("Save", type="primary"):
                    if _save_one(session, a, status, notes.strip(), follow.isoformat() if follow else ""):
                        session.commit()
                        st.toast("Saved")
                        st.rerun()
            with st.expander("Answers sent"):
                _answers(session, j.id)
            st.link_button("Open posting", j.apply_url, type="tertiary", icon=":material/open_in_new:")


def _table(session, settings, rows) -> None:  # noqa: ANN001
    df = _frame(rows)
    edited = st.data_editor(
        df,
        key="tracker_editor",
        width="stretch",
        hide_index=True,
        disabled=["id", "company", "title", "fit", "platform", "applied_at", "url"],
        column_config={
            "id": None,
            "status": st.column_config.SelectboxColumn(
                "Status", options=settings.tracker.statuses, required=True
            ),
            "company": "Company",
            "title": "Role",
            "fit": st.column_config.NumberColumn("Fit", format="%d"),
            "platform": "Platform",
            "applied_at": "Applied",
            "url": st.column_config.LinkColumn("Posting", display_text="open"),
            "notes": st.column_config.TextColumn("Notes", width="large"),
            "follow_up_at": st.column_config.TextColumn("Follow up (YYYY-MM-DD)"),
        },
    )
    if st.button("Save table changes", type="primary"):
        by_id = {a.id: a for a, _ in rows}
        changed = sum(
            _save_one(session, by_id[int(r["id"])], r["status"], r["notes"] or "", r["follow_up_at"] or "")
            for _, r in edited.iterrows()
        )
        session.commit()
        st.toast(f"Saved {changed} change(s)")
        st.rerun()
    applied = [(a, j) for a, j in rows if a.applied_at]
    if applied:
        with st.expander("Answers sent"):
            pick = st.selectbox(
                "Application",
                range(len(applied)),
                format_func=lambda i: f"{applied[i][1].title} at {applied[i][1].company}",
                key="answers_pick",
            )
            _answers(session, applied[pick][1].id)


def render() -> None:
    settings = get_settings()
    session = get_session()
    try:
        page_header("Applications", "Everything you applied to, with status and follow-ups.")
        ghosted = repo.mark_ghosted(session, settings.tracker.ghosted_after_days)
        if ghosted:
            session.commit()
            st.caption(
                f"Marked {ghosted} as Ghosted after {settings.tracker.ghosted_after_days} days of silence."
            )
        rows = repo.all_applications(session)
        if not rows:
            st.info("No applications yet. Approve jobs on the Jobs page to start.")
            return
        counts: dict[str, int] = {}
        for a, _ in rows:
            counts[a.status] = counts.get(a.status, 0) + 1
        tiles([(s, counts.get(s, 0), s == "Interview") for s in settings.tracker.statuses])
        st.write("")
        shown = (
            st.segmented_control(
                "Filter",
                ["All", *settings.tracker.statuses],
                default="All",
                key="apps_filter",
                label_visibility="collapsed",
            )
            or "All"
        )
        if shown != "All":
            rows = [(a, j) for a, j in rows if a.status == shown]
        with st.container(key="only_mobile_apps"):
            _cards(session, settings, rows)
        with st.container(key="only_desktop_apps"):
            _table(session, settings, rows)
        csv = _frame(repo.all_applications(session)).drop(columns=["id"]).to_csv(index=False).encode()
        st.download_button(
            "Export CSV", csv, file_name="applications.csv", mime="text/csv", icon=":material/download:"
        )
        export_path = settings.path(settings.tracker.csv_export_path)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_bytes(csv)
    finally:
        session.close()
