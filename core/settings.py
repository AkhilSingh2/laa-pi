"""Typed configuration: config/config.yaml, then data/settings_overrides.yaml (edits made in the dashboard).

Secrets come from data/secrets.json (set in Settings) or the environment / .env.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"
OVERRIDES_FILE = "data/settings_overrides.yaml"

Platform = Literal["linkedin", "naukri", "direct_ats"]
ApplyMode = Literal["review_mode", "auto_submit_ats"]


class AppConfig(BaseModel):
    timezone: str = "Asia/Kolkata"
    db_path: str = "data/jobs.db"
    profile_path: str = "data/master_profile.json"
    resumes_dir: str = "data/resumes"
    screenshots_dir: str = "data/screenshots"
    browser_profile_dir: str = "data/browser_profile"
    exports_dir: str = "data/exports"
    log_level: str = "INFO"


class LLMConfig(BaseModel):
    provider: str = "anthropic"  # anthropic | deepseek | routerplus | openai
    model: str = "claude-sonnet-4-6"
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: str = ""  # OpenAI-compatible endpoint; empty uses the provider default
    temperature: float = 0.2
    max_tokens: int = 4096
    max_retries: int = 3

    @property
    def api_key(self) -> str | None:
        from core.secrets import get_secret

        return get_secret(self.api_key_env)


class ExperienceRange(BaseModel):
    min: int = 4
    max: int = 8


class SearchConfig(BaseModel):
    titles: list[str] = Field(default_factory=list)
    experience_years: ExperienceRange = Field(default_factory=ExperienceRange)
    locations: list[str] = Field(default_factory=list)
    remote_types: list[str] = Field(default_factory=lambda: ["remote", "hybrid", "onsite"])
    posted_within_days: int = 7
    exclude_title_keywords: list[str] = Field(default_factory=list)
    exclude_companies: list[str] = Field(default_factory=list)


class ApiSourceConfig(BaseModel):
    enabled: bool = True
    companies_csv: str = "config/target_companies.csv"


class LinkedInSourceConfig(BaseModel):
    enabled: bool = True
    queries: list[str] = Field(default_factory=list)
    experience_levels: list[str] = Field(default_factory=lambda: ["Mid-Senior level"])
    location: str = "India"
    remote_filters: list[str] = Field(default_factory=lambda: ["remote", "hybrid", "onsite"])
    max_pages_per_query: int = 3
    easy_apply_only: bool = False


class NaukriSourceConfig(BaseModel):
    enabled: bool = True
    queries: list[str] = Field(default_factory=list)
    experience_years: int = 5
    locations: list[str] = Field(default_factory=list)
    max_pages_per_query: int = 3


class SourcesConfig(BaseModel):
    greenhouse: ApiSourceConfig = Field(default_factory=ApiSourceConfig)
    lever: ApiSourceConfig = Field(default_factory=ApiSourceConfig)
    ashby: ApiSourceConfig = Field(default_factory=ApiSourceConfig)
    linkedin: LinkedInSourceConfig = Field(default_factory=LinkedInSourceConfig)
    naukri: NaukriSourceConfig = Field(default_factory=NaukriSourceConfig)


class DedupConfig(BaseModel):
    tfidf_cosine_threshold: float = 0.88
    url_priority: list[str] = Field(default_factory=lambda: ["direct_ats", "linkedin", "naukri"])


class RubricConfig(BaseModel):
    core_skills_overlap: int = 35
    seniority_match: int = 20
    domain_relevance: int = 15
    location_work_mode: int = 15
    tech_stack_depth: int = 10
    role_scope_clarity: int = 5

    @model_validator(mode="after")
    def _sum_is_100(self) -> RubricConfig:
        total = sum(self.model_dump().values())
        if total != 100:
            raise ValueError(f"matching.rubric weights must sum to 100 (got {total})")
        return self


class MatchingConfig(BaseModel):
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    fit_threshold: int = 75
    rubric: RubricConfig = Field(default_factory=RubricConfig)
    summary_variant_rules: dict[str, list[str] | str] = Field(default_factory=dict)


class DelayRange(BaseModel):
    min: int = 15
    max: int = 45


class ExecutionWindow(BaseModel):
    days: list[str]
    start: str  # "HH:MM"
    end: str


class QuotasConfig(BaseModel):
    daily_caps: dict[str, int] = Field(
        default_factory=lambda: {"linkedin": 20, "naukri": 25, "direct_ats": 30}
    )
    human_delay_seconds: DelayRange = Field(default_factory=DelayRange)
    execution_windows: list[ExecutionWindow] = Field(default_factory=list)
    stop_scheduler_when_caps_reached: bool = True


class AutoSubmitConfig(BaseModel):
    allowed_ats: list[str] = Field(default_factory=lambda: ["greenhouse", "lever", "ashby"])
    min_field_confidence: float = 0.90
    require_all_required_fields: bool = True


class CopilotConfig(BaseModel):
    headless: bool = False
    slow_mo_ms: int = 120
    default_mode_by_platform: dict[str, ApplyMode] = Field(
        default_factory=lambda: {
            "linkedin": "review_mode",
            "naukri": "review_mode",
            "direct_ats": "review_mode",
        }
    )
    auto_submit: AutoSubmitConfig = Field(default_factory=AutoSubmitConfig)
    pause_on: list[str] = Field(default_factory=list)
    screenshot_on_pause: bool = True
    resume_upload_field_hints: list[str] = Field(default_factory=lambda: ["resume", "cv"])
    profile_field_map: dict[str, str] = Field(default_factory=dict)


class PageConfig(BaseModel):
    size: str = "A4"
    margin_mm: int = 14


class BulletsPerRole(BaseModel):
    most_recent: int = 4
    previous: int = 3
    older: int = 2


class ZeroHallucinationConfig(BaseModel):
    allow_only_profile_strings: bool = True
    allow_alias_expansion: bool = True


class ResumeConfig(BaseModel):
    template: str = "engine/templates/resume.html"
    stylesheet: str = "engine/templates/resume.css"
    page: PageConfig = Field(default_factory=PageConfig)
    filename_pattern: str = "{company}_{role}_{date}.pdf"
    bullets_per_role: BulletsPerRole = Field(default_factory=BulletsPerRole)
    max_skills_listed: int = 18
    sections: list[str] = Field(
        default_factory=lambda: ["header", "summary", "skills", "experience", "education"]
    )
    ats_keyword_score_warn_below: int = 70
    zero_hallucination: ZeroHallucinationConfig = Field(default_factory=ZeroHallucinationConfig)


class ContactsConfig(BaseModel):
    max_recruiters: int = 1
    max_peers: int = 1
    use_linkedin_people_search: bool = True
    recruiter_title_keywords: list[str] = Field(default_factory=list)
    peer_title_keywords: list[str] = Field(default_factory=list)
    email_pattern_candidates: list[str] = Field(default_factory=list)
    xray_templates: dict[str, str] = Field(default_factory=dict)


class OutreachConfig(BaseModel):
    auto_send: bool = False
    recruiter_connect_max_chars: int = 300
    recruiter_email_max_words: int = 120
    peer_referral_max_words: int = 100
    require_metric_from_profile: bool = True
    tone: str = "concise, specific, warm, no buzzwords"

    @model_validator(mode="after")
    def _never_auto_send(self) -> OutreachConfig:
        if self.auto_send:
            raise ValueError("outreach.auto_send must remain false (policy guardrail)")
        return self


class SchedulerConfig(BaseModel):
    enabled: bool = True
    discovery_cron: str = "0 9 * * mon-fri"
    apply_cron: str = "30 10,15 * * mon-fri"
    misfire_grace_seconds: int = 600
    max_concurrent_browser_tasks: int = 1


class TrackerConfig(BaseModel):
    statuses: list[str] = Field(
        default_factory=lambda: [
            "Discovered",
            "Ready for Review",
            "Applied",
            "Outreach Sent",
            "Interview",
            "Rejected",
            "Ghosted",
        ]
    )
    ghosted_after_days: int = 14
    csv_export_path: str = "data/exports/applications.csv"


class Settings(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    quotas: QuotasConfig = Field(default_factory=QuotasConfig)
    copilot: CopilotConfig = Field(default_factory=CopilotConfig)
    resume: ResumeConfig = Field(default_factory=ResumeConfig)
    contacts: ContactsConfig = Field(default_factory=ContactsConfig)
    outreach: OutreachConfig = Field(default_factory=OutreachConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    tracker: TrackerConfig = Field(default_factory=TrackerConfig)

    root: Path = Field(default=REPO_ROOT, exclude=True)

    def path(self, relative: str) -> Path:
        """Resolve a config-relative path against the repo root."""
        p = Path(relative)
        return p if p.is_absolute() else self.root / p


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in extra.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def read_overrides(root: Path | None = None) -> dict[str, Any]:
    p = (root or REPO_ROOT) / OVERRIDES_FILE
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


def save_overrides(
    updates: dict[str, Any], root: Path | None = None, config_path: Path | str | None = None
) -> Settings:
    """Merge `updates` (nested dict, e.g. {"quotas": {"daily_caps": {"naukri": 10}}}) into the overrides file.

    The merged result is validated first, so an invalid value raises and nothing is written.
    """
    root = root or REPO_ROOT
    merged = _deep_merge(read_overrides(root), updates)
    with open(Path(config_path) if config_path else DEFAULT_CONFIG_PATH, encoding="utf-8") as fh:
        base = yaml.safe_load(fh) or {}
    settings = Settings.model_validate(_deep_merge(base, merged))
    p = root / OVERRIDES_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, p)
    settings.root = root
    return settings


def load_settings(config_path: Path | str | None = None, root: Path | None = None) -> Settings:
    load_dotenv(REPO_ROOT / ".env", override=False)
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if root is None and os.environ.get("LAA_ROOT"):
        root = Path(os.environ["LAA_ROOT"])  # alternate data root (tests, multiple instances)
    raw = _deep_merge(raw, read_overrides(root))
    settings = Settings.model_validate(raw)
    if root is not None:
        settings.root = root
    for rel in (
        settings.app.resumes_dir,
        settings.app.screenshots_dir,
        settings.app.browser_profile_dir,
        settings.app.exports_dir,
    ):
        settings.path(rel).mkdir(parents=True, exist_ok=True)
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
