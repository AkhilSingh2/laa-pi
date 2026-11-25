"""Lever hosted postings (jobs.lever.co/<company>/<id>/apply)."""

from __future__ import annotations

from playwright.sync_api import Page

from copilot.ats.base import AtsAdapter


class LeverAdapter(AtsAdapter):
    name = "lever"
    verified_direct_ats = True
    apply_button_selectors = [
        "a.postings-btn:has-text('Apply for this job')",
        "a:has-text('Apply for this job')",
        "a[href$='/apply']",
    ]
    submit_selectors = [
        "button#btn-submit",
        "button[type='submit']:has-text('Submit application')",
        "button:has-text('Submit application')",
        "button:has-text('Submit')",
    ]
    success_markers = ["application has been submitted", "application submitted", "thanks for applying"]

    def open(self, page: Page, url: str) -> None:
        if not url.rstrip("/").endswith("/apply"):
            url = url.rstrip("/") + "/apply"
        super().open(page, url)
