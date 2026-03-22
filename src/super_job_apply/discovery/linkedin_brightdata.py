"""LinkedIn job discovery via Bright Data Web Scraper API.

Searches LinkedIn Jobs by keyword and location, returning structured
job data with application URLs. Uses Bright Data's LinkedIn Jobs
dataset (gd_l4dx9j9sscpvs7no2) for reliable scraping.

Requires: BRIGHT_DATA_API_KEY in .env
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

from ..models import JobPosting, SearchCriteria
from .base import JobSource

logger = logging.getLogger(__name__)

LINKEDIN_DATASET_ID = "gd_lpfll7v5hcqtkxl6l"
BRIGHTDATA_API_BASE = "https://api.brightdata.com/datasets/v3"


class LinkedInBrightDataSource(JobSource):
    """Discovers jobs from LinkedIn via Bright Data scraping API."""

    @property
    def source_name(self) -> str:
        return "linkedin"

    def __init__(self):
        self.api_key = os.environ.get("BRIGHT_DATA_API_KEY", "")

    async def discover(self, criteria: SearchCriteria) -> list[JobPosting]:
        """Search LinkedIn Jobs for each query in criteria."""
        if not self.api_key:
            logger.warning("BRIGHT_DATA_API_KEY not set — skipping LinkedIn source")
            return []

        all_jobs: list[JobPosting] = []
        seen_ids: set[str] = set()

        for query in criteria.queries:
            try:
                jobs = await self._search_linkedin(query, criteria)
                # Deduplicate within this run
                for job in jobs:
                    if job.careers_url not in seen_ids:
                        seen_ids.add(job.careers_url)
                        all_jobs.append(job)
            except Exception as e:
                logger.warning(f"LinkedIn search failed for '{query}': {e}")
                continue

        logger.info(f"LinkedInBrightDataSource discovered {len(all_jobs)} jobs")
        return all_jobs

    async def _search_linkedin(
        self, query: str, criteria: SearchCriteria
    ) -> list[JobPosting]:
        """Trigger a LinkedIn job search and poll for results."""
        location = criteria.locations[0] if criteria.locations else "United States"
        limit = min(criteria.num_results_per_query, 25)

        # Build search input
        search_input = {
            "keyword": query,
            "location": location,
            "country": "US",
            "time_range": "Past week" if (criteria.date_range_days or 30) <= 7 else "Past month",
            "job_type": "Full-time",
            "remote": "Remote" if "remote" in location.lower() else "",
        }

        logger.info(f"LinkedIn search: '{query}' in '{location}' (limit {limit})...")

        async with httpx.AsyncClient(timeout=60.0) as client:
            # Step 1: Trigger the scrape
            trigger_resp = await client.post(
                f"{BRIGHTDATA_API_BASE}/trigger",
                params={
                    "dataset_id": LINKEDIN_DATASET_ID,
                    "include_errors": "true",
                    "type": "discover_new",
                    "discover_by": "keyword",
                    "limit_per_input": str(limit),
                },
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=[search_input],
            )

            if trigger_resp.status_code != 200:
                logger.warning(
                    f"LinkedIn trigger failed ({trigger_resp.status_code}): "
                    f"{trigger_resp.text[:200]}"
                )
                return []

            trigger_data = trigger_resp.json()
            snapshot_id = trigger_data.get("snapshot_id")
            if not snapshot_id:
                logger.warning(f"No snapshot_id in response: {trigger_data}")
                return []

            logger.info(f"  Snapshot: {snapshot_id} — polling for results...")

            # Step 2: Poll for results (up to 5 minutes)
            results = await self._poll_results(client, snapshot_id)

        if not results:
            logger.info(f"  No LinkedIn results for '{query}'")
            return []

        # Step 3: Convert to JobPosting objects
        jobs = []
        for r in results:
            job = self._to_job_posting(r)
            if job:
                jobs.append(job)

        logger.info(f"  Found {len(jobs)} LinkedIn jobs for '{query}'")
        return jobs

    async def _poll_results(
        self, client: httpx.AsyncClient, snapshot_id: str, max_wait: int = 300
    ) -> list[dict]:
        """Poll Bright Data for scrape results."""
        url = f"{BRIGHTDATA_API_BASE}/snapshot/{snapshot_id}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        elapsed = 0
        interval = 15

        while elapsed < max_wait:
            resp = await client.get(url, params={"format": "json"}, headers=headers)

            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
                return []
            elif resp.status_code == 202:
                # Still processing
                await asyncio.sleep(interval)
                elapsed += interval
            else:
                logger.warning(f"  Poll error ({resp.status_code}): {resp.text[:200]}")
                return []

        logger.warning(f"  Polling timed out after {max_wait}s")
        return []

    def _to_job_posting(self, raw: dict) -> JobPosting | None:
        """Convert a Bright Data LinkedIn result to a JobPosting."""
        job_title = raw.get("job_title", "")
        company_name = raw.get("company_name", "")

        if not job_title or not company_name:
            return None

        # Prefer apply_link (external company URL) over LinkedIn URL
        apply_url = raw.get("apply_link") or ""
        linkedin_url = raw.get("url", "")

        # If no external apply link, use LinkedIn URL — the url_resolver
        # step in the pipeline will try to find the direct company page later
        careers_url = apply_url or linkedin_url
        if not careers_url:
            return None

        location = raw.get("job_location", "")
        description = raw.get("job_description_formatted", "") or raw.get("job_summary", "")

        # Clean HTML from description
        import re
        from html import unescape
        description_text = re.sub(r"<[^>]+>", "\n", description)
        description_text = unescape(description_text).strip()

        work_type = "remote" if "remote" in location.lower() else ""
        employment_type = raw.get("job_employment_type", "")

        # Extract salary info if available
        salary = raw.get("base_salary", {})
        salary_text = raw.get("job_base_pay_range", "")

        return JobPosting(
            source="linkedin",
            company_name=company_name,
            job_title=job_title,
            careers_url=careers_url,
            company_url=raw.get("company_url"),
            location=location,
            work_type=work_type or employment_type,
            full_description=description_text[:3000],
        )
