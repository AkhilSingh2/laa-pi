"""LinkedIn Easy Apply modal (multi-step). Never auto-submits; review_mode only."""

from __future__ import annotations

from playwright.sync_api import Page

from copilot.ats.base import AtsAdapter
from copilot.browser import first_visible
from core.logging import log

MODAL = ".jobs-easy-apply-modal, div[role='dialog']:has(.jobs-easy-apply-content), div[data-test-modal]"


class LinkedInEasyApplyAdapter(AtsAdapter):
    name = "linkedin_easy_apply"
    verified_direct_ats = False
    apply_button_selectors = [
        "button.jobs-apply-button:has-text('Easy Apply')",
        "button:has-text('Easy Apply')",
        ".jobs-apply-button--top-card button",
    ]
    submit_selectors = [
        f"{MODAL} button[aria-label='Submit application']",
        f"{MODAL} button:has-text('Submit application')",
    ]
    next_selectors = [
        f"{MODAL} button[aria-label='Continue to next step']",
        f"{MODAL} button:has-text('Next')",
        f"{MODAL} button[aria-label='Review your application']",
        f"{MODAL} button:has-text('Review')",
    ]
    form_scope = ".jobs-easy-apply-modal, div[role='dialog']"
    success_markers = ["your application was sent", "application was sent"]
    max_steps = 8

    def form_present(self, page: Page) -> bool:
        return first_visible(page, [MODAL], 2500) is not None

    def is_easy_apply(self, page: Page) -> bool:
        return first_visible(page, self.apply_button_selectors, 3000) is not None

    def advance(self, page: Page) -> str:
        """Click Next/Review. Returns 'submit' when the submit button is visible, 'next' after advancing, 'stuck' otherwise."""
        if self.submit_button(page) is not None:
            return "submit"
        nxt = first_visible(page, self.next_selectors, 2500)
        if nxt is None:
            return "stuck"
        nxt.click()
        page.wait_for_timeout(1200)
        # LinkedIn shows inline validation errors when required fields are empty
        if (
            first_visible(page, [f"{MODAL} .artdeco-inline-feedback--error", f"{MODAL} [role='alert']"], 800)
            is not None
        ):
            log.info("LinkedIn modal shows validation errors on this step")
            return "errors"
        return "submit" if self.submit_button(page) is not None else "next"

    def uncheck_follow_company(self, page: Page) -> None:
        box = first_visible(
            page,
            [f"{MODAL} input#follow-company-checkbox", f"{MODAL} label[for='follow-company-checkbox']"],
            800,
        )
        if box is not None:
            try:
                page.locator("#follow-company-checkbox").uncheck(force=True)
            except Exception:  # noqa: BLE001
                pass

    def dismiss(self, page: Page) -> None:
        btn = first_visible(
            page, ["button[aria-label='Dismiss']", f"{MODAL} button.artdeco-modal__dismiss"], 1500
        )
        if btn is not None:
            btn.click()
            page.wait_for_timeout(600)
            discard = first_visible(
                page,
                ["button[data-control-name='discard_application_confirm_btn']", "button:has-text('Discard')"],
                1500,
            )
            if discard is not None:
                discard.click()
