"""Send an email via Resend's HTTP API.

Resend is a transactional-email API built for sending from apps/scripts, as
opposed to Gmail SMTP (built for a human sending from a mail client). It's
one HTTP POST with an API key -- no SMTP connection/auth handshake, and it's
less likely to have an automated GitHub Actions login flagged as suspicious
the way a personal Gmail account sometimes is.

Attachments (used by `lit-deep-dive --attach-pdfs`) go in that same POST as
base64. Resend caps an email at 40MB *after* encoding, which is low enough for
a handful of arXiv PDFs to hit, so this module also owns the packing: it knows
the limit, so it decides how many emails a set of attachments needs. See
`pack_attachments`.
"""

from __future__ import annotations

import base64
import logging
import math
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"

# Resend's documented ceiling: "no larger than 40MB (including attachments
# after Base64 encoding)".
RESEND_MAX_EMAIL_BYTES = 40 * 1024 * 1024
# What we'll actually fill with attachments, leaving room for the HTML/text
# body, MIME headers and JSON overhead in the same request.
ATTACHMENT_BUDGET_BYTES = 36 * 1024 * 1024


@dataclass
class Attachment:
    filename: str
    content: bytes  # raw bytes; base64-encoded at send time

    @property
    def encoded_size(self) -> int:
        return base64_size(len(self.content))


def base64_size(raw_size: int) -> int:
    """Exact encoded size of `raw_size` bytes -- 4 chars per 3-byte group,
    padded. Exact rather than a 4/3 estimate, since the whole point is to
    stay under a hard limit."""
    return 4 * math.ceil(raw_size / 3)


def pack_attachments(
    attachments: list[Attachment],
    budget: int = ATTACHMENT_BUDGET_BYTES,
) -> tuple[list[list[Attachment]], list[Attachment]]:
    """Split attachments across as many emails as the budget requires.

    Returns (packs, oversized). Greedy in the order given -- that's report
    order (score descending), so the papers that matter most land in the first
    email rather than being shuffled by size.

    An attachment too big for an email *on its own* can't be split by any
    number of emails, so it's returned separately instead of silently dropped:
    the caller rebuilds those cover-only (a few KB) and says so in the body.
    """
    packs: list[list[Attachment]] = []
    oversized: list[Attachment] = []
    current: list[Attachment] = []
    used = 0
    for attachment in attachments:
        size = attachment.encoded_size
        if size > budget:
            oversized.append(attachment)
            continue
        if current and used + size > budget:
            packs.append(current)
            current, used = [], 0
        current.append(attachment)
        used += size
    if current:
        packs.append(current)
    return packs, oversized


def send_email(
    *,
    sender: str,
    recipient: str,
    subject: str,
    html: str,
    text: str,
    attachments: list[Attachment] | None = None,
) -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not set")

    payload: dict = {
        "from": sender,
        "to": [recipient],
        "subject": subject,
        "html": html,
        "text": text,
    }
    if attachments:
        payload["attachments"] = [
            {
                "filename": a.filename,
                "content": base64.b64encode(a.content).decode("ascii"),
                "content_type": "application/pdf",
            }
            for a in attachments
        ]
        encoded_total = sum(a.encoded_size for a in attachments)
        logger.info(
            "Sending %d attachment(s), %.1f MB encoded of the %.0f MB Resend allows",
            len(attachments),
            encoded_total / 1024 / 1024,
            RESEND_MAX_EMAIL_BYTES / 1024 / 1024,
        )

    response = httpx.post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        # Uploading tens of MB takes considerably longer than a bare HTML mail.
        timeout=300.0 if attachments else 30.0,
    )
    if response.is_error:
        logger.error("Resend API error %d: %s", response.status_code, response.text)
    response.raise_for_status()
    logger.info("Sent email to %s (id=%s)", recipient, response.json().get("id"))
