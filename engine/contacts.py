"""Contact & referral discovery.

1. Extract any recruiter / hiring manager named in the JD text (regex + email heuristics).
2. Build X-ray search queries (Google-style `site:linkedin.com/in`) and LinkedIn people-search URLs for
   one Recruiter/Talent Partner and one peer engineer at the company.
3. Infer the probable corporate email pattern from any email found in the JD or company domain.

Browser-based people lookup lives in copilot (Phase 4); this module is pure and testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus, urlparse

from core.models import JobPosting
from core.settings import ContactsConfig

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "protonmail.com", "icloud.com"}
# "Hiring manager: Priya Nair, Engineering Manager" / "Recruiter: Rahul Mehta" / "Contact Jane Doe (Talent Partner)"
_NAMED = re.compile(
    r"(?P<role>hiring manager|recruiter|talent (?:acquisition|partner)|engineering manager|contact|reach out to|poc)"
    r"\s*[:\-–]?\s*(?P<name>[A-Z][a-z]+(?:\s[A-Z][a-z]+){1,2})"
    r"(?:\s*[,(]\s*(?P<title>[A-Z][A-Za-z&/ ]{3,40}))?",
    re.IGNORECASE,
)
_NAME_BLACKLIST = {"job description", "about us", "equal opportunity", "the company", "apply now"}


@dataclass
class ContactLead:
    contact_type: str  # recruiter|peer|hiring_manager
    name: str
    role: str | None
    profile_url: str | None
    email_guess: str | None
    email_pattern: str | None
    source: str  # jd_text|xray_query|linkedin_search
    confidence: float
    search_urls: dict[str, str] = field(default_factory=dict)


def company_domain(posting: JobPosting) -> str | None:
    """Best-effort corporate domain: from JD emails, else from a non-ATS apply/careers URL."""
    for m in _EMAIL.finditer(posting.jd_text):
        dom = m.group(1).lower()
        if dom not in _FREE_MAIL and not any(
            a in dom for a in ("greenhouse", "lever", "ashby", "linkedin", "naukri")
        ):
            return dom
    for key in ("careers_url", "job_url", "hosted_url"):
        url = posting.raw_metadata.get(key)
        if url:
            host = urlparse(url).netloc.lower()
            if host and not any(
                a in host for a in ("greenhouse", "lever", "ashby", "linkedin", "naukri", "myworkdayjobs")
            ):
                return host.removeprefix("www.").removeprefix("jobs.").removeprefix("careers.")
    return None


def infer_email_pattern(sample_email: str | None, name: str | None) -> str | None:
    """Given 'rahul.mehta@acme.com' and 'Rahul Mehta' -> '{first}.{last}@acme.com'."""
    if not sample_email:
        return None
    local, _, dom = sample_email.lower().partition("@")
    local = re.sub(r"\d+$", "", local)
    parts = name.lower().split() if name else []
    if len(parts) >= 2:
        first, last = parts[0], parts[-1]
        candidates = {
            f"{first}.{last}": "{first}.{last}",
            f"{first}{last}": "{first}{last}",
            f"{first}_{last}": "{first}_{last}",
            f"{first[0]}{last}": "{f}{last}",
            f"{first}{last[0]}": "{first}{l}",
            f"{first}": "{first}",
            f"{last}.{first}": "{last}.{first}",
        }
        pat = candidates.get(local)
        if pat:
            return f"{pat}@{dom}"
    # name unknown or does not match the sample: infer from the local-part shape
    if "." in local:
        return f"{{first}}.{{last}}@{dom}"
    if "_" in local:
        return f"{{first}}_{{last}}@{dom}"
    if "-" in local:
        return f"{{first}}-{{last}}@{dom}"
    return None


def guess_email(pattern: str | None, name: str) -> str | None:
    if not pattern:
        return None
    parts = re.sub(r"[^a-z ]", "", name.lower()).split()
    if len(parts) < 2:
        return None
    first, last = parts[0], parts[-1]
    return pattern.format(first=first, last=last, f=first[0], l=last[0])  # noqa: E741


def extract_jd_contacts(posting: JobPosting, cfg: ContactsConfig) -> list[ContactLead]:
    leads: list[ContactLead] = []
    emails = [m.group(0) for m in _EMAIL.finditer(posting.jd_text)]
    corp_email = next((e for e in emails if e.split("@")[1].lower() not in _FREE_MAIL), None)
    seen: set[str] = set()
    for m in _NAMED.finditer(posting.jd_text):
        name = m.group("name").strip()
        if name.lower() in _NAME_BLACKLIST or name.lower() in seen:
            continue
        seen.add(name.lower())
        role_word = m.group("role").lower()
        title = (m.group("title") or "").strip() or None
        if "recruit" in role_word or "talent" in role_word:
            ctype = "recruiter"
        elif "manager" in role_word or (title and "manager" in title.lower()):
            ctype = "hiring_manager"
        else:
            ctype = "recruiter"
        pattern = infer_email_pattern(corp_email, name) if corp_email else None
        leads.append(
            ContactLead(
                contact_type=ctype,
                name=name,
                role=title or role_word.title(),
                profile_url=None,
                email_guess=guess_email(pattern, name) if pattern else None,
                email_pattern=pattern,
                source="jd_text",
                confidence=0.7 if title else 0.55,
                search_urls={"linkedin_people": linkedin_people_search(f"{name} {posting.company}")},
            )
        )
    return leads


def linkedin_people_search(keywords: str) -> str:
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(keywords)}"


def xray_queries(posting: JobPosting, cfg: ContactsConfig, city: str) -> dict[str, dict[str, str]]:
    """Return search URLs (Google + LinkedIn people search) for a recruiter and a peer at the company."""
    company = posting.company
    rec_q = cfg.xray_templates.get("recruiter", 'site:linkedin.com/in "{company}" Recruiter').format(
        company=company, city=city
    )
    peer_q = cfg.xray_templates.get("peer", 'site:linkedin.com/in "{company}" "Software Engineer"').format(
        company=company, city=city
    )
    rec_kw = " OR ".join(f'"{k}"' for k in cfg.recruiter_title_keywords[:3]) or "Recruiter"
    peer_kw = " OR ".join(f'"{k}"' for k in cfg.peer_title_keywords[:3]) or "Software Engineer"
    return {
        "recruiter": {
            "google": f"https://www.google.com/search?q={quote_plus(rec_q)}",
            "linkedin_people": linkedin_people_search(f"{company} {rec_kw}"),
        },
        "peer": {
            "google": f"https://www.google.com/search?q={quote_plus(peer_q)}",
            "linkedin_people": linkedin_people_search(f"{company} {peer_kw} {city}"),
        },
    }


def email_pattern_from_domain(domain: str | None, cfg: ContactsConfig) -> str | None:
    """Default to the most common corporate pattern when no sample is available."""
    if not domain:
        return None
    first = cfg.email_pattern_candidates[0] if cfg.email_pattern_candidates else "{first}.{last}"
    return f"{first}@{domain}"


def discover_contacts(
    posting: JobPosting, cfg: ContactsConfig, city: str
) -> tuple[list[ContactLead], dict[str, dict[str, str]]]:
    """Pure (no browser) discovery: JD-named contacts + search URLs + email pattern inference."""
    leads = extract_jd_contacts(posting, cfg)
    queries = xray_queries(posting, cfg, city)
    domain = company_domain(posting)
    default_pattern = next(
        (lead.email_pattern for lead in leads if lead.email_pattern), None
    ) or email_pattern_from_domain(domain, cfg)
    has_recruiter = any(lead.contact_type == "recruiter" for lead in leads)
    if not has_recruiter and cfg.max_recruiters > 0:
        leads.append(
            ContactLead(
                contact_type="recruiter",
                name=f"Recruiter at {posting.company}",
                role="Talent Acquisition (to identify)",
                profile_url=None,
                email_guess=None,
                email_pattern=default_pattern,
                source="xray_query",
                confidence=0.2,
                search_urls=queries["recruiter"],
            )
        )
    if cfg.max_peers > 0:
        leads.append(
            ContactLead(
                contact_type="peer",
                name=f"Engineer at {posting.company}",
                role="Software Engineer / Tech Lead (to identify)",
                profile_url=None,
                email_guess=None,
                email_pattern=default_pattern,
                source="xray_query",
                confidence=0.2,
                search_urls=queries["peer"],
            )
        )
    return leads, queries
