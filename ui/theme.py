"""Look and layout helpers shared by every page (mobile first, centered max-width on desktop)."""

from __future__ import annotations

import html
from collections.abc import Iterable
from typing import Literal

import streamlit as st

Tone = Literal["ok", "warn", "bad", "info", "mute"]

_CSS = """
<style>
.block-container, [data-testid="stMainBlockContainer"] {
  max-width: 1120px; padding-top: 4.2rem; padding-bottom: 5rem;
}
@media (max-width: 640px) {
  .block-container, [data-testid="stMainBlockContainer"] {
    padding-left: .8rem; padding-right: .8rem; padding-top: 3.6rem;
  }
  h1 { font-size: 1.55rem !important; }
  h2 { font-size: 1.25rem !important; }
  h3 { font-size: 1.08rem !important; }
  [data-testid="stMetricValue"] { font-size: 1.35rem; }
}
h1, h2, h3 { letter-spacing: -0.01em; }

/* cards: st.container(border=True, key="card_...") */
[class*="st-key-card"] {
  background: #FFFFFF; border-radius: .9rem; box-shadow: 0 1px 2px rgba(15,23,42,.06);
}

/* chips */
.laa-chips { display: flex; flex-wrap: wrap; gap: .35rem; margin: .15rem 0 .35rem; }
.laa-chip {
  display: inline-flex; align-items: center; gap: .3rem; padding: .14rem .6rem; border-radius: 999px;
  font-size: .78rem; font-weight: 600; line-height: 1.35rem; border: 1px solid; white-space: nowrap;
}
.laa-chip .dot { width: .45rem; height: .45rem; border-radius: 50%; background: currentColor; }
.laa-chip.ok   { background: #ECFDF5; color: #047857; border-color: #A7F3D0; }
.laa-chip.warn { background: #FFFBEB; color: #B45309; border-color: #FDE68A; }
.laa-chip.bad  { background: #FEF2F2; color: #B91C1C; border-color: #FECACA; }
.laa-chip.info { background: #EEF2FF; color: #4338CA; border-color: #C7D2FE; }
.laa-chip.mute { background: #F1F5F9; color: #475569; border-color: #E2E8F0; }

/* stat tiles: 2 per row on phones, more on desktop */
.laa-tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(128px, 1fr)); gap: .55rem; }
.laa-tile {
  background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: .8rem; padding: .6rem .75rem;
  text-decoration: none !important; color: inherit !important; display: block;
}
.laa-tile .v { font-size: 1.45rem; font-weight: 750; color: #0F172A; line-height: 1.8rem; }
.laa-tile .l { font-size: .78rem; color: #64748B; font-weight: 600; }
.laa-tile.hot { border-color: #A5B4FC; background: #EEF2FF; }

/* header profile chip */
.laa-head { display: flex; align-items: center; justify-content: space-between; gap: .75rem; flex-wrap: wrap;
  margin-bottom: .4rem; }
.laa-me { display: flex; align-items: center; gap: .6rem; text-decoration: none !important; color: inherit !important; }
.laa-avatar { width: 2.3rem; height: 2.3rem; border-radius: 50%; background: linear-gradient(135deg,#6366F1,#4338CA);
  color: #fff; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: .9rem; flex: none; }
.laa-me .n { font-weight: 700; font-size: .95rem; line-height: 1.1rem; }
.laa-me .h { color: #64748B; font-size: .78rem; line-height: 1rem; max-width: 26rem; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 640px) { .laa-me .h { max-width: 14rem; } }

.laa-muted { color: #64748B; font-size: .85rem; }
.laa-title { font-weight: 700; font-size: 1.02rem; line-height: 1.35rem; margin: 0; }
.laa-sub { color: #475569; font-size: .86rem; margin: .1rem 0 .25rem; }

/* sticky bottom action bar: st.container(key="actionbar_...") */
[class*="st-key-actionbar"] {
  position: sticky; bottom: 0; z-index: 60; background: rgba(248,250,252,.96);
  backdrop-filter: blur(6px); border-top: 1px solid #E2E8F0; padding: .55rem 0 .65rem; margin-top: .5rem;
}

/* responsive variants: st.container(key="only_mobile_...") / key="only_desktop_..." */
@media (max-width: 768px) { [class*="st-key-only_desktop"] { display: none !important; } }
@media (min-width: 769px) { [class*="st-key-only_mobile"] { display: none !important; } }

/* phone tab row (Streamlit hides its top nav behind a menu on narrow screens) */
.st-key-only_mobile_tabs {
  flex-wrap: nowrap !important; overflow-x: auto; scrollbar-width: none; gap: .35rem !important;
  margin: -0.6rem 0 .5rem; padding-bottom: .2rem;
}
.st-key-only_mobile_tabs::-webkit-scrollbar { display: none; }
.st-key-only_mobile_tabs [data-testid="stPageLink"] a {
  background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 999px; padding: .2rem .7rem !important;
  white-space: nowrap;
}
.st-key-only_mobile_tabs [data-testid="stPageLink"] a[aria-current="page"],
.st-key-only_mobile_tabs [data-testid="stPageLink-NavLink"][aria-current="page"] {
  background: #EEF2FF; border-color: #A5B4FC;
}
.st-key-only_mobile_tabs > div { flex: none !important; width: auto !important; }

/* job cards */
.laa-job { display: flex; gap: .8rem; align-items: flex-start; }
.laa-score {
  flex: none; width: 3.6rem; height: 3.6rem; border-radius: 1rem; border: 1px solid;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  font-weight: 800; font-size: 1.3rem; line-height: 1.2rem;
}
.laa-score small { font-size: .6rem; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; margin-top: .1rem; }
.laa-score.ok { background: #ECFDF5; color: #047857; border-color: #A7F3D0; }
.laa-score.info { background: #EEF2FF; color: #4338CA; border-color: #C7D2FE; }
.laa-score.warn { background: #FFFBEB; color: #B45309; border-color: #FDE68A; }
.laa-score.mute { background: #F8FAFC; color: #64748B; border-color: #E2E8F0; }
.laa-job-main { min-width: 0; flex: 1; }
.laa-job-main .where { color: #475569; font-size: .86rem; margin: .05rem 0 .3rem; }
.laa-job-main .why { color: #64748B; font-size: .8rem; margin: .35rem 0 0; display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }

/* touch-friendly buttons */
.stButton button, .stDownloadButton button, .stLinkButton a, .stFormSubmitButton button { min-height: 2.6rem; }
[data-testid="stPageLink"] a { padding-left: 0; }
</style>
"""


