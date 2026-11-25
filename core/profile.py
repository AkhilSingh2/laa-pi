"""Pydantic validator and lookup helpers for data/master_profile.json.

The profile is the single source of truth for every fact the system may state.
Nothing in this module mutates the file.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class Location(BaseModel):
    city: str
    state: str = ""
    country: str
    postal_code: str = ""


class Links(BaseModel):
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""


class Summaries(BaseModel):
    default: str
    variants: dict[str, str] = Field(default_factory=dict)

    def get(self, variant: str) -> str:
        return self.variants.get(variant, self.default)


class Basics(BaseModel):
    full_name: str
    first_name: str
    last_name: str
    headline: str
    email: EmailStr
    phone: str
    location: Location
    links: Links = Field(default_factory=Links)
    summaries: Summaries


class WorkAuthorization(BaseModel):
    india: str = ""
    requires_sponsorship: bool = False


class ApplicationDefaults(BaseModel):
    total_experience_years: float
    relevant_experience_years: float
    current_company: str
    current_title: str
    current_ctc_lpa: float = 0.0
    expected_ctc_lpa: float = 0.0
    currency: str = "INR"
    notice_period_days: int
    is_serving_notice: bool = False
    last_working_day: str | None = None
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    work_mode_preferences: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    willing_to_relocate: bool = False
    gender: str = ""
    veteran_status: str = ""
    disability_status: str = ""


class SkillsTaxonomy(BaseModel):
    aliases: dict[str, str] = Field(default_factory=dict)
    model_config = {"extra": "allow"}

    @property
    def categories(self) -> dict[str, list[str]]:
        extra = self.model_extra or {}
        return {k: list(v) for k, v in extra.items() if isinstance(v, list)}

    @property
    def all_skills(self) -> list[str]:
        seen: list[str] = []
        for skills in self.categories.values():
            for s in skills:
                if s not in seen:
                    seen.append(s)
        return seen

    @model_validator(mode="after")
    def _aliases_point_to_real_skills(self) -> SkillsTaxonomy:
        known = set(self.all_skills)
        bad = [a for a, target in self.aliases.items() if target not in known]
        if bad:
            raise ValueError(f"skills_taxonomy.aliases targets not present in taxonomy: {bad}")
        return self


class Bullet(BaseModel):
    text: str
    keywords: list[str] = Field(default_factory=list)
    impact_metric: str = ""


class Experience(BaseModel):
    company: str
    title: str
    location: str = ""
    start_date: str
    end_date: str
    domain_tags: list[str] = Field(default_factory=list)
    bullets: list[Bullet]

    @field_validator("start_date")
    @classmethod
    def _ym(cls, v: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}", v):
            raise ValueError(f"start_date must be YYYY-MM, got {v!r}")
        return v

    @field_validator("end_date")
    @classmethod
    def _ym_or_present(cls, v: str) -> str:
        if v != "Present" and not re.fullmatch(r"\d{4}-\d{2}", v):
            raise ValueError(f"end_date must be YYYY-MM or 'Present', got {v!r}")
        return v

    @property
    def is_current(self) -> bool:
        return self.end_date == "Present"


class Project(BaseModel):
    name: str
    description: str = ""
    url: str = ""
    tech: list[str] = Field(default_factory=list)


class Education(BaseModel):
    institution: str
    location: str = ""
    degree: str
    start_date: str = ""
    end_date: str = ""
    gpa: str = ""


class Certification(BaseModel):
    name: str
    issuer: str = ""
    year: str = ""


class ScreeningQA(BaseModel):
    question_pattern: str
    answer_guideline: str

    @field_validator("question_pattern")
    @classmethod
    def _valid_regex(cls, v: str) -> str:
        re.compile(v, re.IGNORECASE)
        return v

    def matches(self, question: str) -> bool:
        return re.search(self.question_pattern, question, re.IGNORECASE) is not None


class MasterProfile(BaseModel):
    basics: Basics
    application_defaults: ApplicationDefaults
    skills_taxonomy: SkillsTaxonomy
    experience: list[Experience]
    projects: list[Project] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    screening_qa_bank: list[ScreeningQA] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistency(self) -> MasterProfile:
        if not self.experience:
            raise ValueError("experience must contain at least one role")
        current = [e for e in self.experience if e.is_current]
        if current and current[0].company != self.application_defaults.current_company:
            raise ValueError(
                "application_defaults.current_company does not match the 'Present' experience entry"
            )
        return self

    # ------------------------------------------------------------------ lookups

    @property
    def skill_set(self) -> set[str]:
        return set(self.skills_taxonomy.all_skills)

    def canonical_skill(self, term: str) -> str | None:
        """Return the canonical profile skill for `term` (exact or alias, case-insensitive), else None."""
        t = term.strip()
        if not t:
            return None
        lower = {s.lower(): s for s in self.skills_taxonomy.all_skills}
        if t.lower() in lower:
            return lower[t.lower()]
        aliases = {a.lower(): v for a, v in self.skills_taxonomy.aliases.items()}
        return aliases.get(t.lower())

    def all_skill_terms(self) -> set[str]:
        """Canonical skills + aliases, for keyword matching."""
        return set(self.skills_taxonomy.all_skills) | set(self.skills_taxonomy.aliases)

    def all_fact_strings(self) -> set[str]:
        """Every literal fact string that may appear in a tailored resume."""
        facts: set[str] = set(self.all_skill_terms())
        b = self.basics
        facts.update(
            {
                b.full_name,
                b.headline,
                str(b.email),
                b.phone,
                b.location.city,
                b.location.state,
                b.location.country,
                b.summaries.default,
                *b.summaries.variants.values(),
                b.links.linkedin,
                b.links.github,
                b.links.portfolio,
            }
        )
        for e in self.experience:
            facts.update({e.company, e.title, e.location, e.start_date, e.end_date})
            for bl in e.bullets:
                facts.add(bl.text)
                facts.add(bl.impact_metric)
                facts.update(bl.keywords)
        for ed in self.education:
            facts.update({ed.institution, ed.degree, ed.location, ed.start_date, ed.end_date, ed.gpa})
        for p in self.projects:
            facts.update({p.name, p.description, *p.tech})
        for c in self.certifications:
            facts.update({c.name, c.issuer})
        facts.discard("")
        return facts

    def all_metrics(self) -> list[tuple[str, str, str]]:
        """(company, impact_metric, bullet_text) for every bullet with a metric."""
        return [
            (e.company, b.impact_metric, b.text)
            for e in self.experience
            for b in e.bullets
            if b.impact_metric
        ]

    def domain_tags(self) -> set[str]:
        return {t for e in self.experience for t in e.domain_tags}

    def find_screening_answer(self, question: str) -> ScreeningQA | None:
        for qa in self.screening_qa_bank:
            if qa.matches(question):
                return qa
        return None

    def get_path(self, dotted: str) -> Any:
        """Resolve 'basics.links.linkedin' style paths against the profile."""
        node: Any = self.model_dump(mode="json")
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                raise KeyError(dotted)
        return node


def load_profile(path: Path | str) -> MasterProfile:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return MasterProfile.model_validate(data)


@lru_cache(maxsize=4)
def load_profile_cached(path: str) -> MasterProfile:
    return load_profile(path)
