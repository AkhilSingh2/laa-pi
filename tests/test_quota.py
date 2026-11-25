from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core import repository as repo
from core.db import session_factory
from core.models import AtsType, JobPosting, JobStatus, Source
from core.quota import QuotaManager


def test_quota_caps_and_increment(settings, db_session):
    qm = QuotaManager(settings.quotas, settings.app.timezone)
    st = qm.status(db_session, "linkedin")
    assert st.cap == 20 and st.used == 0 and st.remaining == 20
    for _ in range(20):
        qm.record_submission(db_session, "linkedin")
    assert qm.status(db_session, "linkedin").exhausted
    assert not qm.can_submit(db_session, "linkedin")
    assert qm.can_submit(db_session, "naukri")
    assert not qm.all_exhausted(db_session)


def test_execution_windows(settings):
    qm = QuotaManager(settings.quotas, settings.app.timezone)
    tz = ZoneInfo(settings.app.timezone)
    assert qm.in_execution_window(datetime(2026, 11, 18, 10, 0, tzinfo=tz))  # Wed 10:00
    assert not qm.in_execution_window(datetime(2026, 11, 18, 13, 0, tzinfo=tz))  # lunch gap
    assert not qm.in_execution_window(datetime(2026, 11, 21, 10, 0, tzinfo=tz))  # Saturday
    nxt = qm.next_window_start(datetime(2026, 11, 18, 13, 0, tzinfo=tz))
    assert nxt is not None and nxt.hour == 15 and nxt.day == 18


def test_delay_within_bounds(settings):
    qm = QuotaManager(settings.quotas, settings.app.timezone)
    for _ in range(200):
        d = qm.random_delay_seconds()
        assert 15 <= d <= 45


def test_wal_reader_does_not_block_writer(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    factory = session_factory(tmp_path / "jobs.db")
    reader = factory()
    assert reader.execute(text("PRAGMA journal_mode")).scalar() == "wal"
    reader.execute(text("SELECT 1"))
    writer = factory()
    repo.start_run(writer, "discovery", "manual")
    writer.commit()
    reader.close()
    writer.close()

    holder = factory()
    repo.get_quota(holder, "2026-11-19", "linkedin", 20)  # uncommitted write lock
    blocked = factory()
    blocked.connection().exec_driver_sql("PRAGMA busy_timeout=200")
    with pytest.raises(OperationalError):
        repo.start_run(blocked, "discovery", "manual")
    blocked.rollback()
    holder.commit()
    repo.start_run(blocked, "discovery", "manual")
    blocked.commit()
    holder.close()
    blocked.close()


def test_repository_job_lifecycle(db_session):
    p = JobPosting(
        source=Source.GREENHOUSE,
        external_id="x1",
        company="Acme",
        title="Senior Engineer",
        jd_text="JD text",
        apply_url="https://gh/x1",
        ats_type=AtsType.GREENHOUSE,
    )
    row, created = repo.upsert_job(db_session, p)
    assert created and row.status == "discovered"
    row2, created2 = repo.upsert_job(db_session, p)
    assert not created2 and row2.id == row.id
    repo.save_fit_score(db_session, row, score=60, breakdown={}, rationale="meh", threshold=75)
    assert row.status == JobStatus.ARCHIVED_LOW_FIT.value
    repo.save_fit_score(db_session, row, score=88, breakdown={}, rationale="good", threshold=75)
    assert row.status == JobStatus.SCORED.value
    app = repo.ensure_application(db_session, row, mode="review_mode")
    assert app.platform == "direct_ats"
    repo.set_application_status(db_session, app, "Applied", submitted_by="manual")
    assert app.applied_at is not None
    db_session.commit()
    assert len(repo.all_applications(db_session)) == 1
