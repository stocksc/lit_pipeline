"""Strong-model deep read: a quick-hit structured summary of a full paper.

Takes plain extracted text (see pdf_extract.py) rather than sending Claude
the native PDF -- native PDF input gives visual understanding of figures/
tables/layout, but costs far more in tokens for this use case, where
visuals aren't the point and the output is meant to be a compact
click-through teaser, not a full review.

Uses streaming (`client.messages.stream(...).get_final_message()`) rather
than a plain `create`/`parse` call: Claude Opus 5's default adaptive
thinking can still run for a while even on a short output, and streaming
avoids HTTP timeouts on that kind of request. `output_format=DeepReadResult`
still gives us a validated `DeepReadResult` on `message.parsed_output`, same
as the non-streaming `.parse()` path used for triage.
"""

from __future__ import annotations

import logging

from anthropic import Anthropic

from lit_pipeline.arxiv_client import PaperCandidate
from lit_pipeline.config import DeepReadSettings
from lit_pipeline.pricing import LLMUsage
from lit_pipeline.schemas import DeepReadResult

logger = logging.getLogger(__name__)

DEEP_READ_SYSTEM_PROMPT = """You are a research assistant helping a researcher \
keep up with their field. You are given the researcher's stated interests and \
the extracted text of a paper. The researcher will click through to the paper \
itself for full detail, so produce a compact, quick-hit summary, not a full \
review:

1. Score: your own 0-10 relevance rating, using the same scale and interests \
as an initial triage pass would, but now based on the full paper you've \
actually read rather than just an abstract. Rate independently -- it's \
expected and fine for this to differ from any earlier triage score.
2. A summary of the paper in about 100 words.
3. Relevance: bullet points (about 50 words total) on why this paper is \
relevant to the researcher's stated interests.
4. Limitations: bullet points (about 50 words total) on what makes this paper \
NOT directly useful for a practitioner -- e.g. no code/data released, \
synthetic-only validation, proprietary data required, purely theoretical, \
inapplicable to a regulated setting. This is about practical applicability, \
not a generic academic critique.

Also identify every unique institution/affiliation among the paper's authors \
-- usually found near the author list on the first page, or in footnotes. \
List each institution once, in order of first appearance. If none can be \
determined from the text, return an empty list.

Be direct and specific -- avoid vague praise or generic critique."""

MAX_TOKENS = 64000


def deep_read_paper(
    client: Anthropic,
    settings: DeepReadSettings,
    interests: str,
    candidate: PaperCandidate,
    pdf_text: str,
) -> tuple[DeepReadResult, LLMUsage]:
    user_content = (
        f"Researcher's interests:\n{interests}\n\n"
        f"Paper title: {candidate.title}\n\n"
        f"Paper text:\n{pdf_text}"
    )
    with client.messages.stream(
        model=settings.model,
        max_tokens=MAX_TOKENS,
        system=DEEP_READ_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        output_format=DeepReadResult,
    ) as stream:
        message = stream.get_final_message()

    result = message.parsed_output
    if result is None:
        raise ValueError(f"Deep-read call for {candidate.arxiv_id} returned no parsed output")
    # Defensive clamp -- the schema declares 0-10 bounds, but don't trust it blindly.
    result.score = max(0, min(10, result.score))
    usage = LLMUsage(
        model=settings.model,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
    )
    return result, usage
