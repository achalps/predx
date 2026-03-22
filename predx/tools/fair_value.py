"""Fair value computation for cross-venue market making."""
from __future__ import annotations
from typing import Optional

from ..models.common import Orderbook


def compute(
    kalshi_ob: Optional[Orderbook],
    poly_ob: Optional[Orderbook],
) -> dict:
    """
    Compute fair value metrics from two venue orderbooks.

    Returns dict with keys:
        k_mid, p_mid        — per-venue midpoints
        blend               — size-weighted mid blend
        k_micro, p_micro    — per-venue microprice
        micro               — averaged microprice
        cross_spread        — cross-venue arbitrage (>0 = arb exists)
        k_obi, p_obi        — per-venue order book imbalance [-1, 1]
        obi                 — combined OBI
    """
    out: dict = {}

    k_mid = kalshi_ob.mid if kalshi_ob else None
    p_mid = poly_ob.mid if poly_ob else None
    out["k_mid"] = k_mid
    out["p_mid"] = p_mid

    # --- Mid blend (size-weighted) ---
    k_top = _top_size(kalshi_ob)
    p_top = _top_size(poly_ob)
    if k_mid is not None and p_mid is not None:
        total = k_top + p_top
        out["blend"] = (k_mid * k_top + p_mid * p_top) / total if total else (k_mid + p_mid) / 2
    elif k_mid is not None:
        out["blend"] = k_mid
    elif p_mid is not None:
        out["blend"] = p_mid
    else:
        out["blend"] = None

    # --- Microprice per venue ---
    out["k_micro"] = _microprice(kalshi_ob)
    out["p_micro"] = _microprice(poly_ob)
    micros = [m for m in (out["k_micro"], out["p_micro"]) if m is not None]
    out["micro"] = sum(micros) / len(micros) if micros else None

    # --- Cross-venue spread (arb detection) ---
    k_bid = kalshi_ob.best_bid if kalshi_ob else None
    k_ask = kalshi_ob.best_ask if kalshi_ob else None
    p_bid = poly_ob.best_bid if poly_ob else None
    p_ask = poly_ob.best_ask if poly_ob else None
    cross = 0.0
    if k_bid is not None and p_ask is not None:
        cross = max(cross, k_bid - p_ask)
    if p_bid is not None and k_ask is not None:
        cross = max(cross, p_bid - k_ask)
    out["cross_spread"] = cross

    # --- OBI ---
    out["k_obi"] = _obi(kalshi_ob)
    out["p_obi"] = _obi(poly_ob)
    obis = [o for o in (out["k_obi"], out["p_obi"]) if o is not None]
    out["obi"] = sum(obis) / len(obis) if obis else None

    return out


def _top_size(ob: Optional[Orderbook]) -> float:
    if not ob:
        return 0.0
    bid_sz = ob.yes_bids[0].size if ob.yes_bids else 0.0
    ask_sz = ob.yes_asks[0].size if ob.yes_asks else 0.0
    return bid_sz + ask_sz


def _microprice(ob: Optional[Orderbook]) -> Optional[float]:
    if not ob or not ob.yes_bids or not ob.yes_asks:
        return None
    bb, ba = ob.yes_bids[0], ob.yes_asks[0]
    total = bb.size + ba.size
    if total == 0:
        return ob.mid
    return (bb.price * ba.size + ba.price * bb.size) / total


def _obi(ob: Optional[Orderbook]) -> Optional[float]:
    if not ob:
        return None
    d = ob.depth(5)
    return d["imbalance"]
