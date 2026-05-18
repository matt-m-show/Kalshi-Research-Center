"""Pure helper functions for the Research Agent.

These helpers enforce the Research Agent's behavioral rules:
- Liquidity screening
- Source trust-tier validation
- Independent probability estimation guard (anti-anchoring)
- Recommendation schema validation before output
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from src.common.schemas import Confidence, Recommendation, TrustTier
from src.kalshi_client.models import Market, Orderbook


# ---------------------------------------------------------------------------
# Liquidity screening
# ---------------------------------------------------------------------------


def passes_liquidity_floors(
    market: Market,
    min_open_interest: int,
    min_daily_volume: int,
    max_spread_bps: int,
    orderbook: Orderbook | None = None,
) -> tuple[bool, str]:
    """Return (passes, reason). Reason is empty string if passes."""
    if market.open_interest < min_open_interest:
        return False, f"Open interest {market.open_interest} < floor {min_open_interest}"
    if market.volume < min_daily_volume:
        return False, f"Daily volume {market.volume} < floor {min_daily_volume}"
    if orderbook is not None:
        spread = orderbook.spread_bps()
        if spread is not None and spread > max_spread_bps:
            return False, f"Spread {spread}bps > max {max_spread_bps}bps"
    return True, ""


# ---------------------------------------------------------------------------
# Source trust-tier validation
# ---------------------------------------------------------------------------


def validate_sources(sources: list[dict[str, Any]]) -> tuple[bool, str]:
    """Enforce sourcing rules before the Research Agent outputs a recommendation.

    Rules:
    - At least one tier-1 source required.
    - Cannot recommend if ONLY tier-3 (social) sources exist.
    - Cannot recommend if tier-1 sources materially disagree (caller-detected).
    """
    if not sources:
        return False, "No sources provided"

    tiers = {s.get("trust_tier", 3) for s in sources}
    if TrustTier.PRIMARY not in tiers and TrustTier.SOCIAL in tiers and TrustTier.AGGREGATOR not in tiers:
        return False, "Only tier-3 social sources — insufficient for a recommendation"
    if TrustTier.PRIMARY not in tiers:
        return False, "At least one tier-1 primary source is required"
    return True, ""


def infer_confidence(sources: list[dict[str, Any]]) -> Confidence:
    """Downgrade confidence if sources are mostly social."""
    tiers = [s.get("trust_tier", 3) for s in sources]
    social_ratio = tiers.count(TrustTier.SOCIAL) / max(len(tiers), 1)
    if social_ratio > 0.5:
        return Confidence.LOW
    tier1_count = sum(1 for t in tiers if t == TrustTier.PRIMARY)
    if tier1_count >= 2:
        return Confidence.HIGH
    return Confidence.MEDIUM


# ---------------------------------------------------------------------------
# Anti-anchoring guard
# ---------------------------------------------------------------------------


def estimate_independent_prob_prompt() -> str:
    """Return the system-level instruction the Research Agent must follow
    to produce an independent probability estimate before looking at the
    market-implied price.

    This is embedded in the Research Agent's system prompt.
    """
    return (
        "IMPORTANT: You must form and record your probability estimate "
        "BEFORE reading the current market bid/ask or implied probability. "
        "Write your estimate in the recommendation JSON as `estimated_fair_prob`. "
        "Only AFTER recording your estimate should you fetch the market price "
        "and compute `market_implied_prob` and `edge_bps`. "
        "Do not revise your estimate based on the market price alone."
    )


# ---------------------------------------------------------------------------
# Minimum time-to-resolution check
# ---------------------------------------------------------------------------


def passes_time_to_resolution(market: Market, min_hours: float = 1.0) -> tuple[bool, str]:
    """Reject markets resolving too soon."""
    if market.close_time is None:
        return True, ""
    now = datetime.utcnow()
    if market.close_time.tzinfo is not None:
        from datetime import timezone
        now = now.replace(tzinfo=timezone.utc)
    hours_left = (market.close_time - now).total_seconds() / 3600
    if hours_left < min_hours:
        return False, f"Market resolves in {hours_left:.1f}h < minimum {min_hours}h"
    return True, ""


# ---------------------------------------------------------------------------
# Universe filter
# ---------------------------------------------------------------------------


def in_universe(ticker: str, include_patterns: list[str], exclude_patterns: list[str]) -> bool:
    """Check whether a ticker is in the configured universe.

    include_patterns: list of regex patterns; ticker must match at least one.
    exclude_patterns: list of regex patterns; ticker must NOT match any.
    """
    if include_patterns:
        if not any(re.match(p, ticker, re.IGNORECASE) for p in include_patterns):
            return False
    if any(re.match(p, ticker, re.IGNORECASE) for p in exclude_patterns):
        return False
    return True
