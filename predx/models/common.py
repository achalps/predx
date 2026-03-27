from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Exchange(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class MarketStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    SETTLED = "settled"
    HALTED = "halted"


@dataclass
class PriceLevel:
    price: float   # always normalized 0.0–1.0
    size: float    # number of contracts or shares


@dataclass
class Orderbook:
    market_id: str
    exchange: Exchange
    yes_bids: list[PriceLevel]  # sorted descending by price (highest first)
    yes_asks: list[PriceLevel]  # sorted ascending by price (lowest first)
    timestamp: datetime

    @property
    def best_bid(self) -> Optional[float]:
        return self.yes_bids[0].price if self.yes_bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.yes_asks[0].price if self.yes_asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    def depth(self, levels: int = 5) -> dict:
        """Aggregate volume at top N levels on each side."""
        bid_vol = sum(l.size for l in self.yes_bids[:levels])
        ask_vol = sum(l.size for l in self.yes_asks[:levels])
        return {"bid_volume": bid_vol, "ask_volume": ask_vol, "imbalance": (bid_vol - ask_vol) / max(bid_vol + ask_vol, 1)}


@dataclass
class Market:
    id: str                          # exchange-native ticker or condition_id
    exchange: Exchange
    title: str
    status: MarketStatus
    yes_price: Optional[float]       # normalized 0.0–1.0
    no_price: Optional[float]        # normalized 0.0–1.0
    volume: Optional[float]
    open_interest: Optional[float]
    close_time: Optional[datetime]
    category: Optional[str] = None
    event_id: Optional[str] = None
    raw: Optional[dict] = field(default=None, repr=False)


@dataclass
class Trade:
    id: str
    market_id: str
    exchange: Exchange
    price: float           # normalized 0.0–1.0
    size: float
    side: str              # "yes" or "no"
    timestamp: datetime
    taker_side: Optional[str] = None


@dataclass
class Position:
    market_id: str
    exchange: Exchange
    yes_position: float    # positive = long yes
    no_position: float
    market_exposure: Optional[float] = None
    realized_pnl: Optional[float] = None


@dataclass
class Order:
    id: str
    market_id: str
    exchange: Exchange
    side: str
    action: str            # "buy" or "sell"
    price: float           # normalized 0.0–1.0
    original_size: float
    remaining_size: float
    status: str
    created_at: Optional[datetime] = None
