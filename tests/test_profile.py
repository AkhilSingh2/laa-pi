from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.profile import MasterProfile


def test_profile_loads_and_core_fields(profile: MasterProfile):
    assert profile.basics.first_name == "Alex"
    assert profile.application_defaults.notice_period_days == 30
    assert "React" in profile.skill_set
    assert profile.experience[0].is_current


def test_aliases_resolve_to_canonical(profile: MasterProfile):
    assert profile.canonical_skill("K8s") == "Kubernetes (Helm)"
    assert profile.canonical_skill("postgres") == "PostgreSQL"
    assert profile.canonical_skill("react") == "React"
    assert profile.canonical_skill("Java") is None


def test_screening_bank_matches(profile: MasterProfile):
    qa = profile.find_screening_answer("What is your notice period?")
    assert qa is not None and "notice_period_days" in qa.answer_guideline
    assert profile.find_screening_answer("Do you own a car?") is None


def test_get_path(profile: MasterProfile):
    assert profile.get_path("basics.links.linkedin").startswith("https://www.linkedin.com")
    with pytest.raises(KeyError):
        profile.get_path("basics.nope")


def test_fact_strings_include_bullets_and_metrics(profile: MasterProfile):
    facts = profile.all_fact_strings()
    assert "45% reduction in page load times" in facts
    assert "Acme Studios" in facts
    assert "" not in facts


def test_alias_to_unknown_skill_rejected(profile: MasterProfile):
    data = profile.model_dump(mode="json")
    data["skills_taxonomy"]["aliases"]["Foo"] = "NotASkill"
    with pytest.raises(ValidationError):
        MasterProfile.model_validate(data)


def test_current_company_mismatch_rejected(profile: MasterProfile):
    data = profile.model_dump(mode="json")
    data["application_defaults"]["current_company"] = "Elsewhere"
    with pytest.raises(ValidationError):
        MasterProfile.model_validate(data)
