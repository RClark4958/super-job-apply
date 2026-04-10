"""Async email watcher for security codes and verification links.

Runs as a background asyncio task, polling IMAP inbox for:
- Greenhouse security codes (alphanumeric, e.g. "sT7OKRYU")
- Numeric verification codes (6-digit)
- Verification/confirmation links

The watcher runs independently of the browser automation and provides
codes on demand when the browser encounters a security code page.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import os
import re
from datetime import datetime, timezone
from email.header import decode_header

logger = logging.getLogger(__name__)

# Greenhouse-specific code pattern: "paste this code...: {CODE}"
_GREENHOUSE_CODE_PATTERN = re.compile(
    r'(?:code into the security code field|paste this code)[^:]*:\s*([A-Za-z0-9]{6,10})',
    re.IGNORECASE,
)

# Generic numeric code patterns
_NUMERIC_CODE_PATTERNS = [
    re.compile(r'(?:code|verification|security)[\s:]+(\d{4,8})', re.IGNORECASE),
    re.compile(r'(\d{6})\s*(?:is your|security|verification)', re.IGNORECASE),
]

# Verification link patterns
_LINK_PATTERNS = [
    re.compile(r'href=["\']?(https?://[^"\'>\s]*(?:verify|confirm|activate|token|auth)[^"\'>\s]*)', re.IGNORECASE),
]


class EmailWatcher:
    """Background email watcher that polls IMAP for security codes."""

    def __init__(self):
        self.imap_email = os.environ.get("IMAP_EMAIL", "")
        self.imap_password = os.environ.get("IMAP_APP_PASSWORD", "")
        self.imap_server = os.environ.get("IMAP_SERVER", "imap.mail.yahoo.com")
        self.poll_interval = 8
        self._codes: dict[str, str] = {}  # company_hint -> code
        self._latest_code: str | None = None
        self._code_event = asyncio.Event()
        self._running = False
        self._task: asyncio.Task | None = None
        self._seen_ids: set[bytes] = set()

    @property
    def available(self) -> bool:
        return bool(self.imap_email and self.imap_password)

    def start(self) -> None:
        """Start background polling."""
        if not self.available:
            logger.info("IMAP not configured — email watcher disabled")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"Email watcher started — polling {self.imap_email} every {self.poll_interval}s")

    def stop(self) -> None:
        """Stop background polling."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def wait_for_code(self, company_hint: str = "", timeout: float = 120) -> str | None:
        """Wait for a security code. Returns the code string or None on timeout."""
        if not self.available:
            return None

        # Check if we already have a code for this company
        hint_lower = company_hint.lower()
        for key, code in self._codes.items():
            if hint_lower in key.lower() or key.lower() in hint_lower:
                logger.info(f"Email watcher: found cached code '{code}' for {company_hint}")
                return code

        # Wait for new code
        self._code_event.clear()
        logger.info(f"Email watcher: waiting up to {timeout}s for security code ({company_hint})...")
        try:
            await asyncio.wait_for(self._code_event.wait(), timeout=timeout)
            # Check again after event
            for key, code in self._codes.items():
                if hint_lower in key.lower() or key.lower() in hint_lower:
                    return code
            return self._latest_code
        except asyncio.TimeoutError:
            logger.warning(f"Email watcher: no code received for {company_hint} after {timeout}s")
            return None

    async def _poll_loop(self) -> None:
        """Background loop polling inbox."""
        while self._running:
            try:
                found = await asyncio.to_thread(self._check_inbox)
                if found:
                    self._code_event.set()
            except Exception as e:
                logger.debug(f"Email poll error: {e}")
            await asyncio.sleep(self.poll_interval)

    def _check_inbox(self) -> bool:
        """Check inbox for new security codes. Returns True if a new code was found."""
        found_new = False
        try:
            mail = imaplib.IMAP4_SSL(self.imap_server)
            mail.login(self.imap_email, self.imap_password)
            mail.select("INBOX")

            # Search for recent unseen emails
            date_str = datetime.now(timezone.utc).strftime("%d-%b-%Y")
            _, message_ids = mail.search(None, f"(UNSEEN SINCE {date_str})")

            if not message_ids[0]:
                mail.logout()
                return False

            for msg_id in reversed(message_ids[0].split()[-15:]):
                if msg_id in self._seen_ids:
                    continue
                self._seen_ids.add(msg_id)

                _, msg_data = mail.fetch(msg_id, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue

                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                subject = self._decode_header(msg.get("Subject", ""))
                from_addr = (msg.get("From", "") or "").lower()
                body = self._get_body(msg)

                # Greenhouse security code
                if "greenhouse" in from_addr or "security code" in subject.lower():
                    code = self._extract_greenhouse_code(body)
                    if code:
                        # Extract company name from subject
                        company = ""
                        match = re.search(r'application to (.+?)$', subject, re.IGNORECASE)
                        if match:
                            company = match.group(1).strip()
                        self._codes[company] = code
                        self._latest_code = code
                        found_new = True
                        logger.info(f"SECURITY CODE: '{code}' for '{company}' (from {from_addr[:30]})")
                        mail.store(msg_id, "+FLAGS", "\\Seen")
                        continue

                # Generic numeric code
                code = self._extract_numeric_code(body, from_addr)
                if code:
                    self._latest_code = code
                    self._codes["generic"] = code
                    found_new = True
                    logger.info(f"NUMERIC CODE: '{code}' (from {from_addr[:30]})")
                    mail.store(msg_id, "+FLAGS", "\\Seen")

            mail.logout()
        except Exception as e:
            logger.debug(f"IMAP error: {e}")

        return found_new

    def _extract_greenhouse_code(self, body: str) -> str | None:
        """Extract Greenhouse alphanumeric security code."""
        # Strip HTML
        text = re.sub(r'<[^>]+>', ' ', body)
        text = re.sub(r'\s+', ' ', text)

        match = _GREENHOUSE_CODE_PATTERN.search(text)
        if match:
            return match.group(1)

        # Fallback: look for standalone mixed-case alphanumeric (must have both letters and digits)
        fallback = re.search(r'code[^a-zA-Z0-9]{1,5}([A-Za-z0-9]{7,10})\b', text)
        if fallback:
            candidate = fallback.group(1)
            has_letter = any(c.isalpha() for c in candidate)
            has_digit = any(c.isdigit() for c in candidate)
            if has_letter and has_digit:
                return candidate

        return None

    def _extract_numeric_code(self, body: str, from_addr: str) -> str | None:
        """Extract numeric verification code."""
        job_senders = ["greenhouse", "lever", "workday", "icims", "smartrecruiters", "no-reply", "noreply"]
        if not any(s in from_addr for s in job_senders):
            return None

        text = re.sub(r'<[^>]+>', ' ', body)
        for pattern in _NUMERIC_CODE_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(1)
        return None

    def _decode_header(self, header: str) -> str:
        if not header:
            return ""
        parts = decode_header(header)
        return "".join(
            p.decode(c or "utf-8", errors="replace") if isinstance(p, bytes) else p
            for p, c in parts
        )

    def _get_body(self, msg) -> str:
        """Get email body, preferring HTML (codes are often in HTML only)."""
        parts = []
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct in ("text/html", "text/plain"):
                    try:
                        payload = part.get_payload(decode=True)
                        charset = part.get_content_charset() or "utf-8"
                        parts.append((ct, payload.decode(charset, errors="replace")))
                    except Exception:
                        pass
        else:
            try:
                payload = msg.get_payload(decode=True)
                charset = msg.get_content_charset() or "utf-8"
                parts.append((msg.get_content_type(), payload.decode(charset, errors="replace")))
            except Exception:
                pass

        # Prefer HTML (Greenhouse codes are only in HTML)
        html = [p for ct, p in parts if ct == "text/html"]
        plain = [p for ct, p in parts if ct == "text/plain"]
        return (html[0] if html else plain[0]) if (html or plain) else ""
