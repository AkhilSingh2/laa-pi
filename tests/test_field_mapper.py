from __future__ import annotations

from copilot.field_mapper import FieldMapper, FormField, summarize


def _mapper(settings, profile, **kw) -> FieldMapper:
    return FieldMapper(profile, settings.copilot.profile_field_map, resume_pdf="/tmp/resume.pdf", **kw)


def test_direct_profile_fields(settings, profile):
    m = _mapper(settings, profile)
    fields = [
        FormField("k1", "text", "First Name *", required=True),
        FormField("k2", "text", "Last Name", required=True),
        FormField("k3", "email", "Email", required=True),
        FormField("k4", "tel", "Phone", required=True),
        FormField("k5", "url", "LinkedIn Profile"),
        FormField("k6", "text", "Current Company"),
        FormField("k7", "number", "Notice period (days)"),
        FormField("k8", "file", "Resume/CV *", required=True),
        FormField("k9", "file", "Cover Letter"),
    ]
    out = m.map_fields(fields)
    by = {o.field.key: o for o in out}
    assert by["k1"].value == "Alex" and by["k1"].confidence >= 0.9
    assert by["k2"].value == "Morgan"
    assert by["k3"].value == "alex.morgan@example.com"
    assert by["k4"].value.startswith("+91")
    assert by["k5"].value.startswith("https://www.linkedin.com")
    assert by["k6"].value == "Acme Studios"
    assert by["k7"].value == "30"
    assert by["k8"].value == "/tmp/resume.pdf" and by["k8"].value_source == "resume_pdf"
    assert by["k9"].value is None and by["k9"].value_source == "default"
    s = summarize(out)
    assert s["unresolved_required"] == [] and s["min_required_confidence"] >= 0.9


def test_select_and_radio_option_matching(settings, profile):
    m = _mapper(settings, profile)
    fields = [
        FormField(
            "s1",
            "select",
            "Will you now or in the future require sponsorship?",
            required=True,
            options=["Select...", "Yes", "No"],
        ),
        FormField("s2", "radio", "Are you willing to relocate?", required=True, options=["Yes", "No"]),
        FormField("s3", "select", "Gender", options=["Male", "Female", "Decline to self-identify"]),
        FormField("s4", "select", "Country", options=["United States", "India", "Germany"]),
        FormField(
            "s5",
            "checkbox",
            "I agree to the privacy policy",
            required=True,
            options=["I agree to the privacy policy"],
        ),
        FormField(
            "s6",
            "select",
            "Are you legally authorized to work in India?",
            required=True,
            options=["Yes", "No"],
        ),
    ]
    by = {o.field.key: o for o in m.map_fields(fields)}
    assert by["s1"].value == "No"
    assert by["s2"].value == "Yes"
    assert by["s3"].value == "Male"
    assert by["s4"].value == "India"
    assert by["s5"].value_source == "default" and by["s5"].value
    assert by["s6"].value == "Yes"


def test_screening_bank_and_unknown_questions(settings, profile):
    m = _mapper(settings, profile, approved_answers={"Do you have a valid passport?": "Yes"})
    fields = [
        FormField("q1", "text", "What is your notice period?", required=True),
        FormField("q2", "text", "Expected CTC (LPA)", required=True),
        FormField("q3", "textarea", "Describe a time you disagreed with your manager.", required=True),
        FormField("q4", "text", "Do you have a valid passport?", required=True),
        FormField(
            "q5", "textarea", "Why do you want to join us?", required=False
        ),  # needs LLM -> unresolved without key
    ]
    out = m.map_fields(fields)
    by = {o.field.key: o for o in out}
    assert (
        by["q1"].value == "30" and by["q1"].value_source == "profile:application_defaults.notice_period_days"
    )
    expected = f"{profile.application_defaults.expected_ctc_lpa:g}"
    assert (
        by["q2"].value == expected
        and by["q2"].value_source == "profile:application_defaults.expected_ctc_lpa"
    )
    assert by["q3"].value_source == "unresolved" and "unknown" in by["q3"].note
    assert by["q4"].value == "Yes" and by["q4"].value_source == "approved_answer"
    assert by["q5"].value_source == "unresolved"
    s = summarize(out)
    assert s["unresolved_required"] == ["Describe a time you disagreed with your manager."]
    assert "Why do you want to join us?" in s["unresolved_optional"]


def test_chat_answers_come_from_the_profile(settings, profile):
    from copilot.field_mapper import answer_question

    field_map = settings.copilot.profile_field_map
    assert answer_question(profile, field_map, "What is your notice period?") == "30"
    current = profile.application_defaults.current_ctc_lpa
    assert answer_question(profile, field_map, "Current CTC in LPA?") == (f"{current:g}" if current else None)
    assert answer_question(profile, field_map, "Are you willing to relocate?", options=["Yes", "No"]) == "Yes"


def test_chat_answer_is_typed_when_only_skip_chips_are_shown(settings, profile):
    from copilot.field_mapper import answer_question

    field_map = settings.copilot.profile_field_map
    assert (
        answer_question(profile, field_map, "What is your notice period?", options=["Skip this question"])
        == "30"
    )


def test_chat_uses_answers_you_approved(settings, profile):
    from copilot.field_mapper import answer_question

    answer = answer_question(
        profile,
        settings.copilot.profile_field_map,
        "Do you have a valid passport?",
        options=["Skip"],
        approved_answers={"Do you have a valid passport?": "Yes"},
    )
    assert answer == "Yes"


