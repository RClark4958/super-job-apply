"""Exa-powered company discovery and careers page search.

Adapted from the Browserbase template: searches for companies matching criteria,
finds their careers pages, then extracts job details via Stagehand.
"""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlparse

from exa_py import Exa

from ..config import get_model_api_key
from ..models import JOB_DESCRIPTION_SCHEMA, JobPosting, SearchCriteria
from .base import JobSource

logger = logging.getLogger(__name__)


class ExaCompanySource(JobSource):
    """Discovers jobs by finding companies via Exa, then locating their careers pages."""

    @property
    def source_name(self) -> str:
        return "exa_company"

    def __init__(self):
        self.exa = Exa(api_key=os.environ.get("EXA_API_KEY"))

    async def discover(self, criteria: SearchCriteria) -> list[JobPosting]:
        """Search for companies, find careers pages, extract job data."""
        all_jobs: list[JobPosting] = []

        for query in criteria.queries:
            try:
                companies = await self._search_companies(query, criteria.num_results_per_query)
                careers_pages = await self._find_careers_pages(companies, criteria.exclude_domains)

                for page in careers_pages:
                    job = JobPosting(
                        source=self.source_name,
                        company_name=page["company"],
                        job_title=page.get("job_title", "Open Position"),
                        careers_url=page["careers_url"],
                        company_url=page.get("url"),
                    )
                    all_jobs.append(job)

            except Exception as e:
                logger.warning(f"Exa company search failed for query '{query}': {e}")
                continue

        logger.info(f"ExaCompanySource discovered {len(all_jobs)} potential jobs")
        return all_jobs

    async def _search_companies(self, query: str, num_results: int) -> list:
        """Search for companies matching the query using Exa."""
        logger.info(f"Searching for companies: '{query}'...")

        results = await asyncio.to_thread(
            self.exa.search_and_contents,
            query,
            category="company",
            text=True,
            type="auto",
            livecrawl="fallback",
            num_results=num_results,
        )

        logger.info(f"Found {len(results.results)} companies")
        for i, company in enumerate(results.results):
            logger.debug(f"  {i + 1}. {company.title} - {company.url}")

        return results.results

    async def _find_careers_pages(self, companies: list, exclude_domains: list[str]) -> list[dict]:
        """Find careers pages for each discovered company."""
        logger.info("Searching for careers pages...")
        careers_pages = []

        for company in companies:
            try:
                parsed_url = urlparse(company.url)
                company_domain = (
                    parsed_url.hostname.replace("www.", "") if parsed_url.hostname else ""
                )
                logger.debug(f"  Looking for careers page: {company_domain}...")

                careers_result = await asyncio.to_thread(
                    self.exa.search_and_contents,
                    f"{company_domain} careers page",
                    context=True,
                    exclude_domains=exclude_domains,
                    num_results=5,
                    text=True,
                    type="deep",
                    livecrawl="fallback",
                )

                if careers_result.results:
                    careers_url = careers_result.results[0].url
                    logger.info(f"  Found careers page for {company_domain}: {careers_url}")
                    careers_pages.append(
                        {
                            "company": company.title or company_domain,
                            "url": company.url,
                            "careers_url": careers_url,
                        }
                    )
                else:
                    logger.debug(f"  No careers page found for {company_domain}")
            except Exception as e:
                logger.warning(f"  Failed to find careers page for {company.title}: {e}")
                continue

        return careers_pages
