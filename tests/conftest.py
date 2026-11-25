from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.db import session_factory  # noqa: E402
from core.profile import MasterProfile, load_profile  # noqa: E402
from core.settings import Settings, load_settings  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
# Tests run against the committed example profile, never your real data/master_profile.json.
PROFILE = ROOT / "data" / "master_profile.example.json"


@pytest.fixture(scope="session")
def profile() -> MasterProfile:
    return load_profile(PROFILE)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    s = load_settings(ROOT / "config" / "config.yaml", root=tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    # copy static inputs the pipeline reads
    (tmp_path / "data" / "master_profile.json").write_text(
        PROFILE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "config" / "target_companies.csv").write_text(
        (ROOT / "config" / "target_companies.csv").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "engine" / "templates").mkdir(parents=True, exist_ok=True)
    for name in ("resume.html", "resume.css"):
        src = ROOT / "engine" / "templates" / name
        if src.exists():
            (tmp_path / "engine" / "templates" / name).write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8"
            )
    return s


@pytest.fixture()
def db_session(settings: Settings):
    factory = session_factory(settings.path(settings.app.db_path))
    session = factory()
    try:
        yield session
        session.commit()
    finally:
        session.close()


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture()
def sample_jd() -> str:
    return (FIXTURES / "jd_fullstack.txt").read_text(encoding="utf-8")
