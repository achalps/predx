from __future__ import annotations
from typing import Generator, Optional
import httpx

from ..config import PolymarketConfig
from ..models.common import Market, Orderbook, Trade
from ..models.polymarket import PolymarketMarket, orderbook_from_clob, trade_from_polymarket
from ..utils.pagination import offset_paginate


class PolymarketClient:
    """
    Polymarket client spanning three APIs:
      - Gamma API: market/event discovery, metadata, prices
      - CLOB API:  orderbook, price history, order placement
      - Data API:  trade history

    No auth required for read-only access. Set POLYMARKET_PRIVATE_KEY env var
    (or config.private_key) to enable order placement.

    Usage:
        with PolymarketClient() as pm:
            for market in pm.get_markets(active=True, max_items=50):
                print(market.title, market.yes_price)

            # Get orderbook (need token_id from PolymarketMarket)
            raw = pm.get_raw_market("some-condition-id")
            ob = pm.get_orderbook(raw.yes_token_id())
            print(ob.best_bid, ob.best_ask)
    """

    def __init__(self, config: Optional[PolymarketConfig] = None):
        cfg = config or PolymarketConfig()
        self._cfg = cfg
        self._gamma = httpx.Client(base_url=cfg.gamma_url, timeout=cfg.timeout)
        self._clob = httpx.Client(base_url=cfg.clob_url, timeout=cfg.timeout)
        self._data = httpx.Client(base_url=cfg.data_url, timeout=cfg.timeout)
        self._auth = None
        if cfg.private_key:
            from ..auth.polymarket import PolymarketAuth
            self._auth = PolymarketAuth(
                cfg.private_key, cfg.clob_url, cfg.chain_id,
                signature_type=cfg.signature_type,
                funder=cfg.funder,
            )

    # -------------------------------------------------------------------------
    # Market discovery (Gamma API)
    # -------------------------------------------------------------------------

    def get_markets(
        self,
        active: bool = True,
        closed: bool = False,
        tag_slug: Optional[str] = None,
        slug_contains: Optional[str] = None,
        max_items: Optional[int] = None,
    ) -> Generator[Market, None, None]:
        """
        Paginated generator over Polymarket markets. Yields normalized Market objects.

        Args:
            active: Include active markets
            closed: Include closed markets
            tag_slug: Filter by tag (e.g. "politics", "sports", "crypto")
            slug_contains: Filter by slug substring (e.g. "trump", "bitcoin")
            max_items: Stop after N markets
        """
        params: dict = {"limit": 100, "active": str(active).lower(), "closed": str(closed).lower()}
        if tag_slug:
            params["tag_slug"] = tag_slug

        def _fetch(offset: int):
            p = dict(params)
            p["offset"] = offset
            resp = self._gamma.get("/markets", params=p)
            resp.raise_for_status()
            data = resp.json()
            items = data if isinstance(data, list) else data.get("data", [])
            if slug_contains:
                items = [m for m in items if slug_contains.lower() in m.get("slug", "").lower()]
            total = int(resp.headers.get("X-Total-Count", 0)) or len(items) + offset + 1
            return items, total

        for raw in offset_paginate(_fetch, max_items=max_items):
            try:
                yield PolymarketMarket.from_gamma(raw).to_common()
            except Exception:
                continue

    def get_raw_market(self, condition_id: str) -> PolymarketMarket:
        """
        Get a PolymarketMarket with full details including token IDs.
        Uses the CLOB API which supports direct lookup by condition_id.
        Use this when you need yes_token_id() for orderbook access.
        """
        resp = self._clob.get(f"/markets/{condition_id}")
        resp.raise_for_status()
        return PolymarketMarket.from_clob(resp.json())

    def get_market(self, condition_id: str) -> Market:
        """Get a single market, normalized."""
        return self.get_raw_market(condition_id).to_common()

    def search_events(
        self,
        slug: Optional[str] = None,
        tag_slug: Optional[str] = None,
        active: bool = True,
        limit: int = 50,
    ) -> list[dict]:
        """Search events on the Gamma API (raw response)."""
        params: dict = {"limit": limit, "active": str(active).lower()}
        if slug:
            params["slug"] = slug
        if tag_slug:
            params["tag_slug"] = tag_slug
        resp = self._gamma.get("/events", params=params)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("data", [])

    def get_event(self, slug: str) -> dict:
        """Get a single event by slug (raw response includes all child markets)."""
        resp = self._gamma.get(f"/events/{slug}")
        resp.raise_for_status()
        return resp.json()

    # -------------------------------------------------------------------------
    # Orderbook & Prices (CLOB API)
    # -------------------------------------------------------------------------

    def get_orderbook(self, token_id: str) -> Orderbook:
        """
        Get current orderbook for a YES token.

        token_id: The clobTokenId for the Yes outcome.
                  Get it from: client.get_raw_market(condition_id).yes_token_id()
        """
        resp = self._clob.get("/book", params={"token_id": token_id})
        resp.raise_for_status()
        return orderbook_from_clob(token_id, resp.json())

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get the current midpoint price for a token (0.0–1.0)."""
        resp = self._clob.get("/midpoint", params={"token_id": token_id})
        resp.raise_for_status()
        data = resp.json()
        mid = data.get("mid")
        return float(mid) if mid is not None else None

    def get_price_history(
        self,
        token_id: str,
        interval: str = "1d",
        fidelity: int = 60,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> list[dict]:
        """
        Get OHLC price history for a token.

        Args:
            token_id: CLOB token ID (yes outcome)
            interval: Time interval — "1m", "5m", "1h", "6h", "1d", "1w", "max"
            fidelity: Candle size in seconds
            start_ts / end_ts: Unix timestamps to filter range

        Returns:
            List of {"t": timestamp, "p": price} dicts
        """
        params: dict = {"market": token_id, "interval": interval, "fidelity": fidelity}
        if start_ts:
            params["startTs"] = start_ts
        if end_ts:
            params["endTs"] = end_ts
        resp = self._clob.get("/prices-history", params=params)
        resp.raise_for_status()
        return resp.json().get("history", [])

    def get_best_price(self, token_id: str, side: str = "buy") -> Optional[float]:
        """Get best available price for a token on the given side."""
        resp = self._clob.get("/price", params={"token_id": token_id, "side": side})
        resp.raise_for_status()
        data = resp.json()
        price = data.get("price")
        return float(price) if price is not None else None

    # -------------------------------------------------------------------------
    # Trade history (Data API)
    # -------------------------------------------------------------------------

    def get_trades(
        self,
        condition_id: Optional[str] = None,
        max_items: Optional[int] = None,
    ) -> Generator[Trade, None, None]:
        """
        Paginated generator over trade history.

        Args:
            condition_id: Filter to a specific market. None = recent trades across all markets.
            max_items: Stop after N trades.
        """
        params: dict = {"limit": 500}
        if condition_id:
            params["market"] = condition_id

        def _fetch(offset: int):
            p = dict(params)
            p["offset"] = offset
            resp = self._data.get("/trades", params=p)
            resp.raise_for_status()
            data = resp.json()
            items = data if isinstance(data, list) else []
            return items, 0  # Data API has no total count; stops when empty

        for raw in offset_paginate(_fetch, max_items=max_items):
            try:
                yield trade_from_polymarket(raw)
            except Exception:
                continue

    # -------------------------------------------------------------------------
    # Order placement (requires auth)
    # -------------------------------------------------------------------------

    def place_order(
        self,
        token_id: str,
        side: str,           # "BUY" or "SELL"
        price: float,        # 0.0–1.0
        size: float,         # number of shares
        order_type: str = "GTC",   # "GTC" for resting limit, "FOK" for immediate
        neg_risk: bool = False,    # True for negRisk markets (e.g. championship winner)
    ) -> dict:
        """
        Place an order via py_clob_client.
        Requires POLYMARKET_PRIVATE_KEY to be configured.

        order_type:
            "GTC" — Good Till Cancelled (resting limit order, use for MM)
            "FOK" — Fill or Kill (immediate, for hedging)
        neg_risk:
            True for multi-outcome negRisk markets (e.g. March Madness winner)
        """
        if self._auth is None:
            raise ValueError(
                "No private key configured. Set POLYMARKET_PRIVATE_KEY env var."
            )
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY, SELL
        client = self._auth.clob_client
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=BUY if side.upper() == "BUY" else SELL,
        )
        options = PartialCreateOrderOptions(neg_risk=neg_risk)
        ot = OrderType.GTC if order_type.upper() == "GTC" else OrderType.FOK
        signed_order = client.create_order(order_args, options)
        return client.post_order(signed_order, ot)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a single order by ID."""
        if self._auth is None:
            raise ValueError("No private key configured.")
        return self._auth.clob_client.cancel(order_id)

    def cancel_orders(self, order_ids: list[str]) -> dict:
        """Cancel multiple orders by ID list."""
        if self._auth is None:
            raise ValueError("No private key configured.")
        return self._auth.clob_client.cancel_orders(order_ids)

    def cancel_all(self) -> dict:
        """Cancel all open orders across all markets."""
        if self._auth is None:
            raise ValueError("No private key configured.")
        return self._auth.clob_client.cancel_all()

    def get_open_orders(self, token_id: Optional[str] = None) -> list[dict]:
        """
        Get open (resting) orders. Optionally filter by token_id.
        Returns raw CLOB API response list.
        """
        if self._auth is None:
            raise ValueError("No private key configured.")
        from py_clob_client.clob_types import OpenOrderParams
        params = OpenOrderParams(asset_id=token_id) if token_id else None
        resp = self._auth.clob_client.get_orders(params)
        return resp if isinstance(resp, list) else []

    # -------------------------------------------------------------------------
    # Context manager
    # -------------------------------------------------------------------------

    def close(self) -> None:
        for s in [self._gamma, self._clob, self._data]:
            if not s.is_closed:
                s.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
