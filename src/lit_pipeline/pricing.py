"""Per-model USD pricing for cost tracking.

Anthropic doesn't expose pricing via the API, so this table is manually
maintained -- update it if you change which models `config/settings.yaml`
points at, or if Anthropic's prices change. Costs computed from it are
estimates based on the *actual* token counts each response reports
(`response.usage`), not guesses -- the only approximation is the price
table itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPricing:
    input_per_million: float
    output_per_million: float
    # Prompt caching: writing a 5-minute cache entry costs more than plain
    # input, and reading one far less. None means the standard 1.25x / 0.1x of
    # the input price; a model whose rates differ sets them explicitly.
    cache_write_per_million: float | None = None
    cache_read_per_million: float | None = None

    @property
    def cache_write(self) -> float:
        return self.cache_write_per_million if self.cache_write_per_million is not None else self.input_per_million * 1.25

    @property
    def cache_read(self) -> float:
        return self.cache_read_per_million if self.cache_read_per_million is not None else self.input_per_million * 0.1


# USD per 1M tokens, checked against Anthropic's pricing page on 2026-10-01.
# Only Opus 5.5 is used by the default config; the rest are
# here for anyone who swaps a model in settings.yaml, and for the model
# comparison in analysis/model_comparison/.
PRICING: dict[str, ModelPricing] = {
    "claude-haiku-4-5": ModelPricing(input_per_million=1.00, output_per_million=5.00),
    "claude-sonnet-5": ModelPricing(input_per_million=2.00, output_per_million=10.00),
    "claude-sonnet-5-5": ModelPricing(input_per_million=2.00, output_per_million=10.00),
    "claude-opus-5": ModelPricing(input_per_million=5.00, output_per_million=25.00),
    # Cache reads are $0.20, not the standard 0.1x of input.
    "claude-opus-5-5": ModelPricing(input_per_million=4.00, output_per_million=20.00, cache_read_per_million=0.20),
    "claude-fable-5-1": ModelPricing(input_per_million=10.00, output_per_million=50.00),
}


@dataclass(frozen=True)
class LLMUsage:
    model: str
    input_tokens: int
    output_tokens: int
    # The API reports cached prompt tokens separately from input_tokens.
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __add__(self, other: LLMUsage) -> LLMUsage:
        """Two calls' usage as one, e.g. a triage and the tagging call after
        it. Only for calls to the same model, since cost is priced per model."""
        if other.model != self.model:
            raise ValueError(f"Can't add {other.model} usage to {self.model} usage; price them separately")
        return LLMUsage(
            model=self.model,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens + other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
        )

    @property
    def cost_usd(self) -> float:
        pricing = PRICING.get(self.model)
        if pricing is None:
            logger.warning("No pricing entry for model %r; recording cost as 0.0", self.model)
            return 0.0
        return (
            self.input_tokens / 1_000_000 * pricing.input_per_million
            + self.output_tokens / 1_000_000 * pricing.output_per_million
            + self.cache_creation_input_tokens / 1_000_000 * pricing.cache_write
            + self.cache_read_input_tokens / 1_000_000 * pricing.cache_read
        )
