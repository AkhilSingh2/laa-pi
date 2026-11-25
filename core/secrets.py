"""App secrets set from the Settings page (currently only the AI API key).

Stored in data/secrets.json with 0600 permissions, on the same volume as the DB. Values are never returned to
the UI in full; `secret_status` only exposes where the value comes from and its last 4 characters.
Portal (LinkedIn / Naukri) passwords are never stored anywhere: those logins are manual in the browser.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRETS_FILE = "data/secrets.json"
ALLOWED = {"ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "TM_API_KEY", "LLM_API_KEY"}


def _path(root: Path | None) -> Path:
    return (root or Path(os.environ.get("LAA_ROOT") or REPO_ROOT)) / SECRETS_FILE


def _read(root: Path | None) -> dict[str, str]:
    p = _path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def _write(root: Path | None, data: dict[str, str]) -> None:
    p = _path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, p)
    os.chmod(p, 0o600)


def get_secret(name: str, root: Path | None = None) -> str | None:
    """Value saved from the Settings page, falling back to the environment (.env or host env vars)."""
    return _read(root).get(name) or os.environ.get(name) or None


def set_secret(name: str, value: str, root: Path | None = None) -> None:
    if name not in ALLOWED:
        raise ValueError(f"{name} is not a secret this app stores")
    value = value.strip()
    if not value:
        raise ValueError("empty value")
    data = _read(root)
    data[name] = value
    _write(root, data)


def remove_secret(name: str, root: Path | None = None) -> None:
    data = _read(root)
    if data.pop(name, None) is not None:
        _write(root, data)


def secret_status(name: str, root: Path | None = None) -> dict[str, Any]:
    saved = _read(root).get(name)
    env = os.environ.get(name)
    value = saved or env
    return {
        "set": bool(value),
        "source": "settings" if saved else ("environment" if env else None),
        "last4": value[-4:] if value and len(value) >= 8 else None,
    }
