"""Browser automation for job application form filling.

Uses Stagehand act() for atomic browser actions with:
- observe() to discover all form fields before filling
- Programmatic field-to-data matching
- Security code detection and email-based code entry
- ATS platform detection to skip blocked sites and apply platform hints
- Post-submission verification with confidence levels
- Clear logging at every step for visibility
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from stagehand import AsyncStagehand

from ..ats_detection import (
    ATS_FORM_HINTS,
    BLOCKED_ATS,
    JS_SUBMIT_ATS,
    detect_ats_platform,
    get_form_hints,
    is_blocked_ats,
)
from ..config import get_model_api_key
from ..models import (
    ApplicationSettings,
    CandidateProfile,
    JOB_DESCRIPTION_SCHEMA,
    JobPosting,
)
from .ats_fillers import fast_fill_common_fields
from .email_verifier import EmailWatcher
from .form_cache import FormCache
from .submit_handlers import (
    find_apply_link,
    find_form_frame,
    find_new_tab_url,
    js_submit_form,
)
from .uploader import upload_resume

_FORM_CACHE = FormCache()

# Map fast-fill field names → keyword substrings in field_map/observe() descriptions.
# When fast-fill succeeds for a field, these keys are removed from the act() loop
# so we don't re-submit the same value via the LLM.
_FASTFILL_TO_MAP_KEYS: dict[str, set[str]] = {
    "first_name": {"first name"},
    "last_name": {"last name"},
    "full_name": {"first name", "last name"},  # if we filled full_name, both sub-fields are covered
    "email": {"email"},
    "phone": {"phone"},
    "location": {"location", "address", "city"},
    "linkedin": {"linkedin"},
    "website": {"website", "portfolio"},
    "github": {"github"},
}

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Submission verification helpers
# ---------------------------------------------------------------------------

_SUCCESS_INDICATORS = re.compile(
    r"thank\s*you|application\s*received|successfully\s*submitted|"
    r"we.ve\s*received|we\s*have\s*received|received\s*your\s*application|"
    r"application\s*complete|"
    r"you.re\s*all\s*set|your\s*application\s*has\s*been|"
    r"confirmation|submitted\s*successfully|we\s*will\s*review|"
    r"application\s*submitted|thanks\s*for\s*applying|applied\s*successfully",
    re.IGNORECASE,
)

_FAILURE_INDICATORS = re.compile(
    r"required\s*field|please\s*complete|please\s*fill|"
    r"form\s*error|validation\s*error|field\s*is\s*required|is\s*required|can.t\s*be\s*blank|"
    r"must\s*be\s*filled|something\s*went\s*wrong|"
    r"please\s*fix|highlighted\s*in\s*red|"
    r"recaptcha|hcaptcha|captcha\s*challenge",
    re.IGNORECASE,
)

_CAPTCHA_INDICATORS = re.compile(
    r"recaptcha|hcaptcha|captcha|verify\s*you.re\s*human|"
    r"i.m\s*not\s*a\s*robot|challenge",
    re.IGNORECASE,
)


def verify_submission_result(
    page_type: str,
    confirmation_message: str,
    has_form_fields: bool = False,
    page_description: str = "",
    empty_required_fields: list[str] | None = None,
) -> dict:
    """Analyze post-submission page state and return a confidence assessment.

    Returns a dict with:
        confidence: "confirmed" | "likely_submitted" | "uncertain" | "failed"
        reason: human-readable explanation
        has_captcha: bool — captcha challenge detected
    """
    combined_text = f"{page_type} {confirmation_message} {page_description}".strip()

    has_captcha = bool(_CAPTCHA_INDICATORS.search(combined_text))
    has_success = bool(_SUCCESS_INDICATORS.search(combined_text))
    has_failure = bool(_FAILURE_INDICATORS.search(combined_text))
    has_empties = bool(empty_required_fields)

    # Captcha blocks everything
    if has_captcha and not has_success:
        return {
            "confidence": "failed",
            "reason": f"Captcha challenge detected: {combined_text[:120]}",
            "has_captcha": True,
        }

    # Clear confirmation signal
    if has_success and not has_failure:
        return {
            "confidence": "confirmed",
            "reason": f"Success indicator found: {confirmation_message[:120] or page_type}",
            "has_captcha": False,
        }

    # Page type says confirmation but we also see errors
    if "confirmation" in page_type.lower() and not has_failure:
        return {
            "confidence": "confirmed",
            "reason": f"Confirmation page detected: {page_type}",
            "has_captcha": False,
        }

    # Form disappeared (no more form fields) and no error signal
    if not has_form_fields and not has_failure and not has_captcha:
        return {
            "confidence": "likely_submitted",
            "reason": "Form fields no longer visible, no error detected",
            "has_captcha": False,
        }

    # We have errors or required-field warnings
    if has_failure or has_empties:
        return {
            "confidence": "failed",
            "reason": f"Validation errors or required fields: {combined_text[:120]}",
            "has_captcha": has_captcha,
        }

    # Nothing conclusive
    return {
        "confidence": "uncertain",
        "reason": f"No clear success/failure signal: {combined_text[:120]}",
        "has_captcha": has_captcha,
    }


async def apply_to_job(
    job: JobPosting,
    candidate: CandidateProfile,
    settings: ApplicationSettings,
    resume_path: str | None = None,
    cover_letter_text: str | None = None,
    submit: bool = False,
    email_watcher: EmailWatcher | None = None,
) -> dict:
    """Apply to a single job using step-by-step browser automation.

    Flow:
      0. Detect ATS platform; skip if blocked (Greenhouse/Lever)
      1. Navigate to job page
      2. Click Apply
      3. Handle login/signup if needed
      4. Detect if we have an application form
      5. Use observe() to discover all form fields
      6. Fill each field based on candidate data (with platform hints)
      7. Upload resume via Playwright CDP
      8. Click submit
      9. Handle security code if needed
      10. Verify confirmation page with confidence level
    """
    prefix = f"[{job.company_name}]"
    acct_email = candidate.account_email or candidate.email
    model = settings.agent_model

    # === STEP 0: ATS platform detection ===
    ats_platform = detect_ats_platform(job.careers_url)
    logger.info(f"{prefix} ATS platform: {ats_platform or 'unknown'} — {job.careers_url[:80]}")

    if ats_platform and ats_platform in BLOCKED_ATS:
        logger.warning(
            f"{prefix} SKIPPED: {ats_platform} is blocked (reCAPTCHA/hCaptcha). "
            f"URL: {job.careers_url[:100]}"
        )
        return {
            "success": False,
            "message": f"Blocked ATS platform: {ats_platform}",
            "session_url": None,
            "account_created": False,
            "ats_platform": ats_platform,
            "submission_confidence": "failed",
        }

    platform_hints = get_form_hints(ats_platform)

    client = AsyncStagehand(
        browserbase_api_key=os.environ.get("BROWSERBASE_API_KEY"),
        browserbase_project_id=os.environ.get("BROWSERBASE_PROJECT_ID"),
        model_api_key=get_model_api_key(),
    )

    # Enable proxy + stealth to avoid reCAPTCHA detection on Greenhouse etc.
    session_params = {"wait_for_captcha_solves": True}
    if settings.use_proxy:
        session_params["browserbase_session_create_params"] = {
            "proxies": True,
        }

    start_response = await client.sessions.start(model_name=settings.model, **session_params)
    sid = start_response.data.session_id
    session_url = f"https://browserbase.com/sessions/{sid}"
    logger.info(f"{prefix} Session: {session_url}")

    # When the application form lives inside an iframe (Greenhouse/Workable
    # embeds), all Stagehand calls must target that frame's ID.
    frame_target: dict = {"id": None}

    def _frame_kwargs() -> dict:
        return {"frame_id": frame_target["id"]} if frame_target["id"] else {}

    async def act(instruction: str) -> str:
        """Single atomic browser action with logging."""
        try:
            resp = await client.sessions.act(
                id=sid, input=instruction,
                options={"model": {"model_name": model}, "timeout": 30000},
                timeout=60.0,
                **_frame_kwargs(),
            )
            msg = resp.data.result.message if resp.data and resp.data.result else "No result"
            success = resp.data.result.success if resp.data and resp.data.result else False
            logger.info(f"{prefix}   act: {instruction[:60]}... → {'OK' if success else msg[:40]}")
            return msg
        except Exception as e:
            logger.warning(f"{prefix}   act FAILED: {instruction[:50]}... → {e}")
            return f"Error: {e}"

    async def extract(instruction: str) -> dict:
        """Extract structured data from page."""
        try:
            resp = await client.sessions.extract(
                id=sid, instruction=instruction,
                schema={
                    "type": "object",
                    "properties": {
                        "page_type": {"type": "string"},
                        "has_form_fields": {"type": "boolean"},
                        "has_submit_button": {"type": "boolean"},
                        "asks_for_security_code": {"type": "boolean", "description": "Page asks to enter a security code or verification code"},
                        "confirmation_message": {"type": "string"},
                        "page_description": {"type": "string"},
                        "empty_required_fields": {"type": "array", "items": {"type": "string"}},
                    },
                },
                timeout=30.0,
                **_frame_kwargs(),
            )
            return resp.data.result or {}
        except Exception as e:
            logger.warning(f"{prefix}   extract failed: {e}")
            return {}

    try:
        # === STEP 1: Navigate ===
        logger.info(f"{prefix} STEP 1: Navigate to {job.careers_url[:60]}")
        await client.sessions.navigate(id=sid, url=job.careers_url)
        await asyncio.sleep(3)

        # === STEP 2: Assess page and click Apply ===
        logger.info(f"{prefix} STEP 2: Assess page")
        page = await extract("What is this page? Is it an application form with input fields, a job listing with an Apply button, or something else?")
        page_type = page.get("page_type", "other")
        has_form = page.get("has_form_fields", False)
        logger.info(f"{prefix}   Page: {page_type} | Form fields: {has_form} | {page.get('page_description', '')[:60]}")

        if not has_form:
            logger.info(f"{prefix} STEP 2b: Clicking Apply...")

            # Handle privacy/cookie/agreement pages first
            page_desc = page.get("page_description", "").lower()
            if "privacy" in page_desc or "agreement" in page_desc or "cookie" in page_desc or "consent" in page_desc:
                logger.info(f"{prefix}   Privacy/agreement page detected — accepting...")
                await act("Click 'Accept', 'I Agree', 'I Accept', 'Continue', or 'OK' to accept the privacy agreement or cookies")
                await asyncio.sleep(2)

            # Deterministic first: if Apply is a plain <a href>, navigate
            # straight to it — LLM clicks can land on styled overlays and
            # silently do nothing (e.g. UHG's Taleo anchor).
            cdp_url = f"wss://connect.browserbase.com?apiKey={os.environ.get('BROWSERBASE_API_KEY')}&sessionId={sid}"
            try:
                apply_href = await find_apply_link(cdp_url, job.careers_url, prefix)
            except Exception as e:
                logger.warning(f"{prefix}   apply-link check failed: {e}")
                apply_href = None
            if apply_href:
                logger.info(f"{prefix}   Navigating to apply link: {apply_href[:60]}")
                await client.sessions.navigate(id=sid, url=apply_href)
                await asyncio.sleep(4)
            else:
                await act("Click the 'Apply', 'Apply Now', 'Apply for this job', or 'Start Application' button")
                await asyncio.sleep(4)

            # Re-assess
            page = await extract("Is this now an application form with input fields? Or a login page? Or still a listing?")
            page_type = page.get("page_type", "other")
            has_form = page.get("has_form_fields", False)
            logger.info(f"{prefix}   After click: {page_type} | Form: {has_form}")

            if not has_form and page_type not in ("login_page",):
                # The Apply click may have opened the form in a new tab
                # (target=_blank) — Stagehand stays on the original tab and
                # never sees it. Follow the new tab's URL in the main tab.
                cdp_url = f"wss://connect.browserbase.com?apiKey={os.environ.get('BROWSERBASE_API_KEY')}&sessionId={sid}"
                try:
                    new_tab_url = await find_new_tab_url(cdp_url, job.careers_url, prefix)
                except Exception as e:
                    logger.warning(f"{prefix}   new-tab check failed: {e}")
                    new_tab_url = None
                if new_tab_url:
                    logger.info(f"{prefix}   Following new tab: {new_tab_url[:60]}")
                    await client.sessions.navigate(id=sid, url=new_tab_url)
                    await asyncio.sleep(3)
                    page = await extract("Is this an application form with input fields? Or a login page?")
                    page_type = page.get("page_type", "other")
                    has_form = page.get("has_form_fields", False)
                    logger.info(f"{prefix}   After tab-follow: {page_type} | Form: {has_form}")

            if not has_form and page_type not in ("login_page",):
                # Try scrolling down — some sites hide the form below the fold
                await act("Scroll down to find the application form or Apply button")
                await asyncio.sleep(2)
                await act("Click any Apply, Start Application, Submit Application, or Apply for this position button")
                await asyncio.sleep(4)
                page = await extract("Is there an application form with input fields now?")
                page_type = page.get("page_type", "other")
                has_form = page.get("has_form_fields", False)
                logger.info(f"{prefix}   After scroll+click: {page_type} | Form: {has_form}")

            if not has_form and page_type not in ("login_page",):
                # Last resort: check if the URL changed and there's a form on the new page
                await act("Look for any link or button that leads to an application form and click it")
                await asyncio.sleep(3)
                page = await extract("Is there an application form with input fields now?")
                has_form = page.get("has_form_fields", False)

        # === STEP 3: Handle login/signup ===
        if page.get("page_type") == "login_page" or page.get("asks_for_security_code"):
            logger.info(f"{prefix} STEP 3: Login/signup handling")
            await act(f"Sign in or create account with email '{acct_email}' and password '{settings.account_password}'. Name: '{candidate.name}'")
            await asyncio.sleep(3)

        # === STEP 4: Verify we have a form ===
        if not has_form:
            page = await extract("Does this page have a job application form with text input fields?")
            has_form = page.get("has_form_fields", False)

        if not has_form:
            # The form may live inside an iframe (Greenhouse/Workable embed)
            # that main-frame extract() cannot see.
            cdp_url = f"wss://connect.browserbase.com?apiKey={os.environ.get('BROWSERBASE_API_KEY')}&sessionId={sid}"
            try:
                form_frame = await find_form_frame(cdp_url, prefix)
            except Exception as e:
                logger.warning(f"{prefix}   iframe check failed: {e}")
                form_frame = None
            if form_frame:
                frame_target["id"] = form_frame["frame_id"]
                has_form = True
                logger.info(
                    f"{prefix}   Targeting form iframe ({form_frame['input_count']} inputs)"
                )

        if not has_form:
            desc = page.get("page_description", "")[:100]
            logger.warning(f"{prefix} NO FORM FOUND: {page_type} — {desc}")
            return {"success": False, "message": f"No form: {page_type}. {desc}", "session_url": session_url, "account_created": False}

        # === STEP 4.5: Fast-fill common fields via Playwright CSS selectors ===
        # Skips LLM for ~5-9 common fields (name, email, phone, LinkedIn, location, etc.)
        skip_keys: set[str] = set()
        fastfill_count = 0
        try:
            cdp_url = f"wss://connect.browserbase.com?apiKey={os.environ.get('BROWSERBASE_API_KEY')}&sessionId={sid}"
            ff_result = await fast_fill_common_fields(cdp_url, candidate, prefix)
            fastfill_count = ff_result.count
            for field in ff_result.filled:
                skip_keys.update(_FASTFILL_TO_MAP_KEYS.get(field, set()))
        except Exception as e:
            logger.warning(f"{prefix}   fast-fill skipped: {e}")

        # === STEP 5: Observe ALL form fields (scoped to <form> when possible) ===
        logger.info(f"{prefix} STEP 5: Discovering all form fields with observe()...")
        field_names: list[str] = []

        def _desc_of(f) -> str:
            return f.description if hasattr(f, "description") else (
                f.get("description", "") if isinstance(f, dict) else str(f)
            )

        async def _call_observe(selector: str | None) -> list:
            kwargs = {
                "id": sid,
                "instruction": (
                    "Find ALL input fields, text areas, dropdowns, checkboxes, "
                    "radio buttons, and file upload fields in the application form"
                ),
            }
            if selector:
                # Python Stagehand SDK puts selector under options, not top-level
                kwargs["options"] = {"selector": selector}
            kwargs.update(_frame_kwargs())
            resp = await client.sessions.observe(**kwargs)
            data = resp.data if isinstance(resp.data, list) else (
                resp.data.result if hasattr(resp.data, "result") else []
            )
            return data or []

        # Check cache first — skip observe() entirely on hit.
        cached = _FORM_CACHE.lookup(ats_platform, job.careers_url)
        if cached:
            field_names = [c.get("description", "") for c in cached if c.get("description")]
            logger.info(f"{prefix}   cache HIT: {len(field_names)} cached fields (ats={ats_platform})")
            _FORM_CACHE.record_hit(ats_platform, job.careers_url)
        else:
            # Scoping to //form cuts LLM tokens ~2-3x and filters navigation/footer inputs.
            # Fall back to unscoped if the page has no <form> element or observe returns empty.
            try:
                observed = await _call_observe(selector="//form")
                scope = "form-scoped"
                if not observed:
                    logger.info(f"{prefix}   //form empty — retrying unscoped")
                    observed = await _call_observe(selector=None)
                    scope = "unscoped"
                if not observed and not frame_target["id"]:
                    # Nothing on the main frame — the form may be iframe-embedded
                    cdp_url = f"wss://connect.browserbase.com?apiKey={os.environ.get('BROWSERBASE_API_KEY')}&sessionId={sid}"
                    try:
                        form_frame = await find_form_frame(cdp_url, prefix)
                    except Exception as e:
                        logger.warning(f"{prefix}   iframe check failed: {e}")
                        form_frame = None
                    if form_frame:
                        frame_target["id"] = form_frame["frame_id"]
                        observed = await _call_observe(selector=None)
                        scope = f"iframe ({form_frame['input_count']} inputs)"
                field_names = [_desc_of(f) for f in observed]
                logger.info(f"{prefix}   Found {len(field_names)} form fields ({scope})")
                # Persist for next time — store as normalized dicts.
                # 1-2 field observations are usually a privacy/login page,
                # not the real form; caching them poisons every later job
                # on the same hostname.
                if len(field_names) >= 3:
                    _FORM_CACHE.save(
                        ats_platform,
                        job.careers_url,
                        [{"description": d} for d in field_names],
                    )
            except Exception as e:
                logger.warning(f"{prefix}   observe() failed: {e} — falling back to manual fill")
                field_names = []

        # === STEP 6: Fill every field using smart mapping ===
        logger.info(f"{prefix} STEP 6: Filling all fields...")

        name_parts = candidate.name.split()
        first = name_parts[0] if name_parts else candidate.name
        last = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
        sponsor = "No" if not candidate.requires_sponsorship else "Yes"
        relocate = "Yes" if candidate.willing_to_relocate else "No"
        cl = (cover_letter_text or "I am excited about this opportunity.")[:500]
        exp = (candidate.experience_summary or "")[:400]
        portfolio = candidate.portfolio_url or ""

        # Map field descriptions to values
        field_map = {
            "first name": first,
            "last name": last,
            "preferred": first,
            "email": acct_email,
            "phone": candidate.phone,
            "linkedin": candidate.linkedin_url,
            "location": candidate.location,
            "address": candidate.location,
            "city": candidate.location,
            "website": portfolio,
            "github": portfolio,
            "portfolio": portfolio,
            "personal preference": first,  # Name/pronoun preferences
        }

        # Dropdown/select mappings
        select_map = {
            "country dropdown": "United States",
            "visa sponsorship": sponsor,
            "require visa": sponsor,
            "require employment visa": sponsor,
            "open to relocation": relocate,
            "in-person": relocate,
            "hybrid": relocate,
            "interviewed at": "No",
            "applied before": "No",
            "hear about": "Job Board",
            "source": "Other",
            "authorized to work": "Yes",
            "ai policy": "I agree",
            "expertise coding in python": "Yes",
            "makes use of large language models": "Yes",
            "gender dropdown": "Decline to self-identify",
            "hispanic": "Decline to self-identify",
            "veteran status": "I am not a protected veteran",
            "disability status": "I do not wish to answer",
            "race": "Decline to self-identify",
            "ethnicity": "Decline to self-identify",
        }

        # Text area mappings (checked BEFORE select to prevent text areas being matched as selects)
        text_map = {
            "cover letter": cl,
            "why anthropic": cl,
            "why interested": cl,
            "why " + job.company_name.lower(): cl,
            "most complex and interesting": exp or candidate.experience_summary[:300],
            "describe the most complex": exp or candidate.experience_summary[:300],
            "examples of your work with llm": portfolio or candidate.portfolio_url or "",
            "examples of your work": portfolio or candidate.portfolio_url or "",
            "additional information": "Thank you for your consideration.",
            "deadline": "No specific deadlines",
            "timeline consideration": "No specific deadlines",
            "earliest you would want to start": "As soon as possible",
            "start working": "As soon as possible",
            "plan on working": candidate.location,
            "address from which": candidate.location,
            "publication": "",
        }

        fields_filled = fastfill_count  # Count fast-filled fields too

        for desc in field_names:
            dl = desc.lower()

            # Skip file uploads
            if any(kw in dl for kw in ["file upload", "resume", "attach", "cv upload"]):
                continue

            # Skip fields already handled by fast-fill
            if any(skip_kw in dl for skip_kw in skip_keys):
                continue

            # Try text area mapping FIRST (long-form fields like "describe the most complex...")
            matched = False
            for key, value in text_map.items():
                if key in dl:
                    if value:
                        clean_desc = desc.split(' text ')[0].split(' area')[0]
                        await act(f"Type '{value[:400]}' into the '{clean_desc}' field")
                        fields_filled += 1
                    matched = True
                    break

            if matched:
                await asyncio.sleep(0.3)
                continue

            # Try text field mapping (short fields: name, email, phone)
            for key, value in field_map.items():
                if key in dl and value:
                    clean_desc = desc.split(' text ')[0].split(' input')[0]
                    await act(f"Type '{value}' into the '{clean_desc}' field")
                    fields_filled += 1
                    matched = True
                    break

            if matched:
                await asyncio.sleep(0.3)
                continue

            # Try dropdown mapping
            for key, value in select_map.items():
                if key in dl:
                    clean_desc = desc.split(' dropdown')[0].split(' combobox')[0]
                    await act(f"Select '{value}' for the '{clean_desc}' field")
                    fields_filled += 1
                    matched = True
                    break

            if matched:
                await asyncio.sleep(0.3)
                continue

            # Checkbox — check it
            if "checkbox" in dl or "certif" in dl or "consent" in dl or "agree" in dl or "accept" in dl:
                await act(f"Check the '{desc}' checkbox")
                fields_filled += 1
                await asyncio.sleep(0.3)
                continue

        logger.info(f"{prefix}   Filled {fields_filled}/{len(field_names)} fields")

        # === STEP 7: Check all consent/policy checkboxes ===
        logger.info(f"{prefix} STEP 7: Ensuring all checkboxes checked")
        await act("Check ALL unchecked checkboxes on this page, especially certifications, privacy policies, consent forms, and terms of service")
        await asyncio.sleep(0.5)

        # === STEP 8: Upload resume ===
        if resume_path:
            logger.info(f"{prefix} STEP 8: Uploading resume")
            try:
                cdp_url = f"wss://connect.browserbase.com?apiKey={os.environ.get('BROWSERBASE_API_KEY')}&sessionId={sid}"
                await upload_resume(cdp_url, resume_path, prefix)
                logger.info(f"{prefix}   Resume uploaded: {resume_path}")
            except Exception as e:
                logger.warning(f"{prefix}   Resume upload failed: {e}")

        # === STEP 9: Final sweep — fill anything still empty ===
        logger.info(f"{prefix} STEP 9: Final sweep of empty required fields")
        remaining = await extract("List any REQUIRED fields that are still EMPTY. Exclude file upload / resume fields.")
        empty = remaining.get("empty_required_fields", [])

        if empty:
            logger.info(f"{prefix}   {len(empty)} still empty: {[e[:40] for e in empty[:5]]}")
            for field in empty[:10]:
                fl = field.lower()
                if any(skip in fl for skip in ["resume", "cv", "file", "upload"]):
                    continue
                await act(f"Fill or select an appropriate value for: '{field}'")
                await asyncio.sleep(0.3)
        else:
            logger.info(f"{prefix}   All required fields filled")

        # === STEP 10: Submit ===
        confirmation = ""
        submission_confidence = "not_submitted"
        if submit:
            if ats_platform in JS_SUBMIT_ATS:
                logger.info(f"{prefix} STEP 10: JS form.submit() for {ats_platform}")
                cdp_url = f"wss://connect.browserbase.com?apiKey={os.environ.get('BROWSERBASE_API_KEY')}&sessionId={sid}"
                js_result = await js_submit_form(cdp_url, prefix)
                if js_result.get("submitted") and js_result.get("confirmation_text"):
                    confirmation = js_result["confirmation_text"]
                    submission_confidence = "confirmed"
                    logger.info(f"{prefix} CONFIRMED via JS submit: {confirmation[:80]}")
                elif js_result.get("submitted"):
                    # Submitted but no confirmation keyword — fall through to extract()
                    logger.info(f"{prefix}   JS submit fired, verifying with extract...")
                else:
                    logger.warning(f"{prefix}   JS submit failed — falling back to act()")
                    await act("Click the Submit, Submit Application, Apply, or Send Application button")
                await asyncio.sleep(3)
            else:
                logger.info(f"{prefix} STEP 10: Clicking submit")
                await act("Click the Submit, Submit Application, Apply, or Send Application button")
                await asyncio.sleep(6)  # Give Greenhouse time to process + redirect

            # === STEP 11: Handle security code page ===
            post = await extract(
                "What is showing on this page RIGHT NOW? Options: "
                "1) A 'thank you' or 'application received' confirmation page, "
                "2) A page asking to 'enter your security code' with an input field for a code, "
                "3) The same application form with red error messages or 'required' warnings, "
                "4) Something else (describe it)"
            )
            asks_code = post.get("asks_for_security_code", False)
            confirmation = post.get("confirmation_message", "")
            post_type = post.get("page_type", "")

            logger.info(f"{prefix}   Post-submit: type={post_type}, code_asked={asks_code}, confirmation='{confirmation[:60]}'")

            # Handle validation errors — fix and retry submit (up to 3 attempts)
            for retry in range(3):
                if "error" not in post_type.lower() and "validation" not in post_type.lower() and "form" != post_type.lower():
                    break

                logger.info(f"{prefix}   Validation error (attempt {retry+1}/3) — fixing...")
                errors = await extract("List ALL validation error messages shown on this page. Include the exact error text.")
                error_fields = errors.get("empty_required_fields", [])
                error_desc = errors.get("page_description", "")[:120]
                logger.info(f"{prefix}   Errors: {error_desc}")

                if error_fields:
                    for field in error_fields[:10]:
                        fl = field.lower()
                        if any(skip in fl for skip in ["resume", "cv", "file"]):
                            continue
                        await act(f"Fix the error: '{field}'")
                        await asyncio.sleep(0.3)
                else:
                    # No specific fields — try generic fix
                    await act("Scroll through the form and fill any empty required fields highlighted in red or with error messages")
                    await asyncio.sleep(1)

                # Retry submit
                await act("Click the Submit, Submit Application, or Send Application button")
                await asyncio.sleep(4)

                post = await extract("Is this a confirmation/thank you page, security code page, or still has errors?")
                asks_code = post.get("asks_for_security_code", False)
                confirmation = post.get("confirmation_message", "")
                post_type = post.get("page_type", "")
                logger.info(f"{prefix}   Retry {retry+1} post-submit: type={post_type}, code={asks_code}, conf='{confirmation[:60]}'")

            # Handle reCAPTCHA — wait for Browserbase auto-solve then resubmit
            if confirmation and "recaptcha" in (confirmation + post_type).lower():
                logger.info(f"{prefix}   reCAPTCHA detected — waiting for auto-solve (30s)...")
                await asyncio.sleep(30)
                await act("Click the Submit, Submit Application, or Resubmit button")
                await asyncio.sleep(6)
                post = await extract("Is this now a confirmation page, security code page, or still reCAPTCHA?")
                asks_code = post.get("asks_for_security_code", False)
                confirmation = post.get("confirmation_message", "")
                post_type = post.get("page_type", "")
                logger.info(f"{prefix}   After reCAPTCHA wait: type={post_type}, code={asks_code}, conf='{confirmation[:60]}'")

            if asks_code and email_watcher and email_watcher.available:
                logger.info(f"{prefix} STEP 11: Security code required — checking email...")
                code = await email_watcher.wait_for_code(company_hint=job.company_name, timeout=120)
                if code:
                    logger.info(f"{prefix}   Got code: {code}")
                    await act(f"Type '{code}' into the security code or verification code input field")
                    await asyncio.sleep(1)
                    await act("Click the Submit, Verify, or Confirm button")
                    await asyncio.sleep(3)

                    # Check for confirmation after code entry
                    final = await extract("Is this now a confirmation or thank you page? Any success message?")
                    confirmation = final.get("confirmation_message", "")
                    post_type = final.get("page_type", "")
                    logger.info(f"{prefix}   After code: type={post_type}, msg='{confirmation[:60]}'")
                else:
                    logger.warning(f"{prefix}   No security code received from email")

            # --- Enhanced submission verification ---
            verification = verify_submission_result(
                page_type=post_type,
                confirmation_message=confirmation,
                has_form_fields=post.get("has_form_fields", False),
                page_description=post.get("page_description", ""),
                empty_required_fields=post.get("empty_required_fields"),
            )
            submission_confidence = verification["confidence"]
            logger.info(
                f"{prefix}   Verification: {submission_confidence} — {verification['reason'][:100]}"
            )

            # Override confirmation based on confidence
            if submission_confidence == "confirmed":
                if not confirmation:
                    confirmation = verification["reason"]
                logger.info(f"{prefix} CONFIRMED: {confirmation[:80]}")
            elif submission_confidence == "likely_submitted":
                if not confirmation:
                    confirmation = verification["reason"]
                logger.info(f"{prefix} LIKELY SUBMITTED: {verification['reason'][:80]}")
            elif submission_confidence == "failed":
                confirmation = ""
                logger.warning(f"{prefix} SUBMISSION FAILED: {verification['reason'][:80]}")
            else:
                logger.info(f"{prefix} UNCERTAIN: {verification['reason'][:80]}")

        # Determine result
        # A filled form only counts as success if verification did not
        # conclude the submission failed (captcha, validation errors).
        success = has_form and submission_confidence != "failed"
        confirmed = bool(confirmation)
        message = (
            f"Fields filled: {fields_filled}. "
            f"Confidence: {submission_confidence}. "
            f"{confirmation[:80] if confirmation else ''}"
        )

        if confirmed:
            logger.info(f"{prefix} APPLICATION SUBMITTED AND CONFIRMED")
        elif submit:
            logger.info(f"{prefix} Application submitted (confidence: {submission_confidence})")
        else:
            logger.info(f"{prefix} Form filled (dry run)")

        return {
            "success": success,
            "message": message,
            "session_url": session_url,
            "account_created": False,
            "confirmed": confirmed,
            "fields_filled": fields_filled,
            "ats_platform": ats_platform,
            "submission_confidence": submission_confidence,
        }

    except Exception as error:
        logger.error(f"{prefix} ERROR: {error}")
        return {
            "success": False,
            "message": str(error),
            "session_url": session_url,
            "account_created": False,
            "ats_platform": ats_platform,
            "submission_confidence": "failed",
        }
    finally:
        await client.sessions.end(id=sid)


async def apply_with_retry(
    job: JobPosting,
    candidate: CandidateProfile,
    settings: ApplicationSettings,
    resume_path: str | None = None,
    cover_letter_text: str | None = None,
    submit: bool = False,
    email_watcher: EmailWatcher | None = None,
) -> dict:
    """Apply with retry on failure."""
    last = {}
    for attempt in range(settings.max_retries + 1):
        if attempt > 0:
            delay = 2 ** (attempt + 1)
            logger.info(f"[{job.company_name}] Retry {attempt}/{settings.max_retries} in {delay}s...")
            await asyncio.sleep(delay)

        last = await apply_to_job(
            job, candidate, settings, resume_path, cover_letter_text,
            submit=submit, email_watcher=email_watcher,
        )
        if last.get("success"):
            return last

    last["retry_count"] = settings.max_retries
    return last
