"""Build a master profile from an uploaded resume (PDF, DOCX, or text).

With an AI key, Claude extracts the structured profile and is told to use only words from the resume.
Without one, a local reading fills in the contact details, skills and roles it can see, and leaves CTC and
notice for you to type. Either way nothing is saved until you confirm it in Settings.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

from core.llm import LLMClient
from core.profile_edit import ImportedProfile, _year_month, build_profile

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?:\+\d{1,3}[\s-]?)?(?:\d[\s-]?){10,13}")
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract_text(filename: str, data: bytes) -> str:
    ext = filename.lower().rsplit(".", 1)[-1]
    if ext == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if ext == "docx":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml")
        root = ET.fromstring(xml)
        paragraphs = []
        for para in root.iter(f"{_W_NS}p"):
            paragraphs.append("".join(node.text or "" for node in para.iter(f"{_W_NS}t")))
        return "\n".join(paragraphs)
    return data.decode("utf-8", errors="replace")


def _heuristic(text: str) -> ImportedProfile:
    lines = [ln.strip().lstrip("-•").strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    email = _EMAIL.search(text)
    if email is None:
        raise ValueError("No email address found in the resume. Add one to the file, or fill the form below.")
    phone = _PHONE.search(text)
    name = next((ln for ln in lines if "@" not in ln and len(ln) < 60), "Your Name")
    headline = next((ln for ln in lines if ln != name and "@" not in ln), name)
    skills = _section_items(lines, ("skill", "technical"))
    roles = _roles(lines, headline)
    education = _education(lines)
    years = _years(text)
    return ImportedProfile(
        full_name=name,
        email=email.group(0),
        phone=phone.group(0).strip() if phone else "",
        headline=headline[:160],
        summary=headline,
        total_experience_years=years,
        skills=skills[:40],
        roles=roles,
        education=education,
        city=_guess_city(lines),
    )


def _is_header(line: str, headers: tuple[str, ...]) -> bool:
    token = line.lower().strip(":").strip()
    return any(
        token == h or token.startswith(h + " ") or (token.startswith(h) and len(token) <= len(h) + 1)
        for h in headers
    )


def _section_items(lines: list[str], headers: tuple[str, ...]) -> list[str]:
    items: list[str] = []
    capture = False
    for ln in lines:
        if _is_header(ln, headers):
            capture = True
            rest = ln.split(":", 1)[1] if ":" in ln else ""
            items += [p.strip() for p in rest.split(",") if p.strip()]
            continue
        if capture and (
            len(ln) > 80 or ln.lower().rstrip(":") in ("experience", "education", "projects", "work")
        ):
            break
        if capture:
            items += [p.strip() for p in re.split(r"[,|•]", ln) if p.strip()]
    return list(dict.fromkeys(items))


def _roles(lines: list[str], headline: str):
    from core.profile_edit import ImportedRole

    dated = [ln for ln in lines if re.search(r"(?:19|20)\d{2}", ln) and len(ln) < 80]
    bullets = [ln for ln in lines if len(ln) > 80]
    if not dated:
        return [
            ImportedRole(
                company="Add company",
                title=headline[:80] or "Engineer",
                start_date="2020-01",
                end_date="Present",
                bullets=bullets[:8],
            )
        ]
    roles = []
    for ln in dated[:6]:
        years = re.findall(r"(?:19|20)\d{2}", ln)
        start = _year_month(years[0], "2020-01")
        end = (
            "Present"
            if re.search(r"present|current", ln, re.I) or len(years) == 1
            else _year_month(years[-1], start)
        )
        title = re.sub(r"[\d\-/–,|]+", " ", ln)
        title = re.sub(r"\s+", " ", title).strip(" -") or headline
        roles.append(ImportedRole(company=title[:80], title=headline[:80], start_date=start, end_date=end))
    if roles and all(r.end_date != "Present" for r in roles):
        roles[0].end_date = "Present"
    roles[0].bullets = bullets[:8]
    return roles


def _education(lines: list[str]):
    from core.profile_edit import ImportedEducation

    found = []
    for ln in lines:
        if re.search(r"university|college|institute|bachelor|master|b\.?\s?tech|m\.?\s?tech", ln, re.I):
            found.append(ImportedEducation(institution=ln[:120], degree=ln[:120]))
    return found[:3]


def _years(text: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\+?\s*(?:years|yrs)", text, re.I)
    return float(m.group(1)) if m else 0


def _guess_city(lines: list[str]) -> str:
    for city in ("Bangalore", "Bengaluru", "Hyderabad", "Pune", "Mumbai", "Gurugram", "Delhi", "Chennai"):
        if any(city.lower() in ln.lower() for ln in lines[:12]):
            return city
    return ""


_SYSTEM = (
    "Extract a job-application profile from the resume. Every string you return must appear in the resume; "
    "do not invent employers, dates, skills, metrics, or compensation. Use YYYY-MM dates and 'Present' for the "
    "current role. Put the current role first. If CTC or notice period is not stated, use 0 and 30. "
    "If the email is missing, return an empty email."
)


def profile_from_resume(filename: str, data: bytes, llm: LLMClient | None) -> dict:
    text = extract_text(filename, data).strip()
    if len(text) < 40:
        raise ValueError(
            "Could not read text from that file. Export the resume as PDF or DOCX and try again."
        )
    draft = _from_llm(text, llm) if llm is not None and llm.available else None
    if draft is None:
        draft = _heuristic(text)
    if not draft.email:
        raise ValueError("No email address found in the resume. Add one to the file, or fill the form below.")
    return _to_payload(draft)


def _from_llm(text: str, llm: LLMClient) -> ImportedProfile | None:
    try:
        return llm.structured(
            system=_SYSTEM,
            user=text[:20000],
            schema=ImportedProfile,
            tool_name="save_profile",
            description="Profile facts copied from the resume",
        )
    except Exception:  # noqa: BLE001 - a failed extraction falls back to the local reading
        return None


def _to_payload(draft: ImportedProfile) -> dict:
    parts = draft.full_name.split()
    first, last = (parts[0], " ".join(parts[1:])) if parts else ("", "")
    roles = []
    for i, role in enumerate(draft.roles):
        end = _year_month(role.end_date, "Present" if i == 0 else "2020-01")
        roles.append(
            {
                "company": role.company.strip(),
                "title": role.title.strip() or draft.headline[:80],
                "location": role.location,
                "start_date": _year_month(role.start_date, "2020-01"),
                "end_date": end,
                "domain_tags": [],
                "bullets": [
                    {"text": b.strip(), "keywords": [], "impact_metric": ""}
                    for b in role.bullets
                    if b.strip()
                ],
            }
        )
    if not roles:
        roles = [
            {
                "company": "Add company",
                "title": draft.headline[:80] or "Engineer",
                "location": "",
                "start_date": "2020-01",
                "end_date": "Present",
                "domain_tags": [],
                "bullets": [],
            }
        ]
    if not any(r["end_date"] == "Present" for r in roles):
        roles[0]["end_date"] = "Present"
    current = next(r for r in roles if r["end_date"] == "Present")
    values = {
        "full_name": draft.full_name.strip(),
        "first_name": first,
        "last_name": last,
        "headline": draft.headline.strip() or current["title"],
        "email": draft.email.strip(),
        "phone": draft.phone.strip(),
        "city": draft.city.strip() or "City",
        "state": "",
        "country": draft.country.strip() or "India",
        "postal_code": "",
        "linkedin": draft.linkedin.strip(),
        "github": draft.github.strip(),
        "portfolio": "",
        "summary": draft.summary.strip() or draft.headline,
        "summary_variants": {},
        "total_years": draft.total_experience_years or 0,
        "relevant_years": draft.total_experience_years or 0,
        "current_company": current["company"],
        "current_title": current["title"],
        "current_ctc": draft.current_ctc_lpa,
        "expected_ctc": draft.expected_ctc_lpa,
        "currency": "INR",
        "notice_days": draft.notice_period_days,
        "serving_notice": False,
        "last_working_day": "",
        "work_auth": "",
        "sponsorship": False,
        "work_modes": ["Remote", "Hybrid", "Onsite"],
        "preferred_locations": ", ".join(draft.preferred_locations),
        "relocate": False,
        "gender": "",
        "veteran": "",
        "disability": "",
        "skills": {"core": draft.skills} if draft.skills else {"core": ["Add your skills"]},
        "aliases": {},
        "roles": roles,
        "education": [e.model_dump() for e in draft.education],
        "projects": [],
        "certifications": [],
        "screening": [],
    }
    return build_profile(values)
