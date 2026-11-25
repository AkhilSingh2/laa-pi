"""Discovery pipeline: filter -> store -> dedup -> score -> archive/queue -> contacts -> resume -> outreach drafts.

API boards are ingested in-process. Browser postings (LinkedIn/Naukri) are written by the background worker in
pipeline/browser_run.py as they are read; `process_pending` then dedupes, scores and prepares everything still
in DISCOVERED state, whichever source it came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from connectors import build_api_connectors
from core import repository as repo
from core.llm import LLMClient
from core.logging import log
from core.models import (
    AppStatus,
    JobPosting,
    JobStatus,
    RemoteType,
    Source,
    normalize_company,
    normalize_title,
    platform_for,
)
from core.profile import MasterProfile, load_profile
from core.quota import QuotaManager
from core.schema import Job
from core.settings import SearchConfig, Settings
from engine import contacts as contacts_engine
from engine import matcher, outreach, resume_builder


@dataclass
class DiscoveryStats:
    fetched: int = 0  # raw postings returned by the API connectors
    kept: int = 0  # passed the search filters (only these are stored and scored)
    filtered: int = 0  # rejected before storage
    filter_reasons: dict[str, int] = field(default_factory=dict)
    new: int = 0
    duplicates: int = 0
    scored: int = 0
    archived: int = 0
    queued: int = 0
    resumes: int = 0
    outreach_drafts: int = 0
    errors: list[str] = field(default_factory=list)
    friction: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__


# --------------------------------------------------------------------------- search filters

_SENIORITY = re.compile(r"\b(senior|staff|principal|lead)\b")
_WS = re.compile(r"\s+")
_NON_ALPHA = re.compile(r"[^a-z]")
# walk-in hiring events are interview days, not applications the copilot can submit
_WALK_IN_TITLE = re.compile(r"\bwalk[\s-]*ins?\b", re.IGNORECASE)
_WALK_IN_JD = re.compile(
    r"\bwalk[\s-]*ins?\b.{0,40}\b(interview|drive|date|venue|event)s?\b", re.IGNORECASE | re.DOTALL
)


def _role_core(title: str) -> str:
    """Normalized title with seniority words removed, so 'Senior Full-Stack Engineer' -> 'fullstack engineer'."""
    return _WS.sub(" ", _SENIORITY.sub(" ", normalize_title(title))).strip()


def build_role_patterns(search: SearchConfig) -> list[re.Pattern[str]]:
    cores = {_role_core(t) for t in search.titles if t.strip()}
    return [re.compile(rf"\b{re.escape(c)}\b") for c in sorted(cores) if c]


_role_patterns = build_role_patterns  # kept for existing callers and tests


def _location_text_ok(location: str | None, remote: bool, search: SearchConfig) -> bool:
    loc = (location or "").strip().lower()
    if not loc:
        return True  # unknown location: let it through rather than silently drop it
    if "india" in loc:
        return True
    cities = [c.lower() for c in search.locations if c.strip().lower() != "remote"]
    if any(c in loc for c in cities):
        return True
    if remote:
        # remote roles are only relevant when they are open to India
        return _NON_ALPHA.sub("", loc) == "remote"
    return False


def text_reject_reason(
    title: str,
    company: str,
    location: str | None,
    remote: bool,
    search: SearchConfig,
    role_patterns: list[re.Pattern[str]],
) -> str | None:
    """Filters that only need title, company and location, so they can run on a search card before opening it."""
    t = title.lower()
    if _WALK_IN_TITLE.search(title):
        return "walk_in"
    if any(k.lower() in t for k in search.exclude_title_keywords):
        return "title_excluded"
    if any(c.lower() == company.lower() for c in search.exclude_companies):
        return "company_excluded"
    if role_patterns and not any(rx.search(_role_core(title)) for rx in role_patterns):
        return "role_mismatch"
    if not _location_text_ok(location, remote, search):
        return "location"
    return None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _reject_reason(
    p: JobPosting, search: SearchConfig, role_patterns: list[re.Pattern[str]], now: datetime
) -> str | None:
    """Return why a posting is out of scope for the configured search, or None if it should be kept."""
    if not p.jd_text:
        return "no_jd"
    reason = text_reject_reason(
        p.title, p.company, p.location, p.remote_type == RemoteType.REMOTE, search, role_patterns
    )
    if reason:
        return reason
    if _WALK_IN_JD.search(p.jd_text[:2000]):
        return "walk_in"
    lo, hi = matcher.jd_years_range(p.jd_text)
    if lo is not None and lo > search.experience_years.max:
        return "experience_too_senior"
    if hi is not None and hi < search.experience_years.min:
        return "experience_too_junior"
    if p.posted_at and now - _aware(p.posted_at) > timedelta(days=search.posted_within_days):
        return "stale"
    return None


def _posting_from_row(job: Job) -> JobPosting:
    return JobPosting(
        id=job.id,
        source=Source(job.source),
        external_id=job.external_id,
        company=job.company,
        title=job.title,
        location=job.location,
        remote_type=job.remote_type or "unknown",
        jd_text=job.jd_text,
        apply_url=job.apply_url,
        ats_type=job.ats_type or "unknown",
        posted_at=datetime.fromisoformat(job.posted_at) if job.posted_at else None,
        raw_metadata=repo.job_metadata(job),
    )


class DiscoveryPipeline:
    def __init__(
        self, settings: Settings, llm: LLMClient | None = None, profile: MasterProfile | None = None
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.profile = profile or load_profile(settings.path(settings.app.profile_path))
        self.quota = QuotaManager(settings.quotas, settings.app.timezone)

    # ------------------------------------------------------------------ ingestion

    def ingest_api(self, session: Session, stats: DiscoveryStats) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for connector in build_api_connectors(self.settings):
            for t in connector.targets:
                repo.upsert_company(
                    session,
                    name_normalized=normalize_company(t.company),
                    display_name=t.company,
                    ats_type=t.ats_type,
                    board_token=t.board_token,
                    careers_url=t.careers_url,
                    priority=t.priority,
                )
            found = connector.fetch()
            for company, err in connector.errors.items():
                stats.errors.append(f"{connector.source}:{company}: {err}")
            postings.extend(found)
            log.info(
                "{} fetched {} postings ({} errors)", connector.source, len(found), len(connector.errors)
            )
        return postings

    # ------------------------------------------------------------------ stages

    def persist_and_dedupe(
        self, session: Session, postings: list[JobPosting], stats: DiscoveryStats
    ) -> list[Job]:
        # Filter BEFORE storing or scoring so out-of-scope postings never cost an LLM call.
        search = self.settings.search
        role_patterns = build_role_patterns(search)
        now = datetime.now(UTC)
        stats.fetched = len(postings)
        kept: list[JobPosting] = []
        for p in postings:
            reason = _reject_reason(p, search, role_patterns, now)
            if reason:
                stats.filter_reasons[reason] = stats.filter_reasons.get(reason, 0) + 1
                continue
            kept.append(p)
        stats.kept = len(kept)
        stats.filtered = stats.fetched - stats.kept

        new_rows: list[Job] = []
        for p in kept:
            row, created = repo.upsert_job(session, p)
            if created:
                new_rows.append(row)
        stats.new = len(new_rows)
        session.flush()
        return self._dedupe(session, new_rows, stats)

    def _dedupe(self, session: Session, rows: list[Job], stats: DiscoveryStats) -> list[Job]:
        """Mark cross-posted copies as duplicates. Returns the rows that are still canonical."""
        keys = {(r.company_normalized, r.title_normalized) for r in rows}
        pool_rows = [
            r
            for r in session.query(Job).filter(Job.status != JobStatus.DUPLICATE.value).all()
            if (r.company_normalized, r.title_normalized) in keys
        ]
        pool = [_posting_from_row(r) for r in pool_rows]
        result = matcher.dedupe(pool, self.settings.matching)
        by_id = {r.id: r for r in pool_rows}
        for dup_id, canon_id in result.duplicates.items():
            dup, canon = by_id[dup_id], by_id[canon_id]
            if dup.status in (JobStatus.APPLIED.value, JobStatus.APPROVED.value):
                continue  # never demote something already acted on
            # if the previously canonical row is now the duplicate, carry over its score to the new canonical
            if dup.fit_score is not None and canon.fit_score is None:
                canon.fit_score, canon.fit_breakdown, canon.fit_rationale = (
                    dup.fit_score,
                    dup.fit_breakdown,
                    dup.fit_rationale,
                )
                canon.status = dup.status if dup.status != JobStatus.DISCOVERED.value else canon.status
            repo.mark_duplicate(session, dup, canon)
            stats.duplicates += 1
        session.flush()
        return [r for r in rows if r.status != JobStatus.DUPLICATE.value]

    def score(self, session: Session, rows: list[Job], stats: DiscoveryStats) -> list[Job]:
        queued: list[Job] = []
        threshold = self.settings.matching.fit_threshold
        for row in rows:
            if row.fit_score is not None and row.status != JobStatus.DISCOVERED.value:
                continue
            posting = _posting_from_row(row)
            try:
                fit = matcher.score_job(posting, self.profile, self.settings.matching, self.llm)
            except Exception as e:  # noqa: BLE001
                stats.errors.append(f"score {row.id[:8]}: {e}")
                continue
            repo.save_fit_score(
                session,
                row,
                score=fit.score,
                breakdown=matcher.fit_to_breakdown_json(fit),
                rationale=fit.rationale,
                threshold=threshold,
            )
            stats.scored += 1
            if row.status == JobStatus.ARCHIVED_LOW_FIT.value:
                stats.archived += 1
            else:
                queued.append(row)
        session.flush()
        return queued

    def prepare(self, session: Session, rows: list[Job], stats: DiscoveryStats) -> None:
        """For fit-qualified jobs: tailored resume, contacts, outreach drafts; mark Ready for Review."""
        resumes_dir = self.settings.path(self.settings.app.resumes_dir)
        city = self.profile.basics.location.city
        for row in rows:
            posting = _posting_from_row(row)
            breakdown = repo.job_fit_breakdown(row)
            matched = breakdown.get("matched_skills") or []
            evidence = matcher.local_evidence(posting, self.profile, self.settings.matching)
            try:
                if repo.latest_resume(session, row.id) is None:
                    built = resume_builder.build_resume(
                        profile=self.profile,
                        posting=posting,
                        cfg=self.settings.resume,
                        root=self.settings.root,
                        resumes_dir=resumes_dir,
                        llm=self.llm,
                        evidence=evidence,
                    )
                    repo.add_resume(
                        session,
                        job_id=row.id,
                        summary_variant=built.summary_variant,
                        resume_json=built.resume_json,
                        pdf_path=str(built.pdf_path),
                        ats_keyword_score=built.ats_keyword_score,
                        matched_keywords=built.matched_keywords,
                        missing_keywords=built.missing_keywords,
                    )
                    stats.resumes += 1
            except Exception as e:  # noqa: BLE001
                log.exception("resume build failed for {}", row.id)
                stats.errors.append(f"resume {row.id[:8]}: {e}")

            try:
                if not repo.contacts_for_job(session, row.id):
                    leads, _queries = contacts_engine.discover_contacts(posting, self.settings.contacts, city)
                    for lead in leads:
                        repo.add_contact(
                            session,
                            job_id=row.id,
                            company_normalized=row.company_normalized,
                            contact_type=lead.contact_type,
                            name=lead.name,
                            role=lead.role,
                            profile_url=lead.profile_url or lead.search_urls.get("linkedin_people"),
                            email_guess=lead.email_guess,
                            email_pattern=lead.email_pattern,
                            source=lead.source,
                            confidence=lead.confidence,
                        )
            except Exception as e:  # noqa: BLE001
                stats.errors.append(f"contacts {row.id[:8]}: {e}")

            try:
                if not repo.outreach_for_job(session, row.id):
                    existing = repo.contacts_for_job(session, row.id)
                    recruiter = next(
                        (c for c in existing if c.contact_type in ("recruiter", "hiring_manager")), None
                    )
                    peer = next((c for c in existing if c.contact_type == "peer"), None)
                    res = outreach.draft_outreach(
                        profile=self.profile,
                        posting=posting,
                        matched_skills=matched or evidence.matched_skills,
                        cfg=self.settings.outreach,
                        llm=self.llm,
                        recruiter_name=recruiter.name if recruiter else None,
                        peer_name=peer.name if peer else None,
                    )
                    d = res.drafts
                    repo.add_outreach(
                        session,
                        job_id=row.id,
                        contact_id=recruiter.id if recruiter else None,
                        channel="linkedin_connect",
                        body=d.recruiter_connect,
                        metric_used=d.metric_used,
                    )
                    repo.add_outreach(
                        session,
                        job_id=row.id,
                        contact_id=recruiter.id if recruiter else None,
                        channel="email",
                        body=d.recruiter_email,
                        metric_used=d.metric_used,
                    )
                    repo.add_outreach(
                        session,
                        job_id=row.id,
                        contact_id=peer.id if peer else None,
                        channel="peer_referral",
                        body=d.peer_referral,
                        metric_used=d.metric_used,
                    )
                    stats.outreach_drafts += 3
            except Exception as e:  # noqa: BLE001
                stats.errors.append(f"outreach {row.id[:8]}: {e}")

            repo.set_job_status(session, row, JobStatus.READY_FOR_REVIEW)
            mode = self.settings.copilot.default_mode_by_platform.get(
                platform_for(row.source, row.ats_type), "review_mode"
            )
            repo.ensure_application(session, row, mode=mode, status=AppStatus.READY_FOR_REVIEW)
            stats.queued += 1
        session.flush()

    # ------------------------------------------------------------------ entry

    def process_pending(self, session: Session, stats: DiscoveryStats | None = None) -> DiscoveryStats:
        """Dedupe, score and prepare every job still in DISCOVERED state (any source, including browser saves)."""
        stats = stats or DiscoveryStats()
        pending = [
            r for r in repo.jobs_by_status(session, JobStatus.DISCOVERED) if r.canonical_job_id is None
        ]
        rows = self._dedupe(session, pending, stats)
        queued = self.score(session, rows, stats)
        self.prepare(session, queued, stats)
        session.commit()
        return stats

    def run(self, session: Session) -> DiscoveryStats:
        """API boards -> filter -> store -> process everything pending."""
        stats = DiscoveryStats()
        postings = self.ingest_api(session, stats)
        self.persist_and_dedupe(session, postings, stats)
        return self.process_pending(session, stats)
