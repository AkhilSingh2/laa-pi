"""Humanized outreach drafts. Messages are only ever queued for manual copy/send (outreach.auto_send is
enforced false in settings). Every draft must quote exactly one impact_metric that exists in the profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from core.llm import LLMClient, LLMUnavailable
from core.logging import log
from core.models import JobPosting
from core.profile import MasterProfile
from core.settings import OutreachConfig


class OutreachDrafts(BaseModel):
    recruiter_connect: str = Field(description="LinkedIn connection note, strictly under the char limit")
    recruiter_email: str = Field(
        description="Email/InMail body, strictly under the word limit, no subject line"
    )
    peer_referral: str = Field(description="Peer referral request, strictly under the word limit")
    metric_used: str = Field(
        description="The exact impact_metric string quoted, copied verbatim from the list"
    )


@dataclass
class OutreachResult:
    drafts: OutreachDrafts
    recruiter_name: str | None
    peer_name: str | None
    job_ref: str


_SYSTEM = """You write short, specific, human outreach notes for a job seeker. Rules:
- Facts about the candidate come ONLY from CANDIDATE_FACTS. Quote exactly one metric from METRICS, verbatim,
  and copy it into metric_used.
- No buzzwords (passionate, synergy, rockstar, ninja), no flattery, no exclamation marks, no emojis.
- Mention the exact role title and, when given, the Job ID / requisition reference.
- recruiter_connect: under {connect_chars} characters INCLUDING spaces. First person, friendly, one ask.
- recruiter_email: under {email_words} words. 3 short paragraphs max: why this role, one proof point with the
  metric, a low-friction ask. End with the candidate's first name.
- peer_referral: under {peer_words} words. Casual, respectful, peer-to-peer. Mention the shared tech/domain,
  ask whether they'd be open to referring for the specific role/Job ID, and offer to send the resume.
