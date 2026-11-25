"""Every dashboard page renders without exceptions against a seeded temp data root."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from streamlit.util import calc_hash

from core import repository as repo
from core.db import init_db, session_factory
from core.models import AtsType, JobPosting, JobStatus, Source

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["home", "jobs", "applications", "outreach", "command-center", "scheduler", "settings"]


@pytest.fixture()
def ui_root(tmp_path: Path, monkeypatch, sample_jd: str) -> Path:
    (tmp_path / "data").mkdir()
    shutil.copy(ROOT / "data" / "master_profile.example.json", tmp_path / "data" / "master_profile.json")
    monkeypatch.setenv("LAA_ROOT", str(tmp_path))
    monkeypatch.delenv("BROWSER_VIEW_URL", raising=False)
    st.cache_resource.clear()
    db = tmp_path / "data" / "jobs.db"
    init_db(db)
    with session_factory(db)() as s:
        for i, status in enumerate([JobStatus.READY_FOR_REVIEW, JobStatus.APPROVED, JobStatus.DISCOVERED]):
            job, _ = repo.upsert_job(
                s,
                JobPosting(
                    source=Source.GREENHOUSE,
                    external_id=f"ui-{i}",
                    company=f"Acme {i}",
                    title="Senior Full Stack Engineer",
                    location="Bengaluru",
                    jd_text=sample_jd,
                    apply_url=f"https://boards.greenhouse.io/acme/jobs/{i}",
                    ats_type=AtsType.GREENHOUSE,
                ),
            )
            if status != JobStatus.DISCOVERED:
                repo.save_fit_score(
                    s, job, score=82, breakdown={"criteria": {}}, rationale="good", threshold=75
                )
                repo.set_job_status(s, job, status)
                repo.ensure_application(s, job, mode="review_mode")
        s.commit()
    yield tmp_path
    st.cache_resource.clear()


def _run(page: str, query: dict | None = None) -> AppTest:
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    at._page_hash = calc_hash(page)
    for k, v in (query or {}).items():
        at.query_params[k] = v
    at.run()
    return at


MARKERS = {
    "home": "Needs your action",
    "jobs": "## Jobs",
    "applications": "## Applications",
    "outreach": "## Outreach",
    "command-center": "## Command Center",
    "scheduler": "## Scheduler",
    "settings": "## Settings",
}


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(ui_root, page):
    at = _run(page)
    assert not at.exception, [e.value for e in at.exception]
    text = "\n".join(m.value for m in at.markdown)
    assert MARKERS[page] in text


def test_job_detail_and_approve(ui_root):
    with session_factory(ui_root / "data" / "jobs.db")() as s:
        job = repo.jobs_by_status(s, JobStatus.READY_FOR_REVIEW)[0]
        job_id = job.id
    at = _run("jobs", {"job": job_id})
    assert not at.exception
    approve = next(b for b in at.button if b.label == "Approve")
    approve.click().run()
    assert not at.exception
    with session_factory(ui_root / "data" / "jobs.db")() as s:
        assert repo.get_job(s, job_id).status == JobStatus.APPROVED.value
