"""Deduplication and priority fit scoring.

Dedup: postings are grouped by normalized (company, title); within a group, JD texts with TF-IDF cosine
similarity > threshold are treated as the same job. The canonical copy is the one whose apply URL has the
highest source priority (Direct ATS > LinkedIn > Naukri), ties broken by earliest posting.

Scoring: Claude (claude-sonnet-4-6) scores each canonical job 0-100 against master_profile.json using the
weighted rubric from config. A deterministic keyword pre-analysis is computed locally and handed to the model
as evidence so it cannot invent skill overlaps. When no API key is configured, the deterministic heuristic
alone is used and the rationale is labelled as such.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from core.llm import LLMClient, LLMUnavailable
from core.logging import log
from core.models import DIRECT_ATS, FitBreakdownItem, FitScore, JobPosting, Source
from core.profile import MasterProfile
from core.settings import MatchingConfig

# --------------------------------------------------------------------------- dedup

_SOURCE_RANK_DEFAULT = {"direct_ats": 0, "linkedin": 1, "naukri": 2}


def _source_bucket(p: JobPosting) -> str:
    if p.ats_type in DIRECT_ATS or p.source in (Source.GREENHOUSE, Source.LEVER, Source.ASHBY):
        return "direct_ats"
    return p.source.value


def _rank(p: JobPosting, priority: list[str]) -> tuple[int, float]:
    order = {name: i for i, name in enumerate(priority)} or _SOURCE_RANK_DEFAULT
    posted = p.posted_at.timestamp() if p.posted_at else float("inf")
    return (order.get(_source_bucket(p), 99), posted)


@dataclass
class DedupResult:
    canonical: list[JobPosting]
    duplicates: dict[str, str] = field(default_factory=dict)  # dup_id -> canonical_id

    @property
    def groups(self) -> dict[str, list[str]]:
        g: dict[str, list[str]] = defaultdict(list)
        for d, c in self.duplicates.items():
            g[c].append(d)
        return dict(g)


def _jd_similarity_matrix(texts: list[str]) -> np.ndarray:
    if len(texts) == 1:
        return np.ones((1, 1))
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1, sublinear_tf=True)
    try:
        m = vec.fit_transform(texts)
    except ValueError:  # all texts empty
        return np.eye(len(texts))
    return cosine_similarity(m)


def _cluster(group: list[JobPosting], sims: np.ndarray, threshold: float) -> list[list[JobPosting]]:
    """Union-find over the JD similarity graph (identical hash or cosine > threshold)."""
    parent = list(range(len(group)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(group)):
        for j in range(i + 1, len(group)):
            if group[i].jd_hash == group[j].jd_hash or sims[i, j] > threshold:
                parent[find(i)] = find(j)
    clusters: dict[int, list[JobPosting]] = defaultdict(list)
    for i, p in enumerate(group):
        clusters[find(i)].append(p)
    return list(clusters.values())


def dedupe(postings: list[JobPosting], cfg: MatchingConfig) -> DedupResult:
    """Cluster cross-posted jobs and pick the highest-priority apply URL per cluster."""
    threshold = cfg.dedup.tfidf_cosine_threshold
    priority = cfg.dedup.url_priority
    by_key: dict[tuple[str, str], list[JobPosting]] = defaultdict(list)
    for p in postings:
        by_key[(p.company_normalized, p.title_normalized)].append(p)

    canonical: list[JobPosting] = []
    duplicates: dict[str, str] = {}

    for group in by_key.values():
        if len(group) == 1:
            canonical.append(group[0])
            continue
        sims = _jd_similarity_matrix([p.jd_text or p.title for p in group])
        for members in _cluster(group, sims, threshold):
            members.sort(key=lambda p: _rank(p, priority))
            head = members[0]
            canonical.append(head)
            for dup in members[1:]:
                duplicates[dup.id] = head.id
    return DedupResult(canonical=canonical, duplicates=duplicates)


# --------------------------------------------------------------------------- local evidence

_TOKEN = re.compile(r"[a-z0-9+#.]+")
_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|to)?\s*(\d{1,2})?\s*\+?\s*(?:years|yrs)", re.IGNORECASE)
_SENIOR_WORDS = ("senior", "sr.", "sr ", "lead", "staff", "principal")
_JUNIOR_WORDS = ("junior", "jr.", "intern", "trainee", "fresher", "entry level", "graduate")


@dataclass
class LocalEvidence:
    matched_skills: list[str]
    missing_skills: list[str]
    jd_years_min: int | None
    jd_years_max: int | None
    seniority_signal: str
    domain_hits: list[str]
    location_match: bool
    remote_match: bool
    heuristic_score: int
    summary_variant: str

    def as_prompt_block(self) -> str:
        return json.dumps(self.__dict__, indent=2)


def jd_years_range(jd: str) -> tuple[int | None, int | None]:
    """Lowest years requirement and highest upper bound mentioned in a JD (None if not stated)."""
    ymin = ymax = None
    for m in _YEARS.finditer(jd or ""):
        a, b = int(m.group(1)), (int(m.group(2)) if m.group(2) else None)
        if a > 25:
            continue
        ymin = a if ymin is None else min(ymin, a)
        if b:
            ymax = b if ymax is None else max(ymax, b)
    return ymin, ymax


def _skill_terms_in_jd(profile: MasterProfile, jd: str) -> tuple[list[str], list[str]]:
    jd_l = jd.lower()
    matched: list[str] = []
    for canonical in profile.skills_taxonomy.all_skills:
        terms = [canonical] + [a for a, t in profile.skills_taxonomy.aliases.items() if t == canonical]
        # also split composite skills like "AWS (S3, ECR, RDS, IAM, SQS)" into head token "AWS"
        head = re.split(r"[\s(]", canonical, maxsplit=1)[0]
        if len(head) >= 2:
            terms.append(head)
        for t in terms:
            if re.search(rf"(?<![a-z0-9]){re.escape(t.lower())}(?![a-z0-9])", jd_l):
                matched.append(canonical)
                break
    # "missing" = tech tokens in the JD that look like skills but aren't in the profile
    known = {t.lower() for t in profile.all_skill_terms()}
    candidates = {
        "java",
        "go",
        "golang",
        "rust",
        "c++",
        "c#",
        ".net",
        "php",
        "ruby",
        "rails",
        "django",
        "flask",
        "spring",
        "angular",
        "svelte",
        "graphql",
        "grpc",
        "kafka",
        "rabbitmq",
        "redis",
        "elasticsearch",
        "mysql",
        "dynamodb",
        "gcp",
        "azure",
        "kubernetes",
        "terraform",
        "ansible",
        "jenkins",
        "circleci",
        "pytorch",
        "tensorflow",
        "spark",
        "hadoop",
        "scala",
        "kotlin",
        "swift",
        "flutter",
        "react native",
        "cypress",
        "playwright",
        "jest",
        "openai",
        "langchain",
        "vector database",
        "pinecone",
        "weaviate",
        "snowflake",
        "airflow",
        "dbt",
        "bigquery",
    }
    missing = sorted(
        c
        for c in candidates
        if c not in known and re.search(rf"(?<![a-z0-9]){re.escape(c)}(?![a-z0-9])", jd_l)
    )
    return matched, missing


def choose_summary_variant(jd: str, cfg: MatchingConfig, profile: MasterProfile) -> str:
    jd_l = jd.lower()
    best, best_hits = None, 0
    for variant, keywords in cfg.summary_variant_rules.items():
        if (
            variant == "fallback"
            or not isinstance(keywords, list)
            or variant not in profile.basics.summaries.variants
        ):
            continue
        hits = sum(1 for k in keywords if k.lower() in jd_l)
        if hits > best_hits:
            best, best_hits = variant, hits
    fallback = cfg.summary_variant_rules.get("fallback", "default")
    return best or (fallback if isinstance(fallback, str) else "default")


def local_evidence(posting: JobPosting, profile: MasterProfile, cfg: MatchingConfig) -> LocalEvidence:
    jd = posting.jd_text
    title_l = posting.title.lower()
    matched, missing = _skill_terms_in_jd(profile, jd + " " + posting.title)

    ymin, ymax = jd_years_range(jd)
    if any(w in title_l for w in _JUNIOR_WORDS):
        seniority = "junior"
    elif any(w in title_l for w in _SENIOR_WORDS):
        seniority = "senior"
    else:
        seniority = "mid"

    tags = profile.domain_tags()
    blob = (jd + " " + posting.title).lower()
    domain_hits = sorted(t for t in tags if t.replace("-", " ") in blob or t in blob)

    loc = (posting.location or "").lower()
    pref = [p.lower() for p in profile.application_defaults.preferred_locations]
    location_match = (not loc) or any(p in loc for p in pref) or posting.remote_type == "remote"
    modes = [m.lower() for m in profile.application_defaults.work_mode_preferences]
    remote_match = posting.remote_type in ("unknown", *modes)

    # deterministic heuristic (0-100) following the rubric weights
    r = cfg.rubric
    total_skills = max(len(profile.skills_taxonomy.all_skills), 1)
    skills_pts = r.core_skills_overlap * min(len(matched) / 8, 1.0)
    exp = profile.application_defaults.total_experience_years
    if seniority == "junior":
        sen_pts = r.seniority_match * 0.2
    elif ymin is not None and ymin > exp + 2:
        sen_pts = r.seniority_match * 0.3
    elif ymax is not None and ymax < exp - 3:
        sen_pts = r.seniority_match * 0.4
    else:
        sen_pts = r.seniority_match * (1.0 if seniority == "senior" else 0.75)
    dom_pts = r.domain_relevance * min(len(domain_hits) / 3, 1.0)
    loc_pts = r.location_work_mode * ((0.6 if location_match else 0.0) + (0.4 if remote_match else 0.0))
    depth_pts = r.tech_stack_depth * min(len(matched) / total_skills * 2, 1.0)
    scope_pts = r.role_scope_clarity * (1.0 if len(jd) > 800 else 0.5)
    heuristic = int(round(skills_pts + sen_pts + dom_pts + loc_pts + depth_pts + scope_pts))

    return LocalEvidence(
        matched_skills=matched,
        missing_skills=missing,
        jd_years_min=ymin,
        jd_years_max=ymax,
        seniority_signal=seniority,
        domain_hits=domain_hits,
        location_match=location_match,
        remote_match=remote_match,
        heuristic_score=max(0, min(100, heuristic)),
        summary_variant=choose_summary_variant(jd, cfg, profile),
    )


# --------------------------------------------------------------------------- Claude scoring

_SYSTEM = """You are a rigorous technical recruiter scoring how well ONE candidate fits ONE job.
Rules:
- Use ONLY the candidate facts provided. Never assume skills, titles, or years not present in the profile.
- The LOCAL_EVIDENCE block was computed deterministically from the profile; matched_skills is the complete
  list of candidate skills that literally appear in the JD. Do not claim additional overlaps.
