"""We Work Remotely — official RSS feed, LOW risk, full automation permitted.

Programming-category feed. Item titles arrive as ``"Company: Role"``; the
``region`` and ``category`` elements live in WWR's own namespace-free
extension tags, so plain ElementTree is enough — no feed-parsing dependency.
"""

from __future__ import annotations

from xml.etree import ElementTree

from apps.jobs.adapters.base import BaseAdapter, NormalizedJob, first_email, strip_html


class WWRAdapter(BaseAdapter):
    slug = "wwr"
    feed_url = "https://weworkremotely.com/categories/remote-programming-jobs.rss"

    def parse(self, response) -> list[NormalizedJob]:
        # The feed is external input: refuse DTDs outright rather than parse
        # them, which forecloses XXE and entity-expansion (billion-laughs)
        # attacks without pulling in defusedxml. A legitimate RSS feed never
        # carries a DOCTYPE.
        if b"<!DOCTYPE" in response.content or b"<!ENTITY" in response.content:
            raise ValueError("wwr feed contains a DTD; refusing to parse")
        root = ElementTree.fromstring(response.content)
        jobs: list[NormalizedJob] = []
        for item in root.iter("item"):
            title_raw = (item.findtext("title") or "").strip()
            url = (item.findtext("link") or "").strip()
            if not title_raw or not url:
                continue
            company, _, role = title_raw.partition(": ")
            if not role:  # no "Company: Role" separator — keep the whole title
                company, role = "", title_raw
            description = strip_html(item.findtext("description") or "")
            psd: dict = {}
            guid = (item.findtext("guid") or "").strip()
            if guid:
                psd["source_id"] = guid
            email = first_email(description)
            if email:
                psd["apply_email"] = email
            jobs.append(
                NormalizedJob(
                    title=role.strip(),
                    company=company.strip(),
                    url=url,
                    location=(item.findtext("region") or "").strip(),
                    description=description,
                    tags=[c.text.strip() for c in item.findall("category") if c.text][:8],
                    platform_specific_data=psd,
                )
            )
        return [j for j in jobs if j.title]
