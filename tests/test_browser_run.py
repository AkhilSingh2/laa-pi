from __future__ import annotations

import os

from connectors.hooks import CollectHooks
from core import repository as repo
from core.models import JobPosting, JobStatus, Source
from core.schema import Job
from pipeline import browser_run
from pipeline.discovery import DiscoveryPipeline, build_role_patterns, text_reject_reason
from pipeline.runner import request_browser_stop


def test_state_roundtrip_and_stop_flag(settings):
    browser_run.write_state(settings, state="running", pid=os.getpid(), counters={"saved": 2})
    st = browser_run.read_state(settings)
    assert st["state"] == "running" and st["counters"] == {"saved": 2} and st["updated_at"]
    assert browser_run.is_running(settings)  # our own pid is alive
    assert not browser_run.stop_requested(settings)

    assert request_browser_stop(settings)
    assert browser_run.stop_requested(settings)


def test_dead_process_is_not_running(settings):
    browser_run.write_state(settings, state="running", pid=999_999_999)
    assert not browser_run.is_running(settings)
    assert not request_browser_stop(settings)  # nothing to stop


def test_hooks_emit_events_and_default_to_no_op():
    seen: list[tuple[str, str]] = []
    CollectHooks(on_event=lambda k, d: seen.append((k, d))).emit("saved", "x")
    CollectHooks().emit("saved")  # no callback: must not raise
    assert seen == [("saved", "x")]


def test_card_prefilter_matches_full_filter_on_title_company_location(settings):
    patterns = build_role_patterns(settings.search)
    assert (
        text_reject_reason(
            "Senior Software Engineer", "Acme", "Bengaluru, India", False, settings.search, patterns
        )
        is None
    )
    assert (
        text_reject_reason("Sales Manager", "Acme", "Bengaluru", False, settings.search, patterns)
        == "title_excluded"
    )
    assert (
        text_reject_reason("Data Analyst", "Acme", "Bengaluru", False, settings.search, patterns)
        == "role_mismatch"
    )
    assert (
        text_reject_reason("Software Engineer", "Acme", "Austin, TX", False, settings.search, patterns)
        == "location"
    )
    assert text_reject_reason("Software Engineer", "Acme", "India", False, settings.search, patterns) is None


def test_process_pending_scores_rows_saved_by_browser(settings, db_session, profile, sample_jd):
    posting = JobPosting(
        source=Source.NAUKRI,
        external_id="n-123",
        company="Acme Labs",
        title="Senior Full Stack Engineer",
        location="Bengaluru",
        jd_text=sample_jd,
        apply_url="https://www.naukri.com/job-listings-123",
        ats_type="naukri",
    )
    row, created = repo.upsert_job(db_session, posting)
    db_session.commit()
    assert created and row.status == JobStatus.DISCOVERED.value

    pipeline = DiscoveryPipeline(settings, llm=None, profile=profile)
    stats = pipeline.process_pending(db_session)

    db_session.refresh(row)
    assert stats.scored >= 1
    assert row.fit_score is not None and row.status != JobStatus.DISCOVERED.value
    # nothing pending afterwards: a second call is a no-op
    again = pipeline.process_pending(db_session)
    assert again.scored == 0
    assert db_session.query(Job).count() == 1


def test_pause_is_interrupted_by_stop_request():
    import time

    hooks = CollectHooks(should_stop=lambda: True)
    start = time.monotonic()
    assert hooks.pause(30) is True
    assert time.monotonic() - start < 2  # returned at the first check, not after 30 seconds
    assert CollectHooks().pause(0.2) is False
