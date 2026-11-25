"""SQLAlchemy ORM schema for the local SQLite database (data/jobs.db).

All timestamps are stored as ISO-8601 text (UTC) for SQLite portability.
JSON columns are stored as TEXT and (de)serialized by the repository layer.
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, PrimaryKeyConstraint, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Company(Base):
    """Company registry / ATS cache (seeded from config/target_companies.csv)."""

    __tablename__ = "companies"

    name_normalized: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    ats_type: Mapped[str | None] = mapped_column(Text)  # greenhouse|lever|ashby|workday|other
    board_token: Mapped[str | None] = mapped_column(Text)
    careers_url: Mapped[str | None] = mapped_column(Text)
    email_pattern: Mapped[str | None] = mapped_column(Text)  # "{first}.{last}@acme.com" (probable)
    priority: Mapped[int] = mapped_column(Integer, default=3)
    last_fetched_at: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    """Every raw posting ingested (one row per source posting)."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint(
            "source", "company_normalized", "external_id", name="uq_jobs_source_company_external"
        ),
        Index("ix_jobs_dedup", "company_normalized", "title_normalized"),
        Index("ix_jobs_status_score", "status", "fit_score"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # sha1(source:company_normalized:external_id)
    source: Mapped[str] = mapped_column(Text, nullable=False)  # greenhouse|lever|ashby|linkedin|naukri
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    company: Mapped[str] = mapped_column(Text, nullable=False)
    company_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str | None] = mapped_column(Text)
    remote_type: Mapped[str | None] = mapped_column(Text)  # remote|hybrid|onsite|unknown
    jd_text: Mapped[str] = mapped_column(Text, nullable=False)
    jd_hash: Mapped[str] = mapped_column(Text, nullable=False)
    apply_url: Mapped[str] = mapped_column(Text, nullable=False)
    ats_type: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[str] = mapped_column(Text, nullable=False)
    raw_metadata: Mapped[str | None] = mapped_column(Text)  # JSON
    canonical_job_id: Mapped[str | None] = mapped_column(Text, ForeignKey("jobs.id"))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="discovered")
    fit_score: Mapped[int | None] = mapped_column(Integer)
    fit_breakdown: Mapped[str | None] = mapped_column(Text)  # JSON
    fit_rationale: Mapped[str | None] = mapped_column(Text)
    archived_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(Text, ForeignKey("jobs.id"), nullable=False)
    summary_variant: Mapped[str] = mapped_column(Text, nullable=False)
    resume_json: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_path: Mapped[str] = mapped_column(Text, nullable=False)
    ats_keyword_score: Mapped[float | None] = mapped_column(Float)
    matched_keywords: Mapped[str | None] = mapped_column(Text)  # JSON list
    missing_keywords: Mapped[str | None] = mapped_column(Text)  # JSON list
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class Application(Base):
    """CRM row, one per canonical job."""

    __tablename__ = "applications"
    __table_args__ = (Index("ix_applications_status", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(Text, ForeignKey("jobs.id"), nullable=False, unique=True)
    platform: Mapped[str] = mapped_column(Text, nullable=False)  # linkedin|naukri|direct_ats
    mode: Mapped[str] = mapped_column(Text, nullable=False)  # review_mode|auto_submit_ats
    status: Mapped[str] = mapped_column(Text, nullable=False, default="Discovered")
    resume_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("resumes.id"))
    applied_at: Mapped[str | None] = mapped_column(Text)
    submitted_by: Mapped[str | None] = mapped_column(Text)  # auto|manual
    notes: Mapped[str | None] = mapped_column(Text)
    follow_up_at: Mapped[str | None] = mapped_column(Text)
    last_activity_at: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(Text, ForeignKey("jobs.id"), nullable=False)
    company_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    contact_type: Mapped[str] = mapped_column(Text, nullable=False)  # recruiter|peer|hiring_manager
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str | None] = mapped_column(Text)
    profile_url: Mapped[str | None] = mapped_column(Text)
    email_guess: Mapped[str | None] = mapped_column(Text)
    email_pattern: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False)  # jd_text|linkedin_search|xray_query
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class OutreachMessage(Base):
    __tablename__ = "outreach_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(Text, ForeignKey("jobs.id"), nullable=False)
    contact_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("contacts.id"))
    channel: Mapped[str] = mapped_column(Text, nullable=False)  # linkedin_connect|email|inmail|peer_referral
    body: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_used: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="drafted"
    )  # drafted|sent|replied|skipped
    sent_at: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class FormSession(Base):
    """One per copilot attempt."""

    __tablename__ = "form_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(Text, ForeignKey("jobs.id"), nullable=False)
    application_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("applications.id"))
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    # running|awaiting_review|paused_friction|submitted|failed|abandoned
    status: Mapped[str] = mapped_column(Text, nullable=False)
    friction_type: Mapped[str | None] = mapped_column(Text)
    friction_payload: Mapped[str | None] = mapped_column(Text)  # JSON
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    field_map: Mapped[str | None] = mapped_column(Text)  # JSON
    min_confidence: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    ended_at: Mapped[str | None] = mapped_column(Text)


class ScreeningAnswer(Base):
    """Learned Q&A, surfaced for approval in the UI."""

    __tablename__ = "screening_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(Text, ForeignKey("jobs.id"))
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    matched_pattern: Mapped[str | None] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(Text, nullable=False)  # qa_bank|application_defaults|user
    approved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class DailyQuota(Base):
    __tablename__ = "daily_quota"
    __table_args__ = (PrimaryKeyConstraint("date", "platform"),)

    date: Mapped[str] = mapped_column(Text, nullable=False)  # YYYY-MM-DD in app.timezone
    platform: Mapped[str] = mapped_column(Text, nullable=False)  # linkedin|naukri|direct_ats
    submissions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cap: Mapped[int] = mapped_column(Integer, nullable=False)


class RunLog(Base):
    __tablename__ = "run_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_type: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)  # manual|scheduled
    status: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # running|success|partial|failed|stopped_cap_reached
    stats: Mapped[str | None] = mapped_column(Text)  # JSON
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    ended_at: Mapped[str | None] = mapped_column(Text)


class Event(Base):
    """Append-only audit trail."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
