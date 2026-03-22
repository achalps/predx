"""
predx.tools.reward_farmer
─────────────────────────
Polymarket NCAA reward-farming bot.

Strategy:
  - BUY YES token at best bid (join queue)
  - BUY NO token at best bid (join queue on the other side)
  - On every book change: cancel + repost → always back of queue
  - When we accumulate both YES + NO → merge to recover USDC
  - Record all trades to SQLite

Usage:
    from predx.tools.reward_farmer import BotRunner, FarmerConfig, resolve_pair
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ─── Config ───────────────────────────────────────────────────────────────────

@dataclass
class FarmerConfig:
    order_size: float = 1000.0          # contracts per side (min for rewards)
    tick: float = 0.01                  # Polymarket tick size
    reprice_threshold: float = 0.005    # cancel+replace if price moves > 0.5¢
    min_price: float = 0.05             # don't quote below 5¢
    max_price: float = 0.95             # don't quote above 95¢
    merge_threshold: float = 100.0      # merge when min(yes_pos, no_pos) > this
    max_position: float = 5000.0        # max contracts per side per market
    skew_threshold: float = 1000.0      # start skewing when imbalance > this
    skew_factor: float = 0.5            # reduce size by this factor on heavy side
    pregame_size_mult: float = 1.0      # size multiplier pre-game
    live_size_mult: float = 0.5         # size multiplier during game (more volatile)
    # OBI gate
    obi_min_ratio: float = 0.5          # don't BUY if bid_depth/ask_depth < this
    # Take-profit / Stop-loss
    take_profit_pct: float = 2.0        # SELL at avg_price + this % (e.g. 2.0 = +2%)
    stop_loss_pct: float = 3.0          # dump if mid drops this % below avg (e.g. 3.0 = -3%)
    # Volatility regime filter
    vol_cooldown: float = 2.0           # seconds to wait after mid changes before quoting
    dry_run: bool = False
    testing: bool = False              # True = override size to 10, ignore rewards floor
    testing_size: float = 10.0         # size when testing=True
    db_path: str = "trades.db"


# ─── Order book ───────────────────────────────────────────────────────────────

class OrderBook:
    """Live order book from WS. Bids/asks as price → size dicts."""
    def __init__(self, token_id: str):
        self.token_id = token_id
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def apply_snapshot(self, bids: list[dict], asks: list[dict]) -> None:
        self.bids = {float(b["price"]): float(b["size"]) for b in bids if float(b.get("size", 0)) > 0}
        self.asks = {float(a["price"]): float(a["size"]) for a in asks if float(a.get("size", 0)) > 0}

    def apply_delta(self, changes: list[dict]) -> None:
        for c in changes:
            side  = c.get("side", "").upper()
            price = float(c.get("price", 0))
            size  = float(c.get("size", 0))
            book  = self.bids if side == "BUY" else self.asks
            if size == 0:
                book.pop(price, None)
            else:
                book[price] = size

    @property
    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return round((self.best_bid + self.best_ask) / 2, 4)
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return round(self.best_ask - self.best_bid, 4)
        return None

    def obi(self, depth: int = 5) -> float:
        top_bids = sorted(self.bids.keys(), reverse=True)[:depth]
        top_asks = sorted(self.asks.keys())[:depth]
        bid_vol = sum(self.bids[p] for p in top_bids)
        ask_vol = sum(self.asks[p] for p in top_asks)
        total = bid_vol + ask_vol
        return (bid_vol - ask_vol) / total if total > 0 else 0.0


# ─── Trade tape ───────────────────────────────────────────────────────────────

@dataclass
class TradeTick:
    ts: float
    market: str
    side: str
    price: float
    size: float
    obi: float = 0.0        # OBI at time of fill
    mid: float = 0.0        # mid price at time of fill
    bid_depth: float = 0.0  # $ bid depth top 5
    ask_depth: float = 0.0  # $ ask depth top 5
    ob_snapshot: str = ""   # JSON snapshot of full OB at fill time


class TradeTape:
    def __init__(self, db_path: str, maxlen: int = 500):
        self._mem: deque[TradeTick] = deque(maxlen=maxlen)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, dt TEXT, market TEXT, side TEXT, price REAL, size REAL,
                obi REAL DEFAULT 0, mid REAL DEFAULT 0,
                bid_depth REAL DEFAULT 0, ask_depth REAL DEFAULT 0
            )
        """)
        # Add columns if they don't exist (migrate old DB)
        for col, typ in [('obi','REAL'), ('mid','REAL'), ('bid_depth','REAL'), ('ask_depth','REAL'), ('ob_snapshot','TEXT')]:
            try:
                default = '0' if typ == 'REAL' else "''"
                self._db.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ} DEFAULT {default}")
            except Exception:
                pass
        self._db.commit()

    def record(self, tick: TradeTick) -> None:
        self._mem.append(tick)
        dt = datetime.fromtimestamp(tick.ts, tz=timezone.utc).isoformat()
        self._db.execute(
            "INSERT INTO trades (ts,dt,market,side,price,size,obi,mid,bid_depth,ask_depth,ob_snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tick.ts, dt, tick.market, tick.side, tick.price, tick.size,
             tick.obi, tick.mid, tick.bid_depth, tick.ask_depth, tick.ob_snapshot),
        )
        self._db.commit()

    def recent(self, n: int = 20) -> list[TradeTick]:
        return list(self._mem)[-n:]

    def close(self) -> None:
        self._db.close()


