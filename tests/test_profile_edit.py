"""Profile form round-trip and building a profile from resume text."""

from __future__ import annotations

import io
import zipfile

from core.profile import MasterProfile, load_profile
from core.profile_edit import build_profile, form_defaults, write_profile
from core.resume_import import extract_text, profile_from_resume
from tests.conftest import PROFILE

_RESUME = """\
Alex Morgan
Senior Full-Stack Engineer
Bangalore
alex.morgan@example.com
+91 90000 00000
5 years of experience

Skills
React, Python, FastAPI, AWS

Experience
Acme Studios, Senior Full-Stack Engineer, 2024 - Present
Architected and deployed a full-stack platform for AI asset cataloging using React and FastAPI with asynchronous workflows.

Education
Bachelor of Technology, Example Institute of Technology
"""


def test_form_round_trip_keeps_the_real_profile():
    profile = load_profile(PROFILE)
    rebuilt = MasterProfile.model_validate(build_profile(form_defaults(profile)))
    assert rebuilt.model_dump() == profile.model_dump()


def test_notice_ctc_and_locations_edit(tmp_path):
    profile = load_profile(PROFILE)
    values = form_defaults(profile)
    values["notice_days"] = 15
    values["current_ctc"] = 32
    values["expected_ctc"] = 45
    values["preferred_locations"] = "Pune\nRemote"
    values["skills"] = {"core": ["React", "Python"]}
    values["aliases"] = {}
    saved = write_profile(tmp_path / "master_profile.json", build_profile(values))
    assert saved.application_defaults.notice_period_days == 15
    assert saved.application_defaults.current_ctc_lpa == 32
    assert saved.application_defaults.preferred_locations == ["Pune", "Remote"]
    assert saved.skills_taxonomy.all_skills == ["React", "Python"]
    assert (tmp_path / "master_profile.json").exists()


def test_resume_text_builds_a_valid_profile():
    payload = profile_from_resume("resume.txt", _RESUME.encode(), llm=None)
    profile = MasterProfile.model_validate(payload)
    assert profile.basics.email == "alex.morgan@example.com"
    assert "React" in profile.skills_taxonomy.all_skills
    assert profile.application_defaults.current_company
    assert any(role.end_date == "Present" for role in profile.experience)
    assert profile.application_defaults.current_ctc_lpa == 0


def test_docx_text_extraction():
    xml = (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Alex Morgan</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>alex.morgan@example.com</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    text = extract_text("cv.docx", buf.getvalue())
    assert "Alex Morgan" in text
    assert "alex.morgan@example.com" in text
