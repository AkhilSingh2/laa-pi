"""Daily submission caps, execution windows, and humanized delays."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from core import repository as repo
from core.logging import log
from core.settings import QuotasConfig

_DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


@dataclass(frozen=True)
class QuotaStatus:
    platform: str
    used: int
    cap: int

    @property
    def remaining(self) -> int:
        return max(self.cap - self.used, 0)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.cap


class QuotaManager:
    def __init__(self, cfg: QuotasConfig, timezone: str) -> None:
        self.cfg = cfg
        self.tz = ZoneInfo(timezone)

    # ------------------------------------------------------------------ time helpers

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> str:
        return self.now().strftime("%Y-%m-%d")

    def in_execution_window(self, at: datetime | None = None) -> bool:
        if not self.cfg.execution_windows:
            return True
        at = at or self.now()
        day = at.weekday()
        t = at.time()
        for w in self.cfg.execution_windows:
            days = {_DAY_INDEX[d.lower()[:3]] for d in w.days}
            if day not in days:
                continue
            start = dtime.fromisoformat(w.start)
            end = dtime.fromisoformat(w.end)
            if start <= t <= end:
                return True
        return False

    def next_window_start(self, at: datetime | None = None) -> datetime | None:
        at = at or self.now()
        best: datetime | None = None
        for offset in range(0, 8):
            d = at.date().toordinal() + offset
            day_dt = datetime.fromordinal(d).replace(tzinfo=self.tz)
            for w in self.cfg.execution_windows:
                days = {_DAY_INDEX[x.lower()[:3]] for x in w.days}
                if day_dt.weekday() not in days:
                    continue
                start = day_dt.replace(
                    hour=dtime.fromisoformat(w.start).hour,
                    minute=dtime.fromisoformat(w.start).minute,
                    second=0,
                )
                if start > at and (best is None or start < best):
                    best = start
        return best

    # ------------------------------------------------------------------ caps

    def cap_for(self, platform: str) -> int:
        return int(self.cfg.daily_caps.get(platform, 0))

    def status(self, session: Session, platform: str) -> QuotaStatus:
        row = repo.peek_quota(session, self.today(), platform)
        cap = self.cap_for(platform)
        if row is None:
            return QuotaStatus(platform=platform, used=0, cap=cap)
        return QuotaStatus(platform=platform, used=row.submissions, cap=row.cap)

    def all_status(self, session: Session) -> list[QuotaStatus]:
        return [self.status(session, p) for p in self.cfg.daily_caps]

    def can_submit(self, session: Session, platform: str) -> bool:
        return not self.status(session, platform).exhausted

    def all_exhausted(self, session: Session) -> bool:
        return all(s.exhausted for s in self.all_status(session))

    def record_submission(self, session: Session, platform: str) -> QuotaStatus:
        row = repo.increment_quota(session, self.today(), platform, self.cap_for(platform))
        log.info("quota {} -> {}/{}", platform, row.submissions, row.cap)
        return QuotaStatus(platform=platform, used=row.submissions, cap=row.cap)

    # ------------------------------------------------------------------ human-like pacing

    def random_delay_seconds(self) -> float:
        lo, hi = self.cfg.human_delay_seconds.min, self.cfg.human_delay_seconds.max
        base = random.uniform(lo, hi)
        jitter = random.gauss(0, (hi - lo) / 10)
        return max(lo, min(hi, base + jitter))

    def human_delay(self, reason: str = "") -> float:
        secs = self.random_delay_seconds()
        log.debug("human delay {:.1f}s {}", secs, reason)
        time.sleep(secs)
        return secs
