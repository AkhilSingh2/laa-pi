from __future__ import annotations

import httpx
import respx

from core import repository as repo
from core.models import JobStatus
from pipeline.discovery import DiscoveryPipeline
from tests.conftest import load_fixture


def _mock_boards(settings):
    gh = load_fixture("greenhouse_jobs.json")
    lever = load_fixture("lever_postings.json")
    ashby = load_fixture("ashby_jobs.json")
    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/v1/boards/.*").mock(
        return_value=httpx.Response(200, json=gh)
    )
    respx.get(url__regex=r"https://api\.lever\.co/v0/postings/.*").mock(
        return_value=httpx.Response(200, json=lever)
    )
    respx.get(url__regex=r"https://api\.ashbyhq\.com/posting-api/job-board/.*").mock(
        return_value=httpx.Response(200, json=ashby)
    )


@respx.mock
def test_discovery_end_to_end_without_llm(settings, db_session, profile):
    _mock_boards(settings)
    # fixture postings carry fixed old dates; widen the freshness window so this test exercises the pipeline
    settings.search.posted_within_days = 36500
    pipeline = DiscoveryPipeline(settings, llm=None, profile=profile)
    stats = pipeline.run(db_session)

    assert stats.fetched > 0 and stats.fetched == stats.kept + stats.filtered
    assert stats.kept > 0 and stats.new == stats.kept
    # same fixture served for every Greenhouse company -> identical JDs collapse per (company,title) only; no cross-company dedup
    assert stats.duplicates == 0
    assert stats.scored == stats.new
    assert stats.archived + stats.queued == stats.scored
    assert stats.queued >= 1
    assert stats.resumes == stats.queued and stats.outreach_drafts == 3 * stats.queued
    assert not stats.errors, stats.errors

    ready = repo.jobs_by_status(db_session, JobStatus.READY_FOR_REVIEW)
    assert ready and all(j.fit_score >= settings.matching.fit_threshold for j in ready)
    job = ready[0]
    assert repo.latest_resume(db_session, job.id).pdf_path.endswith(".pdf")
    assert len(repo.contacts_for_job(db_session, job.id)) >= 2
    assert {m.channel for m in repo.outreach_for_job(db_session, job.id)} == {
        "linkedin_connect",
        "email",
        "peer_referral",
    }
    app = repo.application_for_job(db_session, job.id)
    assert app is not None and app.status == "Ready for Review"
    assert app.mode == settings.copilot.default_mode_by_platform["direct_ats"]

    # idempotent: a second run adds nothing
    stats2 = pipeline.run(db_session)
    assert stats2.new == 0 and stats2.scored == 0 and stats2.resumes == 0
