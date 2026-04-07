"""Tests for predx.analytics.execution — orderbook analytics."""
from datetime import datetime, timezone

from predx.models.common import Orderbook, PriceLevel, Exchange
from predx.analytics.execution import (
    analyze,
    slippage_curve,
    market_impact,
    microprice,
    obi,
)


def _make_ob(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
) -> Orderbook:
    """Helper: build an Orderbook from (price, size) tuples."""
    return Orderbook(
        market_id="test-market",
        exchange=Exchange.POLYMARKET,
        yes_bids=[PriceLevel(p, s) for p, s in bids],
        yes_asks=[PriceLevel(p, s) for p, s in asks],
        timestamp=datetime.now(timezone.utc),
    )


# -- Symmetric book for baseline tests --
SYMMETRIC = _make_ob(
    bids=[(0.50, 1000), (0.49, 2000), (0.48, 3000)],
    asks=[(0.51, 1000), (0.52, 2000), (0.53, 3000)],
)

# -- Imbalanced book (heavy bids) --
BID_HEAVY = _make_ob(
    bids=[(0.50, 5000), (0.49, 3000), (0.48, 2000)],
    asks=[(0.51, 200), (0.52, 300)],
)

# -- Thin book --
THIN = _make_ob(
    bids=[(0.45, 10)],
    asks=[(0.55, 10)],
)


class TestAnalyze:
    def test_spread(self):
        r = analyze(SYMMETRIC)
        assert abs(r.spread - 0.01) < 1e-9
        assert r.best_bid == 0.50
        assert r.best_ask == 0.51

    def test_spread_bps(self):
        r = analyze(SYMMETRIC)
        # spread = 0.01, mid = 0.505
        assert 190 < r.spread_bps < 200

    def test_microprice_symmetric(self):
        r = analyze(SYMMETRIC)
        # Equal sizes at top → microprice = midpoint
        assert r.microprice == r.mid

    def test_microprice_imbalanced(self):
        r = analyze(BID_HEAVY)
        # Bid has more size → microprice shifts toward ask
        assert r.microprice > r.mid

    def test_obi_symmetric(self):
        r = analyze(SYMMETRIC)
        assert r.obi == 0.0  # equal volume on both sides

    def test_obi_bid_heavy(self):
        r = analyze(BID_HEAVY)
        assert r.obi > 0.5  # strong buy pressure

    def test_pressure_buy(self):
        r = analyze(BID_HEAVY)
        assert r.pressure == "buy"

    def test_pressure_balanced(self):
        r = analyze(SYMMETRIC)
        assert r.pressure == "balanced"

    def test_liquidity_levels(self):
        r = analyze(SYMMETRIC)
        assert r.bid_liquidity.levels == 3
        assert r.ask_liquidity.levels == 3
        assert r.bid_liquidity.total_volume == 6000
        assert r.ask_liquidity.total_volume == 6000

    def test_liquidity_concentration(self):
        r = analyze(SYMMETRIC)
        # Top level = 1000 out of 6000
        assert abs(r.bid_liquidity.concentration - 1000 / 6000) < 0.001

    def test_depth_ratio(self):
        r = analyze(BID_HEAVY)
        assert r.depth_ratio > 1.5

    def test_str(self):
        r = analyze(SYMMETRIC)
        s = str(r)
        assert "Execution Report" in s
        assert "bps" in s

    def test_thin_spread(self):
        r = analyze(THIN)
        assert abs(r.spread - 0.10) < 1e-9


class TestSlippageCurve:
    def test_basic(self):
        curve = slippage_curve(SYMMETRIC, side="buy", sizes=[100, 500, 1000])
        assert len(curve) == 3

    def test_small_order_no_slippage(self):
        curve = slippage_curve(SYMMETRIC, side="buy", sizes=[10])
        pt = curve[0]
        assert pt.avg_price == 0.51  # fills entirely at best ask
        assert pt.levels_consumed == 1
        assert pt.fillable is True

    def test_slippage_increases_with_size(self):
        curve = slippage_curve(SYMMETRIC, side="buy", sizes=[100, 2000, 5000])
        assert curve[0].slippage_bps <= curve[1].slippage_bps <= curve[2].slippage_bps

    def test_large_order_unfillable(self):
        curve = slippage_curve(SYMMETRIC, side="buy", sizes=[100000])
        assert curve[0].fillable is False

    def test_sell_side(self):
        curve = slippage_curve(SYMMETRIC, side="sell", sizes=[500])
        pt = curve[0]
        assert pt.avg_price == 0.50  # fills at best bid
        assert pt.fillable is True

    def test_walks_multiple_levels(self):
        # 1000 at 0.51, need 1500 more from 0.52
        curve = slippage_curve(SYMMETRIC, side="buy", sizes=[2500])
        pt = curve[0]
        assert pt.levels_consumed == 2
        assert pt.avg_price > 0.51

    def test_default_sizes(self):
        curve = slippage_curve(SYMMETRIC, side="buy")
        assert len(curve) == 7


class TestMarketImpact:
    def test_small_buy(self):
        pt = market_impact(SYMMETRIC, size=100, side="buy")
        assert pt.avg_price == 0.51
        assert pt.slippage_bps < 100  # ~99 bps for 1-cent spread on 0.505 mid

    def test_large_buy(self):
        pt = market_impact(SYMMETRIC, size=5000, side="buy")
        assert pt.avg_price > 0.51
        assert pt.levels_consumed == 3

    def test_thin_book_impact(self):
        pt = market_impact(THIN, size=100, side="buy")
        assert pt.fillable is False  # only 10 available


class TestMicroprice:
    def test_symmetric(self):
        mp = microprice(SYMMETRIC)
        assert mp == SYMMETRIC.mid

    def test_bid_heavy(self):
        mp = microprice(BID_HEAVY)
        mid = BID_HEAVY.mid
        assert mp > mid  # shifts toward ask when bids dominate

    def test_empty_book(self):
        empty = _make_ob(bids=[], asks=[])
        assert microprice(empty) is None


class TestOBI:
    def test_symmetric(self):
        assert obi(SYMMETRIC) == 0.0

    def test_bid_heavy(self):
        assert obi(BID_HEAVY) > 0

    def test_custom_levels(self):
        # Only look at top 1 level
        val = obi(SYMMETRIC, levels=1)
        assert val == 0.0  # 1000 bid vs 1000 ask

    def test_empty(self):
        empty = _make_ob(bids=[], asks=[])
        assert obi(empty) == 0.0