# ─── Market pair ──────────────────────────────────────────────────────────────

@dataclass
class MarketPair:
    label: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    neg_risk: bool
    tick_size: float = 0.01
    game_start_time: Optional[str] = None   # ISO timestamp
    rewards_daily_rate: float = 0.0
    rewards_min_size: float = 0.0
    rewards_max_spread: float = 0.0
    kalshi_ticker: Optional[str] = None


# ─── Per-token state ──────────────────────────────────────────────────────────

@dataclass
class RestingOrder:
    order_id: Optional[str] = None
    price: float = 0.0
    size: float = 0.0
    scoring: bool = False         # is this order earning rewards?

    def is_stale(self, target_price: float, target_size: float, threshold: float = 0.005) -> bool:
        if self.order_id is None:
            return True
        return abs(self.price - target_price) > threshold


@dataclass
class TokenState:
    """State for one token (YES or NO) within a market pair."""
    token_id: str
    token_label: str          # e.g. "YES:Miami" or "NO:Purdue"
    ob: OrderBook
    bid: RestingOrder = field(default_factory=RestingOrder)
    ask: RestingOrder = field(default_factory=RestingOrder)   # TP sell order
    position: float = 0.0
    cost_basis: float = 0.0
    fill_count: int = 0
    requote_count: int = 0
    # Volatility tracking
    last_mid: Optional[float] = None
    last_mid_change_ts: float = 0.0   # timestamp of last mid price change
    vol_block_count: int = 0          # how many times volatility filter blocked

    @property
    def avg_price(self) -> float:
        return self.cost_basis / self.position if self.position > 0 else 0.0


@dataclass
class MarketState:
    """Combined state for both tokens in a market."""
    pair: MarketPair
    yes: TokenState
    no: TokenState
    tape: TradeTape
    merge_total: float = 0.0   # total contracts merged so far

    @property
    def mergeable(self) -> float:
        return min(self.yes.position, self.no.position)


# ─── Formatting helpers ──────────────────────────────────────────────────────

def _fmt_num(n: float) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return f"{n:.0f}"


def _fmt_queue(ob_side: dict, price: float) -> str:
    shares = ob_side.get(price, 0)
    dollars = shares * price
    return f"{_fmt_num(shares)} (${_fmt_num(dollars)})"


# ─── Order management ─────────────────────────────────────────────────────────

def _cancel(pm, order_id: str, dry_run: bool, label: str) -> None:
    if dry_run or not order_id:
        return
    try:
        pm.cancel_order(order_id)
    except Exception as e:
        print(f"  [{label}] cancel failed: {e}")


def _check_scoring(pm, order_id: str) -> Optional[bool]:
    """Check if a resting order is earning rewards. Returns True/False/None on error."""
    if not order_id or order_id.startswith("dry-"):
        return None
    try:
        clob = pm._auth.clob_client
        # Build L2 auth headers
        from py_clob_client.headers.headers import create_level_2_headers
        from py_clob_client.signer import Signer
        headers = create_level_2_headers(
            Signer(clob.key, clob.chain_id),
            clob.creds,
            {"method": "GET", "request_path": f"/order-scoring?order_id={order_id}"},
        )
        import httpx
        resp = httpx.get(
            "https://clob.polymarket.com/order-scoring",
            params={"order_id": order_id},
            headers=headers,
            timeout=5,
        )
        if resp.status_code == 200:
            return resp.json().get("scoring", False)
    except Exception:
        pass
    return None


def _place_buy(pm, token_id: str, price: float, size: float,
               neg_risk: bool, dry_run: bool, label: str) -> Optional[str]:
    if dry_run:
        fake_id = f"dry-bid-{int(time.time()*1000)}"
        print(f"  [DRY] {label} BUY {size:.0f} @ {price:.3f}")
        return fake_id
    try:
        resp = pm.place_order(
            token_id=token_id,
            side="BUY",
            price=price,
            size=size,
            order_type="GTC",
            neg_risk=neg_risk,
        )
        oid = resp.get("orderID") or resp.get("id") or "unknown"
        print(f"  [{label}] BUY {size:.0f} @ {price:.3f} → {oid[:12]}")
        return oid
    except Exception as e:
        print(f"  [{label}] place failed: {e}")
        return None


def _place_sell(pm, token_id: str, price: float, size: float,
                neg_risk: bool, dry_run: bool, label: str) -> Optional[str]:
    if dry_run:
        fake_id = f"dry-ask-{int(time.time()*1000)}"
        print(f"  [DRY] {label} SELL {size:.0f} @ {price:.3f}")
        return fake_id
    try:
        resp = pm.place_order(
            token_id=token_id,
            side="SELL",
            price=price,
            size=size,
            order_type="GTC",
            neg_risk=neg_risk,
        )
        oid = resp.get("orderID") or resp.get("id") or "unknown"
        print(f"  [{label}] SELL {size:.0f} @ {price:.3f} → {oid[:12]}")
        return oid
    except Exception as e:
        print(f"  [{label}] sell failed: {e}")
        return None


# ─── Game state ──────────────────────────────────────────────────────────────

def get_game_state(pair: MarketPair) -> str:
    """Returns 'pregame', 'live', or 'unknown'."""
    if not pair.game_start_time:
        return "unknown"
    try:
        from datetime import datetime, timezone as tz
        start = datetime.fromisoformat(pair.game_start_time.replace("Z", "+00:00"))
        now = datetime.now(tz.utc)
        return "pregame" if now < start else "live"
    except Exception:
        return "unknown"


