"""Shared Streamlit helpers: cached settings/profile, DB sessions, small widgets."""

from __future__ import annotations

import base64
import os
from datetime import date
from pathlib import Path
from typing import Any

import streamlit as st
from sqlalchemy import func, select

from core import scheduler_control as ctl
from core.db import session_factory
from core.llm import LLMClient
from core.profile import MasterProfile, load_profile
from core.quota import QuotaManager
from core.schema import Application, Job
from core.settings import Settings, load_settings

HEARTBEAT = ctl.HEARTBEAT_FILE


@st.cache_resource(show_spinner=False)
def get_settings() -> Settings:
    return load_settings()


@st.cache_resource(show_spinner=False)
def get_profile() -> MasterProfile:
    s = get_settings()
    return load_profile(s.path(s.app.profile_path))


def reload_settings() -> None:
    """Call after saving overrides / profile so every page sees the new values."""
    get_settings.clear()
    get_profile.clear()


def get_session():
    s = get_settings()
    return session_factory(s.path(s.app.db_path))()


def get_quota() -> QuotaManager:
    s = get_settings()
    return QuotaManager(s.quotas, s.app.timezone)


def llm_status() -> tuple[bool, str]:
    s = get_settings()
    ok = LLMClient(s.llm).available
    return ok, f"{s.llm.model} ({'key loaded' if ok else s.llm.api_key_env + ' missing -> heuristic mode'})"


def scheduler_heartbeat() -> dict | None:
    return ctl.read_heartbeat(get_settings())


def page(name: str):  # noqa: ANN201
    """st.Page registered in app.py, for st.page_link / st.switch_page."""
    return st.session_state["_pages"][name]


def fmt_when(iso: str | None) -> str:
    """'Thu 19 Nov 09:00' in local time, or '-'."""
    from datetime import datetime

    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%a %d %b %H:%M")


def browser_view_url() -> str | None:
    """Where to watch / drive the server-side browser (noVNC in the Docker image); None when running locally."""
    return os.environ.get("BROWSER_VIEW_URL") or None


def system_status(settings: Settings) -> dict[str, Any]:
    from copilot.connections import read_connections
    from core.browser_lock import holder
    from core.secrets import secret_status
    from pipeline.browser_run import is_running, read_state

    control = ctl.read_control(settings)
    return {
        "scheduler_alive": ctl.scheduler_alive(settings),
        "heartbeat": ctl.read_heartbeat(settings) or {},
        "control": control,
        "browser_holder": holder(settings),
        "collection": read_state(settings),
        "collecting": is_running(settings),
        "ai": secret_status(settings.llm.api_key_env),
        "connections": read_connections(settings),
    }


def job_counts(session) -> dict[str, int]:  # noqa: ANN001
    rows = session.execute(
        select(Job.status, func.count()).where(Job.canonical_job_id.is_(None)).group_by(Job.status)
    ).all()
    return {status: n for status, n in rows}


def app_counts(session) -> dict[str, int]:  # noqa: ANN001
    rows = session.execute(select(Application.status, func.count()).group_by(Application.status)).all()
    return {status: n for status, n in rows}


def follow_ups_due(session) -> int:  # noqa: ANN001
    today = date.today().isoformat()
    return session.execute(
        select(func.count())
        .select_from(Application)
        .where(
            Application.follow_up_at.is_not(None),
            Application.follow_up_at != "",
            Application.follow_up_at <= today,
        )
    ).scalar_one()


def pdf_preview(path: str | Path, height: int = 720) -> None:
    p = Path(path)
    if not p.exists():
        st.warning(f"PDF not found: {p}")
        return
    data = base64.b64encode(p.read_bytes()).decode()
    st.markdown(
        f'<iframe src="data:application/pdf;base64,{data}" width="100%" height="{height}" type="application/pdf"></iframe>',
        unsafe_allow_html=True,
    )
    st.download_button(
        "Download PDF", p.read_bytes(), file_name=p.name, mime="application/pdf", key=f"dl_{p.name}"
    )


def score_badge(score: int | None, threshold: int) -> str:
    if score is None:
        return "unscored"
    if score >= 90:
        return f"{score} (excellent)"
    if score >= threshold:
        return f"{score} (qualified)"
    return f"{score} (below threshold)"


def profile_health(profile: MasterProfile) -> list[str]:
    """Non-blocking warnings about profile fields that affect application forms."""
    issues = []
    ad = profile.application_defaults
    if ad.current_ctc_lpa == 0 or ad.expected_ctc_lpa == 0:
        issues.append(
            "current_ctc_lpa / expected_ctc_lpa are 0 in master_profile.json; CTC fields will be filled with 0."
        )
    if not profile.basics.links.portfolio:
        issues.append("basics.links.portfolio is empty; portfolio fields will be left blank.")
    if not profile.projects:
        issues.append("projects is empty; the Projects section is omitted from resumes.")
    return issues
