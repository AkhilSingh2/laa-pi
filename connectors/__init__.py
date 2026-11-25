"""Job ingestion connectors. API connectors are importable without Playwright."""

from __future__ import annotations

from pathlib import Path

from connectors.ashby import AshbyConnector
from connectors.base import ApiConnector, BaseConnector, TargetCompany, load_target_companies
from connectors.greenhouse import GreenhouseConnector
from connectors.lever import LeverConnector
from core.settings import Settings

__all__ = [
    "ApiConnector",
    "AshbyConnector",
    "BaseConnector",
    "GreenhouseConnector",
    "LeverConnector",
    "TargetCompany",
    "build_api_connectors",
    "load_target_companies",
]


def build_api_connectors(settings: Settings) -> list[ApiConnector]:
    """Instantiate enabled Greenhouse/Lever/Ashby connectors from config/target_companies.csv."""
    out: list[ApiConnector] = []
    mapping = {
        "greenhouse": (settings.sources.greenhouse, GreenhouseConnector),
        "lever": (settings.sources.lever, LeverConnector),
        "ashby": (settings.sources.ashby, AshbyConnector),
    }
    for ats, (cfg, cls) in mapping.items():
        if not cfg.enabled:
            continue
        csv_path: Path = settings.path(cfg.companies_csv)
        targets = load_target_companies(csv_path, ats_type=ats)
        if targets:
            out.append(cls(targets))
    return out
