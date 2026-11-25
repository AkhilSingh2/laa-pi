"""Shared adapter behaviour. Concrete adapters override selectors and multi-step flow where needed."""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PWTimeout

from copilot.browser import first_visible
from copilot.field_mapper import FieldMapping, FormField, extract_fields
from core.logging import log

_CONFIRM_URL = re.compile(r"/(confirmation|thank[-_]?you|thanks|application[-_]submitted)(?:[/?#]|$)")


class AtsAdapter:
    name = "generic"
    #: True only for ATSs whose forms are stable enough to auto-submit (Greenhouse/Lever/Ashby).
    verified_direct_ats = False
    apply_button_selectors: list[str] = [
        "a:has-text('Apply for this job')",
        "a:has-text('Apply now')",
        "button:has-text('Apply now')",
        "a:has-text('Apply')",
        "button:has-text('Apply')",
    ]
    submit_selectors: list[str] = [
        "button[type='submit']:has-text('Submit')",
        "input[type='submit']",
        "button:has-text('Submit application')",
        "button:has-text('Submit Application')",
        "button:has-text('Submit')",
    ]
    form_scope: str | None = None
    #: Full phrases shown after an application is accepted. Single words like "applied" or "thank you" are
    #: avoided on purpose: they appear in job descriptions and sidebars and caused false "submitted" results.
    success_markers: list[str] = [
        "thank you for applying",
        "thanks for applying",
        "application submitted",
        "application has been submitted",
        "application received",
        "we have received your application",
        "successfully submitted",
    ]

    # ------------------------------------------------------------------ navigation

    def open(self, page: Page, url: str) -> None:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(1200)

    def reach_form(self, page: Page) -> bool:
        """Click through to the actual form if the posting page shows a description first."""
        if self.form_present(page):
            return True
        btn = first_visible(page, self.apply_button_selectors, timeout_ms=3000)
        if btn is not None:
            try:
                btn.scroll_into_view_if_needed()
                btn.click()
                page.wait_for_timeout(1500)
            except Exception as e:  # noqa: BLE001
                log.debug("apply button click failed: {}", e)
        return self.form_present(page)

    def form_present(self, page: Page) -> bool:
        try:
            return page.locator("input[type='file'], textarea, input[type='email']").first.is_visible(
                timeout=2500
            )
        except PWTimeout:
            return False
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ fields

    def fields(self, page: Page) -> list[FormField]:
        return extract_fields(page, self.form_scope)

    def fill(self, page: Page, mappings: list[FieldMapping]) -> list[str]:
        """Fill every resolved mapping. Returns labels that failed to fill."""
        failed: list[str] = []
        for m in mappings:
            if m.value_source == "unresolved" or m.value is None or m.value_source == "prefilled":
                continue
            try:
                self._fill_one(page, m)
                page.wait_for_timeout(250)
            except Exception as e:  # noqa: BLE001
                log.warning("fill failed for {!r}: {}", m.field.label, e)
                failed.append(m.field.label)
        return failed

    def _fill_one(self, page: Page, m: FieldMapping) -> None:
        f = m.field
        if f.kind in ("radio", "checkbox"):
            members = page.locator(f"[data-laa-group='{f.key}']")
            n = members.count()
            for i in range(n):
                el = members.nth(i)
                label_text = ""
                el_id = el.get_attribute("id")
                if el_id:
                    label_text = (
                        page.locator(f"label[for='{el_id}']").first.inner_text(timeout=1000)
                        if page.locator(f"label[for='{el_id}']").count()
                        else ""
                    )
                if not label_text:
                    label_text = el.get_attribute("value") or ""
                if label_text.strip().lower() == str(m.value).strip().lower():
                    if not el.is_checked():
                        el.check(force=True)
                    return
            # fall back: first member for single consent checkbox
            if f.kind == "checkbox" and n == 1:
                members.first.check(force=True)
                return
            raise ValueError(f"option {m.value!r} not found")
        loc = page.locator(f"[data-laa-key='{f.key}']").first
        if f.kind == "file":
            loc.set_input_files(str(Path(m.value)))
            return
        if f.kind == "select":
            loc.select_option(label=str(m.value))
            return
        if f.kind == "combobox":
            loc.click()
            page.keyboard.type(str(m.value), delay=40)
            page.wait_for_timeout(700)
            option = first_visible(
                page,
                [f"[role='option']:has-text('{m.value}')", f"div[class*='option']:has-text('{m.value}')"],
                2500,
            )
            if option is not None:
                option.click()
            else:
                page.keyboard.press("Enter")
            return
        loc.click()
        loc.fill("")
        loc.type(str(m.value), delay=25)

    # ------------------------------------------------------------------ submit

    def submit_button(self, page: Page):
        return first_visible(page, self.submit_selectors, timeout_ms=4000)

    def submit(self, page: Page) -> bool:
        """Click submit; True only if the page changed from not-submitted to submitted."""
        btn = self.submit_button(page)
        if btn is None:
            return False
        before = self.looks_submitted(page)
        btn.scroll_into_view_if_needed()
        btn.click()
        page.wait_for_timeout(3500)
        return self.looks_submitted(page) and not before

    def looks_submitted(self, page: Page) -> bool:
        try:
            body = page.locator("body").inner_text(timeout=3000).lower()
        except Exception:  # noqa: BLE001
            return False
        return any(m in body for m in self.success_markers) or bool(_CONFIRM_URL.search(page.url.lower()))
