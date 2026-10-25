"""Quarter-Kelly position sizing for binary (YES/NO) prediction market contracts.

For a binary market where:
  p  = our estimated probability of winning
  q  = 1 - p
  b  = net odds (payout per $1 risked)

In a Kalshi YES contract at limit_price_cents c (0-99):
  - If YES wins: profit per contract = (100 - c) cents
  - If YES loses: loss per contract  = c cents
  So net odds b = (100 - c) / c

Full Kelly fraction = p - q/b = p - (1-p)*c/(100-c)
Quarter Kelly fraction = full_kelly / 4

Contracts to buy = floor(fraction * bankroll_cents / c)
"""

from __future__ import annotations

import math


def full_kelly_fraction(estimated_prob: float, limit_price_cents: int) -> float:
    """Return the full Kelly fraction (0..1) for a YES position.

    Returns 0.0 if the trade has non-positive expected value.
    """
    if not (0 < limit_price_cents < 100):
        raise ValueError(f"limit_price_cents must be 1-99, got {limit_price_cents}")
    if not (0.0 <= estimated_prob <= 1.0):
        raise ValueError(f"estimated_prob must be 0-1, got {estimated_prob}")

    p = estimated_prob
    q = 1.0 - p
    b = (100 - limit_price_cents) / limit_price_cents  # net odds on a win

    fraction = p - q / b
    return max(0.0, fraction)


def quarter_kelly_fraction(estimated_prob: float, limit_price_cents: int) -> float:
    """Return ¼-Kelly fraction. This is the configured sizing mode."""
    return full_kelly_fraction(estimated_prob, limit_price_cents) / 4.0


def kelly_contracts(
    estimated_prob: float,
    limit_price_cents: int,
    bankroll_cents: int,
    max_position_pct: float,
    max_contracts: int,
    kelly_fraction: float = 0.25,
) -> int:
    """Compute the number of contracts to buy under fractional Kelly with hard caps.

    Args:
        estimated_prob: Fair probability of the side being bought.
        limit_price_cents: Proposed limit price (cost per contract) of that side.
        bankroll_cents: Available bankroll in cents.
        max_position_pct: Hard cap as a fraction of bankroll (e.g. 0.02 for 2%).
        max_contracts: Absolute contract count cap.
        kelly_fraction: Fraction of full Kelly to bet (0.25 = quarter Kelly).

    Returns:
        Integer number of contracts (≥ 0). 0 means "don't trade".
    """
    fraction = full_kelly_fraction(estimated_prob, limit_price_cents) * kelly_fraction
    if fraction <= 0.0:
        return 0

    # Dollar amount to risk at ¼-Kelly
    kelly_amount_cents = fraction * bankroll_cents

    # Hard cap: never risk more than max_position_pct of bankroll
    cap_cents = max_position_pct * bankroll_cents
    risk_cents = min(kelly_amount_cents, cap_cents)

    # Convert dollar risk to contract count (each contract costs limit_price_cents)
    contracts = int(risk_cents / limit_price_cents)
    return min(contracts, max_contracts)


def worst_case_loss_cents(contracts: int, limit_price_cents: int) -> int:
    """Total loss if position goes to zero (all contracts expire worthless)."""
    return contracts * limit_price_cents


def expected_value_cents(
    contracts: int,
    limit_price_cents: int,
    estimated_prob: float,
) -> float:
    """Expected value of the position in cents."""
    win_pnl = contracts * (100 - limit_price_cents)
    loss_pnl = -contracts * limit_price_cents
    return estimated_prob * win_pnl + (1 - estimated_prob) * loss_pnl
