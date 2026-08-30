"""LLM client wrapper.

The system works fully without an API key — agents fall back to deterministic
composition. When a key is present, this wrapper enforces a per-run call budget
and returns a structured result that records whether the model was actually used,
so nothing downstream can mistake a template for model output or vice versa.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.core.config import get_settings
from backend.core.logging_config import EVENT_RESEARCH_RUN, get_logger, log_event

logger = get_logger(__name__)


@dataclass
class LlmResponse:
    text: str
    model: str
    used_llm: bool
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model, "used_llm": self.used_llm,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "error": self.error,
        }


@dataclass
class CallBudget:
    """Hard cap on LLM calls per research run — protects against runaway cost."""

    limit: int
    used: int = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def consume(self) -> bool:
        if self.exhausted:
            return False
        self.used += 1
        return True


class LlmClient:
    """Thin Anthropic wrapper. Absent a key, :attr:`available` is False."""

    def __init__(self, budget: CallBudget | None = None) -> None:
        settings = get_settings()
        self.settings = settings
        self.model = settings.ai_model
        self.budget = budget or CallBudget(limit=settings.ai_max_calls_per_run)
        self._client: Any = None
        self._init_error: str | None = None

        if not settings.anthropic_api_key:
            self._init_error = "ANTHROPIC_API_KEY is not set."
            return
        try:
            import anthropic

            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        except Exception as exc:  # noqa: BLE001
            self._init_error = f"Anthropic client unavailable: {exc}"

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def unavailable_reason(self) -> str | None:
        return self._init_error

    def complete(
        self,
        system: str,
        prompt: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LlmResponse:
        """Single completion. Failures degrade to the deterministic path."""
        if not self.available:
            return LlmResponse("", self.model, False, error=self._init_error)
        if not self.budget.consume():
            return LlmResponse(
                "", self.model, False,
                error=f"LLM call budget of {self.budget.limit} exhausted for this run.",
            )
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens or self.settings.ai_max_tokens,
                temperature=(
                    temperature if temperature is not None else self.settings.ai_temperature
                ),
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(
                block.text for block in message.content if getattr(block, "type", "") == "text"
            )
            usage = getattr(message, "usage", None)
            return LlmResponse(
                text=text, model=self.model, used_llm=True,
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
            )
        except Exception as exc:  # noqa: BLE001
            log_event(
                logger, EVENT_RESEARCH_RUN, f"LLM call failed: {exc}",
                model=self.model, error=str(exc),
            )
            return LlmResponse("", self.model, False, error=str(exc))
