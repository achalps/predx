"""Market discovery and screening for Polymarket.

Provides filtering, sorting, and enrichment on top of the Gamma API.
No API keys needed — all data is public.

Usage:
    from predx.analytics import MarketScanner

    scanner = MarketScanner()
    markets = scanner.scan(min_volume_24h=10_000, min_liquidity=50_000)
    for m in markets:
        print(f"{m.question}  spread={m.spread}  vol24h=${m.volume_24h:,.0f}")

    # Reward-eligible markets only
    rewarded = scanner.scan(rewards_only=True)

    # With orderbook enrichment (slower — one API call per market)
    enriched = scanner.enrich(markets[:5])
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from ..config import PolymarketConfig
from ..models.polymarket import PolymarketMarket


@dataclass
class MarketSnapshot:
    """A point-in-time view of a Polymarket market with screening-relevant fields."""

    # Identity
    condition_id: str
    question: str
    slug: str
    category: Optional[str]
    event_slug: Optional[str]

    # Pricing
    yes_price: float
    no_price: float
    best_bid: float
    best_ask: float
    spread: float
    last_trade_price: float

    # Price changes
    price_change_1h: float
    price_change_1d: float
    price_change_1w: float

    # Volume & liquidity
    volume_total: float
    volume_24h: float
    volume_1w: float
    volume_1m: float
    liquidity: float
    open_interest: float

    # Timing
    end_date: Optional[datetime]
    created_at: Optional[datetime]

    # Rewards
    rewards_min_size: float
    rewards_max_spread: float
    competitive: float

    # Flags
    neg_risk: bool
    active: bool

    # Enrichment (filled by enrich())
    orderbook_depth: Optional[dict] = None  # {bid_volume, ask_volume, imbalance}

    # Raw Gamma API response
    raw: dict = field(default=None, repr=False)

    @property
    def has_rewards(self) -> bool:
        return self.rewards_max_spread > 0 and self.rewards_min_size > 0

    @property
    def midpoint(self) -> float:
        return (self.best_bid + self.best_ask) / 2 if self.best_ask > 0 else self.yes_price

    @property
    def hours_to_expiry(self) -> Optional[float]:
        if self.end_date is None:
            return None
        delta = self.end_date - datetime.now(timezone.utc)
        return max(delta.total_seconds() / 3600, 0)

    @property
    def spread_bps(self) -> float:
        """Spread in basis points relative to midpoint."""
        mid = self.midpoint
        return (self.spread / mid * 10_000) if mid > 0 else 0

    @classmethod
    def from_gamma(cls, data: dict) -> MarketSnapshot:
        """Build from a raw Gamma API market dict."""
        import json

        # Parse outcome prices
        try:
            prices = json.loads(data.get("outcomePrices", "[0.5, 0.5]"))
        except (json.JSONDecodeError, TypeError):
            prices = [0.5, 0.5]
        yes_price = float(prices[0]) if prices else 0.5
        no_price = float(prices[1]) if len(prices) > 1 else 1 - yes_price

        # Parse timestamps
        end_date = None
        if data.get("endDateIso"):
            try:
                raw_end = data["endDateIso"]
                if "T" not in str(raw_end):
                    raw_end = str(raw_end) + "T00:00:00Z"
                end_date = datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        created_at = None
        if data.get("createdAt"):
            try:
                created_at = datetime.fromisoformat(
                    str(data["createdAt"]).replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        # Event-level fields
        events = data.get("events", [])
        event = events[0] if events else {}
        event_slug = event.get("slug")
        oi = float(event.get("openInterest", 0) or 0)
        neg_risk = bool(event.get("enableNegRisk", False))

        return cls(
            condition_id=data.get("conditionId", data.get("condition_id", "")),
            question=data.get("question", ""),
            slug=data.get("slug", ""),
            category=data.get("category") or event.get("category"),
            event_slug=event_slug,
            yes_price=yes_price,
            no_price=no_price,
            best_bid=float(data.get("bestBid", 0) or 0),
            best_ask=float(data.get("bestAsk", 0) or 0),
            spread=float(data.get("spread", 0) or 0),
            last_trade_price=float(data.get("lastTradePrice", 0) or 0),
            price_change_1h=float(data.get("oneHourPriceChange", 0) or 0),
            price_change_1d=float(data.get("oneDayPriceChange", 0) or 0),
            price_change_1w=float(data.get("oneWeekPriceChange", 0) or 0),
            volume_total=float(data.get("volumeNum", 0) or 0),
            volume_24h=float(data.get("volume24hr", 0) or 0),
            volume_1w=float(data.get("volume1wk", 0) or 0),
            volume_1m=float(data.get("volume1mo", 0) or 0),
            liquidity=float(data.get("liquidityNum", 0) or 0),
            open_interest=oi,
            end_date=end_date,
            created_at=created_at,
            rewards_min_size=float(data.get("rewardsMinSize", 0) or 0),
            rewards_max_spread=float(data.get("rewardsMaxSpread", 0) or 0),
            competitive=float(data.get("competitive", 0) or 0),
            neg_risk=neg_risk,
            active=bool(data.get("active", False) and not data.get("closed", False)),
            raw=data,
        )


@dataclass
class ScanFilters:
    """Filters for market scanning. All thresholds are inclusive (>=)."""

    min_volume_24h: float = 0
    min_volume_1w: float = 0
    min_liquidity: float = 0
    min_open_interest: float = 0
    max_spread: Optional[float] = None
    min_best_bid: float = 0          # filter out empty books
    max_hours_to_expiry: Optional[float] = None
    min_hours_to_expiry: Optional[float] = None
    category: Optional[str] = None
    rewards_only: bool = False
    neg_risk: Optional[bool] = None  # None=any, True=only negRisk, False=exclude

    def matches(self, m: MarketSnapshot) -> bool:
        if m.volume_24h < self.min_volume_24h:
            return False
        if m.volume_1w < self.min_volume_1w:
            return False
        if m.liquidity < self.min_liquidity:
            return False
        if m.open_interest < self.min_open_interest:
            return False
        if self.max_spread is not None and m.spread > self.max_spread:
            return False
        if m.best_bid < self.min_best_bid:
            return False
        if self.category and m.category != self.category:
            return False
        if self.rewards_only and not m.has_rewards:
            return False
        if self.neg_risk is not None and m.neg_risk != self.neg_risk:
            return False

        hours = m.hours_to_expiry
        if hours is not None:
            if self.max_hours_to_expiry is not None and hours > self.max_hours_to_expiry:
                return False
            if self.min_hours_to_expiry is not None and hours < self.min_hours_to_expiry:
                return False

        return True


class MarketScanner:
    """Scan and filter Polymarket markets.

    Args:
        config: Optional PolymarketConfig (no auth needed for scanning).
    """

    def __init__(self, config: Optional[PolymarketConfig] = None):
        cfg = config or PolymarketConfig()
        self._gamma = httpx.Client(base_url=cfg.gamma_url, timeout=cfg.timeout)
        self._clob = httpx.Client(base_url=cfg.clob_url, timeout=cfg.timeout)

    def scan(
        self,
        *,
        min_volume_24h: float = 0,
        min_volume_1w: float = 0,
        min_liquidity: float = 0,
        min_open_interest: float = 0,
        max_spread: Optional[float] = None,
        min_best_bid: float = 0,
        max_hours_to_expiry: Optional[float] = None,
        min_hours_to_expiry: Optional[float] = None,
        category: Optional[str] = None,
        rewards_only: bool = False,
        neg_risk: Optional[bool] = None,
        sort_by: str = "volume_24h",
        limit: int = 100,
        max_pages: int = 5,
    ) -> list[MarketSnapshot]:
        """Scan Polymarket for active markets matching filters.

        Args:
            sort_by: Field to sort results by (descending). Options:
                volume_24h, volume_1w, volume_total, liquidity,
                spread, competitive, open_interest
            limit: Max results to return.
            max_pages: Max Gamma API pages to fetch (100 markets each).

        Returns:
            Sorted list of MarketSnapshot objects.
        """
        filters = ScanFilters(
            min_volume_24h=min_volume_24h,
            min_volume_1w=min_volume_1w,
            min_liquidity=min_liquidity,
            min_open_interest=min_open_interest,
            max_spread=max_spread,
            min_best_bid=min_best_bid,
            max_hours_to_expiry=max_hours_to_expiry,
            min_hours_to_expiry=min_hours_to_expiry,
            category=category,
            rewards_only=rewards_only,
            neg_risk=neg_risk,
        )

        results: list[MarketSnapshot] = []
        offset = 0
        page_size = 100

        for _ in range(max_pages):
            raw_markets = self._fetch_page(offset, page_size)
            if not raw_markets:
                break

            for raw in raw_markets:
                snap = MarketSnapshot.from_gamma(raw)
                if snap.active and filters.matches(snap):
                    results.append(snap)

            offset += page_size
            if len(raw_markets) < page_size:
                break

        # Sort descending by chosen field
        sort_key = _sort_key(sort_by)
        results.sort(key=sort_key, reverse=True)

        return results[:limit]

    def enrich(self, markets: list[MarketSnapshot]) -> list[MarketSnapshot]:
        """Add orderbook depth data to each market (one API call per market).

        Modifies markets in-place and returns them.
        """
        for m in markets:
            try:
                pm_market = self._get_raw_market(m.condition_id)
                token_id = pm_market.yes_token_id()
                if not token_id:
                    continue
                resp = self._clob.get("/book", params={"token_id": token_id})
                resp.raise_for_status()
                book = resp.json()
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                bid_vol = sum(float(l["size"]) for l in bids[:5])
                ask_vol = sum(float(l["size"]) for l in asks[:5])
                total = bid_vol + ask_vol
                m.orderbook_depth = {
                    "bid_volume": bid_vol,
                    "ask_volume": ask_vol,
                    "imbalance": (bid_vol - ask_vol) / total if total > 0 else 0,
                }
            except Exception:
                continue
        return markets

    def _fetch_page(self, offset: int, limit: int) -> list[dict]:
        resp = self._gamma.get(
            "/markets",
            params={
                "limit": limit,
                "offset": offset,
                "active": "true",
                "closed": "false",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("data", [])

    def _get_raw_market(self, condition_id: str) -> PolymarketMarket:
        resp = self._clob.get(f"/markets/{condition_id}")
        resp.raise_for_status()
        return PolymarketMarket.from_clob(resp.json())

    def close(self):
        self._gamma.close()
        self._clob.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _sort_key(sort_by: str):
    """Return a sort key function for MarketSnapshot."""
    field_map = {
        "volume_24h": lambda m: m.volume_24h,
        "volume_1w": lambda m: m.volume_1w,
        "volume_total": lambda m: m.volume_total,
        "liquidity": lambda m: m.liquidity,
        "spread": lambda m: -m.spread,  # lower spread = better, so negate for desc
        "competitive": lambda m: m.competitive,
        "open_interest": lambda m: m.open_interest,
    }
    return field_map.get(sort_by, field_map["volume_24h"])
