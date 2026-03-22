"""Stagehand-powered browser automation for job application form filling.

Adapted from the Browserbase template. Manages browser sessions, extracts
job details, and uses an AI agent to intelligently fill application forms.
Handles account creation on job sites when required.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from stagehand import AsyncStagehand

from ..config import get_model_api_key
from ..models import (
    ApplicationSettings,
    CandidateProfile,
    JOB_DESCRIPTION_SCHEMA,
    JobPosting,
)
from .uploader import upload_resume

logger = logging.getLogger(__name__)

_ACCOUNT_CREATION_BLOCK = """
- ACCOUNT CREATION / LOGIN HANDLING:
  If the site requires you to create an account or sign in before applying:
  1. First check for "Apply as Guest", "Quick Apply", or "Continue without account" — use these if available
  2. Otherwise, TRY SIGNING IN FIRST with the candidate's email and the provided password — the candidate may already have an account on this site
  3. If sign-in fails (wrong password, no account found), THEN look for "Create Account", "Sign Up", or "Register"
  4. Use the candidate's email and the provided password to create the account
  5. For name fields during registration, use the candidate's first and last name
  6. Fill in any other required profile fields (phone, location) during registration
  7. If asked to verify email:
     - Look for "Continue", "Skip", "Verify Later", "Remind me later", or "Not now" — click it if available
     - If the site blocks you completely until email is verified, STOP and report exactly: "Email verification needed - check your email to verify the account" — the system will pause and wait for the user to verify, then resume automatically
     - Do NOT reload the page or retry verification more than once
  8. If a "Magic Link" login is offered, or if the site says it sent a link/email to continue the application, STOP and report exactly: "Email verification needed - check your email to continue" — the system will automatically check the inbox and handle it
  9. After signing in or creating the account, navigate back to the job posting URL and click Apply again
  10. CRITICAL: If you get stuck in a login/signup loop (same page keeps appearing), stop and report the issue. Do NOT retry more than 3 times."""

_RESUME_UPLOAD_BLOCK = """
- RESUME/FILE UPLOAD HANDLING:
  - Do NOT attempt to click file upload buttons or browse buttons — a separate system handles file uploads
  - If a resume upload field is marked as "required", leave it empty and continue filling other fields
  - Do NOT report failure because of an unfilled file upload field — this is handled automatically after you finish
  - If the form won't let you proceed past a required upload, try clicking "Skip" or moving to the next section"""

_NAVIGATION_BLOCK = """
- NAVIGATION:
  - If an "Apply" or "Apply Now" button takes you to a different site (Workday, Greenhouse, Lever, iCIMS, etc.), that is expected — continue on the new site
  - If you land on a job listing page instead of an application form, look for the specific job title and click into it first
  - If you get stuck in a loop (clicking Apply keeps bringing you to the same page), try looking for alternative application links on the page
  - If the job posting says "closed", "no longer available", or shows a 404, report it — do not try to force an application"""

AGENT_SYSTEM_PROMPT_FILL = f"""You are an intelligent job application assistant with decision-making power.

Your responsibilities:
- Navigate to the job posting and click through to its application page
{_ACCOUNT_CREATION_BLOCK}
{_NAVIGATION_BLOCK}
- Analyze the job description to understand what the company is looking for
- Tailor responses to align with job requirements when available
- Craft thoughtful responses that highlight relevant experience/skills
- For cover letter or "why interested" fields, use the provided cover letter content
- For location/relocation questions, use the willing_to_relocate flag
- For visa/sponsorship questions, answer honestly based on requires_sponsorship
{_RESUME_UPLOAD_BLOCK}
- Use the provided application details as the source of truth
- IMPORTANT: Do NOT click the final submit button — stop after filling all fields

Think critically about each field and present the candidate in the best professional light."""

AGENT_SYSTEM_PROMPT_SUBMIT = f"""You are an intelligent job application assistant with decision-making power.

Your responsibilities:
- Navigate to the job posting and click through to its application page
{_ACCOUNT_CREATION_BLOCK}
{_NAVIGATION_BLOCK}
- Analyze the job description to understand what the company is looking for
- Tailor responses to align with job requirements when available
- Craft thoughtful responses that highlight relevant experience/skills
- For cover letter or "why interested" fields, use the provided cover letter content
- For location/relocation questions, use the willing_to_relocate flag
- For visa/sponsorship questions, answer honestly based on requires_sponsorship
{_RESUME_UPLOAD_BLOCK}
- Use the provided application details as the source of truth
- IMPORTANT: After filling ALL fields, click the submit/apply button to complete the application

Think critically about each field and present the candidate in the best professional light."""


