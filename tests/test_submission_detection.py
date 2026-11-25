"""Submission detection must not fire on pages that merely mention 'applied' or 'thank you'."""

from __future__ import annotations

import pytest

from copilot.ats.base import AtsAdapter
from copilot.ats.greenhouse import GreenhouseAdapter
from copilot.ats.naukri import NaukriAdapter

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


def _html(page, body: str) -> None:
    page.set_content(f"<html><body>{body}</body></html>")


def test_naukri_job_page_mentioning_applied_is_not_submitted(page):
    _html(
        page,
        "<h1>Full Stack Developer</h1><p>Experience with applied machine learning.</p>"
        "<div>Applied by 120 candidates</div><p>Thank you for your interest.</p>"
        "<button id='apply-button'>Apply</button>",
    )
    a = NaukriAdapter()
    assert not a.looks_submitted(page)
    assert not a.already_applied(page)


def test_naukri_applied_state_is_detected(page):
    _html(page, "<h1>Full Stack Developer</h1><button id='already-applied'>Applied</button>")
    assert NaukriAdapter().looks_submitted(page)
    _html(page, "<div class='toast'>You have successfully applied to this job</div>")
    assert NaukriAdapter().looks_submitted(page)


def test_company_website_is_not_the_naukri_apply_button(page):
    _html(
        page,
        "<a id='apply-button' href='https://boards.greenhouse.io/acme/jobs/1'>Apply on company website</a>",
    )
    adapter = NaukriAdapter()
    assert adapter.native_apply_button(page) is None
    link = adapter.company_site_link(page)
    assert link is not None
    assert "greenhouse.io" in (link.get_attribute("href") or "")


def test_naukri_apply_button_is_separate_from_company_site(page):
    _html(
        page,
        "<button id='apply-button'>Apply</button>"
        "<a href='https://jobs.example.com/role'>Apply on company site</a>",
    )
    adapter = NaukriAdapter()
    assert adapter.native_apply_button(page) is not None
    assert adapter.company_site_link(page) is not None


def test_naukri_chat_question_and_chip_are_read(page):
    _html(
        page,
        "<div class='chatbot_Drawer'>"
        "<div class='botMsg'>What is your notice period?</div>"
        "<button class='chip'>15 days</button>"
        "<button class='chip'>30 days</button>"
        "</div>",
    )
    adapter = NaukriAdapter()
    assert adapter.chat_open(page)
    assert adapter.latest_question(page) == "What is your notice period?"
    assert "30 days" in adapter.reply_options(page)
    assert adapter.answer_chat(page, "30 days") is True


def test_naukri_chat_reads_the_newest_bot_question_and_radio_choices(page):
    # structure captured from a live Naukri chatbot
    _html(
        page,
        "<div class='chatbot_Drawer'><ul>"
        "<li class='botItem chatbot_ListItem'><div class='botMsg msg'><span>How many years in NODE?</span></div></li>"
        "<li class='userItem chatbot_ListItem'><div class='botMsg-user msg'>5.9</div></li>"
        "<li class='botItem chatbot_ListItem'><div class='botMsg msg'><span>What is your notice period?</span></div></li>"
        "</ul><div class='singleselect-radiobutton'>"
        "<input type='radio' id='a' name='r' value='15 Days or less'><label for='a' class='ssrc__label'>15 Days or less</label>"
        "<input type='radio' id='b' name='r' value='1 Month'><label for='b' class='ssrc__label'>1 Month</label>"
        "</div><div class='sendMsgbtn_container'><div class='sendMsg' onclick=\"this.dataset.sent='1'\">Save</div></div>"
        "<a class='styles_chip__x styles_non-clickable__y'>REACT</a>"
        "</div>",
    )
    adapter = NaukriAdapter()
    assert adapter.latest_question(page) == "What is your notice period?"
    assert adapter.reply_options(page) == ["15 Days or less", "1 Month"]
    assert adapter.answer_chat(page, "1 Month") is True
    assert page.locator("#b").is_checked()
    assert page.locator("div.sendMsg").get_attribute("data-sent") == "1"


def test_naukri_save_apply_redirect_counts_as_submitted(page):
    page.route(
        "https://www.naukri.com/**", lambda r: r.fulfill(body="<p>Applied to 'Fullstack Developer'</p>")
    )
    try:
        page.goto(
            "https://www.naukri.com/myapply/saveApply?strJobsarr=[1]&multiApplyResp=%7B%221%22%3A500%7D"
        )
        assert not NaukriAdapter().looks_submitted(page)
        page.goto(
            "https://www.naukri.com/myapply/saveApply?strJobsarr=[1]&multiApplyResp=%7B%221%22%3A200%7D"
        )
        assert NaukriAdapter().looks_submitted(page)
    finally:
        page.unroute("https://www.naukri.com/**")


def test_generic_thank_you_text_is_not_a_submission(page):
    _html(page, "<p>Thank you for considering Acme. We successfully scaled to 10M users.</p>")
    assert not AtsAdapter().looks_submitted(page)
    assert not GreenhouseAdapter().looks_submitted(page)
    _html(page, "<h2>Thank you for applying!</h2>")
    assert GreenhouseAdapter().looks_submitted(page)


def test_submit_requires_a_change_to_submitted(page):
    adapter = AtsAdapter()
    _html(
        page,
        "<form onsubmit='event.preventDefault(); document.body.innerHTML=\"<h2>Application submitted</h2>\"'>"
        "<button type='submit'>Submit</button></form>",
    )
    assert adapter.submit(page) is True

    # page already shows a success phrase before the click (e.g. in the JD): clicking must not count
    _html(
        page,
        "<p>Your application submitted through partners is reviewed weekly.</p>"
        "<button type='submit' onclick='return false'>Submit</button>",
    )
    assert adapter.submit(page) is False
