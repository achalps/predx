"""
Execution analytics — understand the cost of trading before you trade.

Analyze orderbooks for slippage, market impact, liquidity, and microstructure
signals. Works with any predx Orderbook (Polymarket or Kalshi).

Usage:
    from predx import PolymarketClient
    from predx.analytics.execution import analyze, slippage_curve

    pm = PolymarketClient()
    raw = pm.get_raw_market("0x...")
    ob = pm.get_orderbook(raw.yes_token_id())

    report = analyze(ob)
    print(report)

    curve = slippage_curve(ob, side="buy", sizes=[100, 500, 1000, 5000])
    for point in curve:
        print(f"  {point.size:>6} shares → avg {point.avg_price:.4f}  slip {point.slippage_bps:.0f}bps")
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models.common import Orderbook, PriceLevel


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SlippagePoint:
    """Cost of executing a specific size."""
    size: float
    avg_price: float
    worst_price: float
    slippage: float          # avg_price - mid (positive = cost)
    slippage_bps: float      # slippage in basis points relative to mid
    total_cost: float        # size * avg_price
    levels_consumed: int     # how many price levels were eaten through
    fillable: bool           # True if full size can be filled


@dataclass
class LiquidityProfile:
    """Depth and concentration on one side of the book."""
    total_volume: float
    levels: int
    volume_at_top: float          # size at best price
    volume_top_3: float
    volume_top_5: float
    avg_price_weighted: float     # volume-weighted average price across all levels
    concentration: float          # top level volume / total volume (0-1)


@dataclass
class ExecutionReport:
    """Full execution analytics for an orderbook."""
    market_id: str
    mid: Optional[float]
    best_bid: Optional[float]
    best_ask: Optional[float]
    spread: Optional[float]
    spread_bps: float

    # Microstructure
    microprice: Optional[float]
    obi: float                    # order book imbalance [-1, 1]

    # Liquidity
    bid_liquidity: LiquidityProfile
    ask_liquidity: LiquidityProfile
    total_liquidity: float        # bid + ask total volume

    # Imbalance signals
    depth_ratio: float            # bid_volume / ask_volume (>1 = more buy support)
    pressure: str                 # "buy", "sell", or "balanced"

    def __str__(self) -> str:
        lines = [
            f"=== Execution Report: {self.market_id[:30]} ===",
            f"  Mid: {self.mid:.4f}    Spread: {self.spread:.4f} ({self.spread_bps:.0f} bps)" if self.mid else "  No mid",
            f"  Microprice: {self.microprice:.4f}    OBI: {self.obi:+.3f}    Pressure: {self.pressure}" if self.microprice else f"  OBI: {self.obi:+.3f}",
            f"  Bid liquidity: {self.bid_liquidity.total_volume:,.0f} across {self.bid_liquidity.levels} levels",
            f"  Ask liquidity: {self.ask_liquidity.total_volume:,.0f} across {self.ask_liquidity.levels} levels",
            f"  Depth ratio: {self.depth_ratio:.2f}x (bid/ask)",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def analyze(ob: Orderbook) -> ExecutionReport:
    """
    Full execution analysis of an orderbook.

    Returns an ExecutionReport with spread, microprice, OBI,
    liquidity profiles, and imbalance signals.
    """
    mid = ob.mid
    spread = ob.spread
    spread_bps = (spread / mid * 10_000) if mid and spread else 0.0

    micro = _microprice(ob)
    obi = _obi(ob)

    bid_liq = _liquidity_profile(ob.yes_bids)
    ask_liq = _liquidity_profile(ob.yes_asks)

    total_liq = bid_liq.total_volume + ask_liq.total_volume
    depth_ratio = bid_liq.total_volume / ask_liq.total_volume if ask_liq.total_volume > 0 else float("inf")

    if depth_ratio > 1.5:
        pressure = "buy"
    elif depth_ratio < 0.67:
        pressure = "sell"
    else:
        pressure = "balanced"

    return ExecutionReport(
        market_id=ob.market_id,
        mid=mid,
        best_bid=ob.best_bid,
        best_ask=ob.best_ask,
        spread=spread,
        spread_bps=spread_bps,
        microprice=micro,
        obi=obi,
        bid_liquidity=bid_liq,
        ask_liquidity=ask_liq,
        total_liquidity=total_liq,
        depth_ratio=depth_ratio,
        pressure=pressure,
    )


def slippage_curve(
    ob: Orderbook,
    side: str = "buy",
    sizes: list[float] | None = None,
) -> list[SlippagePoint]:
    """
    Compute expected slippage at different order sizes.

    Args:
        ob: The orderbook to analyze.
        side: "buy" (lifts asks) or "sell" (hits bids).
        sizes: List of order sizes to simulate. Defaults to
               [10, 50, 100, 500, 1000, 5000, 10000].

    Returns:
        List of SlippagePoint for each size.
    """
    if sizes is None:
        sizes = [10, 50, 100, 500, 1000, 5000, 10000]

    mid = ob.mid
    if mid is None or mid == 0:
        return []

    levels = ob.yes_asks if side == "buy" else ob.yes_bids

    points = []
    for target_size in sizes:
        pt = _simulate_fill(levels, target_size, mid)
        points.append(pt)

    return points


def market_impact(ob: Orderbook, size: float, side: str = "buy") -> SlippagePoint:
    """
    Estimate the market impact of a single order.

    Walks the orderbook to simulate filling `size` contracts and
    returns the expected execution cost.
    """
    mid = ob.mid
    if mid is None or mid == 0:
        return SlippagePoint(
            size=size, avg_price=0, worst_price=0,
            slippage=0, slippage_bps=0, total_cost=0,
            levels_consumed=0, fillable=False,
        )

    levels = ob.yes_asks if side == "buy" else ob.yes_bids
    return _simulate_fill(levels, size, mid)


def microprice(ob: Orderbook) -> Optional[float]:
    """
    Compute the microprice — a size-weighted fair value estimate.

    Better than midpoint because it accounts for the relative sizes
    at the top of book. If the ask has much more size than the bid,
    the microprice shifts toward the bid (fair value is lower).
    """
    return _microprice(ob)


def obi(ob: Orderbook, levels: int = 5) -> float:
    """
    Order Book Imbalance at top N levels.

    Returns a value from -1 to +1:
      +1 = all volume on bid side (buy pressure)
      -1 = all volume on ask side (sell pressure)
       0 = balanced
    """
    return _obi(ob, levels)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _microprice(ob: Orderbook) -> Optional[float]:
    if not ob.yes_bids or not ob.yes_asks:
        return None
    bb, ba = ob.yes_bids[0], ob.yes_asks[0]
    total = bb.size + ba.size
    if total == 0:
        return ob.mid
    return (bb.price * ba.size + ba.price * bb.size) / total


def _obi(ob: Orderbook, levels: int = 5) -> float:
    bid_vol = sum(l.size for l in ob.yes_bids[:levels])
    ask_vol = sum(l.size for l in ob.yes_asks[:levels])
    total = bid_vol + ask_vol
    return (bid_vol - ask_vol) / total if total > 0 else 0.0


def _liquidity_profile(levels: list[PriceLevel]) -> LiquidityProfile:
    if not levels:
        return LiquidityProfile(
            total_volume=0, levels=0, volume_at_top=0,
            volume_top_3=0, volume_top_5=0,
            avg_price_weighted=0, concentration=0,
        )

    total = sum(l.size for l in levels)
    top_1 = levels[0].size
    top_3 = sum(l.size for l in levels[:3])
    top_5 = sum(l.size for l in levels[:5])

    if total > 0:
        avg_pw = sum(l.price * l.size for l in levels) / total
        conc = top_1 / total
    else:
        avg_pw = levels[0].price
        conc = 0

    return LiquidityProfile(
        total_volume=total,
        levels=len(levels),
        volume_at_top=top_1,
        volume_top_3=top_3,
        volume_top_5=top_5,
        avg_price_weighted=avg_pw,
        concentration=conc,
    )


def _simulate_fill(
    levels: list[PriceLevel],
    target_size: float,
    mid: float,
) -> SlippagePoint:
    """Walk price levels and simulate filling target_size contracts."""
    remaining = target_size
    total_cost = 0.0
    levels_used = 0
    worst_price = 0.0

    for level in levels:
        if remaining <= 0:
            break
        fill = min(remaining, level.size)
        total_cost += fill * level.price
        worst_price = level.price
        remaining -= fill
        levels_used += 1

    filled = target_size - remaining
    fillable = remaining <= 0

    if filled > 0:
        avg_price = total_cost / filled
        slippage = abs(avg_price - mid)
        slippage_bps = slippage / mid * 10_000 if mid > 0 else 0
    else:
        avg_price = 0
        slippage = 0
        slippage_bps = 0

    return SlippagePoint(
        size=target_size,
        avg_price=avg_price,
        worst_price=worst_price,
        slippage=slippage,
        slippage_bps=slippage_bps,
        total_cost=total_cost,
        levels_consumed=levels_used,
        fillable=fillable,
    )
