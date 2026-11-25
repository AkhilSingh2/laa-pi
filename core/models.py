"""Shared Pydantic models and enums used across connectors, engine, copilot, and UI."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Source(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    LINKEDIN = "linkedin"
    NAUKRI = "naukri"


class AtsType(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    LINKEDIN_EASY_APPLY = "linkedin_easy_apply"
    NAUKRI = "naukri"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


class RemoteType(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class JobStatus(StrEnum):
    DISCOVERED = "discovered"
    SCORED = "scored"
    ARCHIVED_LOW_FIT = "archived_low_fit"
    READY_FOR_REVIEW = "ready_for_review"
    APPROVED = "approved"
    SKIPPED = "skipped"
    APPLIED = "applied"
    DUPLICATE = "duplicate"


class AppStatus(StrEnum):
    DISCOVERED = "Discovered"
    READY_FOR_REVIEW = "Ready for Review"
    APPLIED = "Applied"
    OUTREACH_SENT = "Outreach Sent"
    INTERVIEW = "Interview"
    REJECTED = "Rejected"
    GHOSTED = "Ghosted"


class FrictionType(StrEnum):
    OTP = "otp"
    CAPTCHA = "captcha"
    BLOCKED = "blocked"  # site returned an access-denied / bot-block page
    LOGIN_REQUIRED = "login_required"
    UNKNOWN_QUESTION = "unknown_question"
    UPLOAD_FAILED = "upload_failed"


class FormSessionStatus(StrEnum):
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    PAUSED_FRICTION = "paused_friction"
    SUBMITTED = "submitted"
    FAILED = "failed"
    ABANDONED = "abandoned"


DIRECT_ATS: frozenset[str] = frozenset({AtsType.GREENHOUSE, AtsType.LEVER, AtsType.ASHBY})


def platform_for(source: str, ats_type: str | None = None) -> str:
    """Map a job source / ATS type onto a quota platform bucket."""
    if source == Source.LINKEDIN or ats_type == AtsType.LINKEDIN_EASY_APPLY:
        return "linkedin"
    if source == Source.NAUKRI or ats_type == AtsType.NAUKRI:
        return "naukri"
    return "direct_ats"


_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_COMPANY_SUFFIX = re.compile(
    r"\b(inc|inc\.|llc|ltd|ltd\.|limited|pvt|pvt\.|private|corp|corporation|co|co\.|technologies|technology|labs|india|group|plc)\b"
)
_TITLE_NOISE = re.compile(
    r"\b(remote|hybrid|onsite|on-site|india|bangalore|bengaluru|hyderabad|pune|gurugram|gurgaon|mumbai|delhi|ncr|usa|uk|eu|m/f/d|f/m/d|[ivx]{1,4})\b"
)
_PARENS = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def normalize_company(name: str) -> str:
    s = _PARENS.sub(" ", name.lower())
    s = _NON_ALNUM.sub(" ", s)
    s = _COMPANY_SUFFIX.sub(" ", s)
    return _WS.sub(" ", s).strip()


def normalize_title(title: str) -> str:
    s = _PARENS.sub(" ", title.lower())
    s = s.replace("-", " ").replace("/", " ")
    s = _NON_ALNUM.sub(" ", s)
    s = _TITLE_NOISE.sub(" ", s)
    s = s.replace("sr ", "senior ").replace("jr ", "junior ")
    s = re.sub(r"\bfull ?stack\b", "fullstack", s)
    s = re.sub(r"\bfront ?end\b", "frontend", s)
    s = re.sub(r"\bback ?end\b", "backend", s)
    return _WS.sub(" ", s).strip()


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class JobPosting(BaseModel):
    """Unified job posting produced by every connector."""

    id: str = ""
    source: Source
    external_id: str
    company: str
    title: str
    location: str | None = None
    remote_type: RemoteType = RemoteType.UNKNOWN
    jd_text: str
    apply_url: str
    ats_type: AtsType = AtsType.UNKNOWN
    posted_at: datetime | None = None
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("jd_text", mode="before")
    @classmethod
    def _strip_jd(cls, v: Any) -> str:
        return (v or "").strip()

    def model_post_init(self, __context: Any) -> None:
        if not self.id:
            self.id = sha1(f"{self.source}:{self.company_normalized}:{self.external_id}")

    @property
    def company_normalized(self) -> str:
        return normalize_company(self.company)

    @property
    def title_normalized(self) -> str:
        return normalize_title(self.title)

    @property
    def jd_hash(self) -> str:
        return sha1(_WS.sub(" ", self.jd_text.lower()).strip())

    @property
    def platform(self) -> str:
        return platform_for(self.source, self.ats_type)


def infer_remote_type(*texts: str | None) -> RemoteType:
    blob = " ".join(t for t in texts if t).lower()
    if "hybrid" in blob:
        return RemoteType.HYBRID
    if "remote" in blob or "work from home" in blob or "wfh" in blob:
        return RemoteType.REMOTE
    if any(k in blob for k in ("onsite", "on-site", "in office", "in-office")):
        return RemoteType.ONSITE
    return RemoteType.UNKNOWN


class FitBreakdownItem(BaseModel):
    score: int
    max: int
    evidence: str


class FitScore(BaseModel):
    score: int = Field(ge=0, le=100)
    breakdown: dict[str, FitBreakdownItem]
    rationale: str
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    summary_variant: str = "default"