# ─── Size computation (cap + skew + game state) ─────────────────────────────

def compute_size(cfg: FarmerConfig, pair: MarketPair,
                 this_pos: float, other_pos: float) -> float:
    """Order size with position cap, inventory skew, and game state multiplier.
    Never goes below rewards_min_size (unless pulling entirely)."""
    # Testing mode: fixed small size, no rewards floor
    if cfg.testing:
        size = cfg.testing_size
    else:
        size = cfg.order_size
    floor = size  # respect whatever size mode we're in

    # Game state multiplier
    state = get_game_state(pair)
    if state == "live":
        size *= cfg.live_size_mult
    elif state == "pregame":
        size *= cfg.pregame_size_mult

    # Position cap — pull entirely
    if this_pos >= cfg.max_position:
        return 0.0

    # Inventory skew — pull entirely if imbalance exceeds threshold
    imbalance = this_pos - other_pos
    if imbalance > cfg.skew_threshold:
        return 0.0  # too heavy on this side, stop buying until other side catches up

    # Floor: never go below rewards minimum (unless we're pulling entirely)
    return max(size, floor)


# ─── Quote logic (BUY on each token) ─────────────────────────────────────────

def _obi_ratio(ob: OrderBook, depth: int = 5) -> float:
    """bid_depth / ask_depth near top of book. >1 = bids dominate, <1 = asks dominate."""
    top_bids = sorted(ob.bids.keys(), reverse=True)[:depth]
    top_asks = sorted(ob.asks.keys())[:depth]
    bid_vol = sum(ob.bids[p] * p for p in top_bids)   # dollar-weighted
    ask_vol = sum(ob.asks[p] * p for p in top_asks)
    return bid_vol / ask_vol if ask_vol > 0 else 999.0


def _momentum_ok(tape: 'TradeTape', token_label: str, lookback: int = 5) -> bool:
    """
    Check recent trade tape for momentum.
    Returns False if last N trades are all sells (price dropping — don't buy into it).
    """
    recent = tape.recent(lookback * 3)  # grab more to filter by market
    # Filter to trades matching this token's price range
    relevant = [t for t in recent if t.market == token_label or
                (not t.side.startswith('FILL'))]
    if len(relevant) < lookback:
        return True  # not enough data, allow
    last_n = relevant[-lookback:]
    sells = sum(1 for t in last_n if t.side == 'SELL')
    return sells < lookback  # block if ALL last N are sells


def _effective_obi_threshold(cfg: FarmerConfig, price: float) -> float:
    """
    Price-adjusted OBI threshold.
    Expensive tokens (>60¢) get tighter gate (harder to buy).
    Cheap tokens (<30¢) get looser gate (easier to buy).
    """
    base = cfg.obi_min_ratio
    if price > 0.60:
        return base * 1.5    # e.g. 0.5 → 0.75 (need 75% bid/ask ratio)
    elif price < 0.30:
        return base * 0.6    # e.g. 0.5 → 0.30 (only need 30% ratio)
    return base


