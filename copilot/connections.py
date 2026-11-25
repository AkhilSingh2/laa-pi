"""Portal connections: manual login and "is the saved session still logged in?" checks.

Runs as its own process (`python -m copilot.connections login|check --platform ...`) holding the browser lock.
Results go to data/exports/connections.json for the Settings and Home pages. The user always types their own
credentials into the real login page; nothing here reads or stores a password.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from typing import Any

from core.settings import Settings

CONNECTIONS_FILE = "data/exports/connections.json"
PLATFORMS = ("linkedin", "naukri")


def read_connections(settings: Settings) -> dict[str, dict[str, Any]]:
    p = settings.path(CONNECTIONS_FILE)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def record(settings: Settings, platform: str, **fields: Any) -> None:
    data = read_connections(settings)
    entry = data.get(platform, {})
    entry.update(fields, updated_at=datetime.now(UTC).isoformat(timespec="seconds"))
    data[platform] = entry
    p = settings.path(CONNECTIONS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def run(settings: Settings, action: str, platforms: list[str], wait_seconds: int = 300) -> int:
    from copilot.browser import BrowserSession
    from core.browser_lock import BrowserBusy, browser_lock

    for p in platforms:
        record(settings, p, state="login_waiting" if action == "login" else "checking")
    try:
        with browser_lock(settings, f"{action} {','.join(platforms)}"), BrowserSession(settings) as browser:
            for p in platforms:
                checked_at = datetime.now(UTC).isoformat(timespec="seconds")
                if action == "login":
                    ok = browser.open_for_manual_login(p, wait_seconds=wait_seconds)
                    detail = "logged in" if ok else f"not completed within {wait_seconds // 60} min"
                else:
                    ok = browser.is_logged_in(p)
                    detail = "session valid" if ok else "logged out - use Log in"
                record(settings, p, state="done", logged_in=ok, checked_at=checked_at, detail=detail)
    except BrowserBusy as e:
        for p in platforms:
            record(settings, p, state="done", detail=f"skipped: {e}")
        return 2
    except Exception as e:  # noqa: BLE001
        for p in platforms:
            record(settings, p, state="done", detail=f"error: {e}")
        return 1
    return 0


def main() -> None:
    from core.logging import setup_logging
    from core.settings import load_settings

    ap = argparse.ArgumentParser(description="laa-pi portal login / connection check")
    ap.add_argument("action", choices=["login", "check"])
    ap.add_argument("--platform", action="append", choices=PLATFORMS)
    ap.add_argument("--wait", type=int, default=300)
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.app.log_level)
    raise SystemExit(run(settings, args.action, args.platform or list(PLATFORMS), args.wait))


if __name__ == "__main__":
    main()
