"""Callbacks that let a browser collector stream results, skip known postings, and honour a stop request.

Collectors stay usable without hooks (fetch() with no arguments returns the full list as before).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from core.models import JobPosting


def _never() -> bool:
    return False


@dataclass
class CollectHooks:
    # called once per accepted posting, as soon as it is read (so the caller can persist it)
    on_posting: Callable[[JobPosting], None] | None = None
    # checked before every card and page; True means finish the current card and return
    should_stop: Callable[[], bool] = _never
    # external ids already stored for this source: skipped before the detail page is opened
    skip_ids: set[str] = field(default_factory=set)
    # (title, company, location) from the search results card -> None to keep, or a rejection reason
    pre_filter: Callable[[str, str, str], str | None] | None = None
    # progress events: seen | known | prefiltered | rejected | read_failed | saved
    on_event: Callable[[str, str], None] | None = None

    def emit(self, kind: str, detail: str = "") -> None:
        if self.on_event is not None:
            self.on_event(kind, detail)

    def pause(self, seconds: float) -> bool:
        """Sleep in short steps so a stop request interrupts a long pause. True if a stop was requested."""
        end = time.monotonic() + seconds
        while (remaining := end - time.monotonic()) > 0:
            if self.should_stop():
                return True
            time.sleep(min(1.0, remaining))
        return False
