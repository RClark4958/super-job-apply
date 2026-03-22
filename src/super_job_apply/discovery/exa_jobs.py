"""Exa-powered direct job search.

Unlike exa_company.py which searches for companies then finds their careers pages,
this source searches Exa directly for job postings and application pages.
"""

from __future__ import annotations

import asyncio
import logging
import os

from exa_py import Exa

from ..models import JobPosting, SearchCriteria
from .base import JobSource

logger = logging.getLogger(__name__)


class ExaJobSource(JobSource):
    """Discovers jobs by searching Exa directly for job postings."""

    @property
    def source_name(self) -> str:
        return "exa_jobs"

    def __init__(self):
        self.exa = Exa(api_key=os.environ.get("EXA_API_KEY"))

    async def discover(self, criteria: SearchCriteria) -> list[JobPosting]:
        """Search Exa directly for job postings."""
        all_jobs: list[JobPosting] = []

        for query in criteria.queries:
            try:
                jobs = await self._search_jobs(query, criteria)
                all_jobs.extend(jobs)
            except Exception as e:
                logger.warning(f"Exa job search failed for query '{query}': {e}")
                continue

        logger.info(f"ExaJobSource discovered {len(all_jobs)} potential jobs")
        return all_jobs

    async def _search_jobs(self, query: str, criteria: SearchCriteria) -> list[JobPosting]:
        """Search for job postings matching the query."""
        logger.info(f"Searching for jobs: '{query}'...")

        search_kwargs = {
            "text": True,
            "type": "auto",
            "livecrawl": "fallback",
            "num_results": criteria.num_results_per_query,
            "exclude_domains": criteria.exclude_domains,
        }

        # Add date filter if specified
        if criteria.date_range_days:
            from datetime import datetime, timedelta, timezone

            start_date = datetime.now(timezone.utc) - timedelta(days=criteria.date_range_days)
            search_kwargs["start_published_date"] = start_date.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        results = await asyncio.to_thread(
            self.exa.search_and_contents,
            f"{query} job posting apply",
            **search_kwargs,
        )

        jobs = []
        for result in results.results:
            # Extract company name from result title or URL
            company_name = self._extract_company_name(result)
            job_title = self._extract_job_title(result)

            job = JobPosting(
                source=self.source_name,
                company_name=company_name,
                job_title=job_title,
                careers_url=result.url,
                full_description=result.text or "",
            )
            jobs.append(job)

        logger.info(f"  Found {len(jobs)} job postings for query '{query}'")
        return jobs

    def _extract_company_name(self, result) -> str:
        """Best-effort extraction of company name from Exa result."""
        if result.title:
            # Common patterns: "Job Title at Company" or "Company - Job Title"
            title = result.title
            if " at " in title:
                return title.split(" at ")[-1].strip()
            if " - " in title:
                parts = title.split(" - ")
                # Usually company is the shorter part
                return min(parts, key=len).strip()
            return title.split("|")[0].strip() if "|" in title else title

        # Fallback to domain
        from urllib.parse import urlparse

        parsed = urlparse(result.url)
        return parsed.hostname.replace("www.", "").split(".")[0] if parsed.hostname else "Unknown"

    def _extract_job_title(self, result) -> str:
        """Best-effort extraction of job title from Exa result."""
        if result.title:
            title = result.title
            if " at " in title:
                return title.split(" at ")[0].strip()
            if " - " in title:
                parts = title.split(" - ")
                return max(parts, key=len).strip()
            return title

        return "Open Position"
