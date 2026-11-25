"""LinkedIn Jobs collector using the persistent (already logged-in) browser profile.

Uses the regular jobs search UI (no private APIs): opens the results page per query, reads the cards, and
clicks each card to read the detail pane. Selectors are lists with fallbacks because LinkedIn's DOM changes
frequently. Human-like delays and the per-query page cap keep traffic well below normal browsing volumes.

Hooks (see connectors/hooks.py) let the caller save each posting as it is read, skip postings already stored,
pre-filter on card text before opening anything, and stop between cards.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote_plus

from playwright.sync_api import Locator, Page

from connectors.base import BaseConnector
from connectors.hooks import CollectHooks
from copilot.browser import BrowserSession, first_visible, gentle_scroll, safe_text
from copilot.friction import detect_friction
from core.logging import log
from core.models import AtsType, JobPosting, Source, infer_remote_type
from core.quota import QuotaManager
from core.settings import LinkedInSourceConfig, SearchConfig

_EXP_LEVEL = {
    "internship": 1,
    "entry level": 2,
    "associate": 3,
    "mid-senior level": 4,
    "director": 5,
    "executive": 6,
}
_WT = {"onsite": 1, "remote": 2, "hybrid": 3}

CARD_SELECTORS = [
    "li[data-occludable-job-id]",
    "div.job-card-container[data-job-id]",
    "div[data-job-id]",
    "li.scaffold-layout__list-item",
    "li.jobs-search-results__list-item",
    ".jobs-search-results__list-item",
    "div.job-card-job-posting-card-wrapper",
    "[data-view-name='job-card']",
    ".job-card-container",
]
CARD_TITLE_SELECTORS = [".job-card-list__title--link", ".job-card-list__title", "a.job-card-container__link"]
CARD_COMPANY_SELECTORS = [".artdeco-entity-lockup__subtitle", ".job-card-container__primary-description"]
CARD_LOCATION_SELECTORS = [".job-card-container__metadata-item", ".artdeco-entity-lockup__caption"]
TITLE_SELECTORS = [
    ".job-details-jobs-unified-top-card__job-title",
    "h1.t-24",
    ".jobs-unified-top-card__job-title",
    "h1",
]
COMPANY_SELECTORS = [
    ".job-details-jobs-unified-top-card__company-name",
    ".jobs-unified-top-card__company-name",
    "a[href*='/company/']",
]
LOCATION_SELECTORS = [
    ".job-details-jobs-unified-top-card__primary-description-container",
    ".jobs-unified-top-card__bullet",
    ".job-details-jobs-unified-top-card__tertiary-description-container",
]
DESC_SELECTORS = [
    "#job-details",
    ".jobs-description__content",
    ".jobs-description-content__text",
    "article.jobs-description__container",
]
EASY_APPLY_SELECTORS = ["button.jobs-apply-button:has-text('Easy Apply')", "button:has-text('Easy Apply')"]
APPLY_SELECTORS = [".jobs-apply-button", "button:has-text('Apply')"]
SHOW_MORE = [
    "button.jobs-description__footer-button",
    "button:has-text('See more')",
    "button:has-text('Show more')",
]


class LinkedInCollector(BaseConnector):
    source = Source.LINKEDIN

    def __init__(
        self, browser: BrowserSession, cfg: LinkedInSourceConfig, search: SearchConfig, quota: QuotaManager
    ) -> None:
        self.browser = browser
        self.cfg = cfg
        self.search = search
        self.quota = quota
        self.friction: str | None = None

    def _locations(self) -> list[tuple[str, bool]]:
        """(location text, remote?) for each preferred location, from Settings > Guardrails / search.locations.

        'Remote' is not a place on LinkedIn; it becomes the India search with the remote work-mode filter.
        Cities are written as 'City, India' so LinkedIn does not fall back to the account's or the IP's city.
        """
        out: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for raw in self.search.locations or [self.cfg.location]:
            name = raw.strip()
            if not name:
                continue
            if name.lower() == "remote":
                key = "remote"
                entry = ("India", True)
            else:
                # Bangalore and Bengaluru are the same city on LinkedIn
                canon = "Bengaluru" if name.lower() in ("bangalore", "bengaluru") else name
                key = canon.lower()
                entry = (f"{canon}, India" if "india" not in canon.lower() else canon, False)
            if key not in seen:
                seen.add(key)
                out.append(entry)
        return out or [(self.cfg.location, False)]

    def _search_url(
        self,
        query: str,
        start: int,
        location: str | None = None,
        *,
        remote: bool = False,
        relax: bool = False,
    ) -> str:
        location = location or self.cfg.location
        params = [
            f"keywords={quote_plus(query)}",
            f"location={quote_plus(location)}",
            f"start={start}",
        ]
        # A long keyword plus experience plus "posted this week" is what makes LinkedIn show 0 jobs.
        # The relaxed URL keeps the role and the location and drops those extra filters.
        if remote:
            params.append("f_WT=2")
        elif not relax:
            wts = [str(_WT[x.lower()]) for x in self.cfg.remote_filters if x.lower() in _WT]
            if wts and len(wts) < 3:
                params.append("f_WT=" + quote_plus(",".join(wts)))
        if not relax:
            levels = [
                str(_EXP_LEVEL[x.lower()]) for x in self.cfg.experience_levels if x.lower() in _EXP_LEVEL
            ]
            if levels:
                params.append("f_E=" + quote_plus(",".join(levels)))
            params.append(f"f_TPR=r{self.search.posted_within_days * 86400}")
        if self.cfg.easy_apply_only:
            params.append("f_AL=true")
        return "https://www.linkedin.com/jobs/search/?" + "&".join(params)

    def fetch(self, hooks: CollectHooks | None = None) -> list[JobPosting]:
        hooks = hooks or CollectHooks()
        page = self.browser.new_page()
        out: dict[str, JobPosting] = {}
        stop = False
        try:
            for query in self.cfg.queries:
                if stop:
                    break
                for location, remote in self._locations():
                    if stop:
                        break
                    for page_idx in range(self.cfg.max_pages_per_query):
                        if hooks.should_stop():
                            stop = True
                            break
                        url = self._search_url(query, page_idx * 25, location, remote=remote)
                        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                        page.wait_for_timeout(2500)
                        if fr := detect_friction(page, expect_logged_in=True):
                            self.friction = f"{fr.type}: {fr.detail}"
                            log.warning("LinkedIn collector paused: {}", self.friction)
                            return list(out.values())
                        cards = self._cards(page)
                        if not cards and page_idx == 0:
                            relaxed = self._search_url(query, 0, location, remote=remote, relax=True)
                            log.info(
                                "LinkedIn: no cards for {!r} in {}; retrying without date/level filters",
                                query,
                                location,
                            )
                            page.goto(relaxed, wait_until="domcontentloaded", timeout=45_000)
                            page.wait_for_timeout(2500)
                            cards = self._cards(page)
                        log.info(
                            "LinkedIn {!r} in {!r} page {} -> {} cards",
                            query,
                            location,
                            page_idx + 1,
                            len(cards),
                        )
                        if not cards:
                            log.warning("LinkedIn showed 0 jobs for {!r} in {!r}", query, location)
                            break
                        for card in cards:
                            if hooks.should_stop():
                                stop = True
                                break
                            hooks.emit("seen")
                            job_id = self._card_id(card)
                            if not job_id:
                                hooks.emit("read_failed", "no id")
                                continue
                            if job_id in out or job_id in hooks.skip_ids:
                                hooks.emit("known")
                                continue
                            if hooks.pre_filter is not None:
                                title, company, location = self._card_preview(card)
                                if title and (reason := hooks.pre_filter(title, company, location)):
                                    hooks.emit("prefiltered", reason)
                                    continue
                            try:
                                posting = self._read_card(page, card, job_id)
                            except Exception as e:  # noqa: BLE001
                                log.debug("card {} failed: {}", job_id, e)
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
                            # human-like pacing only after a card was actually opened
                            page.wait_for_timeout(int(self.quota.random_delay_seconds() * 1000 / 6))
                        if stop:
                            break
                        if hooks.pause(self.quota.random_delay_seconds()):  # human-like pause between pages
                            stop = True
                            break
        finally:
            page.close()
        log.info("LinkedIn collector found {} postings", len(out))
        return list(out.values())

    def _cards(self, page: Page) -> list[Locator]:
        gentle_scroll(page, steps=5, px=700)
        for sel in CARD_SELECTORS:
            loc = page.locator(sel)
            if loc.count():
                return [loc.nth(i) for i in range(loc.count())]
        links = page.locator("a[href*='/jobs/view/']")
        seen: set[str] = set()
        cards: list[Locator] = []
        try:
            count = min(links.count(), 25)
        except Exception:  # noqa: BLE001
            count = 0
        for i in range(count):
            link = links.nth(i)
            href = link.get_attribute("href") or ""
            match = re.search(r"/jobs/view/(\d+)", href)
            if not match or match.group(1) in seen:
                continue
            seen.add(match.group(1))
            cards.append(link)
        return cards

    @staticmethod
    def _card_id(card: Locator) -> str | None:
        job_id = card.get_attribute("data-occludable-job-id") or card.get_attribute("data-job-id")
        if job_id:
            return job_id
        href = card.get_attribute("href")
        if not href:
            link = card.locator("a[href*='/jobs/view/']").first
            href = link.get_attribute("href") if link.count() else None
        m = re.search(r"/jobs/view/(\d+)", href or "")
        return m.group(1) if m else None

    @staticmethod
    def _card_preview(card: Locator) -> tuple[str, str, str]:
        """Title, company and location as shown on the search card, read without opening it. Empty if unknown."""

        def text(selectors: list[str]) -> str:
            for sel in selectors:
                el = card.locator(sel).first
                if el.count():
                    t = safe_text(el)
                    if t:
                        return t
            return ""

        return text(CARD_TITLE_SELECTORS), text(CARD_COMPANY_SELECTORS), text(CARD_LOCATION_SELECTORS)

    def _read_card(self, page: Page, card: Locator, job_id: str) -> JobPosting | None:
        card.scroll_into_view_if_needed()
        card.click()
        page.wait_for_timeout(1500)
        more = first_visible(page, SHOW_MORE, 800)
        if more is not None:
            try:
                more.click()
            except Exception:  # noqa: BLE001
                pass
        title = safe_text(first_visible(page, TITLE_SELECTORS, 3000))
        company = safe_text(first_visible(page, COMPANY_SELECTORS, 2000))
        location = safe_text(first_visible(page, LOCATION_SELECTORS, 1500)).split("·")[0].strip()
        jd = safe_text(first_visible(page, DESC_SELECTORS, 3000))
        if not title or not company or not jd:
            return None
        easy = first_visible(page, EASY_APPLY_SELECTORS, 800) is not None
        posted = self._posted_at(safe_text(first_visible(page, LOCATION_SELECTORS, 800)))
        view_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
        return JobPosting(
            source=self.source,
            external_id=str(job_id),
            company=company,
            title=title,
            location=location or None,
            remote_type=infer_remote_type(location, title, jd[:600]),
            jd_text=jd,
            apply_url=view_url,
            ats_type=AtsType.LINKEDIN_EASY_APPLY if easy else AtsType.EXTERNAL,
            posted_at=posted,
            raw_metadata={"easy_apply": easy, "view_url": view_url},
        )

    @staticmethod
    def _posted_at(text: str) -> datetime | None:
        m = re.search(r"(\d+)\s+(minute|hour|day|week|month)s?\s+ago", text or "", re.IGNORECASE)
        if not m:
            return None
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {
            "minute": timedelta(minutes=n),
            "hour": timedelta(hours=n),
            "day": timedelta(days=n),
            "week": timedelta(weeks=n),
            "month": timedelta(days=30 * n),
        }[unit]
        return datetime.now(UTC) - delta

    def _passes_filters(self, p: JobPosting) -> bool:
        t = p.title.lower()
        if any(k.lower() in t for k in self.search.exclude_title_keywords):
            return False
        if any(c.lower() == p.company.lower() for c in self.search.exclude_companies):
            return False
        return True
