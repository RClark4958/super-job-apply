"""Playwright-based resume file upload via CDP connection.

Adapted from the Browserbase template. Connects to an existing browser session
via CDP URL and uploads the resume file to any file input found on the page.
"""

from __future__ import annotations

import logging

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)


async def upload_resume(
    cdp_url: str,
    resume_path: str,
    log_prefix: str = "",
) -> None:
    """Upload resume file using Playwright, checking main page and iframes.

    Args:
        cdp_url: The CDP WebSocket URL to connect to the browser session.
        resume_path: Path to the resume file to upload.
        log_prefix: Optional prefix for log messages.
    """
    logger.info(f"{log_prefix}Attempting to upload resume...")

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        contexts = browser.contexts
        if not contexts:
            logger.warning(f"{log_prefix}No browser context found")
            return

        pw_context = contexts[0]
        pages = pw_context.pages
        if not pages:
            logger.warning(f"{log_prefix}No page found")
            return

        pw_page = pages[0]

        # Check main page for file input
        main_page_inputs = await pw_page.locator('input[type="file"]').count()

        if main_page_inputs > 0:
            await pw_page.locator('input[type="file"]').first.set_input_files(resume_path)
            logger.info(f"{log_prefix}Resume uploaded successfully from main page!")
            return

        # Check inside iframes for file input
        frames = pw_page.frames
        for frame in frames:
            try:
                frame_input_count = await frame.locator('input[type="file"]').count()
                if frame_input_count > 0:
                    await frame.locator('input[type="file"]').first.set_input_files(resume_path)
                    logger.info(f"{log_prefix}Resume uploaded successfully from iframe!")
                    return
            except Exception:
                # Frame not accessible, continue to next
                pass

        logger.warning(f"{log_prefix}No file upload field found on page")
