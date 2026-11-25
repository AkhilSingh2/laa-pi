from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel

from core.models import (
    AtsType,
    FitBreakdownItem,
    FitScore,
    JobPosting,
    Source,
    normalize_company,
    normalize_title,
)
from engine import matcher


def _post(
    source: Source, ext: str, company: str, title: str, jd: str, ats: AtsType, url: str, posted=None
) -> JobPosting:
    return JobPosting(
        source=source,
        external_id=ext,
        company=company,
        title=title,
        jd_text=jd,
        apply_url=url,
        ats_type=ats,
        posted_at=posted,
    )


def test_normalization():
    assert normalize_company("Acme Technologies Pvt. Ltd.") == normalize_company("ACME")
    assert normalize_title("Sr. Full-Stack Engineer (Remote)") == normalize_title("Senior Fullstack Engineer")


def test_dedupe_prefers_direct_ats_over_linkedin_over_naukri(settings, sample_jd):
    gh = _post(
        Source.GREENHOUSE,
        "1",
        "Acme Inc",
        "Senior Full Stack Engineer",
        sample_jd,
        AtsType.GREENHOUSE,
        "https://gh/1",
    )
    li = _post(
        Source.LINKEDIN,
        "2",
        "Acme",
        "Sr. Full-Stack Engineer",
        sample_jd + "\nApply via LinkedIn.",
        AtsType.EXTERNAL,
        "https://li/2",
    )
    nk = _post(
        Source.NAUKRI, "3", "ACME", "Senior Fullstack Engineer", sample_jd, AtsType.NAUKRI, "https://nk/3"
    )
    other = _post(
        Source.LINKEDIN,
        "4",
        "Acme",
        "Senior Full Stack Engineer",
        "Completely different Java Spring Kafka role, 10 years, banking domain, Mumbai.",
        AtsType.EXTERNAL,
        "https://li/4",
    )

    res = matcher.dedupe([nk, li, gh, other], settings.matching)
    ids = {p.id for p in res.canonical}
    assert gh.id in ids
    assert res.duplicates[li.id] == gh.id and res.duplicates[nk.id] == gh.id
    assert other.id in ids  # same key, dissimilar JD -> stays separate
    assert len(res.canonical) == 2


def test_dedupe_tie_breaks_on_earliest_posting(settings, sample_jd):
    a = _post(
        Source.LINKEDIN,
        "a",
        "Acme",
        "Senior Full Stack Engineer",
        sample_jd,
        AtsType.EXTERNAL,
        "https://li/a",
        datetime(2026, 11, 2, tzinfo=UTC),
    )
    b = _post(
        Source.LINKEDIN,
        "b",
        "Acme",
        "Senior Full Stack Engineer",
        sample_jd,
        AtsType.EXTERNAL,
        "https://li/b",
        datetime(2026, 11, 1, tzinfo=UTC),
    )
    res = matcher.dedupe([a, b], settings.matching)
    assert res.canonical[0].id == b.id


def test_local_evidence_finds_only_profile_skills(settings, profile, sample_jd):
    p = _post(
        Source.GREENHOUSE,
        "1",
        "Acme",
        "Senior Full Stack Engineer",
        sample_jd,
        AtsType.GREENHOUSE,
        "https://gh/1",
    )
    p.location = "Bangalore"
    ev = matcher.local_evidence(p, profile, settings.matching)
    assert {"React", "Next.js", "TypeScript", "Python", "FastAPI", "PostgreSQL", "Kubernetes (Helm)"} <= set(
        ev.matched_skills
    )
    assert all(s in profile.skill_set for s in ev.matched_skills)
    assert "django" in ev.missing_skills
    assert ev.jd_years_min == 5 and ev.seniority_signal == "senior"
    assert ev.location_match and ev.summary_variant in ("fullstack", "ai_platform", "frontend", "default")
    assert ev.heuristic_score >= 75


def test_heuristic_penalizes_mismatch(settings, profile):
    p = _post(
        Source.GREENHOUSE,
        "2",
        "Bank",
        "Junior Java Developer",
        "Entry level Java Spring Boot developer, 0-1 years, Mumbai onsite.",
        AtsType.GREENHOUSE,
        "https://gh/2",
    )
    p.location = "Mumbai"
    ev = matcher.local_evidence(p, profile, settings.matching)
    assert ev.heuristic_score < 50


class _FakeLLM:
    available = True

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    def structured(
        self, *, system: str, user: str, schema: type[BaseModel], tool_name: str, description: str
    ):
        self.calls += 1
        assert "LOCAL_EVIDENCE" in user and "RUBRIC" in user
        return schema.model_validate(self.payload)


def test_score_job_uses_llm_and_strips_hallucinated_skills(settings, profile, sample_jd):
    p = _post(
        Source.GREENHOUSE,
        "1",
        "Acme",
        "Senior Full Stack Engineer",
        sample_jd,
        AtsType.GREENHOUSE,
        "https://gh/1",
    )
    weights = settings.matching.rubric.model_dump()
    payload = {
        "score": 99,  # inconsistent with breakdown; must be recomputed
        "breakdown": {k: {"score": w - 1, "max": w, "evidence": "ok"} for k, w in weights.items()},
        "rationale": "Strong overlap.",
        "matched_skills": ["React", "Java", "Postgres"],  # Java not in profile; Postgres alias -> PostgreSQL
        "missing_skills": ["Django"],
        "summary_variant": "nonexistent",
    }
    llm = _FakeLLM(payload)
    fit = matcher.score_job(p, profile, settings.matching, llm)  # type: ignore[arg-type]
    assert llm.calls == 1
    assert fit.score == 100 - len(weights)
    assert (
        "Java" not in fit.matched_skills
        and "PostgreSQL" in fit.matched_skills
        and "React" in fit.matched_skills
    )
    assert fit.summary_variant in profile.basics.summaries.variants or fit.summary_variant == "default"


def test_score_job_without_llm_is_heuristic(settings, profile, sample_jd):
    p = _post(
        Source.GREENHOUSE,
        "1",
        "Acme",
        "Senior Full Stack Engineer",
        sample_jd,
        AtsType.GREENHOUSE,
        "https://gh/1",
    )
    fit = matcher.score_job(p, profile, settings.matching, None)
    assert isinstance(fit, FitScore)
    assert "Heuristic" in fit.rationale
    assert set(fit.breakdown) == set(settings.matching.rubric.model_dump())
    assert all(isinstance(v, FitBreakdownItem) for v in fit.breakdown.values())
