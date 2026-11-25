from __future__ import annotations

from core.models import AtsType, JobPosting, Source
from engine import contacts, outreach


def _posting(sample_jd: str) -> JobPosting:
    return JobPosting(
        source=Source.GREENHOUSE,
        external_id="4011",
        company="Acme",
        title="Senior Full Stack Engineer",
        location="Bangalore",
        jd_text=sample_jd,
        apply_url="https://boards.greenhouse.io/acme/jobs/4011",
        ats_type=AtsType.GREENHOUSE,
        raw_metadata={"requisition_id": "R-1"},
    )


def test_contacts_extracted_from_jd(settings, sample_jd):
    p = _posting(sample_jd)
    leads, queries = contacts.discover_contacts(p, settings.contacts, city="Bangalore")
    names = {lead.name for lead in leads}
    assert "Priya Nair" in names
    priya = next(lead for lead in leads if lead.name == "Priya Nair")
    assert priya.contact_type == "hiring_manager" and priya.source == "jd_text"
    assert priya.email_pattern == "{first}.{last}@acme.com"
    assert priya.email_guess == "priya.nair@acme.com"
    assert any(lead.contact_type == "recruiter" for lead in leads)
    assert any(lead.contact_type == "peer" for lead in leads)
    assert "site%3Alinkedin.com" in queries["recruiter"]["google"]
    assert "linkedin.com/search/results/people" in queries["peer"]["linkedin_people"]


def test_email_pattern_inference():
    assert contacts.infer_email_pattern("rmehta@acme.com", "Rahul Mehta") == "{f}{last}@acme.com"
    assert contacts.infer_email_pattern("rahul@acme.com", "Rahul Mehta") == "{first}@acme.com"
    assert contacts.guess_email("{f}{last}@acme.com", "Priya Nair") == "pnair@acme.com"
    assert contacts.infer_email_pattern(None, "X Y") is None


def test_local_outreach_drafts_respect_limits_and_metric(settings, profile, sample_jd):
    p = _posting(sample_jd)
    res = outreach.draft_outreach(
        profile=profile,
        posting=p,
        matched_skills=["React", "FastAPI", "AWS (S3, ECR, RDS, IAM, SQS)"],
        cfg=settings.outreach,
        llm=None,
        recruiter_name="Rahul Mehta",
        peer_name="Engineer at Acme",
    )
    d = res.drafts
    assert len(d.recruiter_connect) < 300
    assert len(d.recruiter_email.split()) < 120
    assert len(d.peer_referral.split()) < 100
    assert d.metric_used in {m for _, m, _ in profile.all_metrics()}
    assert d.metric_used in d.recruiter_email
    assert d.recruiter_connect.startswith("Hi Rahul")
    assert d.peer_referral.startswith("Hi there")
    assert "Job ID R-1" in d.peer_referral
    assert outreach.validate_drafts(d, profile, settings.outreach) == []


def test_validate_drafts_flags_violations(settings, profile):
    bad = outreach.OutreachDrafts(
        recruiter_connect="x" * 300,
        recruiter_email="word " * 130,
        peer_referral="Passionate rockstar!",
        metric_used="made up metric",
    )
    issues = outreach.validate_drafts(bad, profile, settings.outreach)
    assert len(issues) >= 4


def test_auto_send_cannot_be_enabled(settings):
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings.outreach.__class__(auto_send=True)
