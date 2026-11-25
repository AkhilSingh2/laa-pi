"""Per-ATS adapters: know how to reach the form, fill mapped fields, upload the resume, and find Submit."""

from __future__ import annotations

from copilot.ats.base import AtsAdapter
from core.models import AtsType


def adapter_for(ats_type: str | None, source: str) -> AtsAdapter:
    from copilot.ats.ashby import AshbyAdapter
    from copilot.ats.generic import GenericAdapter
    from copilot.ats.greenhouse import GreenhouseAdapter
    from copilot.ats.lever import LeverAdapter
    from copilot.ats.linkedin_easy_apply import LinkedInEasyApplyAdapter
    from copilot.ats.naukri import NaukriAdapter

    if ats_type == AtsType.GREENHOUSE:
        return GreenhouseAdapter()
    if ats_type == AtsType.LEVER:
        return LeverAdapter()
    if ats_type == AtsType.ASHBY:
        return AshbyAdapter()
    if ats_type == AtsType.LINKEDIN_EASY_APPLY or source == "linkedin":
        return LinkedInEasyApplyAdapter()
    if ats_type == AtsType.NAUKRI or source == "naukri":
        return NaukriAdapter()
    return GenericAdapter()


def adapter_for_url(url: str) -> AtsAdapter:
    """Pick an adapter from a career-site URL opened out of Naukri or LinkedIn."""
    from copilot.ats.ashby import AshbyAdapter
    from copilot.ats.generic import GenericAdapter
    from copilot.ats.greenhouse import GreenhouseAdapter
    from copilot.ats.lever import LeverAdapter

    u = url.lower()
    if "greenhouse.io" in u:
        return GreenhouseAdapter()
    if "lever.co" in u:
        return LeverAdapter()
    if "ashbyhq.com" in u:
        return AshbyAdapter()
    return GenericAdapter()


__all__ = ["AtsAdapter", "adapter_for"]
