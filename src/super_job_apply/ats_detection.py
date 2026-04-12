"""ATS platform detection, blocking, and form-filling strategy hints.

Identifies which Applicant Tracking System hosts a given job URL and
provides per-platform metadata to guide the browser automation layer.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ATS URL patterns → platform name
# ---------------------------------------------------------------------------

# Each tuple is (hostname_pattern, platform_name).
# Patterns are matched against the URL hostname (after stripping "www.").
# A pattern ending with a dot is a suffix match; otherwise it's an exact or
# substring match against the hostname.
_ATS_PATTERNS: list[tuple[str, str]] = [
    # Greenhouse — both board URLs and company-hosted pages that redirect to GH
    ("boards.greenhouse.io", "greenhouse"),
    ("job-boards.greenhouse.io", "greenhouse"),
    ("greenhouse.io", "greenhouse"),
    # Lever
    ("jobs.lever.co", "lever"),
    ("lever.co", "lever"),
    # Workable
    ("jobs.workable.com", "workable"),
    ("apply.workable.com", "workable"),
    # Ashby
    ("jobs.ashbyhq.com", "ashby"),
    # SmartRecruiters
    ("jobs.smartrecruiters.com", "smartrecruiters"),
    ("careers.smartrecruiters.com", "smartrecruiters"),
    # Jobvite
    ("jobs.jobvite.com", "jobvite"),
    # Workday
    ("myworkdayjobs.com", "workday"),
    ("myworkdaysite.com", "workday"),
    # iCIMS
    ("icims.com", "icims"),
    # Taleo
    ("taleo.net", "taleo"),
    # TeamTailor
    ("teamtailor.com", "teamtailor"),
]

# Query-string markers that betray a Greenhouse backend even on a company domain
_GH_QUERY_MARKERS = re.compile(r"[?&]gh_jid=", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Blocked platforms — these use reCAPTCHA/hCaptcha that defeats automation
# ---------------------------------------------------------------------------

BLOCKED_ATS: set[str] = {"greenhouse", "lever"}

# Domains with no application forms (listing-only aggregators, Cloudflare-blocked)
USELESS_DOMAINS: set[str] = {
    "flexionis.wuaze.com", "hireza.wuaze.com", "jobflarely.liveblog365.com",
}

# ---------------------------------------------------------------------------
# Per-platform form-filling strategy hints
# ---------------------------------------------------------------------------

ATS_FORM_HINTS: dict[str, dict] = {
    "workable": {
        "description": "Standard HTML forms. act() works well.",
        "form_type": "standard_html",
        "needs_special_handling": False,
        "tips": [
            "Fields use plain <input> and <select> elements",
            "Resume upload is a standard file input",
            "Submit button is usually 'Submit Application'",
        ],
    },
    "ashby": {
        "description": "Clean HTML forms, straightforward field layout.",
        "form_type": "standard_html",
        "needs_special_handling": False,
        "tips": [
            "Single-page application form",
            "Standard text inputs and dropdowns",
            "File upload for resume works normally",
        ],
    },
    "smartrecruiters": {
        "description": "Mostly standard HTML with some custom components.",
        "form_type": "standard_html",
        "needs_special_handling": False,
        "tips": [
            "May have multi-step form (click Next between sections)",
            "Some custom dropdown components but generally accessible",
            "Watch for 'Continue' vs 'Submit' buttons between steps",
        ],
    },
    "jobvite": {
        "description": "Standard HTML forms, older-style layout.",
        "form_type": "standard_html",
        "needs_special_handling": False,
        "tips": [
            "Single page application form",
            "Standard file upload for resume",
            "May have EEOC/demographic questions at the end",
        ],
    },
    "workday": {
        "description": "Multi-step wizard with custom components.",
        "form_type": "custom_components",
        "needs_special_handling": True,
        "tips": [
            "Often requires account creation first",
            "Multi-page wizard — navigate with Next/Continue",
            "Custom autocomplete dropdowns — may need type + select",
            "Resume parsing may auto-fill some fields",
        ],
    },
    "icims": {
        "description": "Standard HTML forms, sometimes iframed.",
        "form_type": "standard_html",
        "needs_special_handling": False,
        "tips": [
            "May require clicking into an iframe",
            "Standard text inputs for most fields",
            "File upload for resume",
        ],
    },
    "taleo": {
        "description": "Legacy Oracle ATS with multi-step forms.",
        "form_type": "custom_components",
        "needs_special_handling": True,
        "tips": [
            "Multi-page application flow",
            "May require account creation",
            "Older-style HTML but generally standard inputs",
        ],
    },
    "teamtailor": {
        "description": "Modern clean HTML forms.",
        "form_type": "standard_html",
        "needs_special_handling": False,
        "tips": [
            "Clean single-page forms",
            "Standard HTML inputs",
            "Simple file upload",
        ],
    },
    "greenhouse": {
        "description": "React-based forms with reCAPTCHA. BLOCKED.",
        "form_type": "react_components",
        "needs_special_handling": True,
        "blocked": True,
        "tips": [
            "Uses React comboboxes instead of standard HTML selects",
            "reCAPTCHA v2/v3 blocks automated submissions",
            "Custom file upload component",
        ],
    },
    "lever": {
        "description": "Custom forms with hCaptcha. BLOCKED.",
        "form_type": "custom_components",
        "needs_special_handling": True,
        "blocked": True,
        "tips": [
            "hCaptcha on submission form",
            "Relatively simple form layout but captcha is the blocker",
        ],
    },
    "direct": {
        "description": "Direct company career page — varies widely.",
        "form_type": "unknown",
        "needs_special_handling": False,
        "tips": [
            "Form structure varies per company",
            "observe() + act() field-by-field approach works best",
            "May use any backend ATS — watch for redirects to Greenhouse/Lever",
        ],
    },
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_ats_platform(url: str) -> str | None:
    """Identify what ATS platform a URL belongs to.

    Returns the platform name (e.g. "greenhouse", "workable") or "direct"
    for company-hosted career pages, or None if the URL is unrecognised.

    Also catches company-hosted URLs that secretly redirect to Greenhouse
    by checking for ``gh_jid`` query parameters.
    """
    if not url:
        return None

    # Check for Greenhouse query-string markers first (e.g. careers.airbnb.com?gh_jid=...)
    if _GH_QUERY_MARKERS.search(url):
        return "greenhouse"

    hostname = (urlparse(url).hostname or "").lower().replace("www.", "")

    for pattern, platform in _ATS_PATTERNS:
        if hostname == pattern or hostname.endswith(f".{pattern}"):
            return platform

    # If we have a hostname but didn't match an ATS, it's a direct company page
    if hostname:
        return "direct"

    return None


def is_blocked_ats(url: str) -> bool:
    """Return True if the URL points to a blocked ATS platform or useless domain."""
    platform = detect_ats_platform(url)
    if platform in BLOCKED_ATS:
        return True
    # Also block known useless domains
    hostname = (urlparse(url).hostname or "").lower().replace("www.", "")
    return hostname in USELESS_DOMAINS


def get_form_hints(platform: str | None) -> dict:
    """Return form-filling hints for a given ATS platform.

    Falls back to the "direct" hints if the platform is unknown.
    """
    if platform and platform in ATS_FORM_HINTS:
        return ATS_FORM_HINTS[platform]
    return ATS_FORM_HINTS["direct"]


def get_submittable_platforms() -> set[str]:
    """Return the set of ATS platform names that are NOT blocked."""
    return {name for name in ATS_FORM_HINTS if not ATS_FORM_HINTS[name].get("blocked")}
