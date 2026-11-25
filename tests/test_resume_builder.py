from __future__ import annotations

import pytest
from pypdf import PdfReader

from core.models import AtsType, JobPosting, Source
from engine import matcher
from engine import resume_builder as rb


def _posting(sample_jd: str) -> JobPosting:
    return JobPosting(
        source=Source.GREENHOUSE,
        external_id="4011",
        company="Acme Inc.",
        title="Senior Full Stack Engineer",
        location="Bangalore",
        jd_text=sample_jd,
        apply_url="https://gh/4011",
        ats_type=AtsType.GREENHOUSE,
        raw_metadata={"requisition_id": "R-1"},
    )


def test_local_plan_and_json_are_profile_bound(settings, profile, sample_jd):
    p = _posting(sample_jd)
    ev = matcher.local_evidence(p, profile, settings.matching)
    plan = rb._local_plan(profile, p, settings.resume, ev)
    resume = rb.build_resume_json(profile, plan, settings.resume)
    rb.verify_against_profile(resume, profile)  # must not raise
    assert resume["experience"][0]["company"] == "Acme Studios"
    assert len(resume["experience"][0]["bullets"]) == settings.resume.bullets_per_role.most_recent
    assert len(resume["experience"][1]["bullets"]) <= settings.resume.bullets_per_role.previous
    assert resume["summary_variant"] in ("fullstack", "ai_platform", "frontend", "default")
    all_skills = [s for g in resume["skills"] for s in g["skills"]]
    assert len(all_skills) <= settings.resume.max_skills_listed
    assert all_skills[0] in ev.matched_skills  # JD-relevant skills float to the top


def test_verify_rejects_fabricated_bullet(settings, profile, sample_jd):
    p = _posting(sample_jd)
    plan = rb._local_plan(profile, p, settings.resume, None)
    resume = rb.build_resume_json(profile, plan, settings.resume)
    resume["experience"][0]["bullets"].append("Led a team of 50 engineers at Google.")
    with pytest.raises(rb.HallucinationError):
        rb.verify_against_profile(resume, profile)


def test_plan_with_unknown_skill_and_bad_indices_is_sanitized(settings, profile, sample_jd):
    plan = rb.TailoringPlan(
        summary_variant="bogus",
        skill_order=["Java", "React", "React"],
        roles=[rb.RolePlan(company="Acme Studios", bullet_indices=[99, 4, 4, 0])],
        headline_skills=["Java"],
    )
    resume = rb.build_resume_json(profile, plan, settings.resume)
    rb.verify_against_profile(resume, profile)
    assert resume["summary_variant"] == "default"
    assert resume["experience"][0]["bullet_indices"] == [4, 0]
    assert "Java" not in resume["header"]["headline"]


def test_ats_keyword_score(profile, sample_jd):
    score, matched, missing = rb.ats_keyword_score(
        profile, sample_jd, "React TypeScript Next.js Python FastAPI PostgreSQL AWS Kubernetes"
    )
    assert 0 < score <= 100 and "React" in matched
    assert any(s in missing for s in ("Celery", "SQS", "Storybook", "Terraform", "Docker"))
    assert rb.ats_keyword_score(profile, "no skills here", "x")[0] == 100.0


def test_build_resume_writes_single_page_pdf(settings, profile, sample_jd):
    p = _posting(sample_jd)
    ev = matcher.local_evidence(p, profile, settings.matching)
    out = rb.build_resume(
        profile=profile,
        posting=p,
        cfg=settings.resume,
        root=settings.root,
        resumes_dir=settings.path(settings.app.resumes_dir),
        llm=None,
        evidence=ev,
    )
    assert out.pdf_path.exists() and out.pdf_path.name.startswith("Acme_Inc_Senior_Full_Stack_Engineer_")
    reader = PdfReader(str(out.pdf_path))
    assert len(reader.pages) == 1 and out.page_count == 1
    text = reader.pages[0].extract_text()
    assert "Alex Morgan" in text and "Acme Studios" in text and "Example Institute" in text
    assert out.ats_keyword_score >= 70
    assert "<" not in out.resume_json["summary"]
