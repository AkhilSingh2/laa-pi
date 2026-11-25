"""Read and write data/master_profile.json from the Settings form, and from an uploaded resume.

Every save is validated against MasterProfile before it touches the file. The previous file is kept as
master_profile.backup-<timestamp>.json.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.profile import MasterProfile, load_profile_cached


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _split_list(text: str) -> list[str]:
    raw = text.replace("\n", ",")
    return [p.strip() for p in raw.split(",") if p.strip()]


def write_profile(path: Path | str, payload: dict[str, Any]) -> MasterProfile:
    """Validate, back up the current file, and write. Raises ValidationError when the payload is not a profile."""
    profile = MasterProfile.model_validate(payload)
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        backup = dest.with_name(f"master_profile.backup-{datetime.now():%Y%m%d-%H%M%S}.json")
        backup.write_bytes(dest.read_bytes())
    body = json.dumps(profile.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(dest)
    load_profile_cached.cache_clear()
    return profile


def form_defaults(profile: MasterProfile) -> dict[str, Any]:
    """Flat values the Settings form edits. Nested pieces (roles, skills) stay structured."""
    data = profile.model_dump(mode="json")
    b, ad = data["basics"], data["application_defaults"]
    skills = data["skills_taxonomy"]
    aliases = skills.pop("aliases", {})
    return {
        "full_name": b["full_name"],
        "first_name": b["first_name"],
        "last_name": b["last_name"],
        "headline": b["headline"],
        "email": b["email"],
        "phone": b["phone"],
        "city": b["location"]["city"],
        "state": b["location"].get("state", ""),
        "country": b["location"]["country"],
        "postal_code": b["location"].get("postal_code", ""),
        "linkedin": b["links"].get("linkedin", ""),
        "github": b["links"].get("github", ""),
        "portfolio": b["links"].get("portfolio", ""),
        "summary": b["summaries"]["default"],
        "summary_variants": b["summaries"].get("variants", {}),
        "total_years": ad["total_experience_years"],
        "relevant_years": ad["relevant_experience_years"],
        "current_company": ad["current_company"],
        "current_title": ad["current_title"],
        "current_ctc": ad.get("current_ctc_lpa", 0),
        "expected_ctc": ad.get("expected_ctc_lpa", 0),
        "currency": ad.get("currency", "INR"),
        "notice_days": ad["notice_period_days"],
        "serving_notice": ad.get("is_serving_notice", False),
        "last_working_day": ad.get("last_working_day") or "",
        "work_auth": ad.get("work_authorization", {}).get("india", ""),
        "sponsorship": ad.get("work_authorization", {}).get("requires_sponsorship", False),
        "work_modes": ad.get("work_mode_preferences", []),
        "preferred_locations": ", ".join(ad.get("preferred_locations", [])),
        "relocate": ad.get("willing_to_relocate", False),
        "gender": ad.get("gender", ""),
        "veteran": ad.get("veteran_status", ""),
        "disability": ad.get("disability_status", ""),
        "skills": {k: list(v) for k, v in skills.items() if isinstance(v, list)},
        "aliases": dict(aliases),
        "roles": data["experience"],
        "education": data.get("education", []),
        "projects": data.get("projects", []),
        "certifications": data.get("certifications", []),
        "screening": data.get("screening_qa_bank", []),
    }


def _bullets(text: str, previous: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prev = {b.get("text", ""): b for b in previous}
    out = []
    for line in _lines(text):
        body, sep, metric = line.partition(" || ")
        body, metric = body.strip(), metric.strip()
        old = prev.get(body, {})
        out.append(
            {
                "text": body,
                "keywords": list(old.get("keywords", [])),
                "impact_metric": metric if sep else old.get("impact_metric", ""),
            }
        )
    return out


def build_profile(values: dict[str, Any]) -> dict[str, Any]:
    """Turn form values back into a master_profile.json document."""
    roles = []
    for role in values["roles"]:
        if role.get("remove") or not str(role.get("company", "")).strip():
            continue
        roles.append(
            {
                "company": role["company"].strip(),
                "title": role["title"].strip(),
                "location": role.get("location", "").strip(),
                "start_date": role["start_date"].strip(),
                "end_date": role["end_date"].strip(),
                "domain_tags": _split_list(role.get("domain_tags_text", ""))
                if "domain_tags_text" in role
                else list(role.get("domain_tags", [])),
                "bullets": _bullets(role.get("bullets_text", ""), role.get("bullets", []))
                if "bullets_text" in role
                else role.get("bullets", []),
            }
        )
    current = next((r for r in roles if r["end_date"] == "Present"), None)
    company = values["current_company"].strip()
    title = values["current_title"].strip()
    if current is not None:
        current["company"] = company or current["company"]
        current["title"] = title or current["title"]
        company, title = current["company"], current["title"]

    skills = {k: v for k, v in values["skills"].items() if k != "aliases" and v}
    return {
        "basics": {
            "full_name": values["full_name"].strip(),
            "first_name": values["first_name"].strip(),
            "last_name": values["last_name"].strip(),
            "headline": values["headline"].strip(),
            "email": values["email"].strip(),
            "phone": values["phone"].strip(),
            "location": {
                "city": values["city"].strip(),
                "state": values.get("state", "").strip(),
                "country": values["country"].strip(),
                "postal_code": values.get("postal_code", "").strip(),
            },
            "links": {
                "linkedin": values.get("linkedin", "").strip(),
                "github": values.get("github", "").strip(),
                "portfolio": values.get("portfolio", "").strip(),
            },
            "summaries": {
                "default": values["summary"].strip(),
                "variants": values.get("summary_variants", {}),
            },
        },
        "application_defaults": {
            "total_experience_years": float(values["total_years"]),
            "relevant_experience_years": float(values["relevant_years"]),
            "current_company": company,
            "current_title": title,
            "current_ctc_lpa": float(values["current_ctc"]),
            "expected_ctc_lpa": float(values["expected_ctc"]),
            "currency": values.get("currency", "INR").strip() or "INR",
            "notice_period_days": int(values["notice_days"]),
            "is_serving_notice": bool(values.get("serving_notice")),
            "last_working_day": values.get("last_working_day", "").strip() or None,
            "work_authorization": {
                "india": values.get("work_auth", "").strip(),
                "requires_sponsorship": bool(values.get("sponsorship")),
            },
            "work_mode_preferences": list(values.get("work_modes", [])),
            "preferred_locations": _split_list(values.get("preferred_locations", "")),
            "willing_to_relocate": bool(values.get("relocate")),
            "gender": values.get("gender", "").strip(),
            "veteran_status": values.get("veteran", "").strip(),
            "disability_status": values.get("disability", "").strip(),
        },
        "skills_taxonomy": {**skills, "aliases": values.get("aliases", {})},
        "experience": roles,
        "projects": [p for p in values.get("projects", []) if p.get("name", "").strip()],
        "education": [e for e in values.get("education", []) if e.get("institution", "").strip()],
        "certifications": [c for c in values.get("certifications", []) if c.get("name", "").strip()],
        "screening_qa_bank": [
            q for q in values.get("screening", []) if q.get("question_pattern", "").strip()
        ],
    }


class ImportedRole(BaseModel):
    company: str
    title: str
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    bullets: list[str] = Field(default_factory=list)


class ImportedEducation(BaseModel):
    institution: str
    degree: str = ""
    location: str = ""
    start_date: str = ""
    end_date: str = ""


class ImportedProfile(BaseModel):
    """What the model may return from a resume. Only facts present in the resume text."""

    full_name: str
    email: str
    phone: str = ""
    headline: str = ""
    city: str = ""
    country: str = "India"
    linkedin: str = ""
    github: str = ""
    summary: str = ""
    total_experience_years: float = 0
    notice_period_days: int = 30
    current_ctc_lpa: float = 0
    expected_ctc_lpa: float = 0
    preferred_locations: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    roles: list[ImportedRole] = Field(default_factory=list)
    education: list[ImportedEducation] = Field(default_factory=list)


def _year_month(value: str, fallback: str) -> str:
    import re

    value = (value or "").strip()
    if value.lower() == "present":
        return "Present"
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return value
    year = re.search(r"(19|20)\d{2}", value)
    if not year:
        return fallback
    months = {
        "jan": "01",
        "feb": "02",
        "mar": "03",
        "apr": "04",
        "may": "05",
        "jun": "06",
        "jul": "07",
        "aug": "08",
        "sep": "09",
        "oct": "10",
        "nov": "11",
        "dec": "12",
    }
    mon = next((n for name, n in months.items() if name in value.lower()), "01")
    return f"{year.group(0)}-{mon}"
