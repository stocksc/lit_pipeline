"""Strong-model deep read: structured summary + critique of a full paper.

The PDF is sent to Claude directly as a native `document` content block
(base64-encoded), not pre-extracted text. arXiv PDFs are LaTeX-generated,
multi-column, and full of figures/equations that text-extraction libraries
routinely mangle; Claude's native PDF reading sees the document as it
actually renders.

Uses streaming (`client.messages.stream(...).get_final_message()`) rather
than a plain `create`/`parse` call: a full-paper read plus Claude Opus 5's
default adaptive thinking can run long, and streaming avoids HTTP timeouts
on that kind of request. `output_format=DeepReadResult` still gives us a
validated `DeepReadResult` on `message.parsed_output`, same as the
non-streaming `.parse()` path used for triage.
"""

from __future__ import annotations

import base64
import logging

from anthropic import Anthropic

from lit_pipeline.arxiv_client import PaperCandidate
from lit_pipeline.config import DeepReadSettings
from lit_pipeline.schemas import DeepReadResult

logger = logging.getLogger(__name__)

DEEP_READ_SYSTEM_PROMPT = """You are a research assistant helping a researcher \
keep up with their field. You are given a researcher's stated interests and \
the full text of a paper as a PDF. Read the paper carefully and produce a \
structured summary and critique: what it claims to contribute, how it does \
so, its real weaknesses, and how it relates to the researcher's stated \
interests. Be direct and specific -- avoid vague praise, call out concrete \
limitations a careful reader would raise."""

MAX_TOKENS = 64000


def deep_read_paper(
    client: Anthropic,
    settings: DeepReadSettings,
    interests: str,
    candidate: PaperCandidate,
    pdf_bytes: bytes,
) -> DeepReadResult:
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
    with client.messages.stream(
        model=settings.model,
        max_tokens=MAX_TOKENS,
        system=DEEP_READ_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": f"Researcher's interests:\n{interests}\n\nPaper title: {candidate.title}",
                    },
                ],
            }
        ],
        output_format=DeepReadResult,
    ) as stream:
        message = stream.get_final_message()

    result = message.parsed_output
    if result is None:
        raise ValueError(f"Deep-read call for {candidate.arxiv_id} returned no parsed output")
    return result
