"""Settings: profile, portal connections, AI key, and guardrails (saved to data/settings_overrides.yaml)."""

from __future__ import annotations

import re

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from copilot.connections import PLATFORMS, read_connections
from core import scheduler_control as ctl
from core.secrets import remove_secret, secret_status, set_secret
from core.settings import save_overrides
from pipeline.runner import browser_holder, spawn_connection
from ui.components import (
    browser_view_url,
    fmt_when,
    get_settings,
    reload_settings,
)
from ui.theme import card, chips, page_header, platform_label, title_block

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _save(updates: dict) -> bool:
    settings = get_settings()
    try:
        save_overrides(updates, root=settings.root)
    except (ValidationError, ValueError) as e:
        st.error(f"Not saved: {e}")
        return False
    reload_settings()
    ctl.bump_config_version(get_settings())
    st.toast("Saved")
    return True


# ---------------------------------------------------------------------- profile


def _profile_tab() -> None:
    from ui.profile_form import render as render_profile

    render_profile()


# ---------------------------------------------------------------------- connections


def _connections_tab() -> None:
    settings = get_settings()
    view = browser_view_url()
    st.caption(
        "Log in once by typing your credentials into the real LinkedIn / Naukri page; the browser profile keeps "
        "you signed in. This app never sees or stores your passwords."
        + (
            " On the server, the browser appears in Browser view."
            if view
            else " A browser window opens on this computer."
        )
    )
    if view:
        st.link_button("Open Browser view", view, icon=":material/desktop_windows:")
    _connection_cards(settings)


@st.fragment(run_every=4)
def _connection_cards(settings) -> None:  # noqa: ANN001
    conns = read_connections(settings)
    busy = browser_holder(settings)
    if busy:
        st.info(
            f"Browser in use by {busy.get('owner')} since {fmt_when(busy.get('since'))}.",
            icon=":material/lock:",
        )
    for platform in PLATFORMS:
        c = conns.get(platform, {})
        with card(f"conn_{platform}"):
            title_block(platform_label(platform), c.get("detail") or "Not checked yet")
            if c.get("state") == "login_waiting":
                chips([("Waiting for you to log in (up to 5 min)", "warn")], dot=True)
            elif c.get("state") == "checking":
                chips([("Checking...", "info")], dot=True)
            elif "logged_in" in c:
                chips(
                    [
                        ("Connected" if c["logged_in"] else "Logged out", "ok" if c["logged_in"] else "bad"),
                        (f"Checked {fmt_when(c.get('checked_at'))}", "mute"),
                    ],
                    dot=True,
                )
            working = c.get("state") in ("login_waiting", "checking")
            with st.container(horizontal=True, gap="small"):
                if st.button(
                    "Log in",
                    key=f"login_{platform}",
                    type="primary",
                    disabled=bool(busy) or working,
                    icon=":material/login:",
                ):
                    spawn_connection(settings, "login", [platform])
                    st.toast(f"Opening {platform_label(platform)} login")
                if st.button(
                    "Check now",
                    key=f"check_{platform}",
                    disabled=bool(busy) or working,
                    icon=":material/sync:",
                ):
                    spawn_connection(settings, "check", [platform])
                    st.toast("Checking session")


# ---------------------------------------------------------------------- AI