- Score each rubric criterion from 0 to its max weight, then sum to a 0-100 total. Be calibrated: 90+ means
  near-perfect alignment, 75 means a strong application is justified, below 60 means clear mismatch.
- Penalize hard mismatches: required years far above candidate experience, mandatory stacks the candidate
  lacks (e.g. Java/.NET-only roles), junior roles, or locations/work modes outside preferences.
- Rationale: 2-4 sentences, concrete, referencing JD requirements and profile evidence.
"""


def _fit_user_prompt(
    posting: JobPosting, profile: MasterProfile, cfg: MatchingConfig, ev: LocalEvidence
) -> str:
    rubric = cfg.rubric.model_dump()
    prof = {
        "headline": profile.basics.headline,
        "total_experience_years": profile.application_defaults.total_experience_years,
        "current_title": profile.application_defaults.current_title,
        "preferred_locations": profile.application_defaults.preferred_locations,
        "work_mode_preferences": profile.application_defaults.work_mode_preferences,
        "skills": profile.skills_taxonomy.categories,
        "experience": [
            {
                "company": e.company,
                "title": e.title,
                "period": f"{e.start_date} to {e.end_date}",
                "domain_tags": e.domain_tags,
                "keywords": sorted({k for b in e.bullets for k in b.keywords}),
            }
            for e in profile.experience
        ],
    }
    return (
        f"RUBRIC (criterion: max points):\n{json.dumps(rubric, indent=2)}\n\n"
        f"CANDIDATE_PROFILE:\n{json.dumps(prof, indent=2)}\n\n"
        f"LOCAL_EVIDENCE:\n{ev.as_prompt_block()}\n\n"
        f"JOB:\ncompany: {posting.company}\ntitle: {posting.title}\nlocation: {posting.location}\n"
        f"remote_type: {posting.remote_type}\n\nJOB_DESCRIPTION:\n{posting.jd_text[:9000]}\n\n"
        "Return the breakdown with exactly these criterion keys: "
        + ", ".join(rubric.keys())
        + f". Set summary_variant to one of {list(profile.basics.summaries.variants) + ['default']}."
    )


def _heuristic_fit(ev: LocalEvidence, cfg: MatchingConfig) -> FitScore:
    r = cfg.rubric.model_dump()
    # distribute the heuristic score proportionally to weights for a consistent breakdown shape
    breakdown = {
        k: FitBreakdownItem(
            score=int(round(ev.heuristic_score * w / 100)), max=w, evidence="heuristic (no LLM key)"
        )
        for k, w in r.items()
    }
    return FitScore(
        score=ev.heuristic_score,
        breakdown=breakdown,
        rationale=(
            "Heuristic score (no AI key configured). "
            f"Matched skills: {', '.join(ev.matched_skills) or 'none'}. "
            f"Seniority signal: {ev.seniority_signal}; JD years: {ev.jd_years_min}-{ev.jd_years_max}."
        ),
        matched_skills=ev.matched_skills,
        missing_skills=ev.missing_skills,
        summary_variant=ev.summary_variant,
    )


def _validate_against_evidence(fit: FitScore, ev: LocalEvidence, profile: MasterProfile) -> FitScore:
    """Zero-hallucination guard: matched_skills may only contain real profile skills that appear in the JD."""
    allowed = set(ev.matched_skills)
    cleaned = []
    for s in fit.matched_skills:
        c = profile.canonical_skill(s)
        if c and c in allowed and c not in cleaned:
            cleaned.append(c)
    fit.matched_skills = cleaned or ev.matched_skills
    if fit.summary_variant not in profile.basics.summaries.variants and fit.summary_variant != "default":
        fit.summary_variant = ev.summary_variant
    # keep the total consistent with the breakdown
    total = sum(min(item.score, item.max) for item in fit.breakdown.values())
    for item in fit.breakdown.values():
        item.score = min(item.score, item.max)
    fit.score = max(0, min(100, total))
    return fit


def score_job(
    posting: JobPosting, profile: MasterProfile, cfg: MatchingConfig, llm: LLMClient | None
) -> FitScore:
    ev = local_evidence(posting, profile, cfg)
    if llm is None or not llm.available:
        return _heuristic_fit(ev, cfg)
    try:
        fit = llm.structured(
            system=_SYSTEM,
            user=_fit_user_prompt(posting, profile, cfg, ev),
            schema=FitScore,
            tool_name="submit_fit_score",
            description="Submit the rubric-based fit score for the candidate/job pair.",
        )
    except LLMUnavailable as e:
        log.warning("LLM unavailable, falling back to heuristic: {}", e)
        return _heuristic_fit(ev, cfg)
    return _validate_against_evidence(fit, ev, profile)


def fit_to_breakdown_json(fit: FitScore) -> dict[str, Any]:
    return {
        "criteria": {k: v.model_dump() for k, v in fit.breakdown.items()},
        "matched_skills": fit.matched_skills,
        "missing_skills": fit.missing_skills,
        "summary_variant": fit.summary_variant,
    }
