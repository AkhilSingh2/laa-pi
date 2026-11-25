"""ATS resume customizer.

Flow: master_profile.json + jd_text
  -> TailoredResume JSON (Claude picks ordering/selection; a deterministic local planner is the fallback)
  -> zero-hallucination verification (every string traceable to the profile)
  -> Jinja2 HTML + CSS -> WeasyPrint single-column PDF at data/resumes/{company}_{role}_{date}.pdf
  -> ATS keyword match score (profile skill terms that appear in both JD and rendered text).

The LLM never writes free text into the resume. It only *selects* and *orders* existing profile facts by
identifier (bullet indices, skill names, summary variant key). That makes fabrication structurally impossible.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

from core.llm import LLMClient, LLMUnavailable
from core.logging import log
from core.models import JobPosting
from core.profile import MasterProfile
from core.settings import ResumeConfig
from engine.matcher import LocalEvidence, choose_summary_variant

# --------------------------------------------------------------------------- selection plan (LLM output)


class RolePlan(BaseModel):
    company: str
    bullet_indices: list[int] = Field(
        description="Indices into this role's bullets array, most relevant first"
    )


class TailoringPlan(BaseModel):
    summary_variant: str = Field(description="Key of basics.summaries.variants, or 'default'")
    skill_order: list[str] = Field(
        description="Canonical skill names from skills_taxonomy, most JD-relevant first"
    )
    roles: list[RolePlan]
    headline_skills: list[str] = Field(
        default_factory=list, description="3-6 canonical skills to feature in the headline"
    )


_SYSTEM = """You tailor a resume by SELECTING and ORDERING existing facts. You never write new text.
- summary_variant must be one of the provided variant keys.
- skill_order may only contain the canonical skill names listed; order the ones the JD values most first.
- For each role, choose bullet indices (0-based) that best match the JD, most relevant first, within the
  per-role limits given. Include every role; never drop a role.
