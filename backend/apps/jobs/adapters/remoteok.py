"""RemoteOK — official JSON API, LOW risk, full automation permitted.

``GET https://remoteok.com/api`` returns a JSON array whose FIRST element is
a legal notice object (it has ``legal`` and no ``id``); every following
element is a listing. The API's own terms require linking back to the
listing URL, which the UI does via ``JobListing.url``.
"""

from __future__ import annotations

from apps.jobs.adapters.base import BaseAdapter, NormalizedJob, first_email, strip_html


def _salary(row: dict) -> str:
    low, high = row.get("salary_min") or 0, row.get("salary_max") or 0
    if low and high:
        return f"${low // 1000}k–{high // 1000}k"
    if low or high:
        return f"${(low or high) // 1000}k"
    return ""


class RemoteOKAdapter(BaseAdapter):
    slug = "remoteok"
    feed_url = "https://remoteok.com/api"

    def parse(self, response) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        for row in response.json():
            # The legal-notice preamble and anything without a link.
            if not isinstance(row, dict) or not row.get("id") or not row.get("url"):
                continue
            description = strip_html(row.get("description", ""))
            psd = {"source_id": str(row["id"])}
            if row.get("apply_url"):
                psd["apply_url"] = row["apply_url"]
            email = first_email(description)
            if email:
                psd["apply_email"] = email
            jobs.append(
                NormalizedJob(
                    title=(row.get("position") or "").strip(),
                    company=(row.get("company") or "").strip(),
                    url=row["url"],
                    location=(row.get("location") or "").strip(),
                    salary=_salary(row),
                    description=description,
                    tags=[t for t in (row.get("tags") or []) if t][:8],
                    platform_specific_data=psd,
                )
            )
        return [j for j in jobs if j.title and j.company]
