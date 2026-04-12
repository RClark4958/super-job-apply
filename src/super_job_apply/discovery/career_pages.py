"""Direct company career pages discovery source.

Searches curated company career pages for job listings using Exa,
bypassing Greenhouse/Lever embeds that block with reCAPTCHA.
"""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlparse

from exa_py import Exa

from ..models import JobPosting, SearchCriteria
from .base import JobSource

logger = logging.getLogger(__name__)

# Curated list of company career page URLs.
# These are direct career pages (NOT Greenhouse/Lever embeds).
#
# NOTE — Companies flagged with "# GH/Lever" are known to redirect
# their career pages to Greenhouse or Lever job boards.  We still
# search them via Exa (which often indexes the listing text before
# the redirect), but applicators should be aware that the final
# apply link may land on greenhouse.io or lever.co.
DEFAULT_CAREER_PAGES: dict[str, str] = {
    # --- Cloud / Infrastructure ---
    "Cloudflare": "https://www.cloudflare.com/careers/jobs/",
    "Hashicorp": "https://www.hashicorp.com/careers",
    "Tailscale": "https://tailscale.com/careers",
    "Fly.io": "https://fly.io/jobs/",
    "Railway": "https://railway.app/careers",
    "Render": "https://render.com/careers",
    "Vercel": "https://vercel.com/careers",
    "Supabase": "https://supabase.com/careers",
    "Neon": "https://neon.tech/careers",
    "PlanetScale": "https://planetscale.com/careers",

    # --- Data / Analytics ---
    "Databricks": "https://www.databricks.com/company/careers/open-positions",
    "Datadog": "https://careers.datadoghq.com/",
    "Elastic": "https://www.elastic.co/careers/",
    "MongoDB": "https://www.mongodb.com/careers",
    "Confluent": "https://www.confluent.io/careers/",
    "Cockroach Labs": "https://www.cockroachlabs.com/careers/",
    "Grafana Labs": "https://grafana.com/about/careers/",
    "Temporal": "https://temporal.io/careers",
    "ClickHouse": "https://clickhouse.com/careers",
    "Fivetran": "https://www.fivetran.com/careers",
    "dbt Labs": "https://www.getdbt.com/careers",
    "Dagster": "https://dagster.io/careers",
    "Airbyte": "https://airbyte.com/careers",
    "Timescale": "https://www.timescale.com/careers",

    # --- AI / ML ---
    "Anthropic": "https://www.anthropic.com/careers",  # GH/Lever: redirects to greenhouse
    "OpenAI": "https://openai.com/careers/",
    "Cohere": "https://cohere.com/careers",
    "Hugging Face": "https://huggingface.co/jobs",
    "Weights & Biases": "https://wandb.ai/site/careers",
    "Scale AI": "https://scale.com/careers",
    "Anyscale": "https://www.anyscale.com/careers",
    "Modal": "https://modal.com/careers",
    "LangChain": "https://www.langchain.com/careers",
    "Pinecone": "https://www.pinecone.io/careers/",
    "Weaviate": "https://weaviate.io/company/careers",
    "Replicate": "https://replicate.com/about#careers",
    "Mistral AI": "https://mistral.ai/careers/",
    "Cursor": "https://www.cursor.com/careers",

    # --- Developer Tools / SaaS ---
    "Stripe": "https://stripe.com/jobs/search",
    "Discord": "https://discord.com/careers",
    "Notion": "https://www.notion.so/careers",
    "Figma": "https://www.figma.com/careers/",
    "Retool": "https://retool.com/careers",
    "Linear": "https://linear.app/careers",
    "1Password": "https://1password.com/careers",
    "PostHog": "https://posthog.com/careers",
    "GitLab": "https://about.gitlab.com/jobs/",
    "Pulumi": "https://www.pulumi.com/careers/",
    "Snyk": "https://snyk.io/careers/",
    "LaunchDarkly": "https://launchdarkly.com/careers/",
    "Sentry": "https://sentry.io/careers/",
    "Sourcegraph": "https://about.sourcegraph.com/jobs",

    # --- Fintech ---
    "Plaid": "https://plaid.com/careers/",
    "Brex": "https://www.brex.com/careers",
    "Ramp": "https://ramp.com/careers",
    "Robinhood": "https://robinhood.com/careers/",
    "Rippling": "https://www.rippling.com/careers",

    # --- Other notable tech ---
    "Airbnb": "https://careers.airbnb.com/",
    "Twilio": "https://www.twilio.com/en-us/company/jobs",
    "Pagerduty": "https://careers.pagerduty.com/",
    "CrowdStrike": "https://www.crowdstrike.com/careers/",
    "Okta": "https://www.okta.com/company/careers/",
    "Airtable": "https://airtable.com/careers",
}