def apply_theme() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def chip(text: str, tone: Tone = "mute", dot: bool = False) -> str:
    d = '<span class="dot"></span>' if dot else ""
    return f'<span class="laa-chip {tone}">{d}{html.escape(text)}</span>'


def chips(items: Iterable[tuple[str, Tone]], dot: bool = False) -> None:
    body = "".join(chip(t, tone, dot) for t, tone in items if t)
    if body:
        st.markdown(f'<div class="laa-chips">{body}</div>', unsafe_allow_html=True)


def tiles(items: Iterable[tuple[str, int | str, bool]]) -> None:
    """Stat tiles: (label, value, highlighted)."""
    body = "".join(
        f'<div class="laa-tile{" hot" if hot else ""}"><div class="v">{html.escape(str(v))}</div>'
        f'<div class="l">{html.escape(label)}</div></div>'
        for label, v, hot in items
    )
    st.markdown(f'<div class="laa-tiles">{body}</div>', unsafe_allow_html=True)


def page_header(title: str, subtitle: str | None = None) -> None:
    st.markdown(f"## {title}")
    if subtitle:
        st.markdown(f'<div class="laa-muted">{html.escape(subtitle)}</div>', unsafe_allow_html=True)


def title_block(title: str, sub: str | None = None) -> None:
    st.markdown(
        f'<p class="laa-title">{html.escape(title)}</p>'
        + (f'<p class="laa-sub">{html.escape(sub)}</p>' if sub else ""),
        unsafe_allow_html=True,
    )


def card(key: str):  # noqa: ANN201
    return st.container(border=True, key=f"card_{key}")


def action_bar(key: str):  # noqa: ANN201
    return st.container(key=f"actionbar_{key}", horizontal=True, gap="small")


_PLATFORM = {
    "linkedin": "LinkedIn",
    "naukri": "Naukri",
    "direct_ats": "Direct ATS",
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
}


def platform_label(name: str) -> str:
    return _PLATFORM.get(name, name.replace("_", " ").title())


def initials(name: str) -> str:
    parts = [p for p in name.split() if p]
    return ((parts[0][0] if parts else "?") + (parts[-1][0] if len(parts) > 1 else "")).upper()


def score_tone(score: int | None, threshold: int) -> Tone:
    if score is None:
        return "mute"
    if score >= max(threshold, 90):
        return "ok"
    if score >= threshold:
        return "info"
    return "warn"
