"""Settings > Profile: every profile field, editable, plus building a profile from an uploaded resume."""

from __future__ import annotations

import json

import streamlit as st
from pydantic import ValidationError

from core.llm import LLMClient
from core.profile import MasterProfile
from core.profile_edit import build_profile, form_defaults, write_profile
from core.resume_import import profile_from_resume
from core.settings import save_overrides
from ui.components import get_profile, get_settings, profile_health, reload_settings
from ui.theme import card, title_block

_MODES = ["Remote", "Hybrid", "Onsite"]


def _bullet_text(role: dict) -> str:
    lines = []
    for b in role.get("bullets", []):
        line = b.get("text", "")
        if b.get("impact_metric"):
            line += " || " + b["impact_metric"]
        lines.append(line)
    return "\n".join(lines)


def _alias_text(aliases: dict[str, str]) -> str:
    return "\n".join(f"{a} = {b}" for a, b in aliases.items())


def _parse_aliases(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        alias, _, target = line.partition("=")
        if alias.strip() and target.strip():
            out[alias.strip()] = target.strip()
    return out


def _resume_card(settings) -> None:  # noqa: ANN001
    with card("resume_import"):
        title_block(
            "Start from a resume",
            "Upload a PDF or DOCX. The profile is filled from it, and nothing is saved until you confirm. "
            "With an AI key, the reading is much more accurate.",
        )
        uploaded = st.file_uploader("Resume", type=["pdf", "docx", "txt"], label_visibility="collapsed")
        if uploaded is not None and st.button("Read resume", type="primary", icon=":material/upload_file:"):
            try:
                payload = profile_from_resume(uploaded.name, uploaded.getvalue(), LLMClient(settings.llm))
                MasterProfile.model_validate(payload)
            except (ValidationError, ValueError) as e:
                st.error(f"Could not build a profile from that file: {e}")
            else:
                st.session_state["resume_draft"] = payload
                st.rerun()
        draft = st.session_state.get("resume_draft")
        if not draft:
            return
        basics = draft["basics"]
        st.success(
            f"Read {basics['full_name']} · {basics['email']} · {len(draft['experience'])} role(s). "
            "Check it below, then replace your profile or keep editing the form."
        )
        if st.button("Replace my profile with this", icon=":material/check:"):
            path = settings.path(settings.app.profile_path)
            write_profile(path, draft)
            st.session_state.pop("resume_draft", None)
            _reset_form()
            reload_settings()
            st.toast("Profile replaced. Your previous file was backed up.")
            st.rerun()
        if st.button("Discard this reading", type="tertiary"):
            st.session_state.pop("resume_draft", None)
            st.rerun()


def _roles(base_roles: list[dict]) -> list[dict]:
    blanks = st.session_state.get("profile_blank_roles", 0)
    roles = list(base_roles) + [
        {
            "company": "",
            "title": "",
            "location": "",
            "start_date": "2020-01",
            "end_date": "Present",
            "domain_tags": [],
            "bullets": [],
        }
        for _ in range(blanks)
    ]
    edited = []
    for i, role in enumerate(roles):
        label = role.get("company") or "New role"
        with st.expander(
            f"{label} · {role.get('title', '')}".strip(" ·"), expanded=i == 0 and not role.get("company")
        ):
            company = st.text_input("Company", role.get("company", ""), key=f"role_co_{i}")
            title = st.text_input("Title", role.get("title", ""), key=f"role_title_{i}")
            c1, c2, c3 = st.columns(3)
            location = c1.text_input("Location", role.get("location", ""), key=f"role_loc_{i}")
            start = c2.text_input("Start (YYYY-MM)", role.get("start_date", ""), key=f"role_start_{i}")
            end = c3.text_input("End (YYYY-MM or Present)", role.get("end_date", ""), key=f"role_end_{i}")
            tags = st.text_input(
                "Domain tags, comma separated", ", ".join(role.get("domain_tags", [])), key=f"role_tags_{i}"
            )
            bullets = st.text_area(
                "Bullets, one per line. Add || and a metric to keep the number.",
                _bullet_text(role),
                height=140,
                key=f"role_bullets_{i}",
            )
            remove = st.checkbox("Remove this role", key=f"role_rm_{i}")
            edited.append(
                {
                    **role,
                    "company": company,
                    "title": title,
                    "location": location,
                    "start_date": start,
                    "end_date": end,
                    "domain_tags_text": tags,
                    "bullets_text": bullets,
                    "remove": remove,
                }
            )
    return edited


def _blank_edu() -> dict:
    return {"institution": "", "degree": "", "location": "", "start_date": "", "end_date": "", "gpa": ""}


def _education(rows: list[dict]) -> list[dict]:
    blanks = st.session_state.get("profile_blank_edu", 0)
    rows = [*rows, *[_blank_edu() for _ in range(blanks)]]
    out = []
    for i, ed in enumerate(rows):
        with st.expander(ed.get("institution") or "New education", expanded=not ed.get("institution")):
            out.append(
                {
                    "institution": st.text_input("Institution", ed.get("institution", ""), key=f"edu_in_{i}"),
                    "degree": st.text_input("Degree", ed.get("degree", ""), key=f"edu_deg_{i}"),
                    "location": st.text_input("Location", ed.get("location", ""), key=f"edu_loc_{i}"),
                    "start_date": st.text_input("Start", ed.get("start_date", ""), key=f"edu_s_{i}"),
                    "end_date": st.text_input("End", ed.get("end_date", ""), key=f"edu_e_{i}"),
                    "gpa": st.text_input("GPA", ed.get("gpa", ""), key=f"edu_g_{i}"),
                }
            )
    return out


def _screening(rows: list[dict]) -> list[dict]:
    out = []
    for i, qa in enumerate(rows):
        with st.expander(qa.get("question_pattern", "Question")[:60]):
            out.append(
                {
                    "question_pattern": st.text_input(
                        "Question pattern", qa.get("question_pattern", ""), key=f"qa_q_{i}"
                    ),
                    "answer_guideline": st.text_area(
                        "How to answer", qa.get("answer_guideline", ""), key=f"qa_a_{i}"
                    ),
                }
            )
    return out


def _reset_form() -> None:
    for key in list(st.session_state):
        if str(key).startswith(("p_", "role_", "edu_", "skills_", "qa_", "sum_")):
            del st.session_state[key]


def render() -> None:
    settings = get_settings()
    profile = get_profile()
    defaults = form_defaults(profile)
    st.caption("These facts are the only things resumes, answers and outreach are allowed to say.")
    for warning in profile_health(profile):
        st.warning(warning, icon=":material/info:")
    _resume_card(settings)

    if st.button("Add a role", icon=":material/add:"):
        st.session_state["profile_blank_roles"] = st.session_state.get("profile_blank_roles", 0) + 1
    if st.button("Add education", icon=":material/add:"):
        st.session_state["profile_blank_edu"] = st.session_state.get("profile_blank_edu", 0) + 1

    with st.form("profile_editor"):
        title_block("You")
        c1, c2 = st.columns(2)
        full_name = c1.text_input("Full name", defaults["full_name"], key="p_full_name")
        headline = c2.text_input("Headline", defaults["headline"], key="p_headline")
        c1, c2 = st.columns(2)
        first = c1.text_input("First name", defaults["first_name"], key="p_first")
        last = c2.text_input("Last name", defaults["last_name"], key="p_last")
        c1, c2 = st.columns(2)
        email = c1.text_input("Email", defaults["email"], key="p_email")
        phone = c2.text_input("Phone", defaults["phone"], key="p_phone")
        c1, c2, c3 = st.columns(3)
        city = c1.text_input("City", defaults["city"], key="p_city")
        state = c2.text_input("State", defaults["state"], key="p_state")
        country = c3.text_input("Country", defaults["country"], key="p_country")
        postal = st.text_input("Postal code", defaults["postal_code"], key="p_postal")
        c1, c2, c3 = st.columns(3)
        linkedin = c1.text_input("LinkedIn", defaults["linkedin"], key="p_linkedin")
        github = c2.text_input("GitHub", defaults["github"], key="p_github")
        portfolio = c3.text_input("Portfolio", defaults["portfolio"], key="p_portfolio")
        summary = st.text_area("Summary", defaults["summary"], height=140, key="p_summary")

        st.divider()
        title_block("Work, pay and where you want to work")
        c1, c2 = st.columns(2)
        current_title = c1.text_input("Current title", defaults["current_title"], key="p_title")
        current_company = c2.text_input("Current company", defaults["current_company"], key="p_company")
        c1, c2 = st.columns(2)
        total_years = c1.number_input(
            "Total experience (years)", 0.0, 40.0, float(defaults["total_years"]), 0.5, key="p_years"
        )
        relevant_years = c2.number_input(
            "Relevant experience (years)",
            0.0,
            40.0,
            float(defaults["relevant_years"]),
            0.5,
            key="p_rel_years",
        )
        c1, c2, c3 = st.columns(3)
        current_ctc = c1.number_input(
            "Current CTC (LPA)", 0.0, 200.0, float(defaults["current_ctc"]), 0.5, key="p_ctc"
        )
        expected_ctc = c2.number_input(
            "Expected CTC (LPA)", 0.0, 200.0, float(defaults["expected_ctc"]), 0.5, key="p_ectc"
        )
        currency = c3.text_input("Currency", defaults["currency"], key="p_currency")
        c1, c2 = st.columns(2)
        notice = c1.number_input("Notice period (days)", 0, 180, int(defaults["notice_days"]), key="p_notice")
        serving = c2.checkbox("Serving notice", value=bool(defaults["serving_notice"]), key="p_serving")
        last_day = st.text_input(
            "Last working day (YYYY-MM-DD, optional)", defaults["last_working_day"], key="p_lwd"
        )
        modes = st.multiselect(
            "Work mode",
            _MODES,
            default=[m for m in defaults["work_modes"] if m in _MODES] or _MODES,
            key="p_modes",
        )
        locations = st.text_area(
            "Preferred locations (comma or new line)",
            defaults["preferred_locations"],
            height=80,
            key="p_locations",
        )
        roles_text = st.text_area(
            "Preferred roles to search (one per line)",
            "\n".join(settings.search.titles),
            height=110,
            key="p_roles",
            help="Discovery only keeps jobs in these families.",
        )
        relocate = st.checkbox("Willing to relocate", value=bool(defaults["relocate"]), key="p_relocate")
        work_auth = st.text_input("Work authorization", defaults["work_auth"], key="p_auth")
        sponsorship = st.checkbox(
            "Needs visa sponsorship", value=bool(defaults["sponsorship"]), key="p_sponsor"
        )
        c1, c2, c3 = st.columns(3)
        gender = c1.text_input("Gender", defaults["gender"], key="p_gender")
        veteran = c2.text_input("Veteran status", defaults["veteran"], key="p_veteran")
        disability = c3.text_input("Disability status", defaults["disability"], key="p_disability")

        st.divider()
        title_block("Skills", "One skill per line. Aliases must point at a skill in the lists.")
        skills: dict[str, list[str]] = {}
        for name, items in defaults["skills"].items():
            text = st.text_area(name.replace("_", " "), "\n".join(items), height=110, key=f"skills_{name}")
            skills[name] = [ln.strip() for ln in text.splitlines() if ln.strip()]
        new_group = st.text_input("New skill group name", key="p_new_group")
        new_skills = st.text_area("Skills in that group", height=80, key="p_new_skills")
        if new_group.strip():
            skills[new_group.strip().lower().replace(" ", "_")] = [
                ln.strip() for ln in new_skills.splitlines() if ln.strip()
            ]
        aliases = _parse_aliases(
            st.text_area(
                "Aliases (alias = skill)", _alias_text(defaults["aliases"]), height=110, key="p_aliases"
            )
        )

        st.divider()
        title_block("Experience")
        roles = _roles(defaults["roles"])
        st.divider()
        title_block("Education")
        education = _education(defaults["education"])

        with st.expander("Summary variants"):
            variants = {}
            for key, text in defaults["summary_variants"].items():
                variants[key] = st.text_area(key, text, height=80, key=f"sum_{key}")
        with st.expander("Screening answer guidelines"):
            screening = _screening(defaults["screening"])

        submitted = st.form_submit_button("Save profile", type="primary")

    if not submitted:
        _download(settings)
        return
    values = {
        **defaults,
        "full_name": full_name,
        "first_name": first,
        "last_name": last,
        "headline": headline,
        "email": email,
        "phone": phone,
        "city": city,
        "state": state,
        "country": country,
        "postal_code": postal,
        "linkedin": linkedin,
        "github": github,
        "portfolio": portfolio,
        "summary": summary,
        "summary_variants": variants,
        "total_years": total_years,
        "relevant_years": relevant_years,
        "current_company": current_company,
        "current_title": current_title,
        "current_ctc": current_ctc,
        "expected_ctc": expected_ctc,
        "currency": currency,
        "notice_days": notice,
        "serving_notice": serving,
        "last_working_day": last_day,
        "work_modes": modes,
        "preferred_locations": locations,
        "relocate": relocate,
        "work_auth": work_auth,
        "sponsorship": sponsorship,
        "gender": gender,
        "veteran": veteran,
        "disability": disability,
        "skills": skills,
        "aliases": aliases,
        "roles": roles,
        "education": education,
        "screening": screening,
    }
    try:
        payload = build_profile(values)
        write_profile(settings.path(settings.app.profile_path), payload)
    except (ValidationError, ValueError) as e:
        st.error(f"Not saved: {e}")
        return
    titles = [ln.strip() for ln in roles_text.splitlines() if ln.strip()]
    if titles and titles != list(settings.search.titles):
        save_overrides({"search": {"titles": titles}}, root=settings.root)
    st.session_state["profile_blank_roles"] = 0
    st.session_state["profile_blank_edu"] = 0
    _reset_form()
    reload_settings()
    st.toast("Profile saved")
    st.rerun()


def _download(settings) -> None:  # noqa: ANN001
    path = settings.path(settings.app.profile_path)
    with st.expander("Profile file"):
        st.download_button(
            "Download master_profile.json",
            path.read_bytes(),
            file_name="master_profile.json",
            mime="application/json",
            icon=":material/download:",
        )
        st.caption("Replacing the whole file is still possible. The form above edits the same document.")
        raw = st.file_uploader("Replace with a master_profile.json", type=["json"], key="prof_json")
        if raw is not None and st.button("Validate and replace file"):
            try:
                data = json.loads(raw.getvalue().decode("utf-8"))
                write_profile(path, data)
            except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as e:
                st.error(f"Not replaced: {e}")
            else:
                reload_settings()
                st.rerun()
