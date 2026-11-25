"""Ashby public job board API: https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"""

from __future__ import annotations

from datetime import datetime

from connectors.base import ApiConnector, TargetCompany, html_to_text
from core.models import AtsType, JobPosting, RemoteType, Source, infer_remote_type

BASE = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class AshbyConnector(ApiConnector):
    source = Source.ASHBY

    def fetch_company(self, target: TargetCompany) -> list[JobPosting]:
        resp = self.client.get(BASE.format(token=target.board_token))
        resp.raise_for_status()
        data = resp.json()
        out: list[JobPosting] = []
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            jd = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))
            location = j.get("location")
            secondary = [s.get("location") for s in j.get("secondaryLocations", []) if s.get("location")]
            remote = (
                RemoteType.REMOTE
                if j.get("isRemote")
                else infer_remote_type(location, j.get("title"), jd[:600])
            )
            out.append(
                JobPosting(
                    source=self.source,
                    external_id=str(j["id"]),
                    company=target.company,
                    title=(j.get("title") or "").strip(),
                    location=", ".join([x for x in [location, *secondary] if x]) or None,
                    remote_type=remote,
                    jd_text=jd,
                    apply_url=j.get("applyUrl") or j.get("jobUrl", ""),
                    ats_type=AtsType.ASHBY,
                    posted_at=_parse_dt(j.get("publishedAt")),
                    raw_metadata={
                        "board_token": target.board_token,
                        "job_url": j.get("jobUrl"),
                        "department": j.get("department"),
                        "team": j.get("team"),
                        "employment_type": j.get("employmentType"),
                        "compensation": j.get("compensation"),
                    },
                )
            )
        return out
