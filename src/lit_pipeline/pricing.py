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


# USD per 1M tokens. Sonnet 5 isn't used by this pipeline's default config
# but is included for anyone who swaps deep_read.model to it.
PRICING: dict[str, ModelPricing] = {
    "claude-haiku-4-5": ModelPricing(input_per_million=1.00, output_per_million=5.00),
    "claude-sonnet-5": ModelPricing(input_per_million=3.00, output_per_million=15.00),
    "claude-opus-5": ModelPricing(input_per_million=5.00, output_per_million=25.00),
}


@dataclass(frozen=True)
class LLMUsage:
    model: str
    input_tokens: int
    output_tokens: int

    @property
    def cost_usd(self) -> float:
        pricing = PRICING.get(self.model)
        if pricing is None:
            logger.warning("No pricing entry for model %r; recording cost as 0.0", self.model)
            return 0.0
        return (
            self.input_tokens / 1_000_000 * pricing.input_per_million
            + self.output_tokens / 1_000_000 * pricing.output_per_million
        )
