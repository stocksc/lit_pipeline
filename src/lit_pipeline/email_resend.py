"""Send an email via Resend's HTTP API.

Resend is a transactional-email API built for sending from apps/scripts, as
opposed to Gmail SMTP (built for a human sending from a mail client). It's
one HTTP POST with an API key -- no SMTP connection/auth handshake, and it's
less likely to have an automated GitHub Actions login flagged as suspicious
the way a personal Gmail account sometimes is.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


def send_email(*, sender: str, recipient: str, subject: str, html: str, text: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not set")

    response = httpx.post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "from": sender,
            "to": [recipient],
            "subject": subject,
            "html": html,
            "text": text,
        },
        timeout=30.0,
    )
    response.raise_for_status()
    logger.info("Sent email to %s (id=%s)", recipient, response.json().get("id"))
