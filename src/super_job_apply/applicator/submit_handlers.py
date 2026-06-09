"""ATS-specific submission strategies via Playwright CDP.

Some ATS platforms block standard button clicks (e.g. Lever's hCaptcha +
client-side JS validators) but accept a direct form.submit() call. These
helpers connect to the existing Browserbase session and invoke the right
submission primitive for the detected platform.
"""

from __future__ import annotations

import logging

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)


async def find_new_tab_url(cdp_url: str, original_url: str, prefix: str = "") -> str | None:
    """Return the URL of a newly opened tab, if the last action spawned one.

    Career sites frequently open the application form in a new tab
    (target=_blank). Stagehand keeps acting on the original tab, so the
    form is never seen and the run fails with NO FORM FOUND. The caller
    should re-navigate the session to the returned URL.
    """
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        contexts = browser.contexts
        if not contexts:
            return None
        pages = contexts[0].pages
        if len(pages) < 2:
            return None
        newest = pages[-1]
        url = newest.url
        if url in ("", "about:blank"):
            # Tab still loading — give it a moment to get a real URL
            try:
                await newest.wait_for_load_state("domcontentloaded", timeout=8000)
                url = newest.url
            except Exception:
                pass

        def _norm(u: str) -> str:
            return u.rstrip("/").split("#")[0]

        if url and url != "about:blank" and _norm(url) != _norm(original_url):
            logger.info(f"{prefix} New tab detected: {url[:80]}")
            return url
        return None


_APPLY_LINK_SELECTORS = [
    'a.job-apply',
    'a[class*="apply" i]',
    'a[id*="apply" i]',
    'a[href*="jobapply" i]',
    'a[data-automation*="apply" i]',
    'a[aria-label*="apply" i]',
]


async def find_apply_link(cdp_url: str, current_url: str, prefix: str = "") -> str | None:
    """Return the href of a visible Apply link, if the button is a plain anchor.

    Many career sites (UHG/Taleo, Workday redirects) implement Apply as an
    <a href> to the external ATS. Navigating straight to the href is
    deterministic and avoids LLM clicks landing on styled overlays.
    """
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        contexts = browser.contexts
        if not contexts or not contexts[0].pages:
            return None
        page = contexts[0].pages[0]

        def _norm(u: str) -> str:
            return u.rstrip("/").split("#")[0]

        for sel in _APPLY_LINK_SELECTORS:
            try:
                locs = page.locator(sel)
                n = min(await locs.count(), 5)
                for i in range(n):
                    loc = locs.nth(i)
                    if not await loc.is_visible():
                        continue
                    href = await loc.get_attribute("href") or ""
                    if not href.startswith("http"):
                        continue
                    if _norm(href) == _norm(current_url):
                        continue
                    logger.info(f"{prefix} apply link [{sel[:30]}]: {href[:80]}")
                    return href
            except Exception:
                continue
        return None


async def find_form_frame(cdp_url: str, prefix: str = "") -> dict | None:
    """Find an iframe containing a job application form.

    Greenhouse/Lever/Workable embeds render the whole form inside an
    iframe that main-frame observe()/act()/extract() cannot see. Returns
    {"frame_id": str, "url": str, "input_count": int} for the iframe with
    the most form inputs (None if no iframe has 3+), so the caller can
    pass frame_id to Stagehand calls.
    """
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        contexts = browser.contexts
        if not contexts or not contexts[0].pages:
            return None
        page = contexts[0].pages[0]

        best = None
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                count = await frame.locator("input, textarea, select").count()
            except Exception:
                continue
            if count >= 3 and (best is None or count > best[1]):
                best = (frame, count)

        if best is None:
            return None
        form_frame, count = best

        # Resolve the CDP frame id by matching URLs in the frame tree
        try:
            cdp = await contexts[0].new_cdp_session(page)
            tree = await cdp.send("Page.getFrameTree")
            await cdp.detach()
        except Exception as e:
            logger.warning(f"{prefix} frame-tree lookup failed: {e}")
            return None

        def _walk(node):
            yield node["frame"]
            for child in node.get("childFrames", []):
                yield from _walk(child)

        for f in _walk(tree["frameTree"]):
            if f.get("url") == form_frame.url:
                logger.info(
                    f"{prefix} form iframe found: {count} inputs at {form_frame.url[:70]}"
                )
                return {"frame_id": f["id"], "url": form_frame.url, "input_count": count}
        return None


async def js_submit_form(cdp_url: str, prefix: str = "") -> dict:
    """Submit the first form on the page via JavaScript form.submit().

    Bypasses client-side JS validators and button handlers that block
    Playwright button clicks on Lever and similar ATS platforms.

    Returns a dict: {submitted: bool, confirmation_text: str, url_after: str}
    """
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        contexts = browser.contexts
        if not contexts:
            logger.warning(f"{prefix} js_submit: no browser context")
            return {"submitted": False, "confirmation_text": "", "url_after": ""}

        pages = contexts[0].pages
        if not pages:
            logger.warning(f"{prefix} js_submit: no page")
            return {"submitted": False, "confirmation_text": "", "url_after": ""}

        page = pages[0]
        url_before = page.url

        submitted = await page.evaluate(
            """
            () => {
                const form = document.querySelector('form');
                if (!form) return false;
                try { form.submit(); return true; }
                catch (e) { return false; }
            }
            """
        )

        if not submitted:
            logger.warning(f"{prefix} js_submit: no <form> found or submit threw")
            return {"submitted": False, "confirmation_text": "", "url_after": url_before}

        # Give the server time to respond
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        body_text = ""
        try:
            body_text = await page.evaluate("() => document.body.innerText || ''")
        except Exception:
            pass

        lowered = body_text.lower()
        # Detect common confirmation phrases inline (avoid extra extract() LLM call)
        confirmation_markers = (
            "application confirmed",
            "thank you for applying",
            "thanks for applying",
            "application received",
            "successfully submitted",
            "we've received your application",
            "we have received your application",
        )
        snippet = ""
        for marker in confirmation_markers:
            if marker in lowered:
                idx = lowered.find(marker)
                snippet = body_text[max(0, idx - 20) : idx + 200].strip()
                break

        url_after = page.url
        logger.info(
            f"{prefix} js_submit: submitted=True, url_changed={url_before != url_after}, "
            f"confirmation='{snippet[:80]}'"
        )
        return {
            "submitted": True,
            "confirmation_text": snippet,
            "url_after": url_after,
        }
