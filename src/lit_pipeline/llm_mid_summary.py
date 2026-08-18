"""Cheap-model summary for mid-tier papers.

Papers scoring in [mid_summary_threshold, score_threshold) don't clear the
bar for a full Opus deep-read, but are relevant enough to show more than
just a title. This generates a ~50-word summary from the abstract alone
(no PDF fetch) using the same model as triage -- cheap and fast, since the
abstract is already in hand from ingestion.
"""

from __future__ import annotations

import logging

from anthropic import Anthropic

from lit_pipeline.arxiv_client import PaperCandidate
from lit_pipeline.config import TriageSettings
from lit_pipeline.pricing import LLMUsage
from lit_pipeline.schemas import MidSummaryResult

logger = logging.getLogger(__name__)

MID_SUMMARY_SYSTEM_PROMPT = """You are a research assistant helping a researcher \
keep up with their field. You are given the researcher's stated interests and a \
paper's title and abstract. The researcher will only see this summary -- not \
the full paper -- so make it a self-contained, useful ~50-word summary of what \
the paper is about. Be specific, not vague."""


def mid_summary_paper(
    client: Anthropic,
    settings: TriageSettings,
    interests: str,
    candidate: PaperCandidate,
) -> tuple[MidSummaryResult, LLMUsage]:
    user_content = (
        f"Researcher's interests:\n{interests}\n\n"
        f"Paper title: {candidate.title}\n\n"
        f"Paper abstract: {candidate.abstract}"
    )
    response = client.messages.parse(
        model=settings.model,
        max_tokens=1024,
        system=MID_SUMMARY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        output_format=MidSummaryResult,
    )
    result = response.parsed_output
    if result is None:
        raise ValueError(f"Mid-summary call for {candidate.arxiv_id} returned no parsed output")
    usage = LLMUsage(
        model=settings.model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
    return result, usage
