from __future__ import annotations
from typing import Optional

from ..config import DelphiConfig
from .base import BaseClient


class DelphiClient(BaseClient):
    """
    Delphi Terminal API client.

    Provides enriched market data for Kalshi and Polymarket including:
    - Orderbook analytics (OBI, TFI, depth metrics)
    - OHLCV candles at multiple intervals
    - Cross-venue market search

    Rate limit: 300 req/min.
    Configure via DELPHI_API_KEY env var.

    Usage:
        with DelphiClient() as d:
            markets = d.get_kalshi_markets(status="active", limit=50)
            ob = d.get_kalshi_orderbook(markets[0]["id"])
            ohlcv = d.get_kalshi_ohlcv(markets[0]["id"], interval="1m")
    """

    def __init__(self, config: Optional[DelphiConfig] = None):
        cfg = config or DelphiConfig()
        super().__init__(cfg.base_url, cfg.timeout)
        self._api_key = cfg.api_key

    def _headers(self) -> dict:
        return {"X-API-Key": self._api_key}

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        return self._request("GET", path, params=params, extra_headers=self._headers())

    # -------------------------------------------------------------------------
    # Kalshi endpoints
    # -------------------------------------------------------------------------

    def get_kalshi_markets(
        self,
        status: Optional[str] = "active",
        limit: int = 100,
        event_id: Optional[str] = None,
    ) -> list[dict]:
        """List Kalshi markets. status: 'active', 'closed', or None for all."""
        params: dict = {"limit": limit}
        if status:
            params["status"] = status
        if event_id:
            params["event_id"] = event_id
        return self._get("/klsi/markets", params=params).get("markets", [])

    def get_kalshi_market(self, market_id: str) -> dict:
        """Get a single Kalshi market by Delphi ID."""
        return self._get(f"/klsi/{market_id}/market")

    def get_kalshi_orderbook(self, market_id: str) -> dict:
        """
        Get Kalshi orderbook with raw analytics.
        Returns bids/asks plus pre-computed OBI at multiple depths.
        """
        return self._get(f"/klsi/{market_id}/orderbook")

    def get_kalshi_orderbook_analytics(self, market_id: str) -> dict:
        """
        Get Kalshi orderbook analytics: OBI, spread, depth, microprice.
        Faster than fetching full orderbook when you only need aggregate metrics.
        """
        return self._get(f"/klsi/{market_id}/orderbook_analytics")

    def get_kalshi_best_quotes(self, market_id: str) -> dict:
        """Get best bid/ask with associated quantities."""
        return self._get(f"/klsi/{market_id}/orderbook_analytics/best_quotes")

    def get_kalshi_ohlcv(
        self,
        market_id: str,
        interval: str = "1m",
        limit: int = 500,
    ) -> list[dict]:
        """
        Get OHLCV candles for a Kalshi market.

        Args:
            market_id: Delphi market ID
            interval: "1s", "1m", "5m", "10m", "1h", "1d"
            limit: Number of candles (max varies by interval)

        Returns:
            List of OHLCV dicts with keys: t, o, h, l, c, v
        """
        valid = {"1s", "1m", "5m", "10m", "1h", "1d"}
        if interval not in valid:
            raise ValueError(f"interval must be one of {valid}")
        return self._get(f"/klsi/{market_id}/ohlcv_{interval}", params={"limit": limit})

    def get_kalshi_trades(self, market_id: str, limit: int = 500) -> list[dict]:
        """Get recent trade history for a Kalshi market."""
        return self._get(f"/klsi/{market_id}/tradehistory", params={"limit": limit})

    def get_kalshi_prices(self, market_id: str) -> dict:
        """Get current price summary (bid, ask, last, volume)."""
        return self._get(f"/klsi/{market_id}/prices")

    def get_kalshi_event_markets(self, event_id: str) -> list[dict]:
        """Get all markets under a Kalshi event."""
        return self._get(f"/klsi/event/{event_id}/markets").get("markets", [])

    # -------------------------------------------------------------------------
    # Polymarket endpoints
    # -------------------------------------------------------------------------

    def get_poly_markets(self, limit: int = 100, active: bool = True) -> list[dict]:
        """List Polymarket markets."""
        return self._get("/poly/markets", params={"limit": limit, "active": active}).get("markets", [])

    def get_poly_market(self, market_id: str) -> dict:
        """Get a single Polymarket market by Delphi ID."""
        return self._get(f"/poly/{market_id}/market")

    def get_poly_orderbook(self, market_id: str) -> dict:
        """Get Polymarket orderbook with analytics."""
        return self._get(f"/poly/{market_id}/orderbook")

    def get_poly_trades(self, market_id: str, limit: int = 500) -> list[dict]:
        """Get recent trade history for a Polymarket market."""
        return self._get(f"/poly/{market_id}/tradehistory", params={"limit": limit})

    # -------------------------------------------------------------------------
    # Cross-venue search
    # -------------------------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """
        Search across both Kalshi and Polymarket markets.

        Returns results ranked by relevance with exchange, id, and title fields.
        """
        return self._get("/events/search", params={"q": query, "limit": limit}).get("results", [])

    def suggest(self, query: str) -> list[str]:
        """Autocomplete suggestions for market search."""
        return self._get("/search/suggest", params={"q": query}).get("suggestions", [])

    def get_category_markets(self, category: str, limit: int = 50) -> list[dict]:
        """Get markets in a specific category (e.g. 'politics', 'sports', 'crypto')."""
        return self._get(f"/events/category/{category}", params={"limit": limit}).get("markets", [])

    # -------------------------------------------------------------------------
    # Signal computation helpers
    # -------------------------------------------------------------------------

    def get_signals(self, market_id: str, exchange: str = "kalshi") -> dict:
        """
        Compute alpha signals for a market: OBI, TFI, microprice, spread.

        Args:
            market_id: Delphi market ID
            exchange: "kalshi" or "poly"

        Returns dict with keys:
            obi_top1, obi_top5, obi_top10: Order Book Imbalance at depth levels
            tfi: Trade Flow Imbalance (positive = buy pressure)
            microprice: Volume-weighted mid
            spread: Best ask - best bid
            mid: (best_bid + best_ask) / 2
        """
        if exchange == "kalshi":
            analytics = self.get_kalshi_orderbook_analytics(market_id)
            trades = self.get_kalshi_trades(market_id, limit=200)
        else:
            analytics = self.get_poly_orderbook(market_id)
            trades = self.get_poly_trades(market_id, limit=200)

        # Compute TFI from trade history
        yes_taker = sum(t.get("size", 0) for t in trades if t.get("taker_side") == "yes")
        no_taker = sum(t.get("size", 0) for t in trades if t.get("taker_side") == "no")
        total = yes_taker + no_taker
        tfi = (yes_taker - no_taker) / total if total > 0 else 0.0

        return {
            **analytics,
            "tfi": tfi,
            "yes_taker_volume": yes_taker,
            "no_taker_volume": no_taker,
        }