def _ai_tab() -> None:
    from core.llm import PROVIDERS, key_problem

    settings = get_settings()
    current = settings.llm.provider if settings.llm.provider in PROVIDERS else "openai"
    provider = st.selectbox(
        "Provider",
        list(PROVIDERS),
        index=list(PROVIDERS).index(current),
        format_func=lambda pid: PROVIDERS[pid].label,
        key="ai_provider",
    )
    preset = PROVIDERS[provider]
    name = preset.api_key_env
    status = secret_status(name)
    with card("ai_key"):
        title_block(
            preset.label,
            "Used for fit scores, tailored summaries, screening answers and reading an uploaded resume. "
            "Without a key the app uses an offline heuristic. " + preset.hint,
        )
        if status["set"]:
            chips(
                [
                    (f"Key ending ...{status['last4']}" if status["last4"] else "Key set", "ok"),
                    (f"from {status['source']}", "mute"),
                    (settings.llm.model if current == provider else preset.default_model, "info"),
                ],
                dot=True,
            )
        else:
            chips([("No key", "warn")], dot=True)
        with st.form("ai_form", border=False):
            model_default = (
                settings.llm.model if current == provider and settings.llm.model else preset.default_model
            )
            model = st.text_input("Model", value=model_default, key=f"ai_model_{provider}")
            base_default = (
                settings.llm.base_url if current == provider and settings.llm.base_url else preset.base_url
            )
            base = ""
            if provider == "openai":
                base = st.text_input(
                    "Base URL",
                    value=base_default,
                    key="ai_base",
                    placeholder="https://api.groq.com/openai/v1",
                    help="The /chat/completions path is added if you leave it off.",
                )
            elif preset.base_url:
                st.caption(f"Endpoint: `{preset.base_url}`")
            key = st.text_input(
                "API key",
                type="password",
                placeholder=preset.key_prefix + "…" if preset.key_prefix else "paste the key",
                autocomplete="off",
            )
            if st.form_submit_button("Save", type="primary"):
                problem = key_problem(provider, key) if key.strip() else None
                if problem:
                    st.error(problem)
                elif provider == "openai" and not (base or "").strip():
                    st.error("Paste the base URL for this provider.")
                elif not model.strip():
                    st.error("Enter a model id.")
                else:
                    saved = _save(
                        {
                            "llm": {
                                "provider": provider,
                                "model": model.strip(),
                                "api_key_env": name,
                                "base_url": (base or preset.base_url).strip(),
                            }
                        }
                    )
                    if saved and key.strip():
                        set_secret(name, key)
                        st.toast("Key saved")
                    if saved:
                        st.rerun()
        if status["source"] == "settings" and st.button("Remove saved key", type="tertiary"):
            remove_secret(name)
            st.rerun()
        if status["source"] == "environment":
            st.caption(f"This key comes from the {name} environment variable; change it on the host.")


# ---------------------------------------------------------------------- guardrails


def _lines(text: str) -> list[str]:
    return [x.strip() for x in text.splitlines() if x.strip()]


