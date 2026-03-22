"""Greenhouse and Lever ATS job board discovery.

Searches company job boards directly via their public APIs.
Returns structured job data with direct application URLs — no aggregator noise.

Greenhouse API: https://developers.greenhouse.io/job-board.html
Lever API: https://github.com/lever/postings-api
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from html import unescape

import httpx

from ..models import JobPosting, SearchCriteria
from .base import JobSource

logger = logging.getLogger(__name__)

# Curated list of companies known to use Greenhouse or Lever.
# Users can extend this in config.yaml via search.ats_companies
DEFAULT_GREENHOUSE_COMPANIES = [
    "anthropic", "discord", "airbnb", "notion", "figma", "databricks",
    "cloudflare", "stripe", "openai", "scale", "anyscale", "modal",
    "weights-and-biases", "huggingface", "langchain", "pinecone",
    "weaviate", "prefect", "dagster", "dbt-labs", "fivetran",
    "hashicorp", "datadog", "grafana", "elastic", "confluent",
    "snowflake", "cockroachlabs", "planetscale", "neon",
    "vercel", "supabase", "render", "fly", "railway",
]

DEFAULT_LEVER_COMPANIES = [
    "paralleldomain", "cohere", "moveworks", "snorkelai",
    "deepmind", "adept", "character-ai", "runway",
]


class GreenhouseLeverSource(JobSource):
    """Discovers jobs from Greenhouse and Lever company job boards."""

    @property
    def source_name(self) -> str:
        return "ats_boards"

    def __init__(self, greenhouse_companies: list[str] | None = None, lever_companies: list[str] | None = None):
        self.greenhouse_companies = greenhouse_companies or DEFAULT_GREENHOUSE_COMPANIES
        self.lever_companies = lever_companies or DEFAULT_LEVER_COMPANIES

    async def discover(self, criteria: SearchCriteria) -> list[JobPosting]:
        """Search Greenhouse and Lever boards for matching jobs."""
        all_jobs: list[JobPosting] = []

        # Build keyword matchers from search criteria
        keywords = _extract_keywords(criteria)
        target_locations = [loc.lower() for loc in criteria.locations] if criteria.locations else []

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Greenhouse boards
            for company in self.greenhouse_companies:
                try:
                    jobs = await _fetch_greenhouse(client, company, keywords, target_locations)
                    all_jobs.extend(jobs)
                    if jobs:
                        logger.info(f"  Greenhouse/{company}: {len(jobs)} matching jobs")
                except Exception as e:
                    logger.debug(f"  Greenhouse/{company}: {e}")

            # Lever boards
            for company in self.lever_companies:
                try:
                    jobs = await _fetch_lever(client, company, keywords, target_locations)
                    all_jobs.extend(jobs)
                    if jobs:
                        logger.info(f"  Lever/{company}: {len(jobs)} matching jobs")
                except Exception as e:
                    logger.debug(f"  Lever/{company}: {e}")

        logger.info(f"GreenhouseLeverSource discovered {len(all_jobs)} matching jobs")
        return all_jobs


async def _fetch_greenhouse(
    client: httpx.AsyncClient,
    company_slug: str,
    keywords: list[str],
    locations: list[str],
) -> list[JobPosting]:
    """Fetch jobs from a Greenhouse board, filter by keywords and location."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs?content=true"
    resp = await client.get(url)

    if resp.status_code == 404:
        return []
    resp.raise_for_status()

    data = resp.json()
    jobs_data = data.get("jobs", [])
    matched = []

    for j in jobs_data:
        title = j.get("title", "")
        location = j.get("location", {}).get("name", "")
        content_html = j.get("content", "")
        content_text = _strip_html(content_html)
        company_name = j.get("company_name", company_slug.title())
        apply_url = j.get("absolute_url", "")

        # Keyword match — check title and description
        title_lower = title.lower()
        content_lower = content_text.lower()
        if not _matches_keywords(title_lower, content_lower, keywords):
            continue

        # Location match — skip if locations specified and none match
        if locations:
            loc_lower = location.lower()
            if not any(
                loc in loc_lower or "remote" in loc_lower
                for loc in locations
            ):
                continue

        # Extract requirements from content
        requirements = _extract_section(content_text, ["requirements", "qualifications", "what you'll need", "what we're looking for"])
        responsibilities = _extract_section(content_text, ["responsibilities", "what you'll do", "the role", "about the role"])

        matched.append(JobPosting(
            source="greenhouse",
            company_name=company_name,
            job_title=title,
            careers_url=apply_url,
            company_url=f"https://boards.greenhouse.io/{company_slug}",
            location=location,
            work_type=_detect_work_type(location, content_text),
            requirements=requirements,
            responsibilities=responsibilities,
            full_description=content_text[:3000],
        ))

    return matched


