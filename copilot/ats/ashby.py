"""Ashby hosted job boards (jobs.ashbyhq.com/<company>/<id>/application)."""

from __future__ import annotations

from playwright.sync_api import Page

from copilot.ats.base import AtsAdapter


class AshbyAdapter(AtsAdapter):
    name = "ashby"
    verified_direct_ats = True
    apply_button_selectors = [
        "a:has-text('Apply for this Job')",
        "button:has-text('Apply for this Job')",
        "a[href$='/application']",
        "a:has-text('Apply')",
    ]
    submit_selectors = [
        "button[type='submit']:has-text('Submit Application')",
        "button:has-text('Submit Application')",
        "button:has-text('Submit')",
    ]
    success_markers = [
        "application submitted",
        "thank you for applying",
        "we've received your application",
        "successfully submitted",
    ]

    def open(self, page: Page, url: str) -> None:
        if "/application" not in url:
            url = url.rstrip("/") + "/application"
        super().open(page, url)
