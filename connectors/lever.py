"""Lever public postings API: https://api.lever.co/v0/postings/{token}?mode=json"""

from __future__ import annotations

from datetime import UTC, datetime

from connectors.base import ApiConnector, TargetCompany, html_to_text
from core.models import AtsType, JobPosting, RemoteType, Source, infer_remote_type

BASE = "https://api.lever.co/v0/postings/{token}?mode=json"


def _ms_to_dt(ms: int | None) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


class LeverConnector(ApiConnector):
    source = Source.LEVER

    def fetch_company(self, target: TargetCompany) -> list[JobPosting]:
        resp = self.client.get(BASE.format(token=target.board_token))
        resp.raise_for_status()
        out: list[JobPosting] = []
        for j in resp.json():
            cats = j.get("categories") or {}
            parts = [html_to_text(j.get("descriptionPlain") or j.get("description"))]
            for lst in j.get("lists", []):
                parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
            if j.get("additionalPlain") or j.get("additional"):
                parts.append(html_to_text(j.get("additionalPlain") or j.get("additional")))
            jd = "\n\n".join(p for p in parts if p)
            workplace = (j.get("workplaceType") or "").lower()
            remote = {
                "remote": RemoteType.REMOTE,
                "hybrid": RemoteType.HYBRID,
                "on-site": RemoteType.ONSITE,
                "onsite": RemoteType.ONSITE,
            }.get(workplace) or infer_remote_type(cats.get("location"), j.get("text"))
            all_locs = cats.get("allLocations") or []
            location = cats.get("location") or (all_locs[0] if all_locs else None)
            out.append(
                JobPosting(
                    source=self.source,
                    external_id=str(j["id"]),
                    company=target.company,
                    title=(j.get("text") or "").strip(),
                    location=location,
                    remote_type=remote,
                    jd_text=jd,
                    apply_url=j.get("applyUrl") or j.get("hostedUrl", ""),
                    ats_type=AtsType.LEVER,
                    posted_at=_ms_to_dt(j.get("createdAt")),
                    raw_metadata={
                        "board_token": target.board_token,
                        "hosted_url": j.get("hostedUrl"),
                        "team": cats.get("team"),
                        "department": cats.get("department"),
                        "commitment": cats.get("commitment"),
                        "workplace_type": j.get("workplaceType"),
                    },
                )
            )
        return out
