"""Fallback adapter for unknown career sites: always review_mode, never auto-submit."""

from __future__ import annotations

from copilot.ats.base import AtsAdapter


class GenericAdapter(AtsAdapter):
    name = "generic"
    verified_direct_ats = False
