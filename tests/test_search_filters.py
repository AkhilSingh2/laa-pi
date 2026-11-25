from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.models import JobPosting, Source
from core.settings import Settings
from pipeline.discovery import _reject_reason, _role_patterns

NOW = datetime(2026, 11, 19, tzinfo=UTC)


def _posting(**kw) -> JobPosting:
    base = dict(
        source=Source.GREENHOUSE,
        external_id="1",
        company="Acme",
        title="Senior Full-Stack Engineer",
        location="Bangalore, India",
        jd_text="We need 5+ years of experience with React and Python.",
        apply_url="https://example.com/job/1",
        ats_type="greenhouse",
        posted_at=NOW - timedelta(days=1),
    )
    base.update(kw)
    return JobPosting(**base)


def _reason(p: JobPosting, settings: Settings) -> str | None:
    return _reject_reason(p, settings.search, _role_patterns(settings.search), NOW)


def test_in_scope_posting_is_kept(settings):
    assert _reason(_posting(), settings) is None


def test_role_family_mismatch_rejected(settings):
    assert _reason(_posting(title="Backend Engineer, Payments"), settings) == "role_mismatch"
    assert _reason(_posting(title="Data Scientist"), settings) == "role_mismatch"


def test_role_family_accepts_seniority_and_suffix_variants(settings):
    assert _reason(_posting(title="Staff Software Engineer, Payments"), settings) is None
    assert _reason(_posting(title="Full Stack Developer II"), settings) is None
    # "software engineering manager" must not match "software engineer"
    assert _reason(_posting(title="Software Engineering Director"), settings) == "title_excluded"


def test_location_must_be_india_or_preferred_city(settings):
    assert _reason(_posting(location="San Francisco, CA", remote_type="onsite"), settings) == "location"
    assert _reason(_posting(location="Remote - US", remote_type="remote"), settings) == "location"
    assert _reason(_posting(location="Remote", remote_type="remote"), settings) is None
    assert _reason(_posting(location="Remote - India", remote_type="remote"), settings) is None
    assert _reason(_posting(location="Hyderabad, Telangana", remote_type="hybrid"), settings) is None
    assert _reason(_posting(location=None), settings) is None  # unknown location is not dropped


def test_experience_window(settings):
    too_senior = _posting(jd_text="Requires 10+ years of experience in distributed systems.")
    too_junior = _posting(jd_text="Looking for 1-3 years of experience with React.")
    in_range = _posting(jd_text="Requires 4-7 years of experience with React.")
    no_years = _posting(jd_text="Build great things with React.")
    assert _reason(too_senior, settings) == "experience_too_senior"
    assert _reason(too_junior, settings) == "experience_too_junior"
    assert _reason(in_range, settings) is None
    assert _reason(no_years, settings) is None


def test_stale_postings_rejected_when_date_known(settings):
    assert _reason(_posting(posted_at=NOW - timedelta(days=30)), settings) == "stale"
    assert _reason(_posting(posted_at=None), settings) is None


def test_exclusions_still_apply(settings):
    assert _reason(_posting(company="Acme Studios"), settings) == "company_excluded"
    assert _reason(_posting(title="Senior Engineer Intern"), settings) == "title_excluded"


def test_walk_in_events_are_skipped(settings):
    assert (
        _reason(_posting(title="Walkin Drive - Full stack Developer on 21 Saturday"), settings) == "walk_in"
    )
    assert _reason(_posting(title="Walk-In Interview: Senior Software Engineer"), settings) == "walk_in"
    jd = "Walk-in interview on 21 Nov at our Bengaluru office. Venue: Manyata Tech Park. 5+ years of experience."
    assert _reason(_posting(jd_text=jd), settings) == "walk_in"
    # "walking" or unrelated text must not trigger it
    assert (
        _reason(_posting(jd_text="Build tools for walking-route planning. 5+ years of experience."), settings)
        is None
    )