def _build_agent_instruction(
    job: JobPosting,
    candidate: CandidateProfile,
    cover_letter_text: str | None = None,
    extracted_description: dict | None = None,
    submit: bool = False,
    account_password: str = "",
) -> str:
    """Build the instruction prompt for the form-filling agent."""
    candidate_info = {
        "name": candidate.name,
        "email": candidate.email,
        "phone": candidate.phone,
        "linkedin_url": candidate.linkedin_url,
        "portfolio_url": candidate.portfolio_url,
        "location": candidate.location,
        "willing_to_relocate": candidate.willing_to_relocate,
        "requires_sponsorship": candidate.requires_sponsorship,
        "visa_status": candidate.visa_status,
        "skills": candidate.skills,
        "years_experience": candidate.years_experience,
    }

    job_desc = extracted_description or {
        "jobTitle": job.job_title,
        "companyName": job.company_name,
        "requirements": job.requirements,
        "responsibilities": job.responsibilities,
        "location": job.location,
        "workType": job.work_type,
        "fullDescription": job.full_description,
    }

    has_description = job_desc.get("jobTitle") or job_desc.get("fullDescription")

    parts = []
    parts.append("You are filling out a job application.")

    if has_description:
        parts.append(f"\nJOB DESCRIPTION:\n{json.dumps(job_desc, indent=2)}")
    else:
        parts.append("\nNo detailed job description was found on this page.")

    parts.append(f"\nCANDIDATE INFORMATION:\n{json.dumps(candidate_info, indent=2)}")

    # Account creation credentials — use account_email if set, otherwise primary email
    acct_email = candidate.account_email or candidate.email
    if account_password:
        parts.append(
            f"\nACCOUNT CREATION CREDENTIALS (use if the site requires sign-up to apply):\n"
            f"  Email for account creation: {acct_email}\n"
            f"  Password: {account_password}\n"
            f"  First Name: {candidate.name.split()[0] if candidate.name else ''}\n"
            f"  Last Name: {' '.join(candidate.name.split()[1:]) if candidate.name else ''}\n"
            f"\n  NOTE: Use {acct_email} for creating accounts and signing in.\n"
            f"  For the application form itself, use the primary email: {candidate.email}\n"
            f"\n  INSTRUCTIONS FOR ACCOUNT CREATION:\n"
            f"  - If you see a login/sign-in page, look for 'Create Account', 'Sign Up', or 'Register'\n"
            f"  - If there's a 'Apply as Guest' or 'Quick Apply' option, prefer that instead\n"
            f"  - Create the account using the email and password above\n"
            f"  - Fill in any required profile fields during registration\n"
            f"  - If email verification is required, look for 'Skip', 'Continue', or 'Verify Later'\n"
            f"  - After account creation, return to the job posting and apply\n"
            f"  - If the site already has an account with this email, try signing in with the password\n"
        )

    if cover_letter_text:
        parts.append(f"\nCOVER LETTER (use for 'why interested' or cover letter fields):\n{cover_letter_text}")

    submit_line = (
        "After filling all fields, click the submit/apply button to complete the application"
        if submit else
        "Do NOT click the final submit button"
    )

    parts.append(
        "\nYOUR TASK:\n"
        "- Navigate to the application form (create an account if required using the credentials above)\n"
        "- Fill out all text fields in the application form\n"
        "- Reference specific aspects of the job description\n"
        "- Highlight relevant skills/experience from the candidate's background\n"
        "- Show alignment between candidate and role\n"
        "- Skip file upload fields (resume will be handled separately)\n"
        f"- {submit_line}\n"
        "\nRemember: Maximize the candidate's chances by showing strong alignment with this specific role."
    )

    return "\n".join(parts)


