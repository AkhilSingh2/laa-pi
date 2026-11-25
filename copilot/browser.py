"""Persistent local browser session.

`launch_persistent_context(headless=False)` reuses data/browser_profile/, so you log into LinkedIn / Naukri
manually once and the cookies persist. No credentials are ever read or stored by this project.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, Locator, Page, Playwright, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

from core.logging import log
from core.settings import Settings

LOGIN_URLS = {
    "linkedin": "https://www.linkedin.com/login",
    "naukri": "https://www.naukri.com/nlogin/login",
}
HOME_URLS = {
    "linkedin": "https://www.linkedin.com/feed/",
    "naukri": "https://www.naukri.com/mnjuser/homepage",
}


class BrowserSession:
    def __init__(self, settings: Settings, *, headless: bool | None = None) -> None:
        self.settings = settings
        self.profile_dir: Path = settings.path(settings.app.browser_profile_dir)
        self.headless = settings.copilot.headless if headless is None else headless
        self._pw: Playwright | None = None
        self.context: BrowserContext | None = None

    def __enter__(self) -> BrowserSession:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        headless = self.headless
        if not headless and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            # Linux server without a display: the Docker image provides one (Xvfb on DISPLAY=:99).
            log.warning("no DISPLAY available; launching headless (sites like Naukri may block this)")
            headless = True
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=headless,
            slow_mo=self.settings.copilot.slow_mo_ms,
            viewport={"width": 1366, "height": 900},
            locale="en-IN",
            timezone_id=self.settings.app.timezone,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",  # Docker's small /dev/shm otherwise crashes tabs
            ],
            ignore_default_args=["--enable-automation"],
            accept_downloads=True,
        )
        self.context.set_default_timeout(20_000)
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        try:
            if self.context is not None:
                self.context.close()
        finally:
            if self._pw is not None:
                self._pw.stop()

    # ------------------------------------------------------------------ tabs

    def new_page(self) -> Page:
        assert self.context is not None
        page = self.context.new_page()
        page.set_default_timeout(20_000)
        return page

    def page(self) -> Page:
        assert self.context is not None
        return self.context.pages[0] if self.context.pages else self.new_page()

    # ------------------------------------------------------------------ login state

    def is_logged_in(self, platform: str, page: Page | None = None) -> bool:
        page = page or self.new_page()
        try:
            page.goto(HOME_URLS[platform], wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1500)
            url = page.url.lower()
            if platform == "linkedin":
                return (
                    "/login" not in url
                    and "/checkpoint" not in url
                    and "authwall" not in url
                    and "/uas/" not in url
                )
            if platform == "naukri":
                return "nlogin" not in url and "login" not in url
            return True
        except PWTimeout:
            return False

    def open_for_manual_login(self, platform: str, wait_seconds: int = 300) -> bool:
        """Open the login page and wait (polling) for the user to finish logging in manually."""
        page = self.new_page()
        page.goto(LOGIN_URLS[platform], wait_until="domcontentloaded")
        log.info("Please log in to {} in the browser window (waiting up to {}s)", platform, wait_seconds)
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            url = page.url.lower()
            if platform == "linkedin" and "feed" in url:
                return True
            if platform == "naukri" and "mnjuser" in url:
                return True
        return False


# ---------------------------------------------------------------------- helpers shared by adapters


def first_visible(page: Page, selectors: list[str], timeout_ms: int = 2500) -> Locator | None:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout_ms)
            return loc
        except PWTimeout:
            continue
        except Exception:  # noqa: BLE001
            continue
    return None


def safe_text(loc: Locator | None, default: str = "") -> str:
    if loc is None:
        return default
    try:
        return (loc.inner_text(timeout=3000) or "").strip()
    except Exception:  # noqa: BLE001
        return default


def gentle_scroll(page: Page, steps: int = 6, px: int = 600) -> None:
    for _ in range(steps):
        page.mouse.wheel(0, px)
        page.wait_for_timeout(350)


def screenshot(page: Page, directory: Path, stem: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}_{int(time.time())}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:  # noqa: BLE001
        page.screenshot(path=str(path))
    return str(path)
