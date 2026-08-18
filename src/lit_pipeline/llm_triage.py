"""Cheap-model triage: score a paper's abstract for relevance.

Uses `client.messages.parse(..., output_format=TriageResult)`, which sends a
JSON Schema derived from the Pydantic model and returns an already-validated
`TriageResult` on `response.parsed_output` -- no manual JSON parsing.
"""

from __future__ import annotations

import logging

from anthropic import Anthropic

from lit_pipeline.arxiv_client import PaperCandidate
from lit_pipeline.config import TriageSettings
from lit_pipeline.schemas import TriageResult

logger = logging.getLogger(__name__)

TRIAGE_SYSTEM_PROMPT = """You are a research triage assistant. You are given a \
researcher's stated interests and a single paper's title and abstract. Score \
how relevant this paper is to those interests on a 0-10 scale, where 0 means \
completely unrelated and 10 means a must-read directly in their focus area. \
Be discriminating: most papers should NOT score highly. Reserve 8-10 for \
papers clearly central to the stated interests, not merely adjacent."""


def triage_paper(
    client: Anthropic,
    settings: TriageSettings,
    interests: str,
    candidate: PaperCandidate,
) -> TriageResult:
    user_content = (
        f"Researcher's interests:\n{interests}\n\n"
        f"Paper title: {candidate.title}\n\n"
        f"Paper abstract: {candidate.abstract}"
    )
    response = client.messages.parse(
        model=settings.model,
        max_tokens=1024,
        system=TRIAGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        output_format=TriageResult,
    )
    result = response.parsed_output
    if result is None:
        raise ValueError(f"Triage call for {candidate.arxiv_id} returned no parsed output")
    # Defensive clamp -- the schema declares 0-10 bounds, but don't trust it blindly.
    result.score = max(0, min(10, result.score))
    return result
