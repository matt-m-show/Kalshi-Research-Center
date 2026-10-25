"""Token and dollar accounting for LLM calls.

Prices are USD per million tokens (Anthropic first-party rates). Cache writes
bill at 1.25x the input rate, cache reads at 0.1x. Local models cost $0.
Override or extend via runtime.yaml → model_pricing.
"""

from __future__ import annotations

import dataclasses
from typing import Any

DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
}


@dataclasses.dataclass
class ModelUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0


class UsageMeter:
    def __init__(self, prices: dict[str, Any] | None = None) -> None:
        self.prices = dict(DEFAULT_PRICES)
        for model, p in (prices or {}).items():
            self.prices[model] = (float(p["input"]), float(p["output"]))
        self.by_model: dict[str, ModelUsage] = {}

    def price(self, model: str) -> tuple[float, float]:
        if model.startswith("ollama:"):
            return (0.0, 0.0)
        return self.prices.get(model, self.prices["claude-sonnet-5-5"])

    def add_anthropic(self, model: str, usage: Any) -> float:
        """Record one Anthropic response's usage; returns its cost."""
        p_in, p_out = self.price(model)
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        cost = (inp * p_in + out * p_out + cw * p_in * 1.25 + cr * p_in * 0.1) / 1_000_000
        u = self.by_model.setdefault(model, ModelUsage())
        u.calls += 1
        u.input_tokens += inp
        u.output_tokens += out
        u.cache_write_tokens += cw
        u.cache_read_tokens += cr
        u.cost_usd += cost
        return cost

    def add_local(self, model: str, prompt_tokens: int, output_tokens: int) -> None:
        u = self.by_model.setdefault(f"ollama:{model}", ModelUsage())
        u.calls += 1
        u.input_tokens += prompt_tokens
        u.output_tokens += output_tokens

    @property
    def total_cost(self) -> float:
        return sum(u.cost_usd for u in self.by_model.values())

    def as_dict(self) -> dict[str, dict]:
        return {m: dataclasses.asdict(u) for m, u in self.by_model.items()}
