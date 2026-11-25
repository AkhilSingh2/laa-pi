"""laa-pi dashboard. Run with: uv run streamlit run app.py"""

from __future__ import annotations

import html

import streamlit as st

from core.db import init_db
from core.logging import setup_logging
from ui import applications, command_center, home, jobs, outreach, scheduler_page, settings_page
from ui.components import get_profile, get_settings, system_status
from ui.theme import apply_theme, chip, initials

st.set_page_config(
    page_title="laa-pi | Job Copilot",
    page_icon="assets/logo_icon.svg",
    layout="wide",
    initial_sidebar_state="collapsed",
)
apply_theme()

settings = get_settings()
setup_logging(settings.app.log_level)
init_db(settings.path(settings.app.db_path))
profile = get_profile()

PAGES = {
    "home": st.Page(home.render, title="Home", icon=":material/home:", url_path="home", default=True),
    "jobs": st.Page(jobs.render, title="Jobs", icon=":material/work:", url_path="jobs"),
    "applications": st.Page(
        applications.render, title="Applications", icon=":material/fact_check:", url_path="applications"
    ),
    "outreach": st.Page(outreach.render, title="Outreach", icon=":material/forum:", url_path="outreach"),
    "command": st.Page(
        command_center.render, title="Command Center", icon=":material/bolt:", url_path="command-center"
    ),
    "scheduler": st.Page(
        scheduler_page.render, title="Scheduler", icon=":material/schedule:", url_path="scheduler"
    ),
    "settings": st.Page(
        settings_page.render, title="Settings", icon=":material/settings:", url_path="settings"
    ),
}
st.session_state["_pages"] = PAGES

st.logo("assets/logo.svg", icon_image="assets/logo_icon.svg", size="large")
nav = st.navigation(list(PAGES.values()), position="top")


MOBILE_TABS = [
    ("home", "Home"),
    ("jobs", "Jobs"),
    ("applications", "Apps"),
    ("outreach", "Outreach"),
    ("command", "Run"),
    ("scheduler", "Scheduler"),
    ("settings", "Settings"),
]


def _mobile_tabs() -> None:
    # Streamlit folds the top nav into a hidden menu on phones; keep the pages one tap away instead.
    with st.container(key="only_mobile_tabs", horizontal=True, gap="small"):
        for name, label in MOBILE_TABS:
            st.page_link(PAGES[name], label=label, icon=PAGES[name].icon)


def _status_chips() -> list[str]:
    status = system_status(settings)
    hb = status["heartbeat"]
    items = []
    if status["scheduler_alive"]:
        paused = status["control"].get("paused_all")
        label = "Scheduler paused" if paused else f"Scheduler {hb.get('state', 'idle')}"
        items.append(chip(label, "warn" if paused else "ok", dot=True))
    else:
        items.append(chip("Scheduler off", "mute", dot=True))
    if status["collecting"]:
        items.append(chip("Collecting jobs", "info", dot=True))
    elif status["browser_holder"]:
        items.append(chip(f"Browser: {status['browser_holder'].get('owner', 'busy')}", "info", dot=True))
    ai = status["ai"]["set"]
    items.append(chip("AI on" if ai else "AI off", "ok" if ai else "warn", dot=True))
    return items


def _header(show_status: bool) -> None:
    items = _status_chips() if show_status else []
    b = profile.basics
    st.markdown(
        f'<div class="laa-head"><a class="laa-me" href="settings" target="_self">'
        f'<div class="laa-avatar">{initials(b.full_name)}</div>'
        f'<div><div class="n">{html.escape(b.full_name)}</div><div class="h">{html.escape(b.headline or "")}</div></div>'
        f'</a><div class="laa-chips">{"".join(items)}</div></div>',
        unsafe_allow_html=True,
    )


_mobile_tabs()
_header(show_status=nav.title != "Home")
nav.run()
