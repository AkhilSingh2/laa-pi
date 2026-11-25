"""One browser at a time.

Chromium's persistent profile (data/browser_profile) cannot be opened by two processes; the second one fails
with "Opening in existing browser session". Every process that opens the profile (collector, copilot, login,
connection check, scheduled apply) takes this lock first. A lock whose process has died is cleared automatically.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOCK_FILE = "data/exports/browser.lock"


class BrowserBusy(RuntimeError):
    def __init__(self, holder: dict[str, Any]) -> None:
        self.holder = holder
        super().__init__(f"browser is in use by {holder.get('owner')} (pid {holder.get('pid')})")


def _path(settings) -> Path:  # noqa: ANN001
    return settings.path(LOCK_FILE)


def _alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    return True


def holder(settings) -> dict[str, Any] | None:  # noqa: ANN001
    """Who holds the browser right now, or None. Clears a stale lock left by a dead process."""
    p = _path(settings)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = {}
    if data and _alive(data.get("pid")):
        return data
    p.unlink(missing_ok=True)
    return None


def acquire(settings, owner: str, pid: int | None = None) -> None:  # noqa: ANN001
    """Take the lock for this process (or `pid`). Raises BrowserBusy if another live process holds it."""
    p = _path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    pid = pid or os.getpid()
    payload = json.dumps(
        {"pid": pid, "owner": owner, "since": datetime.now(UTC).isoformat(timespec="seconds")}
    )
    for _ in range(2):
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            current = holder(settings)  # clears a stale lock
            if current is not None:
                if int(current.get("pid", 0)) == pid:
                    return
                raise BrowserBusy(current) from None
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        return
    raise BrowserBusy(holder(settings) or {"owner": "unknown"})


def release(settings, pid: int | None = None) -> None:  # noqa: ANN001
    p = _path(settings)
    current = holder(settings)
    if current is not None and int(current.get("pid", 0)) == (pid or os.getpid()):
        p.unlink(missing_ok=True)


@contextmanager
def browser_lock(settings, owner: str) -> Iterator[None]:  # noqa: ANN001
    acquire(settings, owner)
    try:
        yield
    finally:
        release(settings)
