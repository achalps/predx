from __future__ import annotations
import asyncio
import json
from typing import AsyncGenerator, Callable, Optional

from ..auth.kalshi import KalshiSigner


class KalshiWebSocket:
    """
    Async WebSocket client for Kalshi real-time feeds.

    Channels:
        orderbook_delta  — incremental orderbook updates
        ticker           — best bid/ask/last price changes
        fill             — fills on your orders (auth required)
        trade            — all market trades

    Usage:
        import asyncio
        from predx import KalshiClient
        from predx.ws.kalshi import KalshiWebSocket

        signer = KalshiClient()._signer  # reuse signer from client
        ws = KalshiWebSocket(signer, ws_url)

        async def main():
            async for msg in ws.subscribe(
                channels=["orderbook_delta", "ticker"],
                market_tickers=["SOME-TICKER-1", "SOME-TICKER-2"],
            ):
                print(msg["type"], msg)

        asyncio.run(main())
    """

    def __init__(self, signer: KalshiSigner, ws_url: str):
        self._signer = signer
        self._url = ws_url
        self._seq = 0

    def _build_url(self) -> str:
        return self._url

    def _auth_headers(self) -> dict:
        return self._signer.sign_full_path("GET", "/trade-api/ws/v2")

    async def subscribe(
        self,
        channels: list[str],
        market_tickers: list[str],
        on_error: Optional[Callable[[str], None]] = None,
        reconnect: bool = True,
        max_reconnect_attempts: int = 5,
    ) -> AsyncGenerator[dict, None]:
        """
        Subscribe to channels for the given tickers.
        Yields parsed message dicts. Automatically reconnects on disconnect.

        Message types:
            subscribed      — subscription confirmed
            orderbook_delta — {"market_ticker": ..., "yes": [...], "no": [...], "seq": ...}
            ticker          — {"market_ticker": ..., "yes_bid": ..., "yes_ask": ..., "last_price": ...}
            fill            — your fills
            error           — error from server
        """
        import websockets

        attempts = 0
        while True:
            try:
                url = self._build_url()
                # websockets v14+ uses 'additional_headers'; v10-13 uses 'extra_headers'
                try:
                    conn = websockets.connect(url, additional_headers=self._auth_headers())
                except TypeError:
                    conn = websockets.connect(url, extra_headers=self._auth_headers())
                async with conn as ws:
                    attempts = 0  # reset on successful connect
                    self._seq += 1
                    sub_msg = {
                        "id": self._seq,
                        "cmd": "subscribe",
                        "params": {
                            "channels": channels,
                            "market_tickers": market_tickers,
                        },
                    }
                    await ws.send(json.dumps(sub_msg))

                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            try:
                                yield json.loads(raw)
                            except json.JSONDecodeError:
                                if on_error:
                                    on_error(raw)
                    finally:
                        ping_task.cancel()

            except Exception as e:
                if not reconnect or attempts >= max_reconnect_attempts:
                    raise
                attempts += 1
                wait = min(2 ** attempts, 30)
                await asyncio.sleep(wait)

    async def _ping_loop(self, ws, interval: int = 10) -> None:
        """Send heartbeat pings to keep the connection alive."""
        try:
            while True:
                await asyncio.sleep(interval)
                self._seq += 1
                await ws.send(json.dumps({"id": self._seq, "cmd": "ping"}))
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
