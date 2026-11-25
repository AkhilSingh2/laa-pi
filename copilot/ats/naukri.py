"""Naukri job detail page.

Two apply paths: a one-click Apply (sometimes followed by a chatbot), and "Apply on company website",
which leaves Naukri for the employer's form. The copilot takes whichever path the page offers after you
approve the job. It still stops on OTP, CAPTCHA, a block page, or a question it cannot answer from your profile.
"""

from __future__ import annotations

from playwright.sync_api import Page

from copilot.ats.base import AtsAdapter
from copilot.browser import first_visible, safe_text


def _is_visible(loc) -> bool:  # noqa: ANN001
    try:
        return loc.is_visible()
    except Exception:  # noqa: BLE001
        return False


class NaukriAdapter(AtsAdapter):
    name = "naukri"
    verified_direct_ats = False
    apply_button_selectors = [
        "button#apply-button",
        "button:has-text('Apply')",
        "#already-applied",
    ]
    # Only the job page's Apply button counts as submit. The chatbot's Save button is deliberately excluded:
    # it would send whatever answers are in the drawer without a human checking them.
    submit_selectors = [
        "button#apply-button",
    ]
    form_scope = "div.chatbot_Drawer, div[class*='chatbot']"
    success_markers = [
        "applied successfully",
        "successfully applied",
        "you have successfully applied",
        "your application has been sent",
        "application sent successfully",
    ]
    # the Apply button turns into an "Applied" state once Naukri accepts the application
    applied_state_selectors = ["#already-applied", "button:text-is('Applied')", "span:text-is('Applied')"]
    company_site_selectors = [
        "a:has-text('Apply on company website')",
        "button:has-text('Apply on company website')",
        "a:has-text('Apply on company site')",
        "button:has-text('Apply on company site')",
        "#company-site-button",
        "a#company-url",
    ]
    chatbot_submit_selectors = [
        "div.chatbot_Drawer button:has-text('Save')",
        "div.chatbot_Drawer button:has-text('Submit')",
        "div[class*='chatbot'] button:has-text('Save')",
        "div[class*='chatbot'] button:has-text('Submit')",
    ]
    chat_roots = [
        "div.chatbot_Drawer",
        "div[class*='chatbot_Drawer']",
        "div[class*='chatbot-container']",
        "div[class*='ChatBot']",
        "div[id*='chatbot']",
        "div[class*='chat-container']",
    ]
    question_selectors = [
        "li.botItem .botMsg",
        ".botMsg",
        "[class*='botMsg']",
        "[class*='bot-msg']",
        "[class*='bot_msg']",
        "[class*='botMessage']",
    ]
    chip_selectors = [
        "button[class*='chip']",
        "[class*='suggest'] button",
        "[class*='single-select'] button",
        "button[class*='option']",
        "[class*='chip']",
    ]
    # single-select radios and multi-select checkboxes the chat shows instead of chips
    choice_label_selectors = [
        "label.ssrc__label",
        "[class*='singleselect'] label",
        "[class*='mcc__label']",
        "[class*='multicheckbox'] label",
    ]
    chat_input_selectors = [
        "div.chatbot_Drawer div.textArea[contenteditable='true']",
        "div.chatbot_Drawer input[type='text']",
        "div.chatbot_Drawer textarea",
        "div[class*='chatbot'] input:not([type='hidden']):not([type='radio']):not([type='checkbox']):not([type='file'])",
        "div[class*='chatbot'] textarea",
        "input[placeholder*='Type' i]",
        "textarea[placeholder*='Type' i]",
        "div[contenteditable='true']",
    ]
    # the chat's per-answer send control is a div labelled "Save"; it only sends the current reply
    send_selectors = [
        "div.chatbot_Drawer div.sendMsg",
        "div[class*='sendMsgbtn_container'] div.sendMsg",
        "button[aria-label*='Send' i]",
        "div.chatbot_Drawer button[class*='send']",
        "div[class*='chatbot'] button[class*='send']",
        "button:has-text('Send')",
    ]

    def company_site_link(self, page: Page):
        return first_visible(page, self.company_site_selectors, 1500)

    def native_apply_button(self, page: Page):
        """The in-Naukri Apply button. A control whose label sends you to the company site is not this."""
        btn = first_visible(page, ["button#apply-button", "a#apply-button", "button.apply-button"], 2000)
        if btn is None:
            return None
        if "company" in (safe_text(btn) or "").lower():
            return None
        return btn

    def _surfaces(self, page: Page) -> list:
        frames = [page]
        try:
            frames.extend(f for f in page.frames if f != page.main_frame)
        except Exception:  # noqa: BLE001
            pass
        return frames

    def chat_open(self, page: Page) -> bool:
        for surface in self._surfaces(page):
            if first_visible(surface, self.chat_roots, 600) is not None:
                return True
            if first_visible(surface, self.question_selectors, 400) is not None:
                return True
        return False

    def latest_question(self, page: Page) -> str:
        """Newest bot message. Only the first selector that matches is used: merging selectors put broader matches
        (including our own reply bubble) after the real newest question."""
        for surface in self._surfaces(page):
            for sel in self.question_selectors:
                loc = surface.locator(sel)
                try:
                    count = loc.count()
                except Exception:  # noqa: BLE001
                    continue
                for i in range(count - 1, -1, -1):
                    if t := safe_text(loc.nth(i)):
                        return t
        return ""

    def _chat_root(self, surface):  # noqa: ANN001, ANN202
        """The chat drawer itself. Chips are only read inside it: the job page has non-clickable skill chips too."""
        return first_visible(surface, self.chat_roots, 600)

    def _chips(self, surface) -> list:  # noqa: ANN001
        root = self._chat_root(surface)
        if root is None:
            return []
        out = []
        for sel in self.chip_selectors:
            loc = root.locator(f"{sel}:not([class*='non-clickable'])")
            try:
                count = min(loc.count(), 12)
            except Exception:  # noqa: BLE001
                continue
            out.extend(loc.nth(i) for i in range(count))
            if out:
                break
        return out

    def _choices(self, surface) -> list:  # noqa: ANN001
        root = self._chat_root(surface)
        if root is None:
            return []
        for sel in self.choice_label_selectors:
            loc = root.locator(sel)
            try:
                count = min(loc.count(), 20)
            except Exception:  # noqa: BLE001
                continue
            visible = [loc.nth(i) for i in range(count) if _is_visible(loc.nth(i))]
            if visible:
                return visible
        return []

    def reply_options(self, page: Page) -> list[str]:
        options: list[str] = []
        for surface in self._surfaces(page):
            for el in self._choices(surface) or self._chips(surface):
                t = safe_text(el)
                if t and t not in options and len(t) < 80:
                    options.append(t)
            if options:
                break
        return options

    def _send(self, surface, page: Page) -> None:  # noqa: ANN001
        send = first_visible(surface, self.send_selectors, 1500)
        if send is not None:
            send.click(timeout=5000)
        else:
            page.keyboard.press("Enter")

    def answer_chat(self, page: Page, answer: str) -> bool:
        """Pick a matching radio / checkbox and press Save, click a matching chip, or type the answer and send it."""
        wanted = answer.strip().lower()

        def matches(label: str) -> bool:
            return bool(label) and (label == wanted or wanted in label or label in wanted)

        for surface in self._surfaces(page):
            choices = self._choices(surface)
            if choices:
                exact = [el for el in choices if safe_text(el).strip().lower() == wanted]
                pick = (
                    exact[0]
                    if exact
                    else next((el for el in choices if matches(safe_text(el).strip().lower())), None)
                )
                if pick is None:
                    return False
                try:
                    pick.click(timeout=5000)
                    page.wait_for_timeout(500)
                    self._send(surface, page)
                except Exception:  # noqa: BLE001
                    return False
                page.wait_for_timeout(800)
                return True
            for el in self._chips(surface):
                if matches(safe_text(el).strip().lower()):
                    try:
                        el.click(timeout=5000)
                    except Exception:  # noqa: BLE001
                        continue
                    page.wait_for_timeout(800)
                    return True
            box = first_visible(surface, self.chat_input_selectors, 800)
            if box is None:
                continue
            try:
                box.click()
                box.fill(answer)
            except Exception:  # noqa: BLE001
                box.click()
                page.keyboard.type(answer, delay=20)
            page.wait_for_timeout(300)
            try:
                self._send(surface, page)
            except Exception:  # noqa: BLE001
                page.keyboard.press("Enter")
            page.wait_for_timeout(800)
            return True
        return False

    def form_present(self, page: Page) -> bool:
        return (
            self.chat_open(page)
            or first_visible(page, ["div.chatbot_Drawer", "div[class*='chatbot_Drawer']"], 800) is not None
        )

    def reach_form(self, page: Page) -> bool:
        # Naukri applies on button click; we never click Apply automatically. The copilot surfaces the page for review.
        return self.form_present(page)

    def already_applied(self, page: Page) -> bool:
        return first_visible(page, self.applied_state_selectors, 1200) is not None

    def looks_submitted(self, page: Page) -> bool:
        # after the last chat answer Naukri redirects to /myapply/saveApply with a per-job status of 200
        url = page.url or ""
        if "/myapply/saveApply" in url and ("%3A200" in url or ":200" in url):
            return True
        return self.already_applied(page) or super().looks_submitted(page)

    def submit(self, page: Page) -> bool:
        """Inside the chatbot, Save/Submit sends the application. On the job page there is nothing else to click:
        Apply was already used to open the drawer, and clicking it again is not a confirmation."""
        if self.form_present(page):
            btn = first_visible(page, self.chatbot_submit_selectors, 2500)
            if btn is None:
                return False
            before = self.looks_submitted(page)
            btn.scroll_into_view_if_needed()
            btn.click()
            page.wait_for_timeout(3500)
            return self.looks_submitted(page) and not before
        return False

    def chatbot_questions(self, page: Page) -> list[str]:
        qs: list[str] = []
        for sel in ["div.chatbot_Drawer .botMsg", "div[class*='botMsg']", "li.botMsg"]:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 20)):
                t = safe_text(loc.nth(i))
                if t and t not in qs:
                    qs.append(t)
            if qs:
                break
        return qs
