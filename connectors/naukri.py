"""Naukri collector using the persistent browser profile (search results + job detail pages).

Hooks (see connectors/hooks.py): each posting is handed to the caller as soon as its detail page is read,
postings already stored are skipped before their detail page is opened, the search card (title, company,
location) is pre-filtered, and a stop request is honoured between cards.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote_plus

from playwright.sync_api import Page

from connectors.base import BaseConnector
from connectors.hooks import CollectHooks
from copilot.browser import BrowserSession, first_visible, gentle_scroll, safe_text
from copilot.friction import detect_friction
from core.logging import log
from core.models import AtsType, JobPosting, Source, infer_remote_type
from core.quota import QuotaManager
from core.settings import NaukriSourceConfig, SearchConfig

CARD_SELECTORS = ["div.srp-jobtuple-wrapper", "article.jobTuple", "div[data-job-id]"]
CARD_TITLE = ["a.title", "a.title.fw500", "a[title]"]
CARD_COMPANY = ["a.comp-name", "a.subTitle", "span.comp-name"]
CARD_LOCATION = ["span.locWdth", "li.location span", ".loc span"]
CARD_POSTED = ["span.job-post-day", ".jobTupleFooter span.fleft", "span.type br + span"]
DETAIL_DESC = [
    "section.styles_job-desc-container__txpYf",
    "div.styles_JDC__dang-inner-html__h0K4t",
    "section.job-desc",
    "div.dang-inner-html",
    "div[class*='job-desc']",
]
DETAIL_TITLE = ["h1.styles_jd-header-title__rZwM1", "h1[class*='jd-header-title']", "h1"]
DETAIL_COMPANY = ["div[class*='jd-header-comp-name'] a", "a[class*='comp-name']", "div.jd-header-comp-name a"]
DETAIL_LOCATION = ["span[class*='location'] a", "div[class*='loc'] a", "span.loc"]


class NaukriCollector(BaseConnector):
    source = Source.NAUKRI

    def __init__(
        self, browser: BrowserSession, cfg: NaukriSourceConfig, search: SearchConfig, quota: QuotaManager
    ) -> None:
        self.browser = browser
        self.cfg = cfg
        self.search = search
        self.quota = quota
        self.friction: str | None = None

    def _search_url(self, query: str, location: str, page_no: int) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
        loc_slug = re.sub(r"[^a-z0-9]+", "-", location.lower()).strip("-")
        base = f"https://www.naukri.com/{slug}-jobs-in-{loc_slug}"
        if page_no > 1:
            base += f"-{page_no}"
        params = [
            f"k={quote_plus(query)}",
            f"l={quote_plus(location)}",
            f"experience={self.cfg.experience_years}",
            f"jobAge={self.search.posted_within_days}",
        ]
        return base + "?" + "&".join(params)

    def fetch(self, hooks: CollectHooks | None = None) -> list[JobPosting]:
        hooks = hooks or CollectHooks()
        page = self.browser.new_page()
        out: dict[str, JobPosting] = {}
        stop = False
        try:
            for query in self.cfg.queries:
                if stop:
                    break
                for location in self.cfg.locations:
                    if stop:
                        break
                    for page_no in range(1, self.cfg.max_pages_per_query + 1):
                        if hooks.should_stop():
                            stop = True
                            break
                        page.goto(
                            self._search_url(query, location, page_no),
                            wait_until="domcontentloaded",
                            timeout=45_000,
                        )
                        page.wait_for_timeout(2500)
                        if fr := detect_friction(page, expect_logged_in=False):
                            self.friction = f"{fr.type}: {fr.detail}"
                            log.warning("Naukri collector paused: {}", self.friction)
                            return list(out.values())
                        summaries = self._card_summaries(page)
                        if not summaries:
                            break
                        for s in summaries:
                            if hooks.should_stop():
                                stop = True
                                break
                            hooks.emit("seen")
                            if s["id"] in out or s["id"] in hooks.skip_ids:
                                hooks.emit("known")
                                continue
                            if hooks.pre_filter is not None and s["title"]:
                                if reason := hooks.pre_filter(s["title"], s["company"], s["location"]):
                                    hooks.emit("prefiltered", reason)
                                    continue
                            try:
                                posting = self._read_detail(page, s)
                            except Exception as e:  # noqa: BLE001
                                log.debug("naukri detail failed: {}", e)
                                hooks.emit("read_failed", str(e)[:80])
                                continue
                            if posting is None:
                                hooks.emit("read_failed", "missing title, company or description")
                            elif not self._passes_filters(posting):
                                hooks.emit("rejected", "excluded title or company")
                            else:
                                out[posting.id] = posting
                                if hooks.on_posting is not None:
                                    hooks.on_posting(posting)
                                hooks.emit("saved")
                            # pacing only after a detail page was actually opened
                            page.wait_for_timeout(int(self.quota.random_delay_seconds() * 1000 / 6))
                        if stop:
                            break
                        if hooks.pause(self.quota.random_delay_seconds()):  # human-like pause between pages
                            stop = True
                            break
        finally:
            page.close()
        log.info("Naukri collector found {} postings", len(out))
        return list(out.values())

    def _card_summaries(self, page: Page) -> list[dict]:
        gentle_scroll(page, steps=4, px=800)
        for sel in CARD_SELECTORS:
            loc = page.locator(sel)
            n = loc.count()
            if not n:
                continue
            res = []
            for i in range(n):
                card = loc.nth(i)
                title_el = card.locator(", ".join(CARD_TITLE)).first
                href = title_el.get_attribute("href") if title_el.count() else None
                if not href:
                    continue
                jid = (
                    card.get_attribute("data-job-id")
                    or (re.search(r"-(\d+)(?:\?|$)", href) or [None, None])[1]
                )
                if not jid:
                    continue
                res.append(
                    {
                        "id": jid,
                        "url": href,
                        "title": safe_text(title_el),
                        "company": safe_text(card.locator(", ".join(CARD_COMPANY)).first)
                        if card.locator(", ".join(CARD_COMPANY)).count()
                        else "",
                        "location": safe_text(card.locator(", ".join(CARD_LOCATION)).first)
                        if card.locator(", ".join(CARD_LOCATION)).count()
                        else "",
                        "posted": safe_text(card.locator(", ".join(CARD_POSTED)).first)
                        if card.locator(", ".join(CARD_POSTED)).count()
                        else "",
                    }
                )
            if res:
                return res
        return []

    def _read_detail(self, page: Page, s: dict) -> JobPosting | None:
        detail = self.browser.new_page()
        try:
            detail.goto(s["url"], wait_until="domcontentloaded", timeout=45_000)
            detail.wait_for_timeout(1800)
            jd = safe_text(first_visible(detail, DETAIL_DESC, 4000))
            title = safe_text(first_visible(detail, DETAIL_TITLE, 2000)) or s["title"]
            company = safe_text(first_visible(detail, DETAIL_COMPANY, 1500)) or s["company"]
            location = safe_text(first_visible(detail, DETAIL_LOCATION, 1000)) or s["location"]
            if not jd or not title or not company:
                return None
            return JobPosting(
                source=self.source,
                external_id=str(s["id"]),
                company=company,
                title=title,
                location=location or None,
                remote_type=infer_remote_type(location, title, jd[:600]),
                jd_text=jd,
                apply_url=s["url"].split("?")[0],
                ats_type=AtsType.NAUKRI,
                posted_at=self._posted_at(s.get("posted", "")),
                raw_metadata={"card_location": s["location"], "posted_text": s.get("posted")},
            )
        finally:
            detail.close()

    @staticmethod
    def _posted_at(text: str) -> datetime | None:
        t = (text or "").lower()
        if "just now" in t or "today" in t or "few hours" in t:
            return datetime.now(UTC)
        m = re.search(r"(\d+)\s*(day|week|month)s?\s*ago", t)
        if not m:
            return None
        n, unit = int(m.group(1)), m.group(2)
        return (
            datetime.now(UTC)
            - {"day": timedelta(days=n), "week": timedelta(weeks=n), "month": timedelta(days=30 * n)}[unit]
        )

    def _passes_filters(self, p: JobPosting) -> bool:
        t = p.title.lower()
        if any(k.lower() in t for k in self.search.exclude_title_keywords):
            return False
        return not any(c.lower() == p.company.lower() for c in self.search.exclude_companies)
