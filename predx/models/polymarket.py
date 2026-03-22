from __future__ import annotations
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .common import Exchange, Market, MarketStatus, Orderbook, PriceLevel, Trade


def _parse_ts(ts) -> Optional[datetime]:
    if not ts:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


@dataclass
class PolymarketOutcome:
    clob_token_id: str
    outcome: str   # "Yes" / "No"
    price: float   # 0.0–1.0


@dataclass
class PolymarketMarket:
    """
    Rich Polymarket market object preserving all Gamma API fields.
    Use .to_common() for normalized cross-exchange access.
    Use .yes_token_id() to get the CLOB token ID for orderbook/trading.
    """
    condition_id: str
    question: str
    outcomes: list[PolymarketOutcome]
    end_date: Optional[datetime]
    volume: float
    liquidity: float
    active: bool
    raw: dict = field(repr=False)

    @classmethod
    def from_clob(cls, data: dict) -> "PolymarketMarket":
        """Build from CLOB API /markets/{condition_id} response."""
        tokens = data.get("tokens", [])
        outcomes = [
            PolymarketOutcome(
                clob_token_id=t.get("token_id", ""),
                outcome=t.get("outcome", "Yes" if i == 0 else "No"),
                price=float(t.get("price", 0.5)),
            )
            for i, t in enumerate(tokens)
        ]
        return cls(
            condition_id=data.get("condition_id", ""),
            question=data.get("question", data.get("market_slug", "")),
            outcomes=outcomes,
            end_date=_parse_ts(data.get("end_date_iso")),
            volume=float(data.get("volume", 0) or 0),
            liquidity=float(data.get("liquidity", 0) or 0),
            active=bool(data.get("active", True) and not data.get("closed", False)),
            raw=data,
        )

    @classmethod
    def from_gamma(cls, data: dict) -> "PolymarketMarket":
        # clobTokenIds is a JSON-encoded string embedded inside the market object
        try:
            token_ids = json.loads(data.get("clobTokenIds", "[]"))
        except (json.JSONDecodeError, TypeError):
            token_ids = []

        try:
            outcome_names = json.loads(data.get("outcomes", '["Yes", "No"]'))
        except (json.JSONDecodeError, TypeError):
            outcome_names = data.get("outcomes", ["Yes", "No"])

        try:
            outcome_prices = json.loads(data.get("outcomePrices", "[0.5, 0.5]"))
        except (json.JSONDecodeError, TypeError):
            outcome_prices = [0.5, 0.5]

        outcomes = [
            PolymarketOutcome(
                clob_token_id=tid,
                outcome=name,
                price=float(price),
            )
            for tid, name, price in zip(token_ids, outcome_names, outcome_prices)
        ]

        return cls(
            condition_id=data.get("conditionId", data.get("condition_id", "")),
            question=data.get("question", data.get("title", "")),
            outcomes=outcomes,
            end_date=_parse_ts(data.get("endDate", data.get("end_date_iso"))),
            volume=float(data.get("volume", 0) or 0),
            liquidity=float(data.get("liquidity", 0) or 0),
            active=bool(data.get("active", False)),
            raw=data,
        )

    def yes_token_id(self) -> Optional[str]:
        for o in self.outcomes:
            if o.outcome.lower() == "yes":
                return o.clob_token_id
        return self.outcomes[0].clob_token_id if self.outcomes else None

    def no_token_id(self) -> Optional[str]:
        for o in self.outcomes:
            if o.outcome.lower() == "no":
                return o.clob_token_id
        return self.outcomes[1].clob_token_id if len(self.outcomes) > 1 else None

    def to_common(self) -> Market:
        yes = next((o for o in self.outcomes if o.outcome.lower() == "yes"), None)
        no = next((o for o in self.outcomes if o.outcome.lower() == "no"), None)
        return Market(
            id=self.condition_id,
            exchange=Exchange.POLYMARKET,
            title=self.question,
            status=MarketStatus.OPEN if self.active else MarketStatus.CLOSED,
            yes_price=yes.price if yes else None,
            no_price=no.price if no else None,
            volume=self.volume,
            open_interest=self.liquidity,
            close_time=self.end_date,
            raw=self.raw,
        )


def orderbook_from_clob(token_id: str, data: dict) -> Orderbook:
    def parse(levels: list) -> list[PriceLevel]:
        return [PriceLevel(price=float(l["price"]), size=float(l["size"])) for l in (levels or [])]

    bids = sorted(parse(data.get("bids", [])), key=lambda x: -x.price)
    asks = sorted(parse(data.get("asks", [])), key=lambda x: x.price)

    return Orderbook(
        market_id=token_id,
        exchange=Exchange.POLYMARKET,
        yes_bids=bids,
        yes_asks=asks,
        timestamp=datetime.now(timezone.utc),
    )


def trade_from_polymarket(raw: dict) -> Trade:
    return Trade(
        id=raw.get("id", ""),
        market_id=raw.get("market", raw.get("condition_id", "")),
        exchange=Exchange.POLYMARKET,
        price=float(raw.get("price", 0)),
        size=float(raw.get("size", raw.get("amount", 0))),
        side="yes" if raw.get("outcome", "").lower() == "yes" else "no",
        timestamp=_parse_ts(raw.get("timestamp", raw.get("created_at"))),
        taker_side=raw.get("side"),
    )
