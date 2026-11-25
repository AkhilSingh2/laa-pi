"""Connector base classes and shared helpers (HTML -> text, target company loading)."""

from __future__ import annotations

import csv
import html
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import httpx

from core.models import JobPosting, Source

_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(p|div|li|ul|ol|br|h[1-6]|tr|section)[^>]*>", re.IGNORECASE)
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    s = html.unescape(raw)
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    s = _WS.sub(" ", s)
    s = "\n".join(line.strip() for line in s.splitlines())
    return _NL.sub("\n\n", s).strip()


@dataclass(frozen=True)
class TargetCompany:
    company: str
    ats_type: str
    board_token: str
    careers_url: str = ""
    priority: int = 3
    notes: str = ""


def load_target_companies(csv_path: Path | str, ats_type: str | None = None) -> list[TargetCompany]:
    out: list[TargetCompany] = []
    with open(csv_path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if not row.get("company") or not row.get("board_token"):
                continue
            if ats_type and row.get("ats_type", "").strip().lower() != ats_type:
                continue
            out.append(
                TargetCompany(
                    company=row["company"].strip(),
                    ats_type=row.get("ats_type", "").strip().lower(),
                    board_token=row["board_token"].strip(),
                    careers_url=(row.get("careers_url") or "").strip(),
                    priority=int(row.get("priority") or 3),
                    notes=(row.get("notes") or "").strip(),
                )
            )
    return out


class BaseConnector(ABC):
    source: Source

    @abstractmethod
    def fetch(self) -> list[JobPosting]:
        """Return all postings currently visible from this connector."""


class ApiConnector(BaseConnector):
    """Public JSON board API connector. One HTTP client, polite timeouts, per-company error isolation."""

    def __init__(
        self, targets: list[TargetCompany], client: httpx.Client | None = None, timeout: float = 20.0
    ) -> None:
        self.targets = targets
        self.client = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "laa-pi/0.1 (+local job search assistant)"},
            follow_redirects=True,
        )
        self.errors: dict[str, str] = {}

    @abstractmethod
    def fetch_company(self, target: TargetCompany) -> list[JobPosting]: ...

    def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for t in self.targets:
            try:
                postings.extend(self.fetch_company(t))
            except Exception as e:  # noqa: BLE001 - isolate one board failure from the batch
                self.errors[t.company] = f"{type(e).__name__}: {e}"
        return postings