def refresh_token_quote(pm, ts: TokenState, pair: MarketPair,
                        cfg: FarmerConfig, other_ts: TokenState = None,
                        tape: 'TradeTape' = None) -> None:
    """
    Manages both BUY (quoting) and SELL (TP/SL) orders for this token.

    BUY logic:
      - Join best_bid queue (back of queue via cancel+repost)
      - OBI gate: skip if bid_depth/ask_depth < threshold (price-adjusted)
      - Momentum filter: skip if last N trades are all sells
      - Position cap + skew

    SELL logic (only when holding position):
      - Take-profit: SELL at avg_price + take_profit_pct
      - Stop-loss: if mid drops stop_loss_pct below avg → dump at best_bid
    """
    ob = ts.ob
    bb = ob.best_bid
    ba = ob.best_ask
    mid = ob.mid
    label = ts.token_label

    # ── STOP-LOSS CHECK (before anything else) ──
    if ts.position > 0 and mid is not None and ts.avg_price > 0:
        sl_price = ts.avg_price * (1 - cfg.stop_loss_pct / 100)
        if mid < sl_price:
            print(f"  [STOP-LOSS] {label} mid={mid:.3f} < SL={sl_price:.3f} (avg={ts.avg_price:.3f})")
            _cancel(pm, ts.bid.order_id, cfg.dry_run, label)
            ts.bid = RestingOrder()
            _cancel(pm, ts.ask.order_id, cfg.dry_run, label)
            if bb:
                _place_sell(pm, ts.token_id, bb, ts.position,
                           pair.neg_risk, cfg.dry_run, f"{label}|SL")
            ts.position = 0.0
            ts.cost_basis = 0.0
            ts.ask = RestingOrder()
            return

    # ── TAKE-PROFIT SELL (when holding position) ──
    if ts.position > 0 and ts.avg_price > 0:
        tp_price = round(ts.avg_price * (1 + cfg.take_profit_pct / 100), 2)
        tp_price = max(tp_price, round(ts.avg_price + pair.tick_size, 2))
        if ts.ask.is_stale(tp_price, ts.position, cfg.reprice_threshold):
            _cancel(pm, ts.ask.order_id, cfg.dry_run, label)
            oid = _place_sell(pm, ts.token_id, tp_price, ts.position,
                             pair.neg_risk, cfg.dry_run, f"{label}|TP")
            ts.ask = RestingOrder(order_id=oid, price=tp_price, size=ts.position)
    elif ts.position <= 0 and ts.ask.order_id:
        _cancel(pm, ts.ask.order_id, cfg.dry_run, label)
        ts.ask = RestingOrder()

    # ── BUY QUOTE ──
    if bb is None or bb < cfg.min_price or bb > cfg.max_price:
        if ts.bid.order_id:
            _cancel(pm, ts.bid.order_id, cfg.dry_run, label)
            ts.bid = RestingOrder()
        return

    # Always cancel stale orders far from inside (prevents orphaned orders)
    if ts.bid.order_id and ts.bid.price > 0:
        ticks_away = abs(ts.bid.price - bb) / (pair.tick_size or 0.01)
        if ticks_away >= 2:
            print(f"  [STALE] {label} order @ {ts.bid.price:.3f} is {ticks_away:.0f} ticks from inside {bb:.3f} — cancelling")
            _cancel(pm, ts.bid.order_id, cfg.dry_run, label)
            ts.bid = RestingOrder()

    # Volatility regime filter: pull quotes if mid just changed, wait for cooldown
    now = time.time()
    if mid is not None:
        if ts.last_mid is not None and mid != ts.last_mid:
            ts.last_mid_change_ts = now  # mid just ticked
        ts.last_mid = mid

    time_since_move = now - ts.last_mid_change_ts if ts.last_mid_change_ts > 0 else 999
    if time_since_move < cfg.vol_cooldown:
        # Price just moved — pull quotes, wait for calm
        if ts.bid.order_id:
            _cancel(pm, ts.bid.order_id, cfg.dry_run, label)
            ts.bid = RestingOrder()
            ts.vol_block_count += 1
        return

    # OBI gate: price-adjusted threshold (tighter for expensive tokens)
    ratio = _obi_ratio(ob)
    threshold = _effective_obi_threshold(cfg, bb)
    if ratio < threshold:
        print(f"  [OBI BLOCK] {label} ratio={ratio:.2f} < {threshold:.2f} (px={bb:.2f}) — pulling buy")
        if ts.bid.order_id:
            _cancel(pm, ts.bid.order_id, cfg.dry_run, label)
            ts.bid = RestingOrder()
        return

    # Momentum filter: don't buy if last N trades are all sells
    if tape and not _momentum_ok(tape, label):
        print(f"  [MOMENTUM BLOCK] {label} — last trades all sells, skipping")
        if ts.bid.order_id:
            _cancel(pm, ts.bid.order_id, cfg.dry_run, label)
            ts.bid = RestingOrder()
        return

    # Compute size with skew + cap + game state
    other_pos = other_ts.position if other_ts else 0.0
    size = compute_size(cfg, pair, ts.position, other_pos)

    if size <= 0:
        if ts.bid.order_id:
            _cancel(pm, ts.bid.order_id, cfg.dry_run, label)
            ts.bid = RestingOrder()
        return

    if ts.bid.is_stale(bb, size, cfg.reprice_threshold):
        _cancel(pm, ts.bid.order_id, cfg.dry_run, label)
        oid = _place_buy(pm, ts.token_id, bb, size,
                         pair.neg_risk, cfg.dry_run, label)
        ts.bid = RestingOrder(order_id=oid, price=bb, size=size)
        ts.requote_count += 1

        # Check if order is scoring rewards
        scoring = _check_scoring(pm, oid)
        if scoring is True:
            ts.bid.scoring = True
        elif scoring is False:
            print(f"  [NOT SCORING] {label} BUY @ {bb:.3f} — outside reward range, repricing")
            # Try one tick closer to mid (best_ask - tick)
            ba = ob.best_ask
            if ba and bb < ba:
                better_px = round(ba - pair.tick_size, 4)
                if better_px > bb and better_px >= cfg.min_price:
                    _cancel(pm, oid, cfg.dry_run, label)
                    oid2 = _place_buy(pm, ts.token_id, better_px, size,
                                      pair.neg_risk, cfg.dry_run, f"{label}|REPRICE")
                    ts.bid = RestingOrder(order_id=oid2, price=better_px, size=size)
                    scoring2 = _check_scoring(pm, oid2)
                    if scoring2 is True:
                        ts.bid.scoring = True
                        print(f"  [SCORING ✓] {label} repriced to {better_px:.3f}")
                    elif scoring2 is False:
                        print(f"  [STILL NOT SCORING] {label} @ {better_px:.3f}")


