from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .common import Exchange, Market, MarketStatus, Orderbook, PriceLevel, Trade, Position, Order


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    # Kalshi timestamps are ISO 8601 strings
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def market_from_kalshi(raw: dict) -> Market:
    # Kalshi API v2 returns prices as dollar strings ("0.17") in *_dollars fields.
    # Older/fallback fields use integer cents (yes_bid, yes_ask, last_price).
    def _price(dollars_key: str, cents_key: str) -> float:
        d = raw.get(dollars_key)
        if d is not None:
            try:
                return float(d)
            except (ValueError, TypeError):
                pass
        c = raw.get(cents_key, 0) or 0
        return c / 100 if c else 0.0

    yes_bid = _price("yes_bid_dollars", "yes_bid")
    yes_ask = _price("yes_ask_dollars", "yes_ask")
    last = _price("last_price_dollars", "last_price")

    # Compute yes_price: prefer mid of best bid/ask, fall back to last
    if yes_bid and yes_ask:
        yes_price = (yes_bid + yes_ask) / 2
    elif last:
        yes_price = last
    else:
        yes_price = None

    status_map = {
        "open": MarketStatus.OPEN,
        "active": MarketStatus.OPEN,       # Kalshi API uses "active" for open markets
        "closed": MarketStatus.CLOSED,
        "settled": MarketStatus.SETTLED,
        "finalized": MarketStatus.SETTLED, # Kalshi API uses "finalized" for settled
        "halted": MarketStatus.HALTED,
    }
    status = status_map.get(raw.get("status", "").lower(), MarketStatus.CLOSED)

    return Market(
        id=raw["ticker"],
        exchange=Exchange.KALSHI,
        title=raw.get("title", ""),
        status=status,
        yes_price=yes_price,
        no_price=(1 - yes_price) if yes_price is not None else None,
        volume=raw.get("volume"),
        open_interest=raw.get("open_interest"),
        close_time=_parse_ts(raw.get("close_time")),
        event_id=raw.get("event_ticker"),
        raw=raw,
    )


def orderbook_from_kalshi(ticker: str, data: dict) -> Orderbook:
    """
    Kalshi orderbook response. Supports two API formats:
      New (orderbook_fp): yes_dollars/no_dollars — list of [price_str, size_str]
      Old (orderbook):    yes/no               — list of [price_cents_int, size]

    NO side bids are converted to YES asks: yes_ask = 1 - no_bid
    """
    # New format: prices are decimal strings e.g. ["0.17", "100140.00"]
    def parse_dollars(levels: list) -> list[PriceLevel]:
        out = []
        for level in (levels or []):
            try:
                out.append(PriceLevel(price=float(level[0]), size=float(level[1])))
            except (IndexError, ValueError, TypeError):
                continue
        return out

    # Old format: prices are integer cents e.g. [17, 1000]
    def parse_cents(levels: list) -> list[PriceLevel]:
        return [PriceLevel(price=p / 100, size=s) for p, s in (levels or [])]

    if "yes_dollars" in data or "no_dollars" in data:
        yes_bids = sorted(parse_dollars(data.get("yes_dollars", [])), key=lambda x: -x.price)
        no_levels = parse_dollars(data.get("no_dollars", []))
    else:
        yes_bids = sorted(parse_cents(data.get("yes", [])), key=lambda x: -x.price)
        no_levels = parse_cents(data.get("no", []))

    # Convert NO bids → YES asks: no bid at 0.83 = yes ask at 0.17
    yes_asks = sorted(
        [PriceLevel(price=round(1 - l.price, 4), size=l.size) for l in no_levels],
        key=lambda x: x.price,
    )

    return Orderbook(
        market_id=ticker,
        exchange=Exchange.KALSHI,
        yes_bids=yes_bids,
        yes_asks=yes_asks,
        timestamp=datetime.now(timezone.utc),
    )


def trade_from_kalshi(raw: dict) -> Trade:
    # API v2 uses yes_price_dollars (decimal string); fall back to yes_price (int cents)
    yes_d = raw.get("yes_price_dollars")
    yes_c = raw.get("yes_price", 0) or 0
    price = float(yes_d) if yes_d is not None else (yes_c / 100)

    size_fp = raw.get("count_fp")
    size_c  = raw.get("count", 0) or 0
    size = float(size_fp) if size_fp is not None else float(size_c)

    return Trade(
        id=raw.get("trade_id", ""),
        market_id=raw.get("ticker", ""),
        exchange=Exchange.KALSHI,
        price=price,
        size=size,
        side="yes" if price <= 0.5 else "no",   # low price = YES side bid
        timestamp=_parse_ts(raw.get("created_time")) or datetime.now(timezone.utc),
        taker_side=raw.get("taker_side"),
    )


def position_from_kalshi(raw: dict) -> Position:
    return Position(
        market_id=raw.get("ticker", ""),
        exchange=Exchange.KALSHI,
        yes_position=raw.get("position", 0),
        no_position=raw.get("position", 0) * -1 if raw.get("position", 0) < 0 else 0,
        market_exposure=raw.get("market_exposure"),
        realized_pnl=raw.get("realized_pnl"),
    )


def order_from_kalshi(raw: dict) -> Order:
    return Order(
        id=raw.get("order_id", ""),
        market_id=raw.get("ticker", ""),
        exchange=Exchange.KALSHI,
        side=raw.get("side", ""),
        action=raw.get("action", ""),
        price=raw.get("yes_price", raw.get("no_price", 0)) / 100,
        original_size=raw.get("original_size", 0),
        remaining_size=raw.get("remaining_count", 0),
        status=raw.get("status", ""),
        created_at=_parse_ts(raw.get("created_time")),
    )
