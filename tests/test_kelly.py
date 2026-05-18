"""Tests for ¼-Kelly position sizing."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.qa.kelly import (
    expected_value_cents,
    full_kelly_fraction,
    kelly_contracts,
    quarter_kelly_fraction,
    worst_case_loss_cents,
)


class TestFullKellyFraction:
    def test_positive_edge(self):
        # 60% true probability, 50¢ limit price (fair odds) → positive Kelly
        frac = full_kelly_fraction(0.60, 50)
        assert frac > 0

    def test_zero_edge(self):
        # True prob matches implied prob → no Kelly bet
        frac = full_kelly_fraction(0.50, 50)
        assert frac == pytest.approx(0.0, abs=1e-9)

    def test_negative_edge_clamped(self):
        # Market overpriced vs our estimate
        frac = full_kelly_fraction(0.40, 60)
        assert frac == 0.0

    def test_extreme_edge(self):
        # Near-certain YES at 10¢ → large Kelly fraction
        frac = full_kelly_fraction(0.98, 10)
        assert frac > 0.5

    def test_invalid_prob_raises(self):
        with pytest.raises(ValueError):
            full_kelly_fraction(1.5, 50)

    def test_invalid_price_raises(self):
        with pytest.raises(ValueError):
            full_kelly_fraction(0.6, 0)
        with pytest.raises(ValueError):
            full_kelly_fraction(0.6, 100)


class TestQuarterKelly:
    def test_is_quarter_of_full(self):
        for prob in [0.55, 0.65, 0.75]:
            for price in [30, 50, 70]:
                assert quarter_kelly_fraction(prob, price) == pytest.approx(
                    full_kelly_fraction(prob, price) / 4
                )

    @given(
        prob=st.floats(min_value=0.01, max_value=0.99),
        price=st.integers(min_value=1, max_value=99),
    )
    def test_never_exceeds_full_kelly(self, prob, price):
        assert quarter_kelly_fraction(prob, price) <= full_kelly_fraction(prob, price) + 1e-12

    @given(
        prob=st.floats(min_value=0.01, max_value=0.99),
        price=st.integers(min_value=1, max_value=99),
    )
    def test_always_non_negative(self, prob, price):
        assert quarter_kelly_fraction(prob, price) >= 0.0


class TestKellyContracts:
    def test_basic_sizing(self):
        # 60% true prob, 50¢ limit, $1000 bankroll, 2% cap, 100 contract max
        n = kelly_contracts(0.60, 50, 100_000, 0.02, 100)
        assert n >= 0
        assert n <= 100

    def test_zero_edge_yields_zero(self):
        n = kelly_contracts(0.50, 50, 100_000, 0.02, 100)
        assert n == 0

    def test_max_contracts_respected(self):
        n = kelly_contracts(0.95, 10, 10_000_000, 0.50, 5)
        assert n <= 5

    def test_max_pct_cap(self):
        # Bankroll $10k, 1% cap = $100. At 50¢ limit that's 200 contracts max.
        n = kelly_contracts(0.99, 50, 1_000_000, 0.01, 10_000)
        assert n * 50 <= 1_000_000 * 0.01 + 50  # within one contract of cap


class TestWorstCaseLoss:
    def test_basic(self):
        assert worst_case_loss_cents(100, 50) == 5000

    def test_zero_contracts(self):
        assert worst_case_loss_cents(0, 50) == 0


class TestExpectedValue:
    def test_positive_ev(self):
        ev = expected_value_cents(10, 40, 0.60)
        assert ev > 0

    def test_negative_ev(self):
        ev = expected_value_cents(10, 60, 0.40)
        assert ev < 0