def print_market_status(ms: MarketState, cfg: FarmerConfig) -> None:
    """Print combined status for both tokens."""
    label = ms.pair.label[:28]
    y, n = ms.yes, ms.no

    y_bb = y.ob.best_bid
    y_ba = y.ob.best_ask
    n_bb = n.ob.best_bid

    if y_bb is None or y_ba is None:
        return

    state = get_game_state(ms.pair)
    game_info = f"  {state.upper()}"

    y_ratio = _obi_ratio(y.ob)
    n_ratio = _obi_ratio(n.ob)
    y_q = _fmt_queue(y.ob.bids, y_bb) if y_bb else "—"
    n_q = _fmt_queue(n.ob.bids, n_bb) if n_bb else "—"

    y_depth = sum(sz * px for px, sz in y.ob.bids.items())
    n_depth = sum(sz * px for px, sz in n.ob.bids.items())

    recent = ms.tape.recent(1)
    last_trade = ""
    if recent:
        t = recent[-1]
        last_trade = f"  last={t.side} {t.size:.0f}@{t.price:.3f}"

    tick = ms.pair.tick_size
    reward = ms.pair.rewards_daily_rate

    # TP/SL info per token
    def _upnl(ts):
        mid = ts.ob.mid
        if ts.position > 0 and ts.avg_price > 0 and mid is not None:
            return ts.position * (mid - ts.avg_price)
        return 0.0

    def _tp_sl(ts):
        parts = []
        if ts.position > 0 and ts.avg_price > 0:
            tp = round(ts.avg_price * (1 + cfg.take_profit_pct / 100), 3)
            sl = round(ts.avg_price * (1 - cfg.stop_loss_pct / 100), 3)
            upnl = _upnl(ts)
            parts.append(f"avg={ts.avg_price:.3f} TP={tp:.3f} SL={sl:.3f} uPnL=${upnl:+.2f}")
            if ts.ask.order_id:
                parts.append(f"TP@{ts.ask.price:.3f}")
        return " ".join(parts) if parts else ""

    y_tpsl = _tp_sl(y)
    n_tpsl = _tp_sl(n)
    total_upnl = _upnl(y) + _upnl(n)

    # OBI gate status with effective threshold
    y_obi_thresh = _effective_obi_threshold(cfg, y_bb)
    n_obi_thresh = _effective_obi_threshold(cfg, n_bb) if n_bb else cfg.obi_min_ratio
    obi_gate_y = "✓" if y_ratio >= y_obi_thresh else f"✗({y_ratio:.2f}<{y_obi_thresh:.2f})"
    obi_gate_n = "✓" if n_ratio >= n_obi_thresh else f"✗({n_ratio:.2f}<{n_obi_thresh:.2f})"

    # Skew status
    imbalance = y.position - n.position
    n_imbalance = n.position - y.position
    y_skew = "BLOCKED" if imbalance > cfg.skew_threshold else "ok"
    n_skew = "BLOCKED" if n_imbalance > cfg.skew_threshold else "ok"

    # Vol filter status
    now = time.time()
    y_vol_age = now - y.last_mid_change_ts if y.last_mid_change_ts > 0 else 999
    n_vol_age = now - n.last_mid_change_ts if n.last_mid_change_ts > 0 else 999
    y_vol = "COOLDOWN" if y_vol_age < cfg.vol_cooldown else "ok"
    n_vol = "COOLDOWN" if n_vol_age < cfg.vol_cooldown else "ok"

    # Dollar queue value
    y_q_dollars = f"${y.ob.bids.get(y_bb, 0) * y_bb:,.0f}" if y_bb else "—"
    n_q_dollars = f"${n.ob.bids.get(n_bb, 0) * n_bb:,.0f}" if n_bb else "—"

    print(
        f"[{label}] {y_bb:.3f}/{y_ba:.3f}  tick={tick}{game_info}\n"
        f"  YES BUY {y.bid.size:.0f} @ {y.bid.price:.3f} (queue: {y_q} ({y_q_dollars}))  "
        f"OBI={obi_gate_y}  skew={y_skew}  vol={y_vol}  reward={'✓' if y.bid.scoring else '✗'}\n"
        f"  NO  BUY {n.bid.size:.0f} @ {n.bid.price:.3f} (queue: {n_q} ({n_q_dollars}))  "
        f"OBI={obi_gate_n}  skew={n_skew}  vol={n_vol}  reward={'✓' if n.bid.scoring else '✗'}\n"
        f"  depth: YES=${_fmt_num(y_depth)}  NO=${_fmt_num(n_depth)}  reward=${reward:.0f}/day\n"
        f"  pos: YES={y.position:+.0f}(avg={y.avg_price:.3f},cost=${y.cost_basis:.2f})"
        f"  NO={n.position:+.0f}(avg={n.avg_price:.3f},cost=${n.cost_basis:.2f})\n"
        f"  imbalance={imbalance:+.0f}  uPnL=${total_upnl:+.2f}  exposure=${abs(y.position)*y_bb + abs(n.position)*(n_bb or 0):.2f}\n"
        f"  merge={ms.merge_total:.0f}  fills={y.fill_count+n.fill_count}  "
        f"requotes={y.requote_count+n.requote_count}  vol_blocks={y.vol_block_count+n.vol_block_count}{last_trade}"
        + (f"\n  YES: {y_tpsl}" if y_tpsl else "")
        + (f"\n  NO:  {n_tpsl}" if n_tpsl else "")
    )


# ─── Merge logic ──────────────────────────────────────────────────────────────

def try_merge(pm, ms: MarketState, cfg: FarmerConfig) -> None:
    """Merge YES+NO positions to recover USDC when both sides have inventory."""
    mergeable = ms.mergeable
    if mergeable < cfg.merge_threshold:
        return

    label = ms.pair.label[:20]
    if cfg.dry_run:
        print(f"  [DRY MERGE] {label} would merge {mergeable:.0f} contracts")
        return

    try:
        client = pm._auth.clob_client
        amount = int(mergeable * 1e6)  # scale to contract units
        client.merge_positions(amount, ms.pair.condition_id, ms.pair.neg_risk)
        ms.yes.position -= mergeable
        ms.no.position -= mergeable
        ms.merge_total += mergeable
        print(f"  [MERGE] {label} merged {mergeable:.0f} contracts → USDC recovered")
    except Exception as e:
        print(f"  [MERGE ERR] {label}: {e}")


# ─── WS feed ──────────────────────────────────────────────────────────────────

