"""Model strategy: route each job to the cheapest model that can do it well."""

from __future__ import annotations

import logging
from enum import StrEnum

from jarvis.config import LLMSettings

log = logging.getLogger(__name__)


class ModelTier(StrEnum):
    FAST = "fast"            # summarisation, memory extraction, short confirmations
    GENERAL = "general"      # normal conversation with tool use
    REASONING = "reasoning"  # planning, research synthesis
    VISION = "vision"        # screenshots

# Preferred substitutes if a configured model is missing on the provider.
_FALLBACKS: dict[ModelTier, list[str]] = {
    ModelTier.FAST: ["llama-3.1-8b-instant", "openai/gpt-oss-20b", "gpt-4o-mini"],
    ModelTier.GENERAL: ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "moonshotai/kimi-k2-instruct", "gpt-4o"],
    ModelTier.REASONING: ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "o4-mini", "gpt-4o"],
    ModelTier.VISION: ["meta-llama/llama-4-scout-17b-16e-instruct", "meta-llama/llama-4-maverick-17b-128e-instruct", "gpt-4o"],
}


class ModelStrategy:
    def __init__(self, settings: LLMSettings) -> None:
        self._models: dict[ModelTier, str] = {
            ModelTier.FAST: settings.models.fast,
            ModelTier.GENERAL: settings.models.general,
            ModelTier.REASONING: settings.models.reasoning,
            ModelTier.VISION: settings.models.vision,
        }
        self.warnings: list[str] = []

    def model_for(self, tier: ModelTier) -> str:
        return self._models[tier]

    def reconcile(self, available: list[str]) -> list[str]:
        """Swap configured models the provider doesn't offer for known-good substitutes."""
        if not available:
            return []
        available_set = set(available)
        warnings: list[str] = []
        for tier, model in list(self._models.items()):
            if model in available_set:
                continue
            substitute = next((m for m in _FALLBACKS[tier] if m in available_set), None)
            if substitute:
                warnings.append(f"{tier} model '{model}' unavailable; using '{substitute}'")
                self._models[tier] = substitute
            else:
                warnings.append(f"{tier} model '{model}' not offered by the provider")
        for warning in warnings:
            log.warning(warning)
        self.warnings = warnings
        return warnings

    def snapshot(self) -> dict[str, str]:
        return {tier.value: model for tier, model in self._models.items()}