async def _fetch_lever(
    client: httpx.AsyncClient,
    company_slug: str,
    keywords: list[str],
    locations: list[str],
) -> list[JobPosting]:
    """Fetch jobs from a Lever board, filter by keywords and location."""
    url = f"https://api.lever.co/v0/postings/{company_slug}?mode=json"
    resp = await client.get(url)

    if resp.status_code == 404:
        return []
    resp.raise_for_status()

    postings = resp.json()
    if not isinstance(postings, list):
        return []

    matched = []
    for p in postings:
        title = p.get("text", "")
        categories = p.get("categories", {})
        location = categories.get("location", "")
        commitment = categories.get("commitment", "")
        workplace = p.get("workplaceType", "")
        description = p.get("descriptionPlain", "") or _strip_html(p.get("description", ""))
        apply_url = p.get("applyUrl", "") or p.get("hostedUrl", "")

        # Keyword match
        title_lower = title.lower()
        desc_lower = description.lower()
        if not _matches_keywords(title_lower, desc_lower, keywords):
            continue

        # Location match
        if locations:
            loc_lower = location.lower()
            if not any(
                loc in loc_lower or "remote" in loc_lower or workplace == "remote"
                for loc in locations
            ):
                continue

        # Extract structured sections from lists
        requirements = []
        responsibilities = []
        for section in p.get("lists", []):
            heading = (section.get("text", "") or "").lower()
            items = _strip_html(section.get("content", "")).split("\n")
            items = [i.strip() for i in items if i.strip()]
            if any(kw in heading for kw in ["requirement", "qualification", "need", "looking for"]):
                requirements.extend(items)
            elif any(kw in heading for kw in ["responsibilit", "you'll do", "role", "about"]):
                responsibilities.extend(items)

        work_type = workplace or _detect_work_type(location, description)

        matched.append(JobPosting(
            source="lever",
            company_name=company_slug.replace("-", " ").title(),
            job_title=title,
            careers_url=apply_url,
            company_url=f"https://jobs.lever.co/{company_slug}",
            location=location,
            work_type=work_type,
            requirements=requirements,
            responsibilities=responsibilities,
            full_description=description[:3000],
        ))

    return matched


def _extract_keywords(criteria: SearchCriteria) -> list[str]:
    """Extract matching keywords from search queries."""
    keywords = set()
    role_keywords = [
        "engineer", "developer", "mlops", "llmops", "devops", "platform",
        "data", "ml", "ai", "machine learning", "sre", "reliability",
        "cloud", "infrastructure", "analytics", "databricks", "backend",
        "python", "kubernetes", "terraform",
    ]
    for query in criteria.queries:
        for kw in role_keywords:
            if kw in query.lower():
                keywords.add(kw)
    # Always include these broad terms
    keywords.update(["engineer", "developer"])
    return list(keywords)


def _matches_keywords(title: str, description: str, keywords: list[str]) -> bool:
    """Check if a job matches any of the search keywords."""
    combined = f"{title} {description[:500]}"
    return any(kw in combined for kw in keywords)


def _detect_work_type(location: str, description: str) -> str:
    """Detect remote/hybrid/onsite from location and description."""
    combined = f"{location} {description[:500]}".lower()
    if "remote" in combined:
        return "remote"
    if "hybrid" in combined:
        return "hybrid"
    return "onsite"


def _strip_html(html: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", "\n", html)
    text = unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_section(text: str, headings: list[str]) -> list[str]:
    """Extract bullet points from a section matching given headings."""
    lines = text.split("\n")
    in_section = False
    items = []
    for line in lines:
        line_stripped = line.strip()
        line_lower = line_stripped.lower()
        if any(h in line_lower for h in headings):
            in_section = True
            continue
        if in_section:
            # Stop at next heading
            if line_stripped and line_stripped[0].isupper() and line_stripped.endswith(":"):
                break
            if line_stripped and len(line_stripped) > 10:
                items.append(line_stripped.lstrip("•-–◦ "))
    return items[:15]