async def ws_feed(
    token_states: dict[str, TokenState],   # token_id → TokenState
    queues: dict[str, asyncio.Queue],
    tape: TradeTape,
    condition_ids: set[str],
) -> None:
    from predx.ws.polymarket import PolymarketWebSocket

    token_ids = list(token_states.keys())
    ws = PolymarketWebSocket()

    async for msg in ws.subscribe(token_ids, reconnect=True):
        event = msg.get("event_type") or msg.get("type", "")

        if event == "book":
            tid = msg.get("asset_id", "")
            if tid in token_states:
                token_states[tid].ob.apply_snapshot(msg.get("bids", []), msg.get("asks", []))
                await queues[tid].put("book")

        elif event == "price_change":
            notified: set[str] = set()
            for c in msg.get("price_changes", []):
                tid = c.get("asset_id", "")
                if tid in token_states:
                    token_states[tid].ob.apply_delta([c])
                    if tid not in notified:
                        await queues[tid].put("price_change")
                        notified.add(tid)

        elif event in ("last_trade_price", "trade"):
            tid = msg.get("asset_id") or msg.get("market", "")
            if tid in token_states:
                tick = TradeTick(
                    ts=time.time(),
                    market=token_states[tid].token_label,
                    side=msg.get("side", "").upper(),
                    price=float(msg.get("price", 0)),
                    size=float(msg.get("size", 0)),
                )
                if tick.price > 0 and tick.size > 0:
                    tape.record(tick)


# ─── Quote loop (per token, event-driven) ─────────────────────────────────────

async def token_quote_loop(
    pm, ts: TokenState, other_ts: TokenState, pair: MarketPair,
    queue: asyncio.Queue, cfg: FarmerConfig,
    tape: TradeTape = None,
    print_fn=None,
) -> None:
    """Wait for book events, refresh BUY quote on this token."""
    label = ts.token_label

    # Seed from REST
    try:
        rest_ob = pm.get_orderbook(ts.token_id)
        ts.ob.bids = {lvl.price: lvl.size for lvl in rest_ob.yes_bids}
        ts.ob.asks = {lvl.price: lvl.size for lvl in rest_ob.yes_asks}
        print(f"[{label}] seeded: {ts.ob.best_bid}/{ts.ob.best_ask}")
    except Exception as e:
        print(f"[{label}] REST seed failed: {e}")

    while True:
        await queue.get()
        refresh_token_quote(pm, ts, pair, cfg, other_ts, tape)
        if print_fn:
            print_fn()


# ─── Fill monitor (per market, checks both tokens) ───────────────────────────

def _sync_positions(pm, ms: 'MarketState') -> None:
    """Sync positions for both tokens using the data API (includes avgPrice, PnL)."""
    try:
        import httpx
        funder = pm._cfg.funder or ""
        r = httpx.get(
            "https://data-api.polymarket.com/v1/market-positions",
            params={"market": ms.pair.condition_id, "user": funder, "status": "ALL"},
            timeout=5,
        )
        if r.status_code != 200:
            return
        positions = r.json()
        if not isinstance(positions, list):
            return

        for pos in positions:
            token_id = pos.get("asset_id") or pos.get("token_id", "")
            size = float(pos.get("size", 0))
            avg_price = float(pos.get("avgPrice", 0))
            realized = float(pos.get("realizedPnl", 0))

            for ts_check in [ms.yes, ms.no]:
                if ts_check.token_id == token_id or pos.get("outcome", "").lower() in ts_check.token_label.lower():
                    if abs(size - ts_check.position) > 0.5 or abs(avg_price - ts_check.avg_price) > 0.001:
                        print(f"  [SYNC] {ts_check.token_label} pos={ts_check.position:.0f}→{size:.0f}  "
                              f"avg={ts_check.avg_price:.3f}→{avg_price:.3f}")
                    ts_check.position = size
                    if avg_price > 0:
                        ts_check.avg_price_api = avg_price
                        ts_check.cost_basis = size * avg_price
                    break
    except Exception:
        # Fallback to balance check
        for ts_check in [ms.yes, ms.no]:
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams
                clob = pm._auth.clob_client
                ba = clob.get_balance_allowance(BalanceAllowanceParams(
                    asset_type='CONDITIONAL', token_id=ts_check.token_id,
                ))
                actual = float(ba.get('balance', 0)) / 1e6
                if abs(actual - ts_check.position) > 0.5:
                    print(f"  [SYNC] {ts_check.token_label} internal={ts_check.position:.0f} → actual={actual:.0f}")
                ts_check.position = actual
            except Exception:
                pass


