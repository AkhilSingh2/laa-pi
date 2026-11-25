"""The invisible reCAPTCHA badge Greenhouse puts on every form is not a CAPTCHA the user has to solve."""

from __future__ import annotations

import pytest

from copilot.friction import detect_friction
from core.models import FrictionType

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def page():
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"chromium not available: {e}")
        pg = browser.new_page()
        yield pg
        browser.close()


def test_invisible_recaptcha_badge_is_not_friction(page):
    page.set_content(
        "<form><input name='first_name'></form>"
        "<div class='grecaptcha-badge' style='width:256px;height:60px'>"
        "<div class='grecaptcha-logo' style='width:256px;height:60px'>logo</div><div class='grecaptcha-error'></div>"
        "<textarea id='g-recaptcha-response' class='g-recaptcha-response' style='display:none'></textarea></div>"
    )
    assert detect_friction(page, expect_logged_in=False) is None


def test_visible_captcha_widget_is_friction(page):
    page.set_content("<div class='g-recaptcha' style='width:304px;height:78px'>captcha</div>")
    fr = detect_friction(page, expect_logged_in=False)
    assert fr is not None and fr.type == FrictionType.CAPTCHA
