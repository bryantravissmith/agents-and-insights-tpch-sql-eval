"""API list prices, for models whose CLI reports no cost or a cost it marks as unknown.

Each run's cost says where it came from (`cost_basis`): the CLI's own figure, or tokens priced here. Neither
is what a subscription user pays; both compare models at list price on the same footing.
"""

from __future__ import annotations

from dataclasses import dataclass

CHECKED_ON = "2026-10-31"
GOOGLE = "https://ai.google.dev/gemini-api/docs/pricing"
ANTHROPIC = "https://platform.claude.com/docs/en/about-claude/pricing"


@dataclass(frozen=True)
class Price:
    """USD per million tokens, standard tier. Output includes thinking tokens."""

    input: float
    cached_input: float
    output: float
    source: str
    valid_through: str = ""
    cache_write_5m: float = 0.0
    cache_write_1h: float = 0.0


# Keyed by the model name's prefix, so effort-suffixed agy names (gemini-3.8-flash-medium) match.
PRICES: dict[str, Price] = {
    "gemini-3.8-flash": Price(0.75, 0.075, 3.75, GOOGLE, "2026-12-31"),
    "gemini-4-argon": Price(2.00, 0.10, 10.00, GOOGLE, "2026-11-30"),
    # Haiku 5.5 at its rate for prompts up to 100K tokens; every prompt in this eval is far below that.
    "claude-haiku-5-5": Price(0.10, 0.01, 0.50, ANTHROPIC, cache_write_5m=0.125, cache_write_1h=0.20),
    "claude-sonnet-5-5": Price(2.00, 0.10, 10.00, ANTHROPIC, cache_write_5m=2.50, cache_write_1h=4.00),
    "claude-opus-5-5": Price(4.00, 0.20, 20.00, ANTHROPIC, cache_write_5m=5.00, cache_write_1h=8.00),
    "claude-fable-5-1": Price(10.00, 0.25, 50.00, ANTHROPIC, cache_write_5m=12.50, cache_write_1h=20.00),
}

# What a CLI-reported cost means, by harness.
REPORTED_BASIS = {
    "claude": "Claude Code's reported cost for the evaluated model, or list price where it reports the cost as "
    "unknown (API-equivalent; subscription runs are not billed per token)",
    "pi": "pi's price table (API-equivalent)",
    "codex": "codex's reported cost",
    "grok": "grok CLI's reported cost",
}


def price_for(model: str) -> Price | None:
    matches = [k for k in PRICES if model.startswith(k)]
    return PRICES[max(matches, key=len)] if matches else None


def api_cost(
    model: str,
    tokens_in: float,
    tokens_cache_read: float,
    tokens_out: float,
    cache_write_5m: float = 0,
    cache_write_1h: float = 0,
) -> float | None:
    """List-price cost of one run; `tokens_in` excludes cache reads and writes (as the harness parsers record it)."""
    p = price_for(model)
    if p is None:
        return None
    return (
        tokens_in * p.input
        + tokens_cache_read * p.cached_input
        + tokens_out * p.output
        + cache_write_5m * p.cache_write_5m
        + cache_write_1h * p.cache_write_1h
    ) / 1e6


def cost_basis(harness: str, model: str) -> str:
    if (p := price_for(model)) is not None and harness not in REPORTED_BASIS:
        return f"computed from list price, checked {CHECKED_ON} ({p.source})"
    return REPORTED_BASIS.get(harness, "unavailable")