async def apply_to_job(
    job: JobPosting,
    candidate: CandidateProfile,
    settings: ApplicationSettings,
    resume_path: str | None = None,
    cover_letter_text: str | None = None,
    submit: bool = False,
) -> dict:
    """Apply to a single job: start session, fill form, upload resume.

    Handles account creation on job sites when sign-up is required.

    Args:
        job: The job posting to apply to.
        candidate: Candidate profile.
        settings: Application settings (model, proxy, etc.).
        resume_path: Path to tailored resume file.
        cover_letter_text: Generated cover letter text for form fields.
        submit: If True, the agent will click the submit button after filling.

    Returns:
        Dict with success, message, session_url, account_created.
    """
    log_prefix = f"[{job.company_name}] "
    action = "Submitting" if submit else "Filling"
    logger.info(f"{log_prefix}{action} application for '{job.job_title}'...")

    client = AsyncStagehand(
        browserbase_api_key=os.environ.get("BROWSERBASE_API_KEY"),
        browserbase_project_id=os.environ.get("BROWSERBASE_PROJECT_ID"),
        model_api_key=get_model_api_key(),
    )

    start_response = await client.sessions.start(
        model_name=settings.model,
    )
    session_id = start_response.data.session_id
    session_url = f"https://browserbase.com/sessions/{session_id}"
    logger.info(f"{log_prefix}Session started: {session_url}")

    acct_email = candidate.account_email or candidate.email
    candidate_json = json.dumps({
        "name": candidate.name,
        "email": candidate.email,
        "account_email": acct_email,
        "phone": candidate.phone,
        "linkedin_url": candidate.linkedin_url,
        "location": candidate.location,
        "willing_to_relocate": candidate.willing_to_relocate,
        "requires_sponsorship": candidate.requires_sponsorship,
    }, indent=2)

    try:
        # Navigate to careers page
        await client.sessions.navigate(id=session_id, url=job.careers_url)
        await asyncio.sleep(3)

        # Stagehand best practices: atomic act() calls, observe() before act, extract() to verify
        act_timeout = 60000

        async def do_act(instruction: str) -> str:
            """Run a single atomic act() call. Returns result message."""
            try:
                resp = await client.sessions.act(
                    id=session_id,
                    input=instruction,
                    options={"model": {"model_name": settings.agent_model}, "timeout": act_timeout},
                    timeout=120.0,
                )
                if resp.data and resp.data.result:
                    return resp.data.result.message
                return "No result"
            except Exception as e:
                return f"Error: {e}"

        async def do_extract(instruction: str) -> dict:
            """Extract structured data from the current page."""
            try:
                resp = await client.sessions.extract(
                    id=session_id,
                    instruction=instruction,
                    schema={
                        "type": "object",
                        "properties": {
                            "page_type": {"type": "string", "description": "One of: application_form, job_listing, login_page, aggregator_listing, confirmation, error, other"},
                            "has_form_fields": {"type": "boolean", "description": "Whether the page has input fields like name, email, phone"},
                            "has_submit_button": {"type": "boolean", "description": "Whether there is a submit/apply/send button"},
                            "confirmation_message": {"type": "string", "description": "Any success/confirmation message visible on the page"},
                            "page_description": {"type": "string", "description": "Brief description of what is on the page"},
                        },
                    },
                    timeout=60.0,
                )
                return resp.data.result or {}
            except Exception as e:
                logger.warning(f"{log_prefix}Extract failed: {e}")
                return {}

        # === Step 1: Assess the landing page ===
        logger.info(f"{log_prefix}Step 1: Assessing page...")
        page_info = await do_extract(
            "Analyze this page. Is it a job application form with input fields? "
            "A job listing with an Apply button? A login/signup page? "
            "An aggregator site listing many jobs? Or something else?"
        )
        page_type = page_info.get("page_type", "other")
        logger.info(f"{log_prefix}Page type: {page_type} — {page_info.get('page_description', '')[:80]}")

        # === Step 2: Navigate to actual application form ===
        if page_type in ("aggregator_listing", "job_listing", "other"):
            logger.info(f"{log_prefix}Step 2: Clicking through to application...")
            await do_act(
                "Click the 'Apply', 'Apply Now', or 'Apply for this job' button. "
                "If this is a job listing page, click into the specific job first, then click Apply. "
                f"Target job title: '{job.job_title}'"
            )
            await asyncio.sleep(4)

            # Re-assess after click
            page_info = await do_extract(
                "Analyze this page. Is it a job application form with input fields? "
                "A login/signup page? A confirmation page? Or still a job listing?"
            )
            page_type = page_info.get("page_type", "other")
            logger.info(f"{log_prefix}After click — page type: {page_type}")

            # If still not a form, try one more click
            if page_type not in ("application_form", "login_page"):
                await do_act("Click any 'Apply', 'Apply Now', 'Start Application', or 'Apply for this position' button")
                await asyncio.sleep(3)
                page_info = await do_extract("Is this page now showing a job application form with input fields?")
                page_type = page_info.get("page_type", "other")
                logger.info(f"{log_prefix}Second click — page type: {page_type}")

        # === Step 3: Handle login/signup if needed ===
        if page_type == "login_page" or not page_info.get("has_form_fields"):
            logger.info(f"{log_prefix}Step 3: Handling login/signup...")
            login_result = await do_act(
                f"Try to sign in with email '{acct_email}' and password '{settings.account_password}'. "
                f"If no account exists, create one with email '{acct_email}', password '{settings.account_password}', "
                f"and name '{candidate.name}'. "
                "If there is a 'Continue as Guest' or 'Apply without account' option, click that instead."
            )
            logger.info(f"{log_prefix}Login result: {login_result[:100]}")
            await asyncio.sleep(3)

            # Check for email verification
            login_lower = login_result.lower()
            if any(kw in login_lower for kw in ["verify", "verification", "check your email", "confirm", "magic link", "sent"]):
                logger.info(f"{log_prefix}*** EMAIL VERIFICATION — checking inbox ***")
                from .email_verifier import find_verification_link
                verify_link = await asyncio.to_thread(
                    find_verification_link,
                    recipient_email=acct_email,
                    max_wait_seconds=300,
                    poll_interval=15,
                )
                if verify_link:
                    logger.info(f"{log_prefix}Found verification link — clicking...")
                    try:
                        await client.sessions.navigate(id=session_id, url=verify_link)
                        await asyncio.sleep(5)
                        # Navigate back and re-apply
                        await client.sessions.navigate(id=session_id, url=job.careers_url)
                        await asyncio.sleep(3)
                        await do_act("Click the 'Apply' or 'Apply Now' button")
                        await asyncio.sleep(3)
                    except Exception as e:
                        logger.warning(f"{log_prefix}Verification navigation failed: {e}")
                else:
                    logger.warning(f"{log_prefix}No verification email found after 5 min")

        # === Step 4: Verify we have a form before filling ===
        form_check = await do_extract(
            "Does this page have a job application form with text input fields "
            "(like name, email, phone, or cover letter)? "
            "Is there a submit button?"
        )
        has_form = form_check.get("has_form_fields", False)
        has_submit = form_check.get("has_submit_button", False)
        logger.info(f"{log_prefix}Form check — has fields: {has_form}, has submit: {has_submit}")

        if not has_form:
            # No form found — this is an aggregator redirect or dead end
            return {
                "success": False,
                "message": f"No application form found on page. Page type: {page_type}. {form_check.get('page_description', '')}",
                "session_url": session_url,
                "account_created": False,
            }

        # === Step 5: Fill form fields (atomic actions per field type) ===
        logger.info(f"{log_prefix}Step 5: Filling form fields...")

        # Fill name fields
        await do_act(f"Type '{candidate.name}' into the full name, first name, or name field")
        await asyncio.sleep(1)

        # Fill name parts if separate fields
        name_parts = candidate.name.split()
        if len(name_parts) >= 2:
            await do_act(f"If there are separate first name and last name fields, type '{name_parts[0]}' into first name and '{' '.join(name_parts[1:])}' into last name")
            await asyncio.sleep(1)

        # Fill email
        await do_act(f"Type '{candidate.email}' into the email or email address field")
        await asyncio.sleep(1)

        # Fill phone
        await do_act(f"Type '{candidate.phone}' into the phone or phone number field")
        await asyncio.sleep(1)

        # Fill location
        await do_act(f"Type '{candidate.location}' into any location, city, or address field")
        await asyncio.sleep(1)

        # Fill LinkedIn
        await do_act(f"Type '{candidate.linkedin_url}' into any LinkedIn or LinkedIn URL field")
        await asyncio.sleep(1)

        # Fill cover letter / why interested
        cl_text = (cover_letter_text or "I am excited about this opportunity and believe my skills are a strong match.")[:500]
        await do_act(f"Type the following into any 'cover letter', 'why are you interested', 'additional information', or large text area field: {cl_text}")
        await asyncio.sleep(1)

        # Handle dropdowns and remaining fields
        logger.info(f"{log_prefix}Step 6: Filling dropdowns and remaining fields...")
        sponsorship_answer = "Yes" if candidate.requires_sponsorship else "No"
        relocate_answer = "Yes" if candidate.willing_to_relocate else "No"
        await do_act(
            "Fill any remaining empty required fields. "
            f"For sponsorship questions, select '{sponsorship_answer}'. "
            f"For relocation questions, select '{relocate_answer}'. "
            f"For years of experience, enter '{candidate.years_experience}'. "
            "For any dropdown, select the most appropriate option. "
            "Skip file upload fields."
        )
        await asyncio.sleep(2)

        fill_result = "Form fields filled"

        # === Step 7: Upload resume ===
        if resume_path:
            try:
                cdp_url = (
                    f"wss://connect.browserbase.com"
                    f"?apiKey={os.environ.get('BROWSERBASE_API_KEY')}"
                    f"&sessionId={session_id}"
                )
                await upload_resume(cdp_url, resume_path, log_prefix)
            except Exception as e:
                logger.warning(f"{log_prefix}Could not upload resume: {e}")

        # === Step 8: Submit and verify ===
        submit_result = ""
        if submit:
            logger.info(f"{log_prefix}Step 7: Submitting application...")
            submit_result = await do_act(
                "Click the submit button to complete the job application. "
                "Look for buttons labeled 'Submit', 'Submit Application', 'Apply', 'Send Application', or 'Complete Application'."
            )
            logger.info(f"{log_prefix}Submit click: {submit_result[:100]}")
            await asyncio.sleep(3)

            # Verify submission with extract
            confirmation = await do_extract(
                "Check if the page now shows a confirmation or success message "
                "(like 'application submitted', 'thank you', 'application received', "
                "'we will review your application'). Also check for any error messages."
            )
            conf_msg = confirmation.get("confirmation_message", "")
            conf_type = confirmation.get("page_type", "")
            logger.info(f"{log_prefix}Post-submit: type={conf_type}, confirmation='{conf_msg[:80]}'")

            if conf_msg:
                submit_result = f"CONFIRMED: {conf_msg}"
            elif conf_type == "confirmation":
                submit_result = "CONFIRMED: Confirmation page detected"

        # Determine success based on actual evidence
        success = has_form  # We at least had a real form
        confirmed = "CONFIRMED" in submit_result
        message = f"Form: {has_form} | Submit: {submit_result[:100]}" if submit else f"Form filled (dry run)"

        if confirmed:
            success = True
            logger.info(f"{log_prefix}APPLICATION CONFIRMED: {submit_result[:100]}")
        elif submit and has_submit:
            # Clicked submit on a real form — likely went through even without confirmation extract
            success = True
            logger.info(f"{log_prefix}Application submitted (no confirmation message extracted)")
        elif submit and not has_submit:
            success = False
            message = "Form found but no submit button detected"

        account_created = False  # TODO: detect from login step

        return {
            "success": success,
            "message": message,
            "session_url": session_url,
            "account_created": account_created,
            "confirmed": confirmed,
        }

    except Exception as error:
        logger.error(f"{log_prefix}Application error: {error}")
        return {
            "success": False,
            "message": str(error),
            "session_url": session_url,
            "account_created": False,
        }
    finally:
        await client.sessions.end(id=session_id)
        logger.debug(f"{log_prefix}Session closed")


async def apply_with_retry(
    job: JobPosting,
    candidate: CandidateProfile,
    settings: ApplicationSettings,
    resume_path: str | None = None,
    cover_letter_text: str | None = None,
    submit: bool = False,
) -> dict:
    """Apply to a job with retry logic on failure.

    Uses exponential backoff: 2s, 4s, 8s between retries.
    """
    last_result = {}
    for attempt in range(settings.max_retries + 1):
        if attempt > 0:
            delay = 2 ** (attempt + 1)
            logger.info(
                f"[{job.company_name}] Retry {attempt}/{settings.max_retries} "
                f"after {delay}s delay..."
            )
            await asyncio.sleep(delay)

        last_result = await apply_to_job(
            job, candidate, settings, resume_path, cover_letter_text, submit=submit
        )

        if last_result.get("success"):
            return last_result

    last_result["retry_count"] = settings.max_retries
    return last_result
