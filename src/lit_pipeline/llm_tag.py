"""Tagging: place one paper in the research taxonomy (see taxonomy.py).

The triage and deep-read stages (pipeline_stages.py) call this for every
paper scoring taxonomy.min_score or more, and lit-tag (tagger.py) calls it to
backfill papers triaged before tagging existed. Both use the same prompt, so a
paper is placed the same way whichever path tagged it.

The system prompt -- the instructions, the placement rules and the full
category outline -- is the same for every paper, so it goes in a cached block:
after the first call of a run it's read from the prompt cache at a fraction
of the input price, and each call pays full price only for the paper itself.

The category comes back through structured output whose enum is exactly the
taxonomy's leaves plus "unsorted" (taxonomy.tag_schema), so it's always a real
tag. A safety decline is retried server-side on Anthropic's recommended
fallback model (`fallbacks="default"`); if every model declines, the call
raises and the caller carries on without a new tag.

Usage is recorded against settings.triage.model even when a fallback model
answered, so the triage stage can add it to the triage call's own usage.
"""

from __future__ import annotations

from anthropic import Anthropic

from lit_pipeline.config import Settings
from lit_pipeline.pricing import LLMUsage
from lit_pipeline.taxonomy import Taxonomy, tag_schema

TAG_SYSTEM_PROMPT = """You sort research papers into a fixed taxonomy for a researcher who \
follows algorithmic fairness and fair lending. You are given one paper. Place it in the single \
most specific category that matches its main contribution: what the paper itself contributes, \
not every topic it touches. Answer with that category's id, following the placement rules."""

# Header for the server-side fallback's "default" mode (Anthropic picks the
# fallback model by refusal category).
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Caps thinking and the answer together. The answer is one short id; the rest
# is headroom so thinking never cuts it off.
MAX_TOKENS = 16000


def tag_paper(
    client: Anthropic,
    settings: Settings,
    taxonomy: Taxonomy,
    title: str,
    abstract: str,
    deep_read_summary: str = "",
    deep_read_relevance: str = "",
) -> tuple[str, LLMUsage]:
    """Return (tag, usage). Pass the deep-read fields to place the paper with
    its full-text summary in view; leave them out for an abstract-only tag."""
    user_content = f"Title: {title}\n\nAbstract: {abstract}"
    if deep_read_summary:
        user_content += f"\n\nSummary of the full paper, from a read of its full text: {deep_read_summary}"
    if deep_read_relevance:
        user_content += f"\n\nWhy it matters to the researcher, from the same read: {deep_read_relevance}"

    response = client.beta.messages.parse(
        model=settings.triage.model,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": f"{TAG_SYSTEM_PROMPT}\n\n{taxonomy.prompt_text}",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
        output_format=tag_schema(taxonomy),
        output_config={"effort": settings.taxonomy.effort},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )
    result = response.parsed_output
    if result is None:
        raise ValueError(f"Tagging returned no category (stop_reason={response.stop_reason})")
    usage = LLMUsage(
        model=settings.triage.model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cache_creation_input_tokens=response.usage.cache_creation_input_tokens or 0,
        cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
    )
    return result.category, usage