def _guardrails_tab() -> None:
    s = get_settings()
    with st.form("guardrails"):
        title_block("Search")
        titles = st.text_area("Job titles (one per line)", "\n".join(s.search.titles), height=110)
        locations = st.text_area("Locations (one per line)", "\n".join(s.search.locations), height=90)
        exclude = st.text_area(
            "Skip titles containing (one per line)", "\n".join(s.search.exclude_title_keywords), height=90
        )
        c1, c2, c3 = st.columns(3)
        exp_min = c1.number_input("Min years in JD", 0, 30, s.search.experience_years.min)
        exp_max = c2.number_input("Max years in JD", 0, 40, s.search.experience_years.max)
        posted = c3.number_input("Posted within (days)", 1, 60, s.search.posted_within_days)
        fit = st.slider(
            "Fit threshold (prepare resume + outreach at or above)", 50, 95, s.matching.fit_threshold
        )

        st.divider()
        title_block("Daily apply caps and pacing")
        caps = {}
        cols = st.columns(len(s.quotas.daily_caps))
        for col, (platform, cap) in zip(cols, s.quotas.daily_caps.items(), strict=False):
            caps[platform] = col.number_input(platform_label(platform), 0, 100, cap, key=f"cap_{platform}")
        d1, d2 = st.columns(2)
        dmin = d1.number_input("Delay min (s)", 5, 300, s.quotas.human_delay_seconds.min)
        dmax = d2.number_input("Delay max (s)", 5, 600, s.quotas.human_delay_seconds.max)
        st.caption(
            "Execution windows: applies run only inside these (days like mon-fri or mon,wed; times HH:MM)."
        )
        win_df = pd.DataFrame(
            [{"days": ",".join(w.days), "start": w.start, "end": w.end} for w in s.quotas.execution_windows]
            or [{"days": "mon,tue,wed,thu,fri", "start": "10:00", "end": "18:00"}]
        )
        windows = st.data_editor(
            win_df, num_rows="dynamic", hide_index=True, width="stretch", key="win_editor"
        )

        st.divider()
        title_block("Direct ATS submit mode")
        ats_mode = st.radio(
            "Greenhouse / Lever / Ashby",
            ["review_mode", "auto_submit_ats"],
            index=0 if s.copilot.default_mode_by_platform.get("direct_ats") == "review_mode" else 1,
            format_func=lambda m: (
                "Review before submit" if m == "review_mode" else "Auto-submit when every field is certain"
            ),
            horizontal=True,
        )
        submitted = st.form_submit_button("Save guardrails", type="primary")

    if submitted:
        errors, parsed = [], []
        for _, r in windows.iterrows():
            days_raw = str(r.get("days") or "").strip().lower()
            if not days_raw:
                continue
            days = _expand_days(days_raw)
            if not days:
                errors.append(f"Unrecognised days '{days_raw}'")
            if not (_HHMM.match(str(r.get("start", ""))) and _HHMM.match(str(r.get("end", "")))):
                errors.append(f"Times must be HH:MM (row '{days_raw}')")
            parsed.append({"days": days, "start": str(r.get("start")), "end": str(r.get("end"))})
        if dmin > dmax:
            errors.append("Delay min is larger than delay max")
        if exp_min > exp_max:
            errors.append("Min years is larger than max years")
        if errors:
            st.error("Not saved: " + "; ".join(errors))
        else:
            modes = dict(s.copilot.default_mode_by_platform)
            modes["direct_ats"] = ats_mode
            _save(
                {
                    "search": {
                        "titles": _lines(titles),
                        "locations": _lines(locations),
                        "exclude_title_keywords": _lines(exclude),
                        "experience_years": {"min": int(exp_min), "max": int(exp_max)},
                        "posted_within_days": int(posted),
                    },
                    "matching": {"fit_threshold": int(fit)},
                    "quotas": {
                        "daily_caps": {k: int(v) for k, v in caps.items()},
                        "human_delay_seconds": {"min": int(dmin), "max": int(dmax)},
                        "execution_windows": parsed,
                    },
                    "copilot": {"default_mode_by_platform": modes},
                }
            )

    with card("locked"):
        title_block("Always on (cannot be changed)")
        st.markdown(
            "- Outreach is drafted only; you send every message yourself.\n"
            "- The copilot stops on OTP, CAPTCHA, blocked pages and unknown screening questions.\n"
            "- After you approve a job, Naukri (including its question chat and company websites) and "
            "LinkedIn Easy Apply finish on their own.\n"
            "- It still stops for OTP, CAPTCHA, a blocked page, or a question that is not in your profile.\n"
            "- Resumes contain only facts from your master profile.\n"
            "- Portal passwords are never stored; you log in yourself in the browser."
        )


def _expand_days(raw: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[,\s]+", raw):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a[:3] in _DAYS and b[:3] in _DAYS:
                i, j = _DAYS.index(a[:3]), _DAYS.index(b[:3])
                out += list(_DAYS[i : j + 1]) if i <= j else list(_DAYS[i:] + _DAYS[: j + 1])
            else:
                return []
        elif part[:3] in _DAYS:
            out.append(part[:3])
        else:
            return []
    return list(dict.fromkeys(out))


def render() -> None:
    page_header("Settings")
    profile_tab, conn_tab, ai_tab, guard_tab = st.tabs(["Profile", "Connections", "AI", "Guardrails"])
    with profile_tab:
        _profile_tab()
    with conn_tab:
        _connections_tab()
    with ai_tab:
        _ai_tab()
    with guard_tab:
        _guardrails_tab()
    with st.expander("Use this from your phone, free"):
        st.markdown(
            "The app is already built for a phone. It runs on a computer, and **Tailscale** (free) is how your "
            "phone reaches it from anywhere, including mobile data.\n\n"
            "1. Install Tailscale on this computer and on your phone, and sign in to the same account.\n"
            "2. Leave the app running. With Docker that is `APP_PASSWORD=… docker compose up -d` "
            "(open port **8080**, user `laa`). Without Docker: `uv run streamlit run app.py --server.address 0.0.0.0` "
            "(port **8501**).\n"
            "3. On the phone, open `http://<this-computer-name>:8080` or `:8501`. The computer name is on the "
            "Tailscale app.\n\n"
            "On the same Wi-Fi you can skip Tailscale and use this computer's local IP instead. "
            "Oracle Cloud's Always Free VM is the other free option if it should stay up while your computer is off. "
            "Railway's free plan cannot run the scheduler and the browser."
        )