class _FakeLLM:
    available = True

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    def structured(self, **kw):  # noqa: ANN003
        from copilot.field_mapper import ScreeningAnswerOut

        self.calls += 1
        return ScreeningAnswerOut(answer=self.reply)


def test_greenhouse_attach_buttons_are_told_apart_by_input_id(settings, profile):
    out = _mapper(settings, profile).map_fields(
        [
            FormField("r", "file", "Attach", name="resume", required=True),
            FormField("c", "file", "Attach", name="cover_letter"),
        ]
    )
    assert out[0].value == "/tmp/resume.pdf" and out[0].value_source == "resume_pdf"
    assert out[1].value is None and out[1].note == "cover letter skipped"


def test_questions_mentioning_github_do_not_get_the_profile_url(settings, profile):
    out = _mapper(settings, profile).map_fields(
        [
            FormField(
                "g", "text", "What GenAI or AI-assisted development tools have you used (GitHub, Codex)?"
            ),
            FormField("h", "url", "GitHub", required=True),
            FormField("q", "text", '"How Many years of Experience you have in React? "', required=True),
        ]
    )
    assert out[0].value_source == "unresolved"
    assert out[1].value_source == "profile:basics.links.github"
    assert out[2].value_source == "profile:skill_years"


def test_ashby_autofill_uploader_is_skipped_when_a_resume_field_exists(settings, profile):
    out = _mapper(settings, profile).map_fields(
        [
            FormField("a", "file", ""),
            FormField("r", "file", "Resume", name="_systemfield_resume", required=True),
        ]
    )
    assert out[0].value is None and out[0].note == "autofill uploader skipped"
    assert out[1].value == "/tmp/resume.pdf"


def test_skill_years_come_from_roles_not_total_experience(settings, profile):
    from copilot.field_mapper import skill_years

    years = skill_years(profile)
    total = profile.application_defaults.total_experience_years
    assert years and all(0 < y <= total for y in years.values())
    react = years["React"]
    out = _mapper(settings, profile).map_fields(
        [FormField("y", "text", "How many years of experience do you have in REACT?", required=True)]
    )[0]
    assert out.value == f"{react:g}" and out.value_source == "profile:skill_years"
    assert react < total
    for skill in ("COBOL", "Java"):  # Java must not borrow JavaScript's years
        missing = _mapper(settings, profile).map_fields(
            [FormField("z", "text", f"How many years of experience do you have in {skill}?", required=True)]
        )[0]
        assert missing.value_source == "unresolved", skill


def test_llm_answers_questions_outside_the_bank_and_can_decline(settings, profile):
    llm = _FakeLLM("Yes")
    m = _mapper(settings, profile, llm=llm)
    by = {
        o.field.key: o
        for o in m.map_fields(
            [
                FormField(
                    "a",
                    "radio",
                    "Are you comfortable working from office in Pune?",
                    required=True,
                    options=["Yes", "No"],
                ),
                FormField("b", "text", "Address line 2"),
            ]
        )
    }
    assert by["a"].value == "Yes" and by["a"].value_source == "llm_guideline"
    assert by["b"].value_source == "unresolved" and llm.calls == 1

    declined = _mapper(settings, profile, llm=_FakeLLM("UNKNOWN")).map_fields(
        [FormField("c", "text", "What is your passport number?", required=True)]
    )[0]
    assert declined.value_source == "unresolved"


def _linkedin(locations):
    from connectors.linkedin import LinkedInCollector
    from core.settings import LinkedInSourceConfig, SearchConfig

    return LinkedInCollector(
        None,  # type: ignore[arg-type]
        LinkedInSourceConfig(experience_levels=["Mid-Senior level"], location="India"),
        SearchConfig(posted_within_days=7, locations=locations),
        None,  # type: ignore[arg-type]
    )


def test_linkedin_searches_each_preferred_location_not_a_default_city():
    collector = _linkedin(["Bangalore", "Bengaluru", "Remote", "Gurugram", "Hyderabad", "Pune"])
    locs = collector._locations()
    # Bangalore and Bengaluru are one city; Remote becomes India with the remote filter
    assert ("Bengaluru, India", False) in locs
    assert ("India", True) in locs
    assert [name for name, _ in locs].count("Bengaluru, India") == 1
    assert "Gurugram, India" in [name for name, _ in locs]
    assert not any("Delhi" in name for name, _ in locs)

    bengaluru_url = collector._search_url("Full Stack Engineer", 0, "Bengaluru, India")
    assert "location=Bengaluru%2C+India" in bengaluru_url
    assert "f_E=" in bengaluru_url and "f_TPR=" in bengaluru_url
    remote_url = collector._search_url("Full Stack Engineer", 0, "India", remote=True)
    assert "location=India" in remote_url and "f_WT=2" in remote_url


def test_linkedin_relaxed_search_keeps_the_location():
    collector = _linkedin(["Bangalore"])
    relaxed = collector._search_url("Full Stack Engineer", 0, "Bengaluru, India", relax=True)
    assert "f_E=" not in relaxed and "f_TPR=" not in relaxed
    assert "location=Bengaluru%2C+India" in relaxed


def test_linkedin_falls_back_to_configured_location_when_no_cities():
    collector = _linkedin([])
    assert collector._locations() == [("India", False)]


def test_prefilled_values_are_kept(settings, profile):
    m = _mapper(settings, profile)
    out = m.map_fields([FormField("p1", "email", "Email", required=True, current_value="other@example.com")])
    assert out[0].value == "other@example.com" and out[0].value_source == "prefilled"
