from __future__ import annotations
import asyncio
import json
from typing import AsyncGenerator, Callable, Optional


class PolymarketWebSocket:
    """
    Async WebSocket client for Polymarket real-time orderbook feeds.

    No auth required — public orderbook data only.

    Usage:
        from predx.ws.polymarket import PolymarketWebSocket

        ws = PolymarketWebSocket()

        async def main():
            async for msg in ws.subscribe(
                token_ids=["71321045..."],
            ):
                print(msg)  # {"type": "orderbook", "token_id": ..., "bids": [...], "asks": [...]}

        asyncio.run(main())
    """

    def __init__(self, ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"):
        self._url = ws_url

    async def subscribe(
        self,
        token_ids: list[str],
        on_error: Optional[Callable[[str], None]] = None,
        reconnect: bool = True,
        max_reconnect_attempts: int = 5,
    ) -> AsyncGenerator[dict, None]:
        """
        Subscribe to live orderbook updates for the given token IDs.
        Yields parsed message dicts.

        Message types:
            book   — full orderbook snapshot: {"bids": [...], "asks": [...], "market": token_id, ...}
        """
        import websockets

        attempts = 0
        while True:
            try:
                async with websockets.connect(self._url) as ws:
                    attempts = 0
                    sub_msg = {
                        "assets_ids": token_ids,
                        "type": "market",
                    }
                    await ws.send(json.dumps(sub_msg))

                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                            except json.JSONDecodeError:
                                if on_error:
                                    on_error(raw)
                                continue
                            # Skip pong and empty messages
                            if isinstance(msg, list):
                                for m in msg:
                                    yield m
                            elif isinstance(msg, dict):
                                yield msg
                    finally:
                        ping_task.cancel()

            except Exception:
                if not reconnect or attempts >= max_reconnect_attempts:
                    raise
                attempts += 1
                wait = min(2 ** attempts, 30)
                await asyncio.sleep(wait)

    async def _ping_loop(self, ws, interval: int = 25) -> None:
        """Send keepalive pings to prevent connection timeout."""
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.ping()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
