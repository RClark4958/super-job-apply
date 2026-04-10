"""Browser automation for job application form filling.

Uses Stagehand act() for atomic browser actions with:
- observe() to discover all form fields before filling
- Programmatic field-to-data matching
- Security code detection and email-based code entry
- Clear logging at every step for visibility
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from stagehand import AsyncStagehand

from ..config import get_model_api_key
from ..models import (
    ApplicationSettings,
    CandidateProfile,
    JOB_DESCRIPTION_SCHEMA,
    JobPosting,
)
from .email_verifier import EmailWatcher
from .uploader import upload_resume

logger = logging.getLogger(__name__)


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
      1. Navigate to job page
      2. Click Apply
      3. Handle login/signup if needed
      4. Detect if we have an application form
      5. Use observe() to discover all form fields
      6. Fill each field based on candidate data
      7. Upload resume via Playwright CDP
      8. Click submit
      9. Handle security code if Greenhouse asks for one
      10. Verify confirmation page
    """
    prefix = f"[{job.company_name}]"
    acct_email = candidate.account_email or candidate.email
    model = settings.agent_model

    client = AsyncStagehand(
        browserbase_api_key=os.environ.get("BROWSERBASE_API_KEY"),
        browserbase_project_id=os.environ.get("BROWSERBASE_PROJECT_ID"),
        model_api_key=get_model_api_key(),
    )

    # Enable proxy + stealth to avoid reCAPTCHA detection on Greenhouse etc.
    session_params = {}
    if settings.use_proxy:
        session_params["browserbase_session_create_params"] = {
            "proxies": True,
        }

    start_response = await client.sessions.start(model_name=settings.model, **session_params)
    sid = start_response.data.session_id
    session_url = f"https://browserbase.com/sessions/{sid}"
    logger.info(f"{prefix} Session: {session_url}")

    async def act(instruction: str) -> str:
        """Single atomic browser action with logging."""
        try:
            resp = await client.sessions.act(
                id=sid, input=instruction,
                options={"model": {"model_name": model}, "timeout": 30000},
                timeout=60.0,
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
            await act("Click the 'Apply', 'Apply Now', or 'Apply for this job' button")
            await asyncio.sleep(4)

            # Re-assess
            page = await extract("Is this now an application form with input fields? Or a login page? Or still a listing?")
            page_type = page.get("page_type", "other")
            has_form = page.get("has_form_fields", False)
            logger.info(f"{prefix}   After click: {page_type} | Form: {has_form}")

            if not has_form and page_type not in ("login_page",):
                # Try one more click
                await act("Click any Apply, Start Application, or Submit Application button")
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
            desc = page.get("page_description", "")[:100]
            logger.warning(f"{prefix} NO FORM FOUND: {page_type} — {desc}")
            return {"success": False, "message": f"No form: {page_type}. {desc}", "session_url": session_url, "account_created": False}

        # === STEP 5: Observe ALL form fields ===
        logger.info(f"{prefix} STEP 5: Discovering all form fields with observe()...")
        try:
            obs_resp = await client.sessions.observe(
                id=sid,
                instruction="Find ALL input fields, text areas, dropdowns, checkboxes, radio buttons, and file upload fields in the application form",
            )
            observed = obs_resp.data if isinstance(obs_resp.data, list) else (obs_resp.data.result if hasattr(obs_resp.data, 'result') else [])
            field_names = []
            for f in (observed or []):
                desc = f.description if hasattr(f, 'description') else (f.get('description', '') if isinstance(f, dict) else str(f))
                field_names.append(desc)
            logger.info(f"{prefix}   Found {len(field_names)} form fields")
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
            "most complex and interesting": exp or "I built an enterprise RAG system with Vector Search across 14000+ pages of documentation, and an autonomous coding agent integrating Aider with Claude Opus that executes code in Databricks and creates PRs.",
            "describe the most complex": exp or "I built an enterprise RAG system with Vector Search across 14000+ pages of documentation, and an autonomous coding agent integrating Aider with Claude Opus that executes code in Databricks and creates PRs.",
            "examples of your work with llm": portfolio or "https://github.com/RClark4958",
            "examples of your work": portfolio or "https://github.com/RClark4958",
            "additional information": "Thank you for your consideration.",
            "deadline": "No specific deadlines",
            "timeline consideration": "No specific deadlines",
            "earliest you would want to start": "As soon as possible",
            "start working": "As soon as possible",
            "plan on working": candidate.location,
            "address from which": candidate.location,
            "publication": "",
        }

        fields_filled = 0

        for desc in field_names:
            dl = desc.lower()

            # Skip file uploads
            if any(kw in dl for kw in ["file upload", "resume", "attach", "cv upload"]):
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
        if submit:
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

            if confirmation:
                logger.info(f"{prefix} ✓ CONFIRMED: {confirmation[:80]}")
            elif post_type == "confirmation":
                logger.info(f"{prefix} ✓ CONFIRMED (page type)")
                confirmation = "Confirmation page detected"

        # Determine result
        success = has_form  # We found a form and filled it
        confirmed = bool(confirmation)
        message = f"Fields filled: {fields_filled}. Confirmed: {confirmed}. {confirmation[:80]}"

        if confirmed:
            logger.info(f"{prefix} APPLICATION SUBMITTED AND CONFIRMED")
        elif submit:
            logger.info(f"{prefix} Application submitted (no confirmation detected)")
        else:
            logger.info(f"{prefix} Form filled (dry run)")

        return {
            "success": success,
            "message": message,
            "session_url": session_url,
            "account_created": False,
            "confirmed": confirmed,
            "fields_filled": fields_filled,
        }

    except Exception as error:
        logger.error(f"{prefix} ERROR: {error}")
        return {
            "success": False,
            "message": str(error),
            "session_url": session_url,
            "account_created": False,
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
