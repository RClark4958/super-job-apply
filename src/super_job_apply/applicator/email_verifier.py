"""Automated email verification for job site account creation.

Connects to Gmail via IMAP, finds verification/confirmation emails,
extracts the verification link, and returns it so the browser agent
can navigate to it and complete account activation.
"""

from __future__ import annotations

import email
import imaplib
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.header import decode_header

logger = logging.getLogger(__name__)

# Common patterns for verification AND application continuation links
_VERIFY_LINK_PATTERNS = [
    # Verification links
    r'href=["\']?(https?://[^"\'>\s]*(?:verify|confirm|activate|validation|registration|signup|click)[^"\'>\s]*)',
    r'href=["\']?(https?://[^"\'>\s]*(?:token|code|key|auth)[^"\'>\s]*)',
    # Application continuation links
    r'href=["\']?(https?://[^"\'>\s]*(?:apply|application|continue|complete|finish|resume-application|job)[^"\'>\s]*)',
    r'href=["\']?(https?://[^"\'>\s]*(?:career|jobs|candidate|profile|onboard)[^"\'>\s]*)',
    # Generic action links (buttons in emails)
    r'href=["\']?(https?://[^"\'>\s]*(?:action|redirect|go|start|begin|next)[^"\'>\s]*)',
]

# Subject line keywords that indicate verification OR application continuation emails
_VERIFY_SUBJECT_KEYWORDS = [
    "verify", "confirm", "activate", "registration", "welcome",
    "validate", "account", "sign up", "email address",
    # Application continuation
    "application", "apply", "complete your", "finish your",
    "continue your", "next step", "job", "thank you for applying",
    "we received", "application received", "profile",
]

# Sender domains to ignore (not job-site related)
_IGNORE_DOMAINS = [
    "google.com", "gmail.com", "anthropic.com", "github.com",
    "microsoft.com", "apple.com",
]


def find_verification_link(
    recipient_email: str | None = None,
    imap_password: str | None = None,
    imap_server: str | None = None,
    max_wait_seconds: int = 300,
    poll_interval: int = 15,
    search_since_minutes: int = 10,
) -> str | None:
    """Poll inbox for a verification/continuation email and extract the action link.

    Args:
        recipient_email: The email address to check. Falls back to IMAP_EMAIL env var.
        imap_password: App Password. Falls back to IMAP_APP_PASSWORD or GMAIL_APP_PASSWORD env var.
        imap_server: IMAP server hostname. Falls back to IMAP_SERVER env var.
        max_wait_seconds: Maximum time to wait for the email (default 5 min).
        poll_interval: Seconds between inbox checks (default 15s).
        search_since_minutes: Only look at emails from the last N minutes.

    Returns:
        The verification/action URL if found, or None if timed out.
    """
    recipient_email = recipient_email or os.environ.get("IMAP_EMAIL", "")
    password = imap_password or os.environ.get("IMAP_APP_PASSWORD") or os.environ.get("GMAIL_APP_PASSWORD", "")
    imap_server = imap_server or os.environ.get("IMAP_SERVER", "imap.gmail.com")

    if not password:
        logger.warning("No IMAP_APP_PASSWORD set — cannot check email for verification")
        return None

    logger.info(
        f"Checking {recipient_email} for verification email "
        f"(will poll every {poll_interval}s for up to {max_wait_seconds}s)..."
    )

    deadline = time.time() + max_wait_seconds
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        try:
            link = _check_inbox(
                recipient_email, password, imap_server, search_since_minutes
            )
            if link:
                logger.info(f"Found verification link: {link[:80]}...")
                return link
        except Exception as e:
            logger.warning(f"IMAP check attempt {attempt} failed: {e}")

        remaining = int(deadline - time.time())
        if remaining > poll_interval:
            logger.debug(f"No verification email yet. Retrying in {poll_interval}s ({remaining}s remaining)...")
            time.sleep(poll_interval)
        else:
            break

    logger.warning(f"No verification email found after {max_wait_seconds}s")
    return None


def _check_inbox(
    email_addr: str,
    password: str,
    server: str,
    since_minutes: int,
) -> str | None:
    """Connect to IMAP, search for recent verification emails, extract link."""
    mail = imaplib.IMAP4_SSL(server)
    try:
        mail.login(email_addr, password)
        mail.select("INBOX")

        # Search for recent unread emails
        since_date = datetime.now(timezone.utc)
        date_str = since_date.strftime("%d-%b-%Y")
        _, message_ids = mail.search(None, f'(UNSEEN SINCE {date_str})')

        if not message_ids[0]:
            return None

        ids = message_ids[0].split()
        # Check most recent first
        for msg_id in reversed(ids[-20:]):  # Cap at last 20 emails
            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            # Check subject for verification keywords
            subject = _decode_subject(msg.get("Subject", ""))
            from_addr = msg.get("From", "").lower()

            # Skip emails from ignored domains
            if any(domain in from_addr for domain in _IGNORE_DOMAINS):
                continue

            subject_lower = subject.lower()
            is_verification = any(kw in subject_lower for kw in _VERIFY_SUBJECT_KEYWORDS)

            if not is_verification:
                continue

            # Extract verification link from body
            body = _get_email_body(msg)
            if not body:
                continue

            link = _extract_verify_link(body)
            if link:
                # Mark as read
                mail.store(msg_id, "+FLAGS", "\\Seen")
                return link

        return None
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _decode_subject(subject: str) -> str:
    """Decode email subject header."""
    if not subject:
        return ""
    parts = decode_header(subject)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _get_email_body(msg) -> str:
    """Extract text/html body from email message."""
    body_parts = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type in ("text/html", "text/plain"):
                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    body_parts.append(payload.decode(charset, errors="replace"))
                except Exception:
                    pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            body_parts.append(payload.decode(charset, errors="replace"))
        except Exception:
            pass

    # Prefer HTML (has href links) over plain text
    html_parts = [p for p in body_parts if "<a " in p.lower() or "href" in p.lower()]
    if html_parts:
        return html_parts[0]
    return body_parts[0] if body_parts else ""


def _extract_verify_link(body: str) -> str | None:
    """Extract the verification/confirmation URL from email body."""
    for pattern in _VERIFY_LINK_PATTERNS:
        matches = re.findall(pattern, body, re.IGNORECASE)
        if matches:
            # Return the first match, clean up HTML entities
            link = matches[0].replace("&amp;", "&")
            # Skip common non-verification links
            if any(skip in link.lower() for skip in [
                "unsubscribe", "privacy", "terms", "logo", "footer",
                "tracking", "open", "pixel", ".png", ".jpg", ".gif",
            ]):
                continue
            return link

    return None
