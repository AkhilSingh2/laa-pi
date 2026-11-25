"""Friction detectors: OTP, CAPTCHA, login walls. The copilot pauses (never bypasses) on any hit."""

from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.sync_api import Page

from core.models import FrictionType

_CAPTCHA_SELECTORS = [
    # Greenhouse and others load an invisible reCAPTCHA (size=invisible, a corner badge) on every form; that is
    # not a challenge. Only the checkbox widget or the image-challenge frame (bframe) needs a human.
    "iframe[src*='recaptcha'][src*='bframe']",
    "iframe[src*='recaptcha'][src*='anchor']:not([src*='size=invisible'])",
    "iframe[src*='hcaptcha']",
    "iframe[src*='arkoselabs']",
    "iframe[src*='funcaptcha']",
    "iframe[src*='turnstile']",
    ".g-recaptcha:not([data-size='invisible'])",
    ".h-captcha",
    "#captcha",
    "[id*='captcha' i]:not([id*='recaptcha-response' i])",
    "[class*='captcha' i]:not([class*='grecaptcha']):not([class*='recaptcha-response']):not([data-size='invisible'])",
]
_OTP_SELECTORS = [
    "input[autocomplete='one-time-code']",
    "input[name*='otp' i]",
    "input[id*='otp' i]",
    "input[name*='pin' i][maxlength='6']",
    "input[name*='verification' i]",
    "input[aria-label*='verification code' i]",
]
_OTP_TEXT = re.compile(
    r"(enter (the )?(otp|one[- ]time (password|code)|verification code)|code (we )?sent to|security verification|"
    r"verify (your|it's) (identity|you))",
    re.IGNORECASE,
)
_LOGIN_URL = re.compile(r"/(login|signin|sign-in|checkpoint|authwall|uas/|nlogin)", re.IGNORECASE)


@dataclass
class Friction:
    type: FrictionType
    detail: str
    url: str


def _any_visible(page: Page, selectors: list[str]) -> str | None:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=800):
                return sel
        except Exception:  # noqa: BLE001
            continue
    return None


def detect_friction(page: Page, *, expect_logged_in: bool = True) -> Friction | None:
    url = page.url
    try:
        body = page.locator("body").inner_text(timeout=3000)[:6000]
    except Exception:  # noqa: BLE001
        body = ""

    head = body[:600].lower()
    if ("access denied" in head or page.title().lower().strip() == "access denied") and (
        "reference #" in head or "edgesuite" in head or "permission to access" in head
    ):
        return Friction(FrictionType.BLOCKED, "site returned an Access Denied page (bot protection)", url)
    if sel := _any_visible(page, _CAPTCHA_SELECTORS):
        return Friction(FrictionType.CAPTCHA, f"captcha element visible: {sel}", url)
    if (sel := _any_visible(page, _OTP_SELECTORS)) or _OTP_TEXT.search(body):
        return Friction(FrictionType.OTP, f"OTP / verification prompt detected ({sel or 'text'})", url)
    if expect_logged_in and (_LOGIN_URL.search(url) or _any_visible(page, ["input[type='password']"])):
        return Friction(FrictionType.LOGIN_REQUIRED, "login wall or password field detected", url)
    return None
