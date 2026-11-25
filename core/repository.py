"""Typed CRUD / upsert helpers over the ORM. All JSON columns are (de)serialized here."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from core.models import AppStatus, JobPosting, JobStatus, platform_for, utcnow_iso
from core.schema import (
    Application,
    Company,
    Contact,
    DailyQuota,
    Event,
    FormSession,
    Job,
    OutreachMessage,
    Resume,
    RunLog,
    ScreeningAnswer,
)


def _dumps(obj: Any) -> str | None:
    return None if obj is None else json.dumps(obj, ensure_ascii=False, default=str)


def _loads(text: str | None) -> Any:
    return None if not text else json.loads(text)


# --------------------------------------------------------------------------- events


def record_event(
    session: Session, entity_type: str, entity_id: str | int, event: str, payload: Any = None
) -> None:
    session.add(
        Event(
            entity_type=entity_type,
            entity_id=str(entity_id),
            event=event,
            payload=_dumps(payload),
            created_at=utcnow_iso(),
        )
    )


# --------------------------------------------------------------------------- companies


def upsert_company(
    session: Session,
    *,
    name_normalized: str,
    display_name: str,
    ats_type: str | None = None,
    board_token: str | None = None,
    careers_url: str | None = None,
    priority: int = 3,
    email_pattern: str | None = None,
) -> Company:
    row = session.get(Company, name_normalized)
    if row is None:
        row = Company(name_normalized=name_normalized, display_name=display_name, priority=priority)
        session.add(row)
    row.display_name = display_name or row.display_name
    row.ats_type = ats_type or row.ats_type
    row.board_token = board_token or row.board_token
    row.careers_url = careers_url or row.careers_url
    row.priority = priority or row.priority
    row.email_pattern = email_pattern or row.email_pattern
    return row


def mark_company_fetched(session: Session, name_normalized: str) -> None:
    session.execute(
        update(Company).where(Company.name_normalized == name_normalized).values(last_fetched_at=utcnow_iso())
    )


# --------------------------------------------------------------------------- jobs


def upsert_job(session: Session, posting: JobPosting) -> tuple[Job, bool]:
    """Insert a posting or refresh its mutable fields. Returns (row, created)."""
    now = utcnow_iso()
    row = session.get(Job, posting.id)
    created = row is None
    if created:
        row = Job(
            id=posting.id,
            source=posting.source.value,
            external_id=posting.external_id,
            discovered_at=now,
            status=JobStatus.DISCOVERED.value,
        )
        session.add(row)
    row.company = posting.company
    row.company_normalized = posting.company_normalized
    row.title = posting.title
    row.title_normalized = posting.title_normalized
    row.location = posting.location
    row.remote_type = posting.remote_type.value
    row.jd_text = posting.jd_text
    row.jd_hash = posting.jd_hash
    row.apply_url = posting.apply_url
    row.ats_type = posting.ats_type.value
    row.posted_at = posting.posted_at.isoformat() if posting.posted_at else None
    row.raw_metadata = _dumps(posting.raw_metadata)
    row.updated_at = now
    if created:
        record_event(session, "job", row.id, "discovered", {"source": row.source, "title": row.title})
    return row, created


def get_job(session: Session, job_id: str) -> Job | None:
    return session.get(Job, job_id)


def jobs_by_status(session: Session, *statuses: JobStatus | str, limit: int | None = None) -> list[Job]:
    vals = [s.value if isinstance(s, JobStatus) else s for s in statuses]
    stmt = (
        select(Job)
        .where(Job.status.in_(vals))
        .order_by(Job.fit_score.desc().nullslast(), Job.discovered_at.desc())
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def canonical_jobs(session: Session) -> list[Job]:
    return list(session.scalars(select(Job).where(Job.canonical_job_id.is_(None))))


def set_job_status(session: Session, job: Job, status: JobStatus | str, reason: str | None = None) -> None:
    job.status = status.value if isinstance(status, JobStatus) else status
    if reason is not None:
        job.archived_reason = reason
    job.updated_at = utcnow_iso()
    record_event(session, "job", job.id, f"status:{job.status}", {"reason": reason})


def mark_duplicate(session: Session, dup: Job, canonical: Job) -> None:
    dup.canonical_job_id = canonical.id
    set_job_status(session, dup, JobStatus.DUPLICATE, f"duplicate of {canonical.id}")


def save_fit_score(
    session: Session,
    job: Job,
    *,
    score: int,
    breakdown: dict[str, Any],
    rationale: str,
    threshold: int,
) -> None:
    job.fit_score = score
    job.fit_breakdown = _dumps(breakdown)
    job.fit_rationale = rationale
    if score < threshold:
        set_job_status(session, job, JobStatus.ARCHIVED_LOW_FIT, f"fit {score} < {threshold}")
    else:
        set_job_status(session, job, JobStatus.SCORED)


def job_fit_breakdown(job: Job) -> dict[str, Any]:
    return _loads(job.fit_breakdown) or {}


def job_metadata(job: Job) -> dict[str, Any]:
    return _loads(job.raw_metadata) or {}


# --------------------------------------------------------------------------- resumes


def add_resume(
    session: Session,
    *,
    job_id: str,
    summary_variant: str,
    resume_json: dict[str, Any],
    pdf_path: str,
    ats_keyword_score: float,
    matched_keywords: list[str],
    missing_keywords: list[str],
) -> Resume:
    row = Resume(
        job_id=job_id,
        summary_variant=summary_variant,
        resume_json=_dumps(resume_json) or "{}",
        pdf_path=pdf_path,
        ats_keyword_score=ats_keyword_score,
        matched_keywords=_dumps(matched_keywords),
        missing_keywords=_dumps(missing_keywords),
        created_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    record_event(session, "job", job_id, "resume_generated", {"resume_id": row.id, "ats": ats_keyword_score})
    return row


def latest_resume(session: Session, job_id: str) -> Resume | None:
    return session.scalars(
        select(Resume).where(Resume.job_id == job_id).order_by(Resume.created_at.desc()).limit(1)
    ).first()


def resume_payload(resume: Resume) -> dict[str, Any]:
    return _loads(resume.resume_json) or {}


# --------------------------------------------------------------------------- applications


def ensure_application(
    session: Session, job: Job, mode: str, status: AppStatus | str = AppStatus.DISCOVERED
) -> Application:
    row = session.scalars(select(Application).where(Application.job_id == job.id)).first()
    now = utcnow_iso()
    if row is None:
        row = Application(
            job_id=job.id,
            platform=platform_for(job.source, job.ats_type),
            mode=mode,
            status=status.value if isinstance(status, AppStatus) else status,
            last_activity_at=now,
            created_at=now,
        )
        session.add(row)
        session.flush()
        record_event(session, "application", row.id, "created", {"job_id": job.id})
    return row


def set_application_status(
    session: Session,
    app: Application,
    status: AppStatus | str,
    *,
    notes: str | None = None,
    submitted_by: str | None = None,
) -> None:
    app.status = status.value if isinstance(status, AppStatus) else status
    app.last_activity_at = utcnow_iso()
    if notes:
        app.notes = (app.notes + "\n" if app.notes else "") + notes
    if submitted_by:
        app.submitted_by = submitted_by
    if app.status == AppStatus.APPLIED.value and not app.applied_at:
        app.applied_at = app.last_activity_at
    record_event(session, "application", app.id, f"status:{app.status}", {"notes": notes})


def application_for_job(session: Session, job_id: str) -> Application | None:
    return session.scalars(select(Application).where(Application.job_id == job_id)).first()


def all_applications(session: Session) -> list[tuple[Application, Job]]:
    stmt = (
        select(Application, Job)
        .join(Job, Job.id == Application.job_id)
        .order_by(Application.last_activity_at.desc())
    )
    return [(a, j) for a, j in session.execute(stmt).all()]


def mark_ghosted(session: Session, after_days: int) -> int:
    cutoff = (datetime.now(UTC) - timedelta(days=after_days)).isoformat()
    rows = session.scalars(
        select(Application).where(
            Application.status == AppStatus.APPLIED.value, Application.last_activity_at < cutoff
        )
    ).all()
    for app in rows:
        set_application_status(session, app, AppStatus.GHOSTED, notes=f"auto: no activity for {after_days}d")
    return len(rows)


# --------------------------------------------------------------------------- contacts & outreach


def add_contact(session: Session, **kwargs: Any) -> Contact:
    row = Contact(created_at=utcnow_iso(), **kwargs)
    session.add(row)
    session.flush()
    return row


def contacts_for_job(session: Session, job_id: str) -> list[Contact]:
    return list(session.scalars(select(Contact).where(Contact.job_id == job_id)))


def add_outreach(
    session: Session, *, job_id: str, contact_id: int | None, channel: str, body: str, metric_used: str | None
) -> OutreachMessage:
    row = OutreachMessage(
        job_id=job_id,
        contact_id=contact_id,
        channel=channel,
        body=body,
        char_count=len(body),
        word_count=len(body.split()),
        metric_used=metric_used,
        created_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


def outreach_for_job(session: Session, job_id: str) -> list[OutreachMessage]:
    return list(session.scalars(select(OutreachMessage).where(OutreachMessage.job_id == job_id)))


def outreach_by_status(session: Session, *statuses: str) -> list[tuple[OutreachMessage, Job, Contact | None]]:
    stmt = (
        select(OutreachMessage, Job, Contact)
        .join(Job, Job.id == OutreachMessage.job_id)
        .outerjoin(Contact, Contact.id == OutreachMessage.contact_id)
        .where(OutreachMessage.status.in_(statuses))
        .order_by(OutreachMessage.created_at.desc())
    )
    return [(m, j, c) for m, j, c in session.execute(stmt).all()]


def set_outreach_status(session: Session, msg: OutreachMessage, status: str) -> None:
    msg.status = status
    if status == "sent":
        msg.sent_at = utcnow_iso()
    record_event(session, "outreach", msg.id, f"status:{status}")


# --------------------------------------------------------------------------- form sessions / screening


def start_form_session(
    session: Session, *, job_id: str, application_id: int | None, platform: str, mode: str
) -> FormSession:
    row = FormSession(
        job_id=job_id,
        application_id=application_id,
        platform=platform,
        mode=mode,
        status="running",
        started_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


def finish_form_session(
    session: Session,
    fs: FormSession,
    *,
    status: str,
    friction_type: str | None = None,
    friction_payload: Any = None,
    screenshot_path: str | None = None,
    field_map: Any = None,
    min_confidence: float | None = None,
) -> None:
    fs.status = status
    fs.friction_type = friction_type
    fs.friction_payload = _dumps(friction_payload)
    fs.screenshot_path = screenshot_path
    if field_map is not None:
        fs.field_map = _dumps(field_map)
    fs.min_confidence = min_confidence
    fs.ended_at = utcnow_iso()
    record_event(session, "form_session", fs.id, f"status:{status}", {"friction": friction_type})


def paused_sessions(session: Session) -> list[tuple[FormSession, Job]]:
    stmt = (
        select(FormSession, Job)
        .join(Job, Job.id == FormSession.job_id)
        .where(FormSession.status.in_(["paused_friction", "awaiting_review"]))
        .order_by(FormSession.started_at.desc())
    )
    return [(f, j) for f, j in session.execute(stmt).all()]


_PLACEHOLDERS = {"", "select", "select an option", "pick date", "choose", "please select", "start typing"}


def normalize_question(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#]+", " ", (text or "").lower())).strip()


def similar_question(
    rows: list[ScreeningAnswer], question_text: str, threshold: int = 90
) -> ScreeningAnswer | None:
    """The stored question that asks the same thing ("What is your notice period?" / "Notice period?")."""
    from rapidfuzz import fuzz

    q = normalize_question(question_text)
    if not q:
        return None
    best, best_score = None, 0.0
    for row in rows:
        r = normalize_question(row.question_text)
        score = 100.0 if r == q else fuzz.token_sort_ratio(r, q)
        if score > best_score:
            best, best_score = row, score
    return best if best_score >= threshold else None


def add_screening_answer(
    session: Session,
    *,
    job_id: str | None,
    question_text: str,
    matched_pattern: str | None,
    answer: str | None,
    confidence: float | None,
    source: str,
    approved: bool = False,
) -> ScreeningAnswer | None:
    """One row per distinct question: a similar question asked again by another job reuses the existing row.

    Widget placeholders ("Select...", "Pick date...") are not questions you can answer, so they are not stored.
    """
    if normalize_question(question_text) in _PLACEHOLDERS:
        return None
    existing = similar_question(list(session.scalars(select(ScreeningAnswer))), question_text)
    if existing is not None:
        if not existing.approved and answer:
            existing.answer = answer
            existing.confidence = confidence
        return existing
    row = ScreeningAnswer(
        job_id=job_id,
        question_text=question_text,
        matched_pattern=matched_pattern,
        answer=answer,
        confidence=confidence,
        source=source,
        approved=1 if approved else 0,
        created_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


def unapproved_screening_answers(session: Session) -> list[ScreeningAnswer]:
    return list(session.scalars(select(ScreeningAnswer).where(ScreeningAnswer.approved == 0)))


def approved_screening_answers(session: Session) -> list[ScreeningAnswer]:
    return list(
        session.scalars(
            select(ScreeningAnswer)
            .where(ScreeningAnswer.approved == 1)
            .order_by(ScreeningAnswer.question_text)
        )
    )


def merge_duplicate_questions(session: Session) -> int:
    """Collapse rows that ask the same question, keeping the approved one (or the oldest). Returns rows removed."""
    kept: list[ScreeningAnswer] = []
    removed = 0
    rows = sorted(session.scalars(select(ScreeningAnswer)), key=lambda r: (-r.approved, r.id))
    for row in rows:
        twin = similar_question(kept, row.question_text)
        if twin is None:
            kept.append(row)
            continue
        if not twin.answer and row.answer:
            twin.answer = row.answer
        session.delete(row)
        removed += 1
    return removed


def requeue_jobs_waiting_on(session: Session, question_text: str) -> int:
    """Put jobs the copilot stopped on because of this question back in the apply queue."""
    q = normalize_question(question_text)[:60]
    if not q:
        return 0
    stmt = select(Job).where(
        Job.status == JobStatus.READY_FOR_REVIEW.value, Job.archived_reason.like("copilot stopped:%")
    )
    count = 0
    for job in session.scalars(stmt):
        if q in normalize_question(job.archived_reason or ""):
            set_job_status(
                session, job, JobStatus.APPROVED, "re-queued: you answered a question it stopped on"
            )
            count += 1
    return count


def last_answers_sent(session: Session, job_id: str) -> list[dict[str, Any]]:
    """What the copilot entered on the submitted application for this job."""
    fs = session.scalars(
        select(FormSession)
        .where(FormSession.job_id == job_id, FormSession.status == "submitted")
        .order_by(FormSession.started_at.desc())
        .limit(1)
    ).first()
    if fs is None or not fs.field_map:
        return []
    try:
        return list(json.loads(fs.field_map))
    except (ValueError, TypeError):
        return []


def approved_answer_for(session: Session, question_text: str) -> ScreeningAnswer | None:
    return session.scalars(
        select(ScreeningAnswer)
        .where(
            ScreeningAnswer.approved == 1, func.lower(ScreeningAnswer.question_text) == question_text.lower()
        )
        .limit(1)
    ).first()


# --------------------------------------------------------------------------- quota


def peek_quota(session: Session, date: str, platform: str) -> DailyQuota | None:
    """Read the quota row without inserting. Dashboard reads must not take a write lock."""
    return session.get(DailyQuota, {"date": date, "platform": platform})


def get_quota(session: Session, date: str, platform: str, cap: int) -> DailyQuota:
    row = peek_quota(session, date, platform)
    if row is None:
        row = DailyQuota(date=date, platform=platform, submissions=0, cap=cap)
        session.add(row)
        session.flush()
    elif row.cap != cap:
        row.cap = cap
    return row


def increment_quota(session: Session, date: str, platform: str, cap: int) -> DailyQuota:
    row = get_quota(session, date, platform, cap)
    row.submissions += 1
    return row


def quotas_for_date(session: Session, date: str) -> list[DailyQuota]:
    return list(session.scalars(select(DailyQuota).where(DailyQuota.date == date)))


# --------------------------------------------------------------------------- run log


def start_run(session: Session, run_type: str, trigger: str) -> RunLog:
    row = RunLog(run_type=run_type, trigger=trigger, status="running", started_at=utcnow_iso())
    session.add(row)
    session.flush()
    return row


def finish_run(
    session: Session, run: RunLog, status: str, stats: dict[str, Any] | None = None, error: str | None = None
) -> None:
    run.status = status
    run.stats = _dumps(stats)
    run.error = error
    run.ended_at = utcnow_iso()


def recent_runs(session: Session, limit: int = 20) -> list[RunLog]:
    return list(session.scalars(select(RunLog).order_by(RunLog.started_at.desc()).limit(limit)))


def run_stats(run: RunLog) -> dict[str, Any]:
    return _loads(run.stats) or {}