async def fill_monitor(pm, ms: MarketState, cfg: FarmerConfig,
                       queues: dict[str, asyncio.Queue] = None) -> None:
    """Poll open orders for both tokens. Detect fills, update positions, try merge.
    On fill: notify the other side's queue to trigger immediate requote.
    Syncs positions with on-chain balances every 5 seconds."""
    last_sync = 0.0
    sync_interval = 5.0

    while True:
        await asyncio.sleep(1.0)
        if cfg.dry_run:
            continue

        # ── Position sync every 5s ──
        now = time.time()
        if now - last_sync > sync_interval:
            _sync_positions(pm, ms)
            last_sync = now

        try:
            for ts, other_ts in [(ms.yes, ms.no), (ms.no, ms.yes)]:
                live = {o["id"] for o in pm.get_open_orders(token_id=ts.token_id)}

                # ── Check BUY fill ──
                if ts.bid.order_id and not ts.bid.order_id.startswith("dry-"):
                    if ts.bid.order_id not in live:
                        sz = ts.bid.size
                        px = ts.bid.price
                        state = get_game_state(ms.pair)
                        obi = _obi_ratio(ts.ob)
                        mid = ts.ob.mid or 0.0
                        top_bids = sorted(ts.ob.bids.keys(), reverse=True)[:5]
                        top_asks = sorted(ts.ob.asks.keys())[:5]
                        bd = sum(ts.ob.bids[p] * p for p in top_bids)
                        ad = sum(ts.ob.asks[p] * p for p in top_asks)
                        # Full OB snapshot for post-trade analysis
                        import json as _json
                        ob_snap = _json.dumps({
                            "bids": {str(p): ts.ob.bids[p] for p in sorted(ts.ob.bids.keys(), reverse=True)[:10]},
                            "asks": {str(p): ts.ob.asks[p] for p in sorted(ts.ob.asks.keys())[:10]},
                        })
                        print(f"  [FILL] {ts.token_label} BUY {sz:.0f} @ {px:.3f}  "
                              f"game={state}  OBI={obi:.2f}  mid={mid:.3f}  bid$={bd:.0f}  ask$={ad:.0f}")
                        ts.position += sz
                        ts.cost_basis += sz * px
                        ts.fill_count += 1
                        ms.tape.record(TradeTick(
                            ts=time.time(), market=ts.token_label,
                            side="FILL_BUY", price=px, size=sz,
                            obi=obi, mid=mid, bid_depth=bd, ask_depth=ad,
                            ob_snapshot=ob_snap,
                        ))
                        ts.bid = RestingOrder()

                        # React: requote other side + this side (for TP placement)
                        if queues and other_ts.token_id in queues:
                            await queues[other_ts.token_id].put("fill_react")
                        if queues and ts.token_id in queues:
                            await queues[ts.token_id].put("fill_self")

                # ── Check SELL (TP) fill ──
                if ts.ask.order_id and not ts.ask.order_id.startswith("dry-"):
                    if ts.ask.order_id not in live:
                        sz = ts.ask.size
                        px = ts.ask.price
                        pnl = (px - ts.avg_price) * sz if ts.avg_price > 0 else 0
                        print(f"  [TP FILL] {ts.token_label} SELL {sz:.0f} @ {px:.3f}  pnl=${pnl:.2f}")
                        ts.position = max(0, ts.position - sz)
                        ts.cost_basis = ts.position * ts.avg_price  # recalc
                        ts.fill_count += 1
                        obi = _obi_ratio(ts.ob)
                        mid = ts.ob.mid or 0.0
                        ms.tape.record(TradeTick(
                            ts=time.time(), market=ts.token_label,
                            side="FILL_SELL_TP", price=px, size=sz,
                            obi=obi, mid=mid,
                        ))
                        ts.ask = RestingOrder()

            # Try merge if both sides have inventory
            try_merge(pm, ms, cfg)

        except Exception as e:
            print(f"[{ms.pair.label[:20]}] fill_monitor err: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def run(pairs: list[MarketPair], cfg: FarmerConfig,
              stop_event: Optional[asyncio.Event] = None,
              shared_states: Optional[dict] = None) -> dict[str, MarketState]:
    from predx import PolymarketClient
    pm = PolymarketClient()

    tape = TradeTape(cfg.db_path)

    market_states: dict[str, MarketState] = {}  # condition_id → MarketState
    token_states:  dict[str, TokenState] = {}   # token_id → TokenState
    queues:        dict[str, asyncio.Queue] = {}
    condition_ids: set[str] = set()

    for pair in pairs:
        yes_ob = OrderBook(pair.yes_token_id)
        no_ob  = OrderBook(pair.no_token_id)

        # Get outcome names from pair label
        parts = pair.label.split(" vs ")
        yes_label = f"YES:{parts[0][:15]}" if parts else "YES"
        no_label  = f"NO:{parts[1][:15]}" if len(parts) > 1 else "NO"

        yes_ts = TokenState(token_id=pair.yes_token_id, token_label=yes_label, ob=yes_ob)
        no_ts  = TokenState(token_id=pair.no_token_id,  token_label=no_label,  ob=no_ob)

        ms = MarketState(pair=pair, yes=yes_ts, no=no_ts, tape=tape)
        market_states[pair.condition_id] = ms

        token_states[pair.yes_token_id] = yes_ts
        token_states[pair.no_token_id]  = no_ts
        queues[pair.yes_token_id] = asyncio.Queue()
        queues[pair.no_token_id]  = asyncio.Queue()
        condition_ids.add(pair.condition_id)

    if shared_states is not None:
        shared_states.update(market_states)

    print(f"\n{'─'*60}")
    print(f"Reward farmer | {len(pairs)} market(s) | dry_run={cfg.dry_run}")
    print(f"Size: {cfg.order_size:.0f}  MaxPos: {cfg.max_position:.0f}  Skew@: {cfg.skew_threshold:.0f}")
    print(f"Pre-game mult: {cfg.pregame_size_mult}x  Live mult: {cfg.live_size_mult}x")
    print(f"Strategy: BUY YES + BUY NO → merge when both sides fill")
    print(f"DB: {cfg.db_path}")
    print(f"{'─'*60}\n")

    tasks = []

    # Single WS feed for all tokens
    tasks.append(asyncio.create_task(
        ws_feed(token_states, queues, tape, condition_ids)
    ))

    # Per-market: quote loops for YES and NO tokens + fill monitor
    for ms in market_states.values():
        # Throttled print: only print status on YES token updates (avoid double-printing)
        def make_print_fn(ms_ref=ms):
            return lambda: print_market_status(ms_ref, cfg)

        tasks.append(asyncio.create_task(
            token_quote_loop(pm, ms.yes, ms.no, ms.pair, queues[ms.yes.token_id], cfg, tape, make_print_fn())
        ))
        tasks.append(asyncio.create_task(
            token_quote_loop(pm, ms.no, ms.yes, ms.pair, queues[ms.no.token_id], cfg, tape)
        ))
        tasks.append(asyncio.create_task(
            fill_monitor(pm, ms, cfg, queues)
        ))

    if stop_event:
        async def _wait_stop():
            while not stop_event.is_set():
                await asyncio.sleep(0.5)
            for t in tasks:
                t.cancel()
        tasks.append(asyncio.create_task(_wait_stop()))

    try:
        await asyncio.gather(*tasks)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        print("\nShutting down — cancelling all open orders...")
        if not cfg.dry_run:
            try:
                pm.cancel_all()
                print("Done.")
            except Exception as e:
                print(f"cancel_all: {e}")
        tape.close()
        pm.close()

    return market_states