- Address the recipient by first name when a real name is given; otherwise use a neutral opener like "Hi there".
"""


def _facts(profile: MasterProfile, posting: JobPosting, matched_skills: list[str]) -> dict:
    ad = profile.application_defaults
    return {
        "first_name": profile.basics.first_name,
        "current_title": ad.current_title,
        "current_company": ad.current_company,
        "total_experience_years": ad.total_experience_years,
        "matched_skills_for_this_jd": matched_skills[:8],
        "linkedin": profile.basics.links.linkedin,
    }


def job_reference(posting: JobPosting) -> str:
    meta = posting.raw_metadata or {}
    ref = meta.get("requisition_id") or meta.get("internal_job_id") or posting.external_id
    return f"{posting.title} (Job ID {ref})" if ref else posting.title


def _user_prompt(
    profile: MasterProfile,
    posting: JobPosting,
    matched_skills: list[str],
    recruiter_name: str | None,
    peer_name: str | None,
) -> str:
    metrics = [m for _, m, _ in profile.all_metrics()]
    return (
        f"CANDIDATE_FACTS: {_facts(profile, posting, matched_skills)}\n\n"
        f"METRICS (quote exactly one, verbatim): {metrics}\n\n"
        f"TARGET: company={posting.company}; role={job_reference(posting)}; location={posting.location}\n"
        f"RECRUITER_NAME: {recruiter_name or 'unknown'}\nPEER_NAME: {peer_name or 'unknown'}\n\n"
        f"JOB_DESCRIPTION (for role fit only):\n{posting.jd_text[:5000]}"
    )


def _first(name: str | None) -> str | None:
    if not name or name.lower().startswith(("recruiter at", "engineer at")):
        return None
    return name.split()[0]


def _pick_metric(profile: MasterProfile, matched_skills: list[str]) -> tuple[str, str]:
    """Deterministic: metric from the bullet with most overlap with matched skills; prefer current role."""
    best = None
    best_key = (-1, 1)
    for i, e in enumerate(profile.experience):
        for b in e.bullets:
            if not b.impact_metric:
                continue
            overlap = len({k.lower() for k in b.keywords} & {s.lower() for s in matched_skills})
            key = (overlap, -i)
            if key > best_key:
                best_key, best = key, (e.company, b.impact_metric)
    return best or (profile.experience[0].company, profile.experience[0].bullets[0].impact_metric)


def _local_drafts(
    profile: MasterProfile,
    posting: JobPosting,
    matched_skills: list[str],
    recruiter_name: str | None,
    peer_name: str | None,
    cfg: OutreachConfig,
) -> OutreachDrafts:
    first = profile.basics.first_name
    ad = profile.application_defaults
    company, metric = _pick_metric(profile, matched_skills)
    skills = ", ".join(s.split(" (")[0] for s in matched_skills[:3]) or "React, Python and AWS"
    ref = job_reference(posting)
    r_hi = f"Hi {_first(recruiter_name)}" if _first(recruiter_name) else "Hi there"
    p_hi = f"Hi {_first(peer_name)}" if _first(peer_name) else "Hi there"

    connect = (
        f"{r_hi}, I'm {first}, a {ad.current_title.split(' (')[0]} at {ad.current_company} ({ad.total_experience_years:g} yrs, {skills}). "
        f"I applied for {posting.title} at {posting.company} and would value connecting. Recent result: {metric}."
    )
    connect = _fit_chars(connect, cfg.recruiter_connect_max_chars)

    email = (
        f"{r_hi},\n\n"
        f"I applied for the {ref} role at {posting.company}. I'm currently a {ad.current_title.split(' (')[0]} at "
        f"{ad.current_company} with {ad.total_experience_years:g} years across {skills}, which maps closely to what the posting asks for.\n\n"
        f"One relevant proof point from {company}: {metric}.\n\n"
        f"If it's useful, I'm happy to share a short summary of how my background lines up or to find 15 minutes this week.\n\n{first}"
    )
    email = _fit_words(email, cfg.recruiter_email_max_words)

    peer = (
        f"{p_hi}, I'm {first}, a {ad.current_title.split(' (')[0]} at {ad.current_company} working with {skills}. "
        f"I noticed {posting.company} is hiring for {ref} and it looks like a strong match for my background "
        f"(e.g. {metric}). Would you be open to referring me? Happy to send my resume and the posting link."
    )
    peer = _fit_words(peer, cfg.peer_referral_max_words)
    return OutreachDrafts(
        recruiter_connect=connect, recruiter_email=email, peer_referral=peer, metric_used=metric
    )


def _fit_chars(text: str, limit: int) -> str:
    if len(text) < limit:
        return text
    cut = text[: limit - 1]
    cut = cut[: cut.rfind(".") + 1] if "." in cut[limit // 2 :] else cut.rstrip(" ,;") + "."
    return cut


def _fit_words(text: str, limit: int) -> str:
    words = text.split()
    if len(words) < limit:
        return text
    return " ".join(words[: limit - 1]).rstrip(",;") + "."


def validate_drafts(drafts: OutreachDrafts, profile: MasterProfile, cfg: OutreachConfig) -> list[str]:
    """Return a list of violations (empty when compliant)."""
    issues: list[str] = []
    if len(drafts.recruiter_connect) >= cfg.recruiter_connect_max_chars:
        issues.append(
            f"recruiter_connect has {len(drafts.recruiter_connect)} chars (limit {cfg.recruiter_connect_max_chars})"
        )
    if len(drafts.recruiter_email.split()) >= cfg.recruiter_email_max_words:
        issues.append(
            f"recruiter_email has {len(drafts.recruiter_email.split())} words (limit {cfg.recruiter_email_max_words})"
        )
    if len(drafts.peer_referral.split()) >= cfg.peer_referral_max_words:
        issues.append(
            f"peer_referral has {len(drafts.peer_referral.split())} words (limit {cfg.peer_referral_max_words})"
        )
    metrics = {m for _, m, _ in profile.all_metrics()}
    if cfg.require_metric_from_profile:
        if drafts.metric_used not in metrics:
            issues.append("metric_used is not a verbatim impact_metric from the profile")
        elif (
            drafts.metric_used not in drafts.recruiter_connect + drafts.recruiter_email + drafts.peer_referral
        ):
            issues.append("metric_used does not appear in any draft")
    banned = re.compile(
        r"\b(passionate|synergy|rockstar|ninja|guru)\b|!|[\U0001F300-\U0001FAFF]", re.IGNORECASE
    )
    for name in ("recruiter_connect", "recruiter_email", "peer_referral"):
        if banned.search(getattr(drafts, name)):
            issues.append(f"{name} contains banned phrasing/punctuation")
    return issues


def draft_outreach(
    *,
    profile: MasterProfile,
    posting: JobPosting,
    matched_skills: list[str],
    cfg: OutreachConfig,
    llm: LLMClient | None,
    recruiter_name: str | None = None,
    peer_name: str | None = None,
) -> OutreachResult:
    drafts: OutreachDrafts | None = None
    if llm is not None and llm.available:
        system = _SYSTEM.format(
            connect_chars=cfg.recruiter_connect_max_chars,
            email_words=cfg.recruiter_email_max_words,
            peer_words=cfg.peer_referral_max_words,
        )
        try:
            candidate = llm.structured(
                system=system,
                user=_user_prompt(profile, posting, matched_skills, recruiter_name, peer_name),
                schema=OutreachDrafts,
                tool_name="submit_outreach_drafts",
                description="Submit the three outreach drafts.",
            )
            issues = validate_drafts(candidate, profile, cfg)
            if issues:
                log.warning("LLM outreach drafts rejected ({}); using local templates", "; ".join(issues))
            else:
                drafts = candidate
        except (LLMUnavailable, ValueError) as e:
            log.warning("outreach via LLM failed, using local templates: {}", e)
    if drafts is None:
        drafts = _local_drafts(profile, posting, matched_skills, recruiter_name, peer_name, cfg)
        issues = validate_drafts(drafts, profile, cfg)
        if issues:
            raise ValueError("local outreach templates violated policy: " + "; ".join(issues))
    return OutreachResult(
        drafts=drafts, recruiter_name=recruiter_name, peer_name=peer_name, job_ref=job_reference(posting)
    )
