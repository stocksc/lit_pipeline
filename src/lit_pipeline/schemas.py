"""Pydantic schemas for structured LLM output.

These are passed as `output_format=` to `client.messages.parse(...)`, which
returns an already-validated instance on `response.parsed_output` — no manual
JSON parsing or prompt-engineered JSON formatting required.
"""

from __future__ import annotations

from typing import Optional

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


class MidSummaryResult(BaseModel):
    """Cheap-model summary for mid-tier papers (scored between
    mid_summary_threshold and score_threshold), generated from the abstract
    alone -- no PDF fetch."""

    summary: str = Field(description="A concise summary of the paper's abstract, in about 50 words.")


class DeepReadResult(BaseModel):
    """Strong-model quick-hit summary of a full paper. Deliberately compact --
    these are click-through teasers, not full reviews."""

    score: int = Field(
        ge=0,
        le=10,
        description=(
            "Your own relevance rating for this paper on the same 0-10 scale as the "
            "initial triage, now informed by the full paper text rather than just the "
            "abstract. Rate independently based on the researcher's stated interests and "
            "what you've actually read -- it's expected and fine for this to come out "
            "lower (or higher) than the paper's original triage score."
        ),
    )
    summary: str = Field(description="A concise summary of the paper, in about 100 words.")
    relevance: list[str] = Field(
        description=(
            "Bullet points (about 50 words total) on why this paper is relevant to the "
            "researcher's stated interests."
        )
    )
    limitations: list[str] = Field(
        description=(
            "Bullet points (about 50 words total) on what makes this paper NOT directly "
            "useful for a practitioner -- e.g. no code/data released, synthetic-only "
            "validation, proprietary data required, purely theoretical, inapplicable to a "
            "regulated setting. Not a generic academic critique -- specifically practical "
            "applicability gaps."
        )
    )
    author_affiliations: list[str] = Field(
        description=(
            "Each unique institution/affiliation among the paper's authors, listed once, "
            "in order of first appearance (usually found near the author list on the first "
            "page or in footnotes). Empty list if none can be determined from the text."
        )
    )