# ─── Background thread runner (for notebook) ─────────────────────────────────

class BotRunner:
    """
    Run the bot in a background thread so the notebook kernel stays free.

    Usage:
        runner = BotRunner(pairs, cfg)
        runner.start()
        runner.status()                  # check live state
        runner.cfg.order_size = 500      # change size mid-run
        runner.stop()
    """
    def __init__(self, pairs: list[MarketPair], cfg: FarmerConfig):
        self.pairs = pairs
        self.cfg = cfg
        self._stop = None
        self._thread = None
        self._loop = None
        self.states: dict[str, MarketState] = {}

    def start(self) -> None:
        import threading
        if self._thread and self._thread.is_alive():
            print("Bot already running. Call .stop() first.")
            return
        self._thread = threading.Thread(target=self._run_in_thread, daemon=True)
        self._thread.start()
        print("Bot started in background thread.")

    def _run_in_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop = asyncio.Event()
        try:
            self._loop.run_until_complete(
                run(self.pairs, self.cfg, stop_event=self._stop,
                    shared_states=self.states)
            )
        except Exception as e:
            print(f"Bot crashed: {e}")
        finally:
            self._loop.close()

    def stop(self) -> None:
        if self._stop:
            self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        print("Bot stopped.")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> None:
        if not self.states:
            print("No state yet.")
            return
        for ms in self.states.values():
            print_market_status(ms, self.cfg)


# ─── CLI entrypoint ──────────────────────────────────────────────────────────

def resolve_pair(pm, condition_id: str, kalshi_ticker: Optional[str] = None,
                  market_data: Optional[dict] = None) -> MarketPair:
    """
    Resolve a condition_id into a MarketPair with token IDs + metadata.
    If market_data is provided (from get_sampling_markets), use it directly
    to avoid an extra API call.
    """
    if market_data:
        tokens = market_data.get("tokens", [])
        rewards = market_data.get("rewards") or {}
        rate = 0
        for ri in rewards.get("rates", []) or []:
            if "2791bca1f2de4661" in ri.get("asset_address", "").lower():
                rate = ri.get("rewards_daily_rate", 0)
        return MarketPair(
            label=market_data.get("question", ""),
            condition_id=condition_id,
            yes_token_id=tokens[0]["token_id"] if len(tokens) > 0 else "",
            no_token_id=tokens[1]["token_id"] if len(tokens) > 1 else "",
            neg_risk=bool(market_data.get("neg_risk", False)),
            tick_size=float(market_data.get("minimum_tick_size", 0.01)),
            game_start_time=market_data.get("game_start_time"),
            rewards_daily_rate=rate,
            rewards_min_size=float(rewards.get("min_size", 0)),
            rewards_max_spread=float(rewards.get("max_spread", 0)),
            kalshi_ticker=kalshi_ticker,
        )

    raw = pm.get_raw_market(condition_id)
    tokens = raw.raw.get("tokens", [])
    if len(tokens) < 2:
        raise ValueError(f"Expected 2 tokens, got {len(tokens)}")
    return MarketPair(
        label=raw.question,
        condition_id=condition_id,
        yes_token_id=tokens[0]["token_id"],
        no_token_id=tokens[1]["token_id"],
        neg_risk=bool(raw.raw.get("negRisk", False)),
        kalshi_ticker=kalshi_ticker,
    )


def main() -> None:
    from predx.config import load_dotenv_if_present
    load_dotenv_if_present()

    cfg = FarmerConfig(
        order_size=1000.0,
        dry_run=True,
        db_path="trades.db",
    )

    MARKETS = [
        "0x320a8227bf171846c24d01a6640e638da65f0e5e9a2c9f2346b5c248b5eb4ea5",  # Iowa vs Florida
    ]

    from predx import PolymarketClient
    pm = PolymarketClient()
    pairs = []
    for cid in MARKETS:
        try:
            pair = resolve_pair(pm, cid, None)
            print(f"  ✓ {pair.label}")
            pairs.append(pair)
        except Exception as e:
            print(f"  ✗ {cid}: {e}")
            sys.exit(1)
    pm.close()

    if not pairs:
        print("No markets.")
        sys.exit(1)

    try:
        asyncio.run(run(pairs, cfg))
    except KeyboardInterrupt:
        print("\nDone.")


if __name__ == "__main__":
    main()
