"""Greenhouse hosted job boards (boards.greenhouse.io / job-boards.greenhouse.io / embedded iframes)."""

from __future__ import annotations

import re

from playwright.sync_api import Page

from copilot.ats.base import AtsAdapter

_GH_JID = re.compile(r"[?&]gh_jid=(\d+)")


def embed_form_url(apply_url: str, board_token: str | None) -> str:
    """Company career sites (databricks.com/...?gh_jid=N) load the Greenhouse form in a cross-origin iframe the
    copilot cannot fill. Greenhouse serves the same form directly at its embed URL."""
    m = _GH_JID.search(apply_url or "")
    if not m or not board_token or "greenhouse.io" in apply_url:
        return apply_url
    return f"https://job-boards.greenhouse.io/embed/job_app?for={board_token}&token={m.group(1)}"


class GreenhouseAdapter(AtsAdapter):
    name = "greenhouse"
    verified_direct_ats = True
    apply_button_selectors = [
        "a#apply_button",
        "a:has-text('Apply for this job')",
        "button:has-text('Apply')",
        "a:has-text('Apply')",
    ]
    submit_selectors = [
        "#submit_app",
        "button[type='submit']:has-text('Submit application')",
        "button[type='submit']:has-text('Submit Application')",
        "input[type='submit'][value*='Submit']",
        "button:has-text('Submit')",
    ]
    form_scope = None
    success_markers = [
        "thank you for applying",
        "application has been submitted",
        "application submitted",
    ]

    def open(self, page: Page, url: str) -> None:
        # New Greenhouse boards render the form at /jobs/<id>#app ; old ones at #application_form.
        super().open(page, url)
        if "/embed/job_app" in page.url:
            return
        if "#app" not in page.url and "application" not in page.url:
            try:
                page.goto(url.split("#")[0] + "#app", wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(800)
            except Exception:  # noqa: BLE001
                pass
