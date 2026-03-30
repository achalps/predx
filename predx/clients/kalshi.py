from __future__ import annotations
from datetime import datetime
from typing import TYPE_CHECKING, Generator, Optional

from ..config import KalshiConfig

if TYPE_CHECKING:
    from ..auth.kalshi import KalshiSigner
from ..models.common import Market, Orderbook, Trade, Position, Order
from ..models.kalshi import (
    market_from_kalshi,
    orderbook_from_kalshi,
    trade_from_kalshi,
    position_from_kalshi,
    order_from_kalshi,
)
from ..utils.pagination import cursor_paginate
from .base import BaseClient


class KalshiClient(BaseClient):
    """
    Kalshi REST API client.

    Read-only endpoints (markets, trades, events, series) work without auth.
    Portfolio and order endpoints require auth via env vars or KalshiConfig:
        KALSHI_API_KEY
        KALSHI_PRIVATE_KEY_PATH

    Usage:
        # No auth needed for reads:
        with KalshiClient() as k:
            for market in k.get_markets(status="open"):
                print(market.id, market.yes_price)

        # Auth needed for orders/portfolio:
        with KalshiClient(KalshiConfig(api_key="...", private_key_path="...")) as k:
            k.place_order(...)
    """

    def __init__(self, config: Optional[KalshiConfig] = None):
        cfg = config or KalshiConfig.with_defaults()
        super().__init__(cfg.base_url, cfg.timeout, cfg.max_retries)
        self._signer: Optional["KalshiSigner"] = None
        if cfg.api_key and cfg.private_key_path:
            from ..auth.kalshi import KalshiSigner
            self._signer = KalshiSigner(cfg.api_key, cfg.private_key_path)
        self._ws_url = cfg.ws_url

    def _auth(self, method: str, path: str) -> dict:
        """Generate fresh auth headers. Returns empty dict if no auth configured."""
        if self._signer is None:
            return {}
        return self._signer.sign(method, path)

    def _require_auth(self) -> None:
        """Raise if auth is not configured (for portfolio/order endpoints)."""
        if self._signer is None:
            raise ValueError(
                "Auth required. Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH env vars."
            )

    # -------------------------------------------------------------------------
    # Markets
    # -------------------------------------------------------------------------

    def get_markets(
        self,
        event_ticker: Optional[str] = None,
        series_ticker: Optional[str] = None,
        status: Optional[str] = None,
        min_close_ts: Optional[datetime] = None,
        max_close_ts: Optional[datetime] = None,
        max_items: Optional[int] = None,
    ) -> Generator[Market, None, None]:
        """
        Paginated generator over markets. Yields normalized Market objects.

        Args:
            event_ticker: Filter to markets under this event (e.g. "PRES-2024")
            series_ticker: Filter to markets in this series (e.g. "PRES")
            status: "open", "closed", or "settled" (API filter values)
                    Note: returned market objects use "active"/"finalized" internally,
                    which predx normalizes to MarketStatus.OPEN/SETTLED automatically.
            min_close_ts / max_close_ts: Filter by close time
            max_items: Stop after this many markets (None = all)
        """
        params: dict = {"limit": 200}
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if min_close_ts:
            params["min_close_ts"] = int(min_close_ts.timestamp())
        if max_close_ts:
            params["max_close_ts"] = int(max_close_ts.timestamp())

        def _fetch(cursor: Optional[str]):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            data = self._request(
                "GET", "/markets", params=p,
                extra_headers=self._auth("GET", "/markets"),
            )
            return data.get("markets", []), data.get("cursor")

        for raw in cursor_paginate(_fetch, max_items=max_items):
            yield market_from_kalshi(raw)

    def get_market(self, ticker: str) -> Market:
        """Get a single market by ticker."""
        path = f"/markets/{ticker}"
        data = self._request("GET", path, extra_headers=self._auth("GET", path))
        return market_from_kalshi(data["market"])

    def get_event_markets(self, event_ticker: str) -> list[Market]:
        """Get all markets under a specific event."""
        return list(self.get_markets(event_ticker=event_ticker))

    def get_series(self, limit: int = 100) -> list[dict]:
        """List all series (raw API response)."""
        data = self._request(
            "GET", "/series", params={"limit": limit},
            extra_headers=self._auth("GET", "/series"),
        )
        return data.get("series", [])

    def get_events(
        self,
        series_ticker: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """List events (raw API response). Use get_markets() for normalized data."""
        params: dict = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        data = self._request(
            "GET", "/events", params=params,
            extra_headers=self._auth("GET", "/events"),
        )
        return data.get("events", [])

    # -------------------------------------------------------------------------
    # Orderbook & Trades
    # -------------------------------------------------------------------------

    def get_orderbook(self, ticker: str, depth: int = 10) -> Orderbook:
        """
        Get current orderbook for a market.

        YES bids and asks are normalized to 0.0–1.0.
        NO bids are converted to YES asks automatically.
        """
        path = f"/markets/{ticker}/orderbook"
        data = self._request(
            "GET", path, params={"depth": depth},
            extra_headers=self._auth("GET", path),
        )
        # API v2 uses "orderbook_fp" (dollar prices); fall back to legacy "orderbook" (cent prices)
        book_data = data.get("orderbook_fp") or data.get("orderbook") or {}
        return orderbook_from_kalshi(ticker, book_data)

    def get_trades(
        self,
        ticker: Optional[str] = None,
        min_ts: Optional[datetime] = None,
        max_ts: Optional[datetime] = None,
        max_items: Optional[int] = None,
    ) -> Generator[Trade, None, None]:
        """
        Paginated generator over trade history.

        Args:
            ticker: Filter to a specific market. None = all markets.
            min_ts / max_ts: Time range filter.
            max_items: Stop after N trades.
        """
        params: dict = {"limit": 1000}
        if ticker:
            params["ticker"] = ticker
        if min_ts:
            params["min_ts"] = int(min_ts.timestamp() * 1000)
        if max_ts:
            params["max_ts"] = int(max_ts.timestamp() * 1000)

        def _fetch(cursor: Optional[str]):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            data = self._request(
                "GET", "/markets/trades", params=p,
                extra_headers=self._auth("GET", "/markets/trades"),
            )
            return data.get("trades", []), data.get("cursor")

        for raw in cursor_paginate(_fetch, max_items=max_items):
            yield trade_from_kalshi(raw)

    # -------------------------------------------------------------------------
    # Portfolio (requires auth, which all Kalshi calls do)
    # -------------------------------------------------------------------------

    def get_balance(self) -> dict:
        """Returns balance dict with 'balance' key in cents."""
        self._require_auth()
        return self._request(
            "GET", "/portfolio/balance",
            extra_headers=self._auth("GET", "/portfolio/balance"),
        )

    def get_positions(self, ticker: Optional[str] = None) -> list[Position]:
        """Get all open positions."""
        self._require_auth()
        params = {}
        if ticker:
            params["ticker"] = ticker
        data = self._request(
            "GET", "/portfolio/positions", params=params,
            extra_headers=self._auth("GET", "/portfolio/positions"),
        )
        return [position_from_kalshi(p) for p in data.get("market_positions", [])]

    def get_orders(
        self,
        ticker: Optional[str] = None,
        status: Optional[str] = None,
        max_items: Optional[int] = None,
    ) -> Generator[Order, None, None]:
        """Paginated generator over orders."""
        self._require_auth()
        params: dict = {"limit": 100}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status

        def _fetch(cursor: Optional[str]):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            data = self._request(
                "GET", "/portfolio/orders", params=p,
                extra_headers=self._auth("GET", "/portfolio/orders"),
            )
            return data.get("orders", []), data.get("cursor")

        for raw in cursor_paginate(_fetch, max_items=max_items):
            yield order_from_kalshi(raw)

    def get_fills(
        self,
        ticker: Optional[str] = None,
        max_items: Optional[int] = None,
    ) -> Generator[dict, None, None]:
        """Paginated generator over fill history (raw API response)."""
        self._require_auth()
        params: dict = {"limit": 100}
        if ticker:
            params["ticker"] = ticker

        def _fetch(cursor: Optional[str]):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            data = self._request(
                "GET", "/portfolio/fills", params=p,
                extra_headers=self._auth("GET", "/portfolio/fills"),
            )
            return data.get("fills", []), data.get("cursor")

        for raw in cursor_paginate(_fetch, max_items=max_items):
            yield raw

    # -------------------------------------------------------------------------
    # Order management
    # -------------------------------------------------------------------------

    def place_order(
        self,
        ticker: str,
        side: str,          # "yes" or "no"
        action: str,        # "buy" or "sell"
        count: int,         # number of contracts
        order_type: str = "limit",
        yes_price: Optional[int] = None,   # in cents (0-99)
        no_price: Optional[int] = None,    # in cents (0-99)
        expiration_ts: Optional[int] = None,
    ) -> Order:
        """
        Place a limit or market order.

        Prices are in cents (Kalshi native). For a YES buy at 65 cents: yes_price=65.
        """
        self._require_auth()
        body: dict = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        if expiration_ts is not None:
            body["expiration_ts"] = expiration_ts

        data = self._request(
            "POST", "/portfolio/orders", json=body,
            extra_headers=self._auth("POST", "/portfolio/orders"),
        )
        return order_from_kalshi(data["order"])

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a single order by ID."""
        self._require_auth()
        path = f"/portfolio/orders/{order_id}"
        return self._request("DELETE", path, extra_headers=self._auth("DELETE", path))

    def cancel_all_orders(self, ticker: Optional[str] = None) -> dict:
        """Cancel all open orders, optionally filtered to a specific market."""
        self._require_auth()
        params = {}
        if ticker:
            params["ticker"] = ticker
        return self._request(
            "DELETE", "/portfolio/orders", params=params,
            extra_headers=self._auth("DELETE", "/portfolio/orders"),
        )