- headline_skills: 3-6 canonical skills to feature in the headline, JD-relevant first.
"""


def _plan_prompt(profile: MasterProfile, posting: JobPosting, cfg: ResumeConfig, variant_hint: str) -> str:
    limits = cfg.bullets_per_role
    roles = []
    for i, e in enumerate(profile.experience):
        lim = limits.most_recent if i == 0 else limits.previous if i == 1 else limits.older
        roles.append(
            {
                "company": e.company,
                "title": e.title,
                "max_bullets": min(lim, len(e.bullets)),
                "bullets": [
                    {"index": j, "text": b.text, "keywords": b.keywords} for j, b in enumerate(e.bullets)
                ],
            }
        )
    return (
        f"SUMMARY_VARIANT_KEYS: {list(profile.basics.summaries.variants) + ['default']} (local hint: {variant_hint})\n\n"
        f"CANONICAL_SKILLS: {json.dumps(profile.skills_taxonomy.all_skills)}\n\n"
        f"ROLES:\n{json.dumps(roles, indent=1)}\n\n"
        f"JOB: {posting.company} - {posting.title}\n\nJOB_DESCRIPTION:\n{posting.jd_text[:9000]}"
    )


def _local_plan(
    profile: MasterProfile, posting: JobPosting, cfg: ResumeConfig, evidence: LocalEvidence | None
) -> TailoringPlan:
    """Deterministic fallback: rank by keyword overlap with the JD."""
    jd_l = (posting.jd_text + " " + posting.title).lower()

    def hits(terms: list[str]) -> int:
        return sum(1 for t in terms if t.lower() in jd_l)

    matched = set(evidence.matched_skills) if evidence else set()
    skills = sorted(
        profile.skills_taxonomy.all_skills,
        key=lambda s: (
            0 if s in matched else 1,
            0 if s.lower() in jd_l else 1,
            profile.skills_taxonomy.all_skills.index(s),
        ),
    )
    limits = cfg.bullets_per_role
    roles: list[RolePlan] = []
    for i, e in enumerate(profile.experience):
        lim = limits.most_recent if i == 0 else limits.previous if i == 1 else limits.older
        ranked = sorted(range(len(e.bullets)), key=lambda j: (-hits(e.bullets[j].keywords), j))
        roles.append(RolePlan(company=e.company, bullet_indices=ranked[:lim]))
    variant = evidence.summary_variant if evidence else "default"
    return TailoringPlan(summary_variant=variant, skill_order=skills, roles=roles, headline_skills=skills[:5])


# --------------------------------------------------------------------------- render model


@dataclass
class BuiltResume:
    resume_json: dict[str, Any]
    pdf_path: Path
    html: str
    summary_variant: str
    ats_keyword_score: float
    matched_keywords: list[str]
    missing_keywords: list[str]
    page_count: int


def _safe(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")
    return s[:40] or "x"


def _group_skills(profile: MasterProfile, ordered: list[str], max_total: int) -> list[dict[str, Any]]:
    chosen = ordered[:max_total]
    labels = {
        "frontend_ui": "Frontend",
        "backend_apis": "Backend & APIs",
        "devops_cloud": "Cloud & DevOps",
        "ai_media": "AI & Media",
        "architecture_quality": "Architecture & Quality",
    }
    groups: list[dict[str, Any]] = []
    for cat, items in profile.skills_taxonomy.categories.items():
        picked = [s for s in chosen if s in items]
        if picked:
            groups.append({"label": labels.get(cat, cat.replace("_", " ").title()), "skills": picked})
    # order groups by position of their first chosen skill
    groups.sort(key=lambda g: chosen.index(g["skills"][0]))
    return groups


def build_resume_json(profile: MasterProfile, plan: TailoringPlan, cfg: ResumeConfig) -> dict[str, Any]:
    b = profile.basics
    # strictly re-validate plan against profile; drop anything unknown
    canonical = profile.skills_taxonomy.all_skills
    skill_order = [s for s in plan.skill_order if s in canonical]
    skill_order += [s for s in canonical if s not in skill_order]
    headline_skills = [s for s in plan.headline_skills if s in canonical][:6] or skill_order[:5]
    variant = plan.summary_variant if plan.summary_variant in b.summaries.variants else "default"

    exp_out = []
    limits = cfg.bullets_per_role
    plan_by_company = {r.company: r for r in plan.roles}
    for i, e in enumerate(profile.experience):
        lim = limits.most_recent if i == 0 else limits.previous if i == 1 else limits.older
        rp = plan_by_company.get(e.company)
        idx = [j for j in (rp.bullet_indices if rp else []) if 0 <= j < len(e.bullets)]
        seen: list[int] = []
        for j in idx:
            if j not in seen:
                seen.append(j)
        if not seen:
            seen = list(range(min(lim, len(e.bullets))))
        seen = seen[:lim]
        exp_out.append(
            {
                "company": e.company,
                "title": e.title,
                "location": e.location,
                "start_date": e.start_date,
                "end_date": e.end_date,
                "bullets": [e.bullets[j].text for j in seen],
                "bullet_indices": seen,
            }
        )

    links = [x for x in (b.links.linkedin, b.links.github, b.links.portfolio) if x]
    headline_short = [re.split(r"\s*\(", s)[0] for s in headline_skills]
    return {
        "header": {
            "full_name": b.full_name,
            "headline": f"{profile.application_defaults.current_title.split(' (')[0]} | "
            + ", ".join(headline_short),
            "email": str(b.email),
            "phone": b.phone,
            "location": f"{b.location.city}, {b.location.country}",
            "links": [re.sub(r"^https?://(www\.)?", "", x) for x in links],
        },
        "summary": b.summaries.get(variant),
        "summary_variant": variant,
        "skills": _group_skills(profile, skill_order, cfg.max_skills_listed),
        "experience": exp_out,
        "projects": [{"name": p.name, "description": p.description} for p in profile.projects],
        "education": [
            {
                "degree": ed.degree,
                "institution": ed.institution,
                "start_date": ed.start_date,
                "end_date": ed.end_date,
            }
            for ed in profile.education
        ],
        "certifications": [c.name for c in profile.certifications],
    }


# --------------------------------------------------------------------------- zero-hallucination verification


class HallucinationError(ValueError):
    pass


def verify_against_profile(resume: dict[str, Any], profile: MasterProfile) -> None:
    """Every content-bearing string in the resume must exist verbatim in the profile (or be derived from it)."""
    facts = profile.all_fact_strings()
    short_skills = {re.split(r"\s*\(", s)[0] for s in profile.skills_taxonomy.all_skills}
    problems: list[str] = []

    def check(value: str, where: str) -> None:
        if value and value not in facts and value not in short_skills:
            problems.append(f"{where}: {value[:80]!r}")

    h = resume["header"]
    check(h["full_name"], "header.full_name")
    for part in h["headline"].split(" | ", 1)[1].split(", "):
        check(part, "header.headline")
    check(resume["summary"], "summary")
    for g in resume["skills"]:
        for s in g["skills"]:
            check(s, "skills")
    for role in resume["experience"]:
        check(role["company"], "experience.company")
        check(role["title"], "experience.title")
        for bl in role["bullets"]:
            check(bl, "experience.bullet")
    for ed in resume["education"]:
        check(ed["degree"], "education.degree")
        check(ed["institution"], "education.institution")
    if problems:
        raise HallucinationError(
            "Resume contains strings not present in master_profile.json: " + "; ".join(problems)
        )


# --------------------------------------------------------------------------- ATS keyword score


def ats_keyword_score(
    profile: MasterProfile, jd_text: str, rendered_text: str
) -> tuple[float, list[str], list[str]]:
    """Share of JD-present profile skill terms that also appear in the rendered resume text."""
    jd_l, res_l = jd_text.lower(), rendered_text.lower()

    def present(term: str, blob: str) -> bool:
        head = re.split(r"\s*\(", term)[0].lower()
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(head)}(?![a-z0-9])", blob))

    in_jd: list[str] = []
    for canonical in profile.skills_taxonomy.all_skills:
        terms = [canonical] + [a for a, t in profile.skills_taxonomy.aliases.items() if t == canonical]
        if any(present(t, jd_l) for t in terms):
            in_jd.append(canonical)
    if not in_jd:
        return 100.0, [], []
    matched = [
        s
        for s in in_jd
        if present(s, res_l)
        or any(present(a, res_l) for a, t in profile.skills_taxonomy.aliases.items() if t == s)
    ]
    missing = [s for s in in_jd if s not in matched]
    return round(100.0 * len(matched) / len(in_jd), 1), matched, missing


# --------------------------------------------------------------------------- render


def _env(template_dir: Path) -> Environment:
    return Environment(loader=FileSystemLoader(str(template_dir)), autoescape=select_autoescape(["html"]))


def render_html(resume: dict[str, Any], cfg: ResumeConfig, root: Path) -> str:
    tpl_path = root / cfg.template
    css = (root / cfg.stylesheet).read_text(encoding="utf-8")
    css = css.replace("size: A4;", f"size: {cfg.page.size};").replace(
        "margin: 14mm 15mm;", f"margin: {cfg.page.margin_mm}mm {cfg.page.margin_mm + 1}mm;"
    )
    tpl = _env(tpl_path.parent).get_template(tpl_path.name)
    return tpl.render(r=resume, css=css, sections=cfg.sections)


def html_to_pdf(html: str, out_path: Path) -> int:
    from weasyprint import HTML  # imported lazily; needs pango/cairo at runtime

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = HTML(string=html).render()
    doc.write_pdf(str(out_path))
    return len(doc.pages)


def _plain_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def resume_filename(cfg: ResumeConfig, company: str, role: str, when: date | None = None) -> str:
    return cfg.filename_pattern.format(
        company=_safe(company), role=_safe(role), date=(when or date.today()).strftime("%Y%m%d")
    )


# --------------------------------------------------------------------------- public entry


def build_resume(
    *,
    profile: MasterProfile,
    posting: JobPosting,
    cfg: ResumeConfig,
    root: Path,
    resumes_dir: Path,
    llm: LLMClient | None,
    evidence: LocalEvidence | None = None,
    matching_cfg=None,
    write_pdf: bool = True,
) -> BuiltResume:
    variant_hint = (
        evidence.summary_variant
        if evidence
        else (choose_summary_variant(posting.jd_text, matching_cfg, profile) if matching_cfg else "default")
    )
    plan: TailoringPlan | None = None
    if llm is not None and llm.available:
        try:
            plan = llm.structured(
                system=_SYSTEM,
                user=_plan_prompt(profile, posting, cfg, variant_hint),
                schema=TailoringPlan,
                tool_name="submit_tailoring_plan",
                description="Select and order profile facts for this job.",
            )
        except (LLMUnavailable, ValueError) as e:
            log.warning("tailoring plan via LLM failed, using local planner: {}", e)
    if plan is None:
        plan = _local_plan(profile, posting, cfg, evidence)

    resume = build_resume_json(profile, plan, cfg)
    verify_against_profile(resume, profile)

    html = render_html(resume, cfg, root)
    pdf_path = resumes_dir / resume_filename(cfg, posting.company, posting.title)
    pages = html_to_pdf(html, pdf_path) if write_pdf else 0
    if write_pdf and pages > 1:
        # tighten: drop the last bullet of each role until it fits (max 3 passes)
        for _ in range(3):
            trimmed = False
            for role in resume["experience"]:
                if len(role["bullets"]) > 2:
                    role["bullets"].pop()
                    role["bullet_indices"].pop()
                    trimmed = True
            if not trimmed:
                break
            html = render_html(resume, cfg, root)
            pages = html_to_pdf(html, pdf_path)
            if pages == 1:
                break
    score, matched, missing = ats_keyword_score(profile, posting.jd_text, _plain_text(html))
    return BuiltResume(
        resume_json=resume,
        pdf_path=pdf_path,
        html=html,
        summary_variant=resume["summary_variant"],
        ats_keyword_score=score,
        matched_keywords=matched,
        missing_keywords=missing,
        page_count=pages,
    )
