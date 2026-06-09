"""Fast-path form filling using direct Playwright CSS selectors.

For common fields (name, email, phone, LinkedIn, location), we can fill
values directly via Playwright without invoking Stagehand act() / the LLM.
This is 5-20x faster per field and more deterministic.

After fast-fill runs, observe() + act() still handles unknown / custom
fields (EEO, cover letter, company-specific questions).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from playwright.async_api import async_playwright

from ..models import CandidateProfile

logger = logging.getLogger(__name__)


# Attribute-substring selectors that match across most ATS platforms.
# Order matters: most specific first. Stop at the first hit per field.
_FIELD_SELECTORS: dict[str, list[str]] = {
    "first_name": [
        'input[name="first_name"]',
        'input[id="first_name"]',
        'input[name*="first_name" i]',
        'input[id*="first_name" i]',
        'input[name="firstName"]',
        'input[id="firstName"]',
        'input[placeholder*="First name" i]',
        'input[aria-label*="First name" i]',
    ],
    "last_name": [
        'input[name="last_name"]',
        'input[id="last_name"]',
        'input[name*="last_name" i]',
        'input[id*="last_name" i]',
        'input[name="lastName"]',
        'input[id="lastName"]',
        'input[placeholder*="Last name" i]',
        'input[aria-label*="Last name" i]',
    ],
    "full_name": [
        'input[name="name"]',
        'input[id="name"]',
        'input[name*="full_name" i]',
        'input[id*="full_name" i]',
        'input[name*="fullName" i]',
        'input[placeholder*="Full name" i]',
        'input[aria-label*="Full name" i]',
    ],
    "email": [
        'input[type="email"]',
        'input[name*="email" i]',
        'input[id*="email" i]',
        'input[aria-label*="Email" i]',
    ],
    "phone": [
        'input[type="tel"]',
        'input[name*="phone" i]',
        'input[id*="phone" i]',
        'input[aria-label*="Phone" i]',
    ],
    "location": [
        'input[name*="location" i]',
        'input[id*="location" i]',
        'input[name*="city" i]',
        'input[id*="city" i]',
        'input[name*="current_location" i]',
        'input[name*="address" i]',
        'input[placeholder*="Location" i]',
    ],
    "linkedin": [
        'input[name*="linkedin" i]',
        'input[id*="linkedin" i]',
        'input[placeholder*="linkedin" i]',
        'input[aria-label*="linkedin" i]',
    ],
    "website": [
        'input[name="website"]',
        'input[name*="portfolio" i]',
        'input[name*="website" i]',
        'input[name*="personal_site" i]',
        'input[placeholder*="Portfolio" i]',
    ],
    "github": [
        'input[name*="github" i]',
        'input[id*="github" i]',
        'input[placeholder*="github" i]',
    ],
    "company": [
        'input[name*="org" i]',
        'input[name*="company" i]',
        'input[id*="company" i]',
        'input[placeholder*="Current company" i]',
    ],
}


@dataclass
class FastFillResult:
    filled: list[str]
    skipped: list[str]
    errors: list[str]

    @property
    def count(self) -> int:
        return len(self.filled)


def _candidate_value(candidate: CandidateProfile, field: str) -> str:
    """Resolve a field name to a candidate value (empty string if unknown)."""
    acct = candidate.account_email or candidate.email
    first_name, _, last_name = candidate.name.partition(" ")
    portfolio = candidate.portfolio_url or ""
    return {
        "first_name": first_name,
        "last_name": last_name,
        "full_name": candidate.name,
        "email": acct,
        "phone": candidate.phone,
        "location": candidate.location,
        "linkedin": candidate.linkedin_url,
        "website": portfolio,
        "github": portfolio,
        "company": "",  # current company unknown — skip
    }.get(field, "")


async def fast_fill_common_fields(
    cdp_url: str,
    candidate: CandidateProfile,
    prefix: str = "",
) -> FastFillResult:
    """Direct-fill common fields via Playwright. Returns stats for logging.

    Connects to the existing Browserbase session over CDP. Uses each
    field's selector list and stops on first visible, writable match.
    Skips fields with no matching element or no candidate value.
    """
    filled: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            errors.append(f"cdp connect: {e}")
            return FastFillResult(filled, skipped, errors)

        contexts = browser.contexts
        if not contexts or not contexts[0].pages:
            errors.append("no browser context/page")
            return FastFillResult(filled, skipped, errors)
        page = contexts[0].pages[0]

        # Forms are often embedded in an iframe (e.g. Greenhouse embeds on
        # company career pages) — search every frame, main frame first.
        frames = page.frames

        for field, selectors in _FIELD_SELECTORS.items():
            value = _candidate_value(candidate, field)
            if not value:
                skipped.append(field)
                continue

            hit = False
            for frame in frames:
                for sel in selectors:
                    try:
                        loc = frame.locator(sel).first
                        if await loc.count() == 0:
                            continue
                        if not await loc.is_visible():
                            continue
                        if not await loc.is_editable():
                            continue
                        await loc.fill(value, timeout=3000)
                        filled.append(field)
                        hit = True
                        break
                    except Exception as e:
                        # Selector invalid or element detached — try next
                        errors.append(f"{field} [{sel[:40]}]: {str(e)[:60]}")
                        continue
                if hit:
                    break

            if not hit:
                skipped.append(field)

    logger.info(
        f"{prefix} fast-fill: {len(filled)} filled, {len(skipped)} skipped, "
        f"{len(errors)} errors. Filled: {filled}"
    )
    return FastFillResult(filled, skipped, errors)