# Companies known to use Greenhouse/Lever for their actual application forms.
# We still discover listings from their career pages (Exa indexes the content),
# but the final "Apply" button will redirect to greenhouse.io or lever.co.
# The applicator layer should be aware of this.
GREENHOUSE_LEVER_COMPANIES = {
    "Anthropic",       # job-boards.greenhouse.io/anthropic
    "Discord",         # greenhouse
    "Notion",          # greenhouse
    "Figma",           # greenhouse
    "Linear",          # greenhouse
    "Temporal",        # greenhouse
    "Cockroach Labs",  # greenhouse
    "PostHog",         # greenhouse (via Ashby in some cases)
    "Weights & Biases",  # greenhouse
    "Sourcegraph",     # greenhouse
    "Airbnb",          # greenhouse — all positions 404 or redirect to GH
}


class CareerPageSource(JobSource):
    """Discovers jobs by searching curated company career pages via Exa.

    Uses Exa's site-scoped search to find job listings on known company
    career pages that match the candidate's keywords.  This avoids the
    Greenhouse/Lever reCAPTCHA problem because we surface listings from
    the company's own domain.
    """

    @property
    def source_name(self) -> str:
        return "career_page"

    def __init__(self, career_page_urls: dict[str, str] | None = None):
        """Initialize with Exa client and career page list.

        Args:
            career_page_urls: Optional dict mapping company name -> career URL.
                              If None, uses DEFAULT_CAREER_PAGES.
                              Can also be extended at runtime from config.yaml.
        """
        self.exa = Exa(api_key=os.environ.get("EXA_API_KEY"))
        self.career_pages: dict[str, str] = dict(DEFAULT_CAREER_PAGES)
        if career_page_urls:
            self.career_pages.update(career_page_urls)

    async def discover(self, criteria: SearchCriteria) -> list[JobPosting]:
        """Search each career page for jobs matching the candidate's keywords.

        For every company career page, runs a site-scoped Exa search using
        the candidate's search queries as keywords.
        """
        all_jobs: list[JobPosting] = []

        # Build a compact keyword string from the search queries.
        # We combine a few representative keywords rather than sending
        # every long-form query to keep Exa results focused.
        keywords = self._build_keywords(criteria.queries)
        logger.info(
            f"CareerPageSource: searching {len(self.career_pages)} company "
            f"career pages with keywords: {keywords!r}"
        )

        # Process companies concurrently in batches to stay within rate limits.
        BATCH_SIZE = 5
        companies = list(self.career_pages.items())

        for i in range(0, len(companies), BATCH_SIZE):
            batch = companies[i : i + BATCH_SIZE]
            tasks = [
                self._search_company(company, url, keywords, criteria)
                for company, url in batch
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for (company, _), result in zip(batch, results):
                if isinstance(result, Exception):
                    logger.warning(
                        f"  CareerPageSource: failed for {company}: {result}"
                    )
                    continue
                all_jobs.extend(result)

            # Small delay between batches to be nice to the Exa API.
            if i + BATCH_SIZE < len(companies):
                await asyncio.sleep(1)

        logger.info(f"CareerPageSource discovered {len(all_jobs)} potential jobs")
        return all_jobs

    # URL path segments that indicate non-job content.
    _NOISE_PATH_SEGMENTS = {
        "/blog/", "/blog.", "/press/", "/changelog/", "/docs/",
        "/documentation/", "/resources/", "/news/", "/events/",
        "/podcast/", "/webinar/", "/case-stud", "/customer/",
        "/legal/", "/privacy/", "/terms/", "/security/",
        "/pricing/", "/product/", "/features/", "/integrations/",
        "/guides/", "/tutorials/", "/learn/", "/academy/",
        "/community/", "/forum/", "/support/", "/help/",
        "/about/", "/contact/", "/team/", "/investors/",
    }

    async def _search_company(
        self,
        company: str,
        career_url: str,
        keywords: str,
        criteria: SearchCriteria,
    ) -> list[JobPosting]:
        """Search a single company career page for matching jobs."""
        domain = urlparse(career_url).hostname or ""
        domain = domain.replace("www.", "")

        # Extract the career page path to scope the search tightly.
        # e.g. "cloudflare.com/careers/jobs/" → search only under /careers/
        career_path = urlparse(career_url).path.rstrip("/")
        # Use the first two path segments (e.g. /careers or /company/careers)
        path_parts = [p for p in career_path.split("/") if p]
        if path_parts:
            scope_path = "/" + "/".join(path_parts[:2])
            query = f"site:{domain}{scope_path} {keywords}"
        else:
            query = f"site:{domain} {keywords}"

        logger.debug(f"  Searching {company}: {query}")

        search_kwargs: dict = {
            "text": True,
            "type": "auto",
            "livecrawl": "fallback",
            "num_results": min(criteria.num_results_per_query, 10),
        }

        # Add date filter if specified.
        if criteria.date_range_days:
            from datetime import datetime, timedelta, timezone

            start_date = datetime.now(timezone.utc) - timedelta(
                days=criteria.date_range_days
            )
            search_kwargs["start_published_date"] = start_date.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"
            )

        results = await asyncio.to_thread(
            self.exa.search_and_contents,
            query,
            **search_kwargs,
        )

        jobs: list[JobPosting] = []
        for result in results.results:
            # Skip URLs that are clearly non-job content (blog, docs, etc.)
            result_path = urlparse(result.url).path.lower()
            if any(seg in result_path for seg in self._NOISE_PATH_SEGMENTS):
                logger.debug(f"  {company}: skipped noise URL {result.url[:80]}")
                continue

            job_title = self._extract_job_title(result)
            if not self._is_relevant(job_title, result.text or "", keywords):
                continue

            gh_lever_note = ""
            if company in GREENHOUSE_LEVER_COMPANIES:
                gh_lever_note = (
                    " [NOTE: Apply link may redirect to Greenhouse/Lever]"
                )

            job = JobPosting(
                source=self.source_name,
                company_name=company,
                job_title=job_title,
                careers_url=result.url,
                company_url=career_url,
                full_description=(result.text or "") + gh_lever_note,
                location=self._extract_location(result.text or ""),
            )
            jobs.append(job)

        if jobs:
            logger.info(f"  {company}: found {len(jobs)} matching jobs")
        else:
            logger.debug(f"  {company}: no matching jobs found")

        return jobs

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    @staticmethod
    def _build_keywords(queries: list[str]) -> str:
        """Distil search queries into a compact keyword string for Exa.

        Instead of sending long-form queries like "AI engineer remote jobs
        apply now late March 2026", we extract the core role keywords.
        """
        # Common filler words to strip out.
        STOP_WORDS = {
            "jobs", "job", "apply", "now", "new", "postings", "listings",
            "openings", "positions", "hiring", "late", "early", "mid",
            "2024", "2025", "2026", "2027", "remote", "onsite", "hybrid",
        }
        seen: set[str] = set()
        keywords: list[str] = []
        for query in queries:
            for word in query.lower().split():
                cleaned = word.strip(",.;:!?\"'()")
                if cleaned and cleaned not in STOP_WORDS and cleaned not in seen:
                    seen.add(cleaned)
                    keywords.append(cleaned)

        # Keep it under ~15 terms so the Exa query stays focused.
        return " ".join(keywords[:15])

    @staticmethod
    def _extract_job_title(result) -> str:
        """Best-effort extraction of job title from an Exa result."""
        if result.title:
            title = result.title
            # Patterns: "Title - Company", "Title | Company", "Title at Company"
            for sep in [" - ", " | ", " at "]:
                if sep in title:
                    return title.split(sep)[0].strip()
            return title.strip()
        return "Open Position"

    @staticmethod
    def _extract_location(text: str) -> str | None:
        """Try to pull a location from the listing text."""
        import re

        # Look for common location patterns.
        patterns = [
            r"(?:Location|Office)[:\s]+([A-Z][a-zA-Z\s,]+(?:Remote|Hybrid)?)",
            r"(Remote(?:\s*[/,]\s*(?:US|USA|Worldwide|Global|North America))?)",
            r"((?:San Francisco|New York|Austin|Seattle|London|Berlin|Toronto)"
            r"(?:\s*,\s*[A-Z]{2})?)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text[:2000])  # Only search the top portion.
            if match:
                return match.group(1).strip()
        return None

    @staticmethod
    def _is_relevant(title: str, text: str, keywords: str) -> bool:
        """Quick relevance check — is this actually a job listing?

        Must have BOTH:
        1. A job-listing structural signal (apply button, responsibilities section, etc.)
        2. A role-type keyword in the title
        Filters out blog posts, docs, changelogs that happen to contain tech keywords.
        """
        title_lower = title.lower()
        combined = (title + " " + text[:3000]).lower()

        # --- Gate 1: Title must contain a role-type word ---
        role_words = [
            "engineer", "developer", "manager", "analyst", "scientist",
            "designer", "architect", "lead", "director", "specialist",
            "coordinator", "associate", "intern", "devops", "sre",
            "platform", "infrastructure", "backend", "frontend",
            "fullstack", "full-stack", "full stack", "staff",
            "principal", "senior", "junior", "head of",
        ]
        has_role_in_title = any(rw in title_lower for rw in role_words)
        if not has_role_in_title:
            return False

        # --- Gate 2: Body must have job-listing structure signals ---
        # These distinguish a job posting from a blog post or docs page.
        structure_signals = [
            "responsibilities", "qualifications", "requirements",
            "what you'll do", "what you will do", "about the role",
            "about this role", "we're looking for", "we are looking for",
            "you will", "you'll", "your role", "the role",
            "apply now", "apply for this", "submit application",
            "equal opportunity", "compensation", "salary range",
            "benefits", "years of experience", "years experience",
        ]
        if not any(signal in combined for signal in structure_signals):
            return False

        return True
