"""Greenhouse public board API: https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"""

from __future__ import annotations

from datetime import datetime

from connectors.base import ApiConnector, TargetCompany, html_to_text
from core.models import AtsType, JobPosting, Source, infer_remote_type

BASE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GreenhouseConnector(ApiConnector):
    source = Source.GREENHOUSE

    def fetch_company(self, target: TargetCompany) -> list[JobPosting]:
        resp = self.client.get(BASE.format(token=target.board_token))
        resp.raise_for_status()
        data = resp.json()
        out: list[JobPosting] = []
        for j in data.get("jobs", []):
            jd = html_to_text(j.get("content"))
            location = (j.get("location") or {}).get("name")
            offices = ", ".join(o.get("name", "") for o in j.get("offices", []) if o.get("name"))
            out.append(
                JobPosting(
                    source=self.source,
                    external_id=str(j["id"]),
                    company=target.company,
                    title=j.get("title", "").strip(),
                    location=location or offices or None,
                    remote_type=infer_remote_type(location, j.get("title"), jd[:600]),
                    jd_text=jd,
                    apply_url=j.get("absolute_url", ""),
                    ats_type=AtsType.GREENHOUSE,
                    posted_at=_parse_dt(j.get("first_published") or j.get("updated_at")),
                    raw_metadata={
                        "board_token": target.board_token,
                        "departments": [d.get("name") for d in j.get("departments", [])],
                        "offices": offices,
                        "internal_job_id": j.get("internal_job_id"),
                        "requisition_id": j.get("requisition_id"),
                        "metadata": j.get("metadata"),
                    },
                )
            )
        return out
