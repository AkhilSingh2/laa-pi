"""Settings overrides, secrets store, browser lock and the scheduler control channel."""

from __future__ import annotations

import os
import stat
import subprocess
import sys

import pytest
from pydantic import ValidationError

from core import browser_lock
from core import scheduler_control as ctl
from core.secrets import get_secret, remove_secret, secret_status, set_secret
from core.settings import load_settings, read_overrides, save_overrides
from tests.conftest import ROOT

CONFIG = ROOT / "config" / "config.yaml"


def test_overrides_merge_and_persist(tmp_path):
    save_overrides(
        {"quotas": {"daily_caps": {"naukri": 7}}, "matching": {"fit_threshold": 80}}, root=tmp_path
    )
    s = load_settings(CONFIG, root=tmp_path)
    assert s.quotas.daily_caps["naukri"] == 7
    assert s.quotas.daily_caps["linkedin"] == 20  # untouched keys keep config.yaml values
    assert s.matching.fit_threshold == 80
    save_overrides({"quotas": {"daily_caps": {"linkedin": 5}}}, root=tmp_path)
    assert read_overrides(tmp_path)["quotas"]["daily_caps"] == {"naukri": 7, "linkedin": 5}


def test_invalid_override_is_not_written(tmp_path):
    with pytest.raises(ValidationError):
        save_overrides({"outreach": {"auto_send": True}}, root=tmp_path)
    assert read_overrides(tmp_path) == {}


def test_secret_store_masks_and_restricts(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert secret_status("ANTHROPIC_API_KEY", tmp_path)["set"] is False
    set_secret("ANTHROPIC_API_KEY", "sk-ant-test-1234567890abcd", tmp_path)
    p = tmp_path / "data" / "secrets.json"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    st = secret_status("ANTHROPIC_API_KEY", tmp_path)
    assert st == {"set": True, "source": "settings", "last4": "abcd"}
    assert get_secret("ANTHROPIC_API_KEY", tmp_path).endswith("abcd")
    with pytest.raises(ValueError):
        set_secret("LINKEDIN_PASSWORD", "hunter2", tmp_path)
    remove_secret("ANTHROPIC_API_KEY", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-key-0000wxyz")
    assert secret_status("ANTHROPIC_API_KEY", tmp_path)["source"] == "environment"


def test_browser_lock_exclusive_and_stale(settings):
    browser_lock.acquire(settings, "collector")
    assert browser_lock.holder(settings)["owner"] == "collector"
    browser_lock.acquire(settings, "collector")  # re-entrant for the same pid
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        browser_lock.release(settings)
        browser_lock.acquire(settings, "copilot", pid=child.pid)
        with pytest.raises(browser_lock.BrowserBusy):
            browser_lock.acquire(settings, "login")
    finally:
        child.kill()
        child.wait()
    # holder died: the lock is cleared and can be taken again
    assert browser_lock.holder(settings) is None
    with browser_lock.browser_lock(settings, "check"):
        assert browser_lock.holder(settings)["owner"] == "check"
    assert browser_lock.holder(settings) is None


def test_scheduler_control_roundtrip(settings):
    assert ctl.read_control(settings)["paused_jobs"] == []
    ctl.set_paused(settings, "apply", True)
    c = ctl.read_control(settings)
    assert ctl.is_paused(c, "apply") and not ctl.is_paused(c, "discovery")
    ctl.set_paused(settings, "all", True)
    assert ctl.is_paused(ctl.read_control(settings), "discovery")
    ctl.set_paused(settings, "all", False)
    ctl.set_paused(settings, "apply", False)
    assert not ctl.is_paused(ctl.read_control(settings), "apply")

    ctl.request_run_now(settings, "discovery")
    ctl.request_run_now(settings, "discovery")  # de-duplicated
    assert ctl.take_run_now(settings) == ["discovery"]
    assert ctl.take_run_now(settings) == []

    ctl.bump_config_version(settings)
    assert ctl.read_control(settings)["config_version"] == 1
    with pytest.raises(ValueError):
        ctl.request_run_now(settings, "rm -rf")


def test_scheduler_alive_from_heartbeat(settings):
    import json
    from datetime import UTC, datetime, timedelta

    p = settings.path(ctl.HEARTBEAT_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    assert not ctl.scheduler_alive(settings)
    p.write_text(json.dumps({"at": datetime.now(UTC).isoformat(), "state": "idle"}))
    assert ctl.scheduler_alive(settings)
    p.write_text(json.dumps({"at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(), "state": "idle"}))
    assert not ctl.scheduler_alive(settings)
    p.write_text(json.dumps({"at": datetime.now(UTC).isoformat(), "state": "stopped"}))
    assert not ctl.scheduler_alive(settings)
