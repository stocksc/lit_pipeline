"""Pydantic schemas for structured LLM output.

These are passed as `output_format=` to `client.messages.parse(...)`, which
returns an already-validated instance on `response.parsed_output` — no manual
JSON parsing or prompt-engineered JSON formatting required.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class TriageResult(BaseModel):
    """Cheap-model relevance score for a single paper's abstract."""

    score: int = Field(
        ge=0,
        le=10,
        description="Relevance to the user's stated interests: 0 (irrelevant) to 10 (must-read).",
    )
    rationale: str = Field(description="One or two sentences explaining the score.")
    matched_interest: Optional[str] = Field(
        default=None,
        description="Which stated interest this paper matches most closely, if any.",
    )


class DeepReadResult(BaseModel):
    """Strong-model structured summary + critique of a full paper."""

    summary: str = Field(description="A concise 3-5 sentence summary of the paper.")
    key_contributions: list[str] = Field(
        description="The paper's main claimed contributions, as short bullet points."
    )
    methodology: str = Field(description="A brief description of the approach/methods used.")
    limitations: list[str] = Field(
        description="Weaknesses, gaps, or critique points, as short bullet points."
    )
    relevance_to_interests: str = Field(
        description="Why this paper matters (or doesn't) given the user's stated interests."
    )
    novel_or_incremental: Literal["novel", "incremental", "survey", "other"]
    worth_followup: bool = Field(
        description="Whether this paper warrants deeper personal follow-up beyond this summary."
    )
