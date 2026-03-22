"""Resolve aggregator URLs to direct company career page URLs.

When a job was discovered via an aggregator (Indeed, Dice, etc.), the stored
URL points to the aggregator listing — not the actual application form. This
module scrapes the aggregator page to extract the real company name and job
title, then uses Exa to find the direct company career page.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from urllib.parse import urlparse

from exa_py import Exa

logger = logging.getLogger(__name__)

# Domains that are aggregators/job boards (not direct company pages)
AGGREGATOR_DOMAINS = {
    "indeed.com", "ziprecruiter.com", "dice.com", "glassdoor.com",
    "dailyremote.com", "remotive.com", "remotejobleads.com",
    "remotefront.com", "opentoworkremote.com", "jobicy.com",
    "careerbox.42web.io", "careerwave.lovestoblog.com",
    "remotepulse.42web.io", "fitt.co", "lensa.com",
    "tallo.com", "dynamitejobs.com", "virtualvocations.com",
    "itradar.io", "euremotejobs.com", "join.com",
    "digitalnovascotia.com", "platformengineering.org",
    "jobs.digitalhire.com", "citizenremote.com",
    "jobgether.com", "remoterocketship.com",
    "linkedin.com",
}


def is_aggregator(url: str) -> bool:
    """Check if a URL points to an aggregator site."""
    domain = (urlparse(url).hostname or "").replace("www.", "")
    return any(agg in domain for agg in AGGREGATOR_DOMAINS)


async def resolve_to_direct_url(
    company_name: str,
    job_title: str,
    aggregator_url: str,
) -> str | None:
    """Find the direct company career page URL for a job found via an aggregator.

    Uses Exa search to find the actual company's application page.

    Args:
        company_name: Company name (may be extracted from aggregator listing).
        job_title: Job title.
        aggregator_url: The aggregator URL we want to replace.

    Returns:
        Direct company career page URL, or None if not found.
    """
    exa = Exa(api_key=os.environ.get("EXA_API_KEY"))

    # Clean up company name — remove aggregator artifacts
    clean_company = _clean_company_name(company_name)
    clean_title = _clean_job_title(job_title)

    if not clean_company or clean_company in ("Open Position", "Indeed", "Dice", "Remote"):
        # Company name is generic — use the job title to search
        search_query = f"{clean_title} careers apply"
    else:
        search_query = f"{clean_company} {clean_title} careers apply"

    logger.info(f"Resolving URL for '{clean_company}' — '{clean_title}'")
    logger.info(f"  Search query: {search_query}")

    try:
        results = await asyncio.to_thread(
            exa.search_and_contents,
            search_query,
            text=True,
            type="auto",
            livecrawl="fallback",
            num_results=5,
            exclude_domains=list(AGGREGATOR_DOMAINS),
        )

        if not results.results:
            logger.warning(f"  No direct URL found for '{clean_company}'")
            return None

        # Pick the best result — prefer URLs with "career", "jobs", "apply" in path
        for result in results.results:
            url_lower = result.url.lower()
            if any(kw in url_lower for kw in ["career", "jobs", "apply", "position", "opening"]):
                logger.info(f"  Found direct URL: {result.url}")
                return result.url

        # Fallback to first result
        best = results.results[0].url
        logger.info(f"  Best match URL: {best}")
        return best

    except Exception as e:
        logger.warning(f"  URL resolution failed: {e}")
        return None


async def resolve_aggregator_jobs(db_path: str) -> dict:
    """Resolve all aggregator-failed jobs in the database to direct URLs.

    Updates the jobs table with resolved URLs and resets application status
    to 'approved' for jobs that get resolved.

    Returns:
        Dict with counts: resolved, unresolved, skipped.
    """
    import aiosqlite
    from ..models import ApplicationStatus

    resolved = 0
    unresolved = 0
    skipped = 0

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT a.id as app_id, j.id as job_id, j.company_name, j.job_title, j.careers_url
            FROM applications a JOIN jobs j ON a.job_id = j.id
            WHERE a.status = 'failed'
            AND a.error_message LIKE '%Aggregator%'
        """)
        rows = await cursor.fetchall()

        logger.info(f"Resolving {len(rows)} aggregator URLs...")

        for r in rows:
            company = r["company_name"]
            title = r["job_title"]
            old_url = r["careers_url"]

            # Skip generic Indeed "about" pages — no specific job info
            if old_url.endswith("/about") or old_url.endswith("/"):
                if title == "Open Position":
                    logger.info(f"  Skipping generic listing: {company}")
                    skipped += 1
                    # Mark as permanently skipped
                    await db.execute(
                        "UPDATE applications SET status = 'skipped', error_message = 'Generic aggregator page — no specific job to resolve' WHERE id = ?",
                        (r["app_id"],)
                    )
                    continue

            direct_url = await resolve_to_direct_url(company, title, old_url)

            if direct_url and not is_aggregator(direct_url):
                # Update the job URL
                await db.execute(
                    "UPDATE jobs SET careers_url = ? WHERE id = ?",
                    (direct_url, r["job_id"])
                )
                # Reset application to approved for retry
                await db.execute(
                    "UPDATE applications SET status = 'approved', error_message = NULL, retry_count = 0 WHERE id = ?",
                    (r["app_id"],)
                )
                resolved += 1
                logger.info(f"  RESOLVED: {company} — {title}")
                logger.info(f"    Old: {old_url}")
                logger.info(f"    New: {direct_url}")
            else:
                unresolved += 1
                logger.info(f"  UNRESOLVED: {company} — {title}")

            # Rate limit Exa calls
            await asyncio.sleep(2)

        await db.commit()

    return {"resolved": resolved, "unresolved": unresolved, "skipped": skipped}


def _clean_company_name(name: str) -> str:
    """Strip aggregator artifacts from company names."""
    # Remove common suffixes
    for suffix in [
        " - Indeed", " | Indeed", " - Dice", " | Dice",
        " - LinkedIn", " | LinkedIn", " - ZipRecruiter",
        " | Remote Jobs USA", " | Remote Jobs",
        " - DailyRemote", " - Jobicy", " | Lensa",
        " - Jobgether", " | Citizen Remote",
        " jobs in Remote", " jobs in United States",
        " (NOW HIRING)", " Mar 2026", " Feb 2026",
    ]:
        if name.endswith(suffix):
            name = name[: -len(suffix)]

    # Remove leading dollar ranges
    name = re.sub(r"^\$[\d,k]+-\$?[\d,k]+\s+", "", name)

    # Remove leading numbers with commas
    name = re.sub(r"^[\d,]+\+?\s+", "", name)

    return name.strip()


def _clean_job_title(title: str) -> str:
    """Strip generic/aggregator artifacts from job titles."""
    if title == "Open Position":
        return ""
    # Remove location suffixes
    title = re.sub(r"\s*\(Remote\)\s*$", "", title)
    title = re.sub(r"\s*\|\s*Remote Jobs USA\s*$", "", title)
    return title.strip()
