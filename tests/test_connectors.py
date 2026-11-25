from __future__ import annotations

import httpx
import respx

from connectors import build_api_connectors
from connectors.ashby import AshbyConnector
from connectors.base import TargetCompany, html_to_text, load_target_companies
from connectors.greenhouse import GreenhouseConnector
from connectors.lever import LeverConnector
from core.models import AtsType, RemoteType, Source
from tests.conftest import load_fixture

ACME = TargetCompany(company="Acme", ats_type="", board_token="acme")


def test_html_to_text_strips_and_unescapes():
    assert html_to_text("&lt;p&gt;Hello &amp; <b>world</b>&lt;/p&gt;") == "Hello & world"
    assert "\n" in html_to_text("<li>a</li><li>b</li>")


@respx.mock
def test_greenhouse_connector_maps_fields():
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
        return_value=httpx.Response(200, json=load_fixture("greenhouse_jobs.json"))
    )
    jobs = GreenhouseConnector([ACME]).fetch()
    assert len(jobs) == 2
    j = jobs[0]
    assert j.source == Source.GREENHOUSE and j.ats_type == AtsType.GREENHOUSE
    assert j.external_id == "4011" and j.company == "Acme"
    assert j.remote_type == RemoteType.HYBRID
    assert "FastAPI" in j.jd_text and "<" not in j.jd_text
    assert j.posted_at is not None and j.posted_at.year == 2026
    assert j.apply_url.endswith("/4011")
    assert j.id and len(j.id) == 40


@respx.mock
def test_lever_connector_joins_lists_and_workplace_type():
    respx.get("https://api.lever.co/v0/postings/acme?mode=json").mock(
        return_value=httpx.Response(200, json=load_fixture("lever_postings.json"))
    )
    jobs = LeverConnector([ACME]).fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.remote_type == RemoteType.HYBRID
    assert j.location == "Bengaluru"
    assert "Requirements" in j.jd_text and "Terraform" in j.jd_text
    assert j.apply_url.endswith("/apply")
    assert j.posted_at is not None


@respx.mock
def test_ashby_connector_skips_unlisted_and_flags_remote():
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true").mock(
        return_value=httpx.Response(200, json=load_fixture("ashby_jobs.json"))
    )
    jobs = AshbyConnector([ACME]).fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.remote_type == RemoteType.REMOTE
    assert "Bangalore" in (j.location or "")
    assert j.ats_type == AtsType.ASHBY


@respx.mock
def test_board_failure_is_isolated():
    respx.get("https://boards-api.greenhouse.io/v1/boards/bad/jobs?content=true").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
        return_value=httpx.Response(200, json=load_fixture("greenhouse_jobs.json"))
    )
    c = GreenhouseConnector([TargetCompany("Bad", "greenhouse", "bad"), ACME])
    jobs = c.fetch()
    assert len(jobs) == 2
    assert "Bad" in c.errors


def test_load_target_companies_filters_by_ats(settings):
    rows = load_target_companies(settings.path("config/target_companies.csv"), ats_type="lever")
    assert rows and all(r.ats_type == "lever" for r in rows)
    connectors = build_api_connectors(settings)
    assert {type(c).__name__ for c in connectors} == {
        "GreenhouseConnector",
        "LeverConnector",
        "AshbyConnector",
    }
