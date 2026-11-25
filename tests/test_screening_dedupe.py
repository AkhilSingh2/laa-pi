from __future__ import annotations

from copilot.field_mapper import FieldMapper, FormField
from core import repository as repo
from core.models import AtsType, JobPosting, JobStatus, Source


def _ask(session, question: str, answer: str | None = None):
    return repo.add_screening_answer(
        session,
        job_id=None,
        question_text=question,
        matched_pattern=None,
        answer=answer,
        confidence=0.0,
        source="user",
    )


def test_similar_questions_share_one_row(db_session):
    a = _ask(db_session, "What is your current CTC?")
    b = _ask(db_session, "what is your current ctc")
    c = _ask(db_session, "What is your current  CTC ?*")
    other = _ask(db_session, "What is your expected CTC?")
    assert a.id == b.id == c.id
    assert other.id != a.id
    assert len(repo.unapproved_screening_answers(db_session)) == 2


def test_approved_answer_is_not_overwritten(db_session):
    row = _ask(db_session, "Are you available for a face to face interview?", "Yes")
    row.approved = 1
    _ask(db_session, "Are you available for a face-to-face interview", "No")
    assert row.answer == "Yes"


def test_merge_existing_duplicates(db_session):
    from core.schema import ScreeningAnswer

    for q in ("Notice period?", "notice period", "Notice Period"):
        db_session.add(
            ScreeningAnswer(question_text=q, source="user", approved=0, created_at="2026-11-20T00:00:00Z")
        )
    db_session.flush()
    assert repo.merge_duplicate_questions(db_session) == 2
    db_session.flush()
    assert len(repo.unapproved_screening_answers(db_session)) == 1


def test_reworded_question_uses_saved_answer(profile, settings):
    mapper = FieldMapper(
        profile,
        settings.copilot.profile_field_map,
        approved_answers={"Are you available for a F2F interview on 21st?": "Yes"},
    )
    m = mapper.map_fields(
        [FormField("q", "text", "Are you available for F2F interview on the 21st", required=True)]
    )[0]
    assert m.value == "Yes" and m.value_source == "approved_answer"


def test_answering_requeues_waiting_jobs(db_session):
    job, _ = repo.upsert_job(
        db_session,
        JobPosting(
            source=Source.GREENHOUSE,
            external_id="q1",
            company="Acme",
            title="Engineer",
            jd_text="JD",
            apply_url="https://gh/q1",
            ats_type=AtsType.GREENHOUSE,
        ),
    )
    repo.set_job_status(
        db_session,
        job,
        JobStatus.READY_FOR_REVIEW,
        "copilot stopped: unresolved required fields: How many years of Vite experience do you have?",
    )
    assert repo.requeue_jobs_waiting_on(db_session, "How many years of Vite experience do you have?") == 1
    assert job.status == JobStatus.APPROVED.value
