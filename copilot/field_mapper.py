"""Form field discovery and profile-value mapping.

`extract_fields(page)` walks the live DOM (inputs/selects/textareas/radios/checkboxes/file inputs) and returns a
list of `FormField`s with the best human-readable label it can find.

`map_fields(fields, ...)` is pure Python (testable without a browser): it maps each field to a value from
master_profile.json via config `copilot.profile_field_map`, the screening Q&A bank, or previously approved
answers, and attaches a confidence. Anything unresolved and required becomes an `unknown_question` friction.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from playwright.sync_api import Page
from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process

from core.llm import LLMClient, LLMUnavailable
from core.logging import log
from core.profile import MasterProfile
from core.repository import normalize_question

# ------------------------------------------------------------------------------------------------ models


@dataclass
class FormField:
    key: str  # stable handle used to locate the element again (data-laa-key)
    kind: str  # text|email|tel|number|url|textarea|select|radio|checkbox|file|date|hidden
    label: str
    name: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)
    placeholder: str = ""
    current_value: str = ""
    maxlength: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FieldMapping:
    field: FormField
    value: Any
    value_source: str  # profile:<path>|qa_bank|approved_answer|llm_guideline|resume_pdf|default|unresolved
    confidence: float
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.field.label,
            "kind": self.field.kind,
            "required": self.field.required,
            "value": self.value if self.field.kind != "file" else str(self.value),
            "value_source": self.value_source,
            "confidence": round(self.confidence, 3),
            "note": self.note,
        }


# ------------------------------------------------------------------------------------------------ DOM extraction

_EXTRACT_JS = r"""
() => {
  const seen = new Set();
  const out = [];
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return (r.width > 0 || r.height > 0 || el.type === 'file') && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const textOf = (el) => (el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '');
  const labelFor = (el) => {
    let t = '';
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); t = textOf(l); }
    if (!t && el.getAttribute('aria-label')) t = el.getAttribute('aria-label');
    if (!t && el.getAttribute('aria-labelledby')) {
      t = el.getAttribute('aria-labelledby').split(/\s+/).map(id => textOf(document.getElementById(id))).join(' ');
    }
    if (!t) { const l = el.closest('label'); t = textOf(l); }
    if (!t) {
      const grp = el.closest('fieldset, [role=group], [role=radiogroup], .form-group, .field, .application-question, .jobs-easy-apply-form-section__grouping, [class*=question]');
      if (grp) { const lg = grp.querySelector('legend, label, .label, h3, h4, [class*=label], [class*=question-text], span'); t = textOf(lg); }
    }
    if (!t) {
      let p = el.previousElementSibling; let hops = 0;
      while (p && hops < 3 && !t) { t = textOf(p); p = p.previousElementSibling; hops++; }
    }
    if (!t && el.placeholder) t = el.placeholder;
    if (!t && el.name) t = el.name.replace(/[_\-\[\]]+/g, ' ');
    return (t || '').slice(0, 300);
  };
  let i = 0;
  const els = document.querySelectorAll('input, select, textarea');
  for (const el of els) {
    const type = (el.getAttribute('type') || (el.tagName.toLowerCase() === 'input' ? 'text' : el.tagName)).toLowerCase();
    if (['hidden','submit','button','image','reset','search'].includes(type)) continue;
    if (!visible(el) && type !== 'file') continue;
    if (type === 'radio' || type === 'checkbox') {
      const groupKey = el.name || el.closest('fieldset, [role=radiogroup], [role=group]')?.id || labelFor(el);
      const gk = 'grp:' + type + ':' + groupKey;
      if (seen.has(gk)) continue;
      seen.add(gk);
      const members = el.name ? Array.from(document.querySelectorAll(`input[type="${type}"][name="${CSS.escape(el.name)}"]`)) : [el];
      const options = members.map(m => {
        let l = '';
        if (m.id) l = textOf(document.querySelector(`label[for="${CSS.escape(m.id)}"]`));
        if (!l) l = textOf(m.closest('label')) || m.value || m.getAttribute('aria-label') || '';
        return l;
      }).filter(Boolean);
      const key = 'laa-' + (i++);
      members.forEach(m => m.setAttribute('data-laa-group', key));
      const grp = el.closest('fieldset, [role=radiogroup], [role=group], .form-group, .field, [class*=question]');
      const legend = grp ? textOf(grp.querySelector('legend, label, h3, h4, [class*=label], [class*=question-text]')) : '';
      out.push({key, kind: type, label: legend || labelFor(el), name: el.name || '', required: el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(legend), options, placeholder: '', current_value: members.filter(m => m.checked).map(m => m.value).join(','), maxlength: null});
      continue;
    }
    const key = 'laa-' + (i++);
    el.setAttribute('data-laa-key', key);
    const kind = el.tagName.toLowerCase() === 'textarea' ? 'textarea' : el.tagName.toLowerCase() === 'select' ? 'select' : type;
    const options = kind === 'select' ? Array.from(el.options).map(o => o.text.trim()).filter(Boolean) : [];
    const lbl = labelFor(el);
    out.push({key, kind, label: lbl, name: el.name || (kind === 'file' ? (el.id || '') : ''), required: el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(lbl), options, placeholder: el.placeholder || '', current_value: kind === 'file' ? '' : (el.value || ''), maxlength: el.maxLength > 0 ? el.maxLength : null});
  }
  // custom comboboxes / typeaheads (Greenhouse/Ashby use react-select style widgets)
  for (const el of document.querySelectorAll('[role=combobox]:not(input), [class*=select__control]')) {
    if (!visible(el)) continue;
    const key = 'laa-' + (i++);
    el.setAttribute('data-laa-key', key);
    out.push({key, kind: 'combobox', label: labelFor(el), name: '', required: el.getAttribute('aria-required') === 'true', options: [], placeholder: '', current_value: textOf(el), maxlength: null});
  }
  return out;
}
"""


def extract_fields(page: Page, scope_selector: str | None = None) -> list[FormField]:
    if scope_selector:
        js = _EXTRACT_JS.replace(
            "document.querySelectorAll('input, select, textarea')",
            f"document.querySelector({scope_selector!r}).querySelectorAll('input, select, textarea')",
        )
        raw = page.evaluate(js)
    else:
        raw = page.evaluate(_EXTRACT_JS)
    fields = [FormField(**{k: v for k, v in r.items() if k in FormField.__dataclass_fields__}) for r in raw]
    # drop obvious noise
    return [f for f in fields if f.label or f.kind == "file"]


# ------------------------------------------------------------------------------------------------ mapping

_YES = {"yes", "y", "true", "i agree", "i do", "authorized", "yes, i am", "i am"}
_NO = {"no", "n", "false", "i do not", "not", "no, i am not"}
_RESUME_LABEL = re.compile(r"(?<![a-z])(resume|cv|curriculum)(?![a-z])", re.IGNORECASE)
_COVER_LABEL = re.compile(r"cover[ _-]?letter", re.IGNORECASE)
_QUESTIONY = re.compile(
    r"\?|why|describe|tell us|explain|how (do|would|did)|what (is|are|would)", re.IGNORECASE
)


class ScreeningAnswerOut(BaseModel):
    answer: str = Field(description="The final answer text, written in first person, concise")


_UNKNOWN = "UNKNOWN"
_OPEN_GUIDELINE = (
    "Answer only from CANDIDATE_FACTS. If the facts do not clearly support an answer, reply exactly "
    f"{_UNKNOWN}. For a number (years, CTC, days) reply with just the number. Years with a specific skill come "
    "from years_by_skill only, never from total_experience_years. For a 1-10 self-rating of a skill use 8 when "
    "years_by_skill shows 3 or more years, 7 for 1 to 3 years, 6 when the skill is listed but under 1 year. "
    "For a yes/no question reply Yes or No. If OPTIONS are given, reply with exactly one option, copied verbatim. "
    "Open-ended questions about motivation, interests, plans, strengths or approach are not facts to look up: "
    "answer them in 2 to 4 sentences that connect specific experience_bullets to the JOB. Reserve "
    f"{_UNKNOWN} for factual questions (dates, IDs, certifications, skills, availability) the facts do not cover."
)
_COVER_GUIDELINE = (
    "Write a cover letter of 120 to 180 words for the JOB, in first person, plain text, no greeting placeholders "
    "or brackets. Use only CANDIDATE_FACTS: name the current role and company, two concrete achievements from "
    "experience_bullets that fit the JOB, and the notice period. Never invent numbers, companies, or skills."
)
_SKILL_YEARS_Q = re.compile(
    r"\b(?:years?|yrs?)\b.*?\b(?:experience|exp)\b.*?\b(?:in|with|on|using)\s+(?P<skill>[\w.+#/ -]{1,40}?)\s*\??$",
    re.IGNORECASE,
)


def _months(start: str, end: str) -> int:
    from datetime import date

    sy, sm = (int(x) for x in start.split("-"))
    if end == "Present":
        today = date.today()
        ey, em = today.year, today.month
    else:
        ey, em = (int(x) for x in end.split("-"))
    return max(0, (ey - sy) * 12 + (em - sm) + 1)


def skill_years(profile: MasterProfile) -> dict[str, float]:
    """Years per skill, counted from the roles whose bullets name the skill (keyword or text)."""
    aliases = {a.lower(): v for a, v in profile.skills_taxonomy.aliases.items()}
    months: dict[str, int] = {}
    for e in profile.experience:
        n = _months(e.start_date, e.end_date)
        text = " ".join(b.text for b in e.bullets).lower()
        kws = {k.lower() for b in e.bullets for k in b.keywords}
        for skill in profile.skills_taxonomy.all_skills:
            names = {skill.lower()} | {a for a, v in aliases.items() if v == skill}
            if names & kws or any(re.search(rf"(?<!\w){re.escape(x)}(?!\w)", text) for x in names):
                months[skill] = months.get(skill, 0) + n
    cap = profile.application_defaults.total_experience_years
    return {s: min(round(m / 12, 1), cap) for s, m in months.items()}


def _resolve_skill(profile: MasterProfile, term: str) -> str | None:
    term = term.strip(" ?.").strip()
    if not term:
        return None
    if hit := profile.canonical_skill(term):
        return hit

    # whole-name comparison: partial scorers match "Java" to "JavaScript (ES6+)"
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9+#]", "", re.sub(r"\([^)]*\)", "", s.lower()))

    names = {norm(s): s for s in profile.skills_taxonomy.all_skills}
    best = process.extractOne(norm(term), list(names), scorer=fuzz.ratio)
    return names[best[0]] if best and best[1] >= 90 else None


_SCREENING_SYSTEM = """You answer a job-application screening question for the candidate.
Use ONLY the CANDIDATE_FACTS and follow the ANSWER_GUIDELINE exactly. Never invent numbers, companies, or skills.
If the guideline references a profile field, use its value verbatim. Keep it under 90 words unless a number/short
phrase is clearly expected (then answer with just that)."""


def answer_question(
    profile: MasterProfile,
    field_map: dict[str, str],
    question: str,
    *,
    options: list[str] | None = None,
    llm: LLMClient | None = None,
    approved_answers: dict[str, str] | None = None,
    job_context: str = "",
) -> str | None:
    """Answer one screening question from the profile. None means we must not guess.

    Chat options are often suggestion chips ("Skip", "Yes") next to a free-text box, so an answer that matches
    no option is still returned as text; the adapter types it when no chip fits.
    """
    mapper = FieldMapper(
        profile, field_map, llm=llm, approved_answers=approved_answers, job_context=job_context
    )
    mapping = mapper.map_fields(
        [FormField("q", "radio" if options else "text", question, required=True, options=list(options or []))]
    )[0]
    if options and (mapping.value_source == "unresolved" or mapping.value in (None, "")):
        mapping = mapper.map_fields([FormField("q", "text", question, required=True)])[0]
    if mapping.value_source == "unresolved" or mapping.value in (None, ""):
        return None
    text = str(mapping.value).strip()
    if re.search(r"ctc|salary|compensation|package", question, re.IGNORECASE) and re.fullmatch(
        r"0+(?:\.0+)?(?:\s+\w+)*", text
    ):
        return None
    return text


def _fmt_value(value: Any, kind: str) -> Any:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(value)
    return value if value is not None else ""


def _is_yes_no(options: list[str]) -> bool:
    lows = {o.strip().lower() for o in options}
    return bool(lows & _YES) and bool(lows & _NO)


def _match_option(options: list[str], desired: Any) -> tuple[str | None, float]:
    """Pick the option best matching the desired value. Returns (option, confidence)."""
    if not options:
        return None, 0.0
    d = str(desired).strip().lower()
    if isinstance(desired, bool) or d in _YES | _NO:
        want_yes = desired is True or d in _YES
        for o in options:
            ol = o.strip().lower()
            if (want_yes and (ol in _YES or ol.startswith("yes"))) or (
                not want_yes and (ol in _NO or ol.startswith("no"))
            ):
                return o, 0.95
    # exact / contains
    for o in options:
        if o.strip().lower() == d:
            return o, 0.98
    for o in options:
        if d and (d in o.lower() or o.lower() in d):
            return o, 0.9
    best = process.extractOne(d, options, scorer=fuzz.WRatio)
    if best and best[1] >= 85:
        return best[0], round(best[1] / 100 * 0.9, 2)
    return None, 0.0


class FieldMapper:
    def __init__(
        self,
        profile: MasterProfile,
        field_map: dict[str, str],
        *,
        llm: LLMClient | None = None,
        approved_answers: dict[str, str] | None = None,
        resume_pdf: str | None = None,
        job_context: str = "",
    ) -> None:
        self.profile = profile
        self.patterns = [(re.compile(pat, re.IGNORECASE), path) for pat, path in field_map.items()]
        self.llm = llm
        self.approved = {normalize_question(k): v for k, v in (approved_answers or {}).items()}
        self.resume_pdf = resume_pdf
        self.job_context = job_context

    # -------------------------------------------------------------- profile lookups

    def _profile_value_for(self, label: str) -> tuple[str, Any] | None:
        # "What AI tools have you used (GitHub, Codex...)?" mentions github but is not asking for the profile URL
        questiony = "?" in label or len(label) > 60
        for rx, path in self.patterns:
            if questiony and path.startswith("basics."):
                continue
            if rx.search(label):
                try:
                    return path, self.profile.get_path(path)
                except KeyError:
                    continue
        return None

    def _approved_for(self, label: str) -> str | None:
        """Your answer to this question or a reworded version of it ("Notice period?" / "What is your notice period")."""
        q = normalize_question(label)
        if not q or not self.approved:
            return None
        if q in self.approved:
            return self.approved[q]
        best = process.extractOne(q, list(self.approved), scorer=fuzz.token_sort_ratio)
        return self.approved[best[0]] if best and best[1] >= 90 else None

    def _screening_answer(self, label: str, f: FormField | None = None) -> tuple[str, str, float] | None:
        if (ans := self._approved_for(label)) is not None:
            return ans, "approved_answer", 0.92
        qa = self.profile.find_screening_answer(label)
        ad = self.profile.application_defaults
        if qa is not None:
            # guidelines that just point at defaults are deterministic
            if "notice_period" in qa.answer_guideline:
                return f"{ad.notice_period_days} days", "qa_bank", 0.9
            if "ctc" in qa.answer_guideline.lower():
                if re.search(r"current", label, re.IGNORECASE):
                    return f"{ad.current_ctc_lpa:g} LPA {ad.currency}", "qa_bank", 0.85
                return f"{ad.expected_ctc_lpa:g} LPA {ad.currency}", "qa_bank", 0.85
        elif f is None or not (f.required or f.kind == "textarea" or _QUESTIONY.search(label)):
            # optional, non-question fields (address line 2, middle name) are left blank rather than guessed
            return None
        guideline = qa.answer_guideline if qa is not None else _OPEN_GUIDELINE
        answer = self._ask_llm(label, f.options if f is not None else [], guideline)
        if answer is None:
            return None
        return answer, "llm_guideline", 0.8

    def _ask_llm(self, label: str, options: list[str], guideline: str) -> str | None:
        if self.llm is None or not self.llm.available:
            return None
        opts = f"\n\nOPTIONS: {options}" if options else ""
        job = f"\n\nJOB: {self.job_context}" if self.job_context else ""
        try:
            out = self.llm.structured(
                system=_SCREENING_SYSTEM,
                user=(
                    f"QUESTION: {label}{opts}\n\nANSWER_GUIDELINE: {guideline}{job}\n\n"
                    f"CANDIDATE_FACTS: {self._facts()}"
                ),
                schema=ScreeningAnswerOut,
                tool_name="submit_answer",
                description="Submit the screening answer.",
            )
        except (LLMUnavailable, ValueError) as e:
            log.warning("screening answer via LLM failed: {}", e)
            return None
        answer = out.answer.strip()
        # a partly-unknown answer ("React: 3 years. Vite: UNKNOWN") is not something to send
        if not answer or _UNKNOWN in answer.upper():
            return None
        return answer

    def _llm_option(self, f: FormField, label: str) -> FieldMapping | None:
        """Options that do not literally match the profile value (30 days vs "1 Month"): let the model pick one."""
        answer = self._ask_llm(label, f.options, _OPEN_GUIDELINE)
        opt, conf = _match_option(f.options, answer) if answer else (None, 0.0)
        if opt is None:
            return None
        return FieldMapping(f, opt, "llm_guideline", min(0.8, conf))

    def _facts(self) -> dict[str, Any]:
        ad = self.profile.application_defaults
        b = self.profile.basics
        return {
            "headline": b.headline,
            "location": f"{b.location.city}, {b.location.country}",
            "total_experience_years": ad.total_experience_years,
            "current_title": ad.current_title,
            "current_company": ad.current_company,
            "current_ctc_lpa": ad.current_ctc_lpa,
            "expected_ctc_lpa": ad.expected_ctc_lpa,
            "notice_period_days": ad.notice_period_days,
            "is_serving_notice": ad.is_serving_notice,
            "last_working_day": ad.last_working_day or "not serving notice",
            "work_authorization_india": ad.work_authorization.india,
            "requires_sponsorship": ad.work_authorization.requires_sponsorship,
            "work_mode_preferences": ad.work_mode_preferences,
            "preferred_locations": ad.preferred_locations,
            "willing_to_relocate": ad.willing_to_relocate,
            "education": [f"{e.degree}, {e.institution} ({e.end_date})" for e in self.profile.education],
            "roles": [
                f"{e.title} at {e.company} ({e.start_date} to {e.end_date})" for e in self.profile.experience
            ],
            "skills": self.profile.skills_taxonomy.categories,
            "years_by_skill": skill_years(self.profile),
            "experience_bullets": [b.text for e in self.profile.experience for b in e.bullets],
        }

    # -------------------------------------------------------------- main

    def map_fields(self, fields: list[FormField]) -> list[FieldMapping]:
        # Ashby's unlabeled "autofill from resume" uploader re-renders the whole form when given a file, which
        # detaches every field mapped after it; with a labeled resume input present it is left alone
        labeled_resume = any(f.kind == "file" and _RESUME_LABEL.search(f"{f.label} {f.name}") for f in fields)
        out: list[FieldMapping] = []
        for f in fields:
            if labeled_resume and f.kind == "file" and not f.label.strip() and not f.required:
                out.append(FieldMapping(f, None, "default", 0.9, "autofill uploader skipped"))
                continue
            out.append(self._map_one(f))
        return out

    def _map_one(self, f: FormField) -> FieldMapping:
        label = (f.label or f.name or "").strip().strip("\"'“”").strip()
        # files
        if f.kind == "file":
            # Greenhouse labels both upload buttons "Attach"; the input id (resume / cover_letter) says which
            ident = f"{label} {f.name}"
            if _COVER_LABEL.search(ident):
                return FieldMapping(
                    f, None, "default", 0.9 if not f.required else 0.3, "cover letter skipped"
                )
            if self.resume_pdf and (_RESUME_LABEL.search(ident) or not label):
                return FieldMapping(f, self.resume_pdf, "resume_pdf", 0.95)
            return FieldMapping(f, None, "unresolved", 0.0, "unknown file field")

        # already filled by the portal (e.g. prefilled email) -> keep
        if f.current_value and f.kind not in ("radio", "checkbox", "select"):
            return FieldMapping(f, f.current_value, "prefilled", 0.9, "kept portal-prefilled value")

        # "years of experience in <skill>": counted from the roles that used the skill, never total experience
        if (m := _SKILL_YEARS_Q.search(label)) and f.kind not in ("checkbox", "file"):
            skill = _resolve_skill(self.profile, m.group("skill"))
            years = skill_years(self.profile).get(skill) if skill else None
            if years:
                value = f"{years:g}"
                if f.kind in ("select", "radio"):
                    opt, conf = _match_option(f.options, value)
                    if opt:
                        return FieldMapping(f, opt, "profile:skill_years", conf)
                    if picked := self._llm_option(f, label):
                        return picked
                    return FieldMapping(f, None, "unresolved", 0.0, f"no option matched {value!r}")
                return FieldMapping(f, value, "profile:skill_years", 0.9)
            ans = self._screening_answer(label, f)
            if ans:
                return FieldMapping(f, ans[0], ans[1], ans[2])
            return FieldMapping(f, None, "unresolved", 0.0, "no role in the profile shows this skill")

        # profile direct maps
        hit = self._profile_value_for(label)
        if hit:
            path, value = hit
            if f.kind in ("select", "radio", "checkbox"):
                opt, conf = _match_option(f.options, _fmt_value(value, f.kind))
                if not opt and isinstance(value, str) and _is_yes_no(f.options):
                    # e.g. "Authorized to work for any employer" -> Yes
                    negative = re.search(r"\b(not|no|none|require[sd]?)\b", value, re.IGNORECASE) is not None
                    opt, conf = _match_option(f.options, not negative)
                    conf = min(conf, 0.9)
                if opt:
                    return FieldMapping(f, opt, f"profile:{path}", conf)
                if picked := self._llm_option(f, label):
                    return picked
                return FieldMapping(f, None, "unresolved", 0.0, f"no option matched {value!r}")
            return FieldMapping(f, _fmt_value(value, f.kind), f"profile:{path}", 0.95)

        if (
            f.kind == "textarea"
            and _COVER_LABEL.search(label)
            and (text := self._ask_llm(label, [], _COVER_GUIDELINE))
        ):
            return FieldMapping(f, text, "llm_guideline", 0.8, "cover letter written from the profile")

        # consent / acknowledgement checkboxes
        if f.kind == "checkbox" and re.search(
            r"(agree|consent|acknowledge|privacy|terms|certify|accurate)", label, re.IGNORECASE
        ):
            opt = f.options[0] if f.options else "Yes"
            return FieldMapping(f, opt, "default", 0.85, "consent checkbox")

        # yes/no questions about things we can infer safely
        if f.kind in ("select", "radio") and {o.lower() for o in f.options} & _YES:
            ad = self.profile.application_defaults
            lower = label.lower()
            if "sponsor" in lower:
                opt, c = _match_option(f.options, ad.work_authorization.requires_sponsorship)
                return FieldMapping(
                    f, opt, "profile:application_defaults.work_authorization.requires_sponsorship", c
                )
            if "relocat" in lower:
                opt, c = _match_option(f.options, ad.willing_to_relocate)
                return FieldMapping(f, opt, "profile:application_defaults.willing_to_relocate", c)
            if re.search(
                r"(18|eighteen) years|legally (eligible|authori[sz]ed)|authori[sz]ed to work", lower
            ):
                opt, c = _match_option(f.options, True)
                return FieldMapping(f, opt, "profile:application_defaults.work_authorization.india", c * 0.95)

        # screening questions
        ans = self._screening_answer(label, f)
        if ans:
            text, src, conf = ans
            if f.kind in ("select", "radio", "checkbox"):
                opt, oconf = _match_option(f.options, text)
                if opt:
                    return FieldMapping(f, opt, src, min(conf, oconf))
                if picked := self._llm_option(f, label):
                    return picked
                return FieldMapping(f, None, "unresolved", 0.0, f"no option matched answer {text!r}")
            if f.maxlength and len(text) > f.maxlength:
                text = text[: f.maxlength - 1].rstrip() + "."
            return FieldMapping(f, text, src, conf)

        # unresolved
        note = (
            "unknown screening question"
            if (_QUESTIONY.search(label) or f.kind == "textarea")
            else "no mapping"
        )
        return FieldMapping(f, None, "unresolved", 0.0, note)


def summarize(mappings: list[FieldMapping]) -> dict[str, Any]:
    required = [m for m in mappings if m.field.required]
    unresolved_required = [m for m in required if m.value_source == "unresolved"]
    min_conf = min((m.confidence for m in required if m.value_source != "unresolved"), default=1.0)
    return {
        "total": len(mappings),
        "required": len(required),
        "unresolved_required": [m.field.label for m in unresolved_required],
        "unresolved_optional": [
            m.field.label for m in mappings if not m.field.required and m.value_source == "unresolved"
        ],
        "min_required_confidence": round(min_conf if required else 1.0, 3),
    }
