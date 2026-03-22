"""
Live cross-venue orderbook dashboard (TUI).

Usage:
    python -m predx.tools.live_dash KXMARMAD-26-DUKE <poly_token_id>
    python -m predx.tools.live_dash KXMARMAD-26-DUKE --poly-cid <condition_id>
    python -m predx.tools.live_dash --depth 10 TICKER TOKEN_ID
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..config import KalshiConfig, PolymarketConfig
from ..models.common import Exchange, Orderbook, PriceLevel, Trade
from ..models.kalshi import orderbook_from_kalshi
from ..models.polymarket import orderbook_from_clob
from ..ws.kalshi import KalshiWebSocket
from ..ws.polymarket import PolymarketWebSocket
from ..auth.kalshi import KalshiSigner
from . import fair_value


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class DashboardState:
    kalshi_ob: Optional[Orderbook] = None
    poly_ob: Optional[Orderbook] = None
    kalshi_trades: deque = field(default_factory=lambda: deque(maxlen=20))
    poly_trades: deque = field(default_factory=lambda: deque(maxlen=20))
    kalshi_connected: bool = False
    poly_connected: bool = False
    kalshi_ticker: str = ""
    poly_token_id: str = ""
    kalshi_updates: int = 0
    poly_updates: int = 0
    label: str = ""


# ---------------------------------------------------------------------------
# Kalshi feed
# ---------------------------------------------------------------------------

async def kalshi_feed(state: DashboardState):
    cfg = KalshiConfig()
    signer = KalshiSigner(cfg.api_key, cfg.private_key_path)
    ws = KalshiWebSocket(signer, cfg.ws_url)

    state.kalshi_connected = True
    try:
        async for msg in ws.subscribe(
            channels=["orderbook_delta", "ticker", "trade"],
            market_tickers=[state.kalshi_ticker],
            reconnect=True,
        ):
            msg_type = msg.get("type", "")
            m = msg.get("msg", {})

            if msg_type == "orderbook_snapshot":
                data = {}
                if "yes_dollars_fp" in m:
                    data["yes_dollars"] = m["yes_dollars_fp"]
                    data["no_dollars"] = m.get("no_dollars_fp", [])
                else:
                    data["yes"] = m.get("yes", [])
                    data["no"] = m.get("no", [])
                state.kalshi_ob = orderbook_from_kalshi(state.kalshi_ticker, data)
                state.kalshi_updates += 1

            elif msg_type == "orderbook_delta":
                # Apply delta to existing orderbook
                if state.kalshi_ob:
                    _apply_kalshi_delta(state.kalshi_ob, m)
                    state.kalshi_ob.timestamp = datetime.now(timezone.utc)
                    state.kalshi_updates += 1

            elif msg_type == "ticker":
                # Lightweight BBO update
                if state.kalshi_ob:
                    bid_d = m.get("yes_bid_dollars")
                    ask_d = m.get("yes_ask_dollars")
                    if bid_d is not None and state.kalshi_ob.yes_bids:
                        state.kalshi_ob.yes_bids[0] = PriceLevel(
                            price=float(bid_d),
                            size=state.kalshi_ob.yes_bids[0].size,
                        )
                    if ask_d is not None and state.kalshi_ob.yes_asks:
                        state.kalshi_ob.yes_asks[0] = PriceLevel(
                            price=float(ask_d),
                            size=state.kalshi_ob.yes_asks[0].size,
                        )
                    state.kalshi_updates += 1

            elif msg_type == "trade":
                t = m
                price_d = t.get("yes_price_dollars")
                price = float(price_d) if price_d else (t.get("yes_price", 0) / 100)
                size_fp = t.get("count_fp")
                size = float(size_fp) if size_fp else float(t.get("count", 0))
                state.kalshi_trades.appendleft(Trade(
                    id=t.get("trade_id", ""),
                    market_id=state.kalshi_ticker,
                    exchange=Exchange.KALSHI,
                    price=price,
                    size=size,
                    side=t.get("taker_side", "yes"),
                    timestamp=datetime.now(timezone.utc),
                ))

    except Exception:
        state.kalshi_connected = False
        raise


def _apply_kalshi_delta(ob: Orderbook, delta: dict):
    """Apply an orderbook_delta message to an existing Orderbook."""
    # Delta messages contain updated levels for yes and/or no side
    # Format varies — handle both old and new
    for key, is_yes in [("yes_dollars_fp", True), ("no_dollars_fp", False),
                         ("yes", True), ("no", False)]:
        levels = delta.get(key)
        if not levels:
            continue
        for level in levels:
            try:
                if isinstance(level[0], str):
                    price, size = float(level[0]), float(level[1])
                else:
                    price, size = level[0] / 100, float(level[1])
            except (IndexError, ValueError, TypeError):
                continue

            if is_yes:
                _update_level(ob.yes_bids, price, size, descending=True)
            else:
                ask_price = round(1 - price, 4)
                _update_level(ob.yes_asks, ask_price, size, descending=False)


def _update_level(levels: list[PriceLevel], price: float, size: float, descending: bool):
    """Insert, update, or remove a price level in a sorted list."""
    for i, lv in enumerate(levels):
        if abs(lv.price - price) < 1e-6:
            if size <= 0:
                levels.pop(i)
            else:
                levels[i] = PriceLevel(price=price, size=size)
            return
    if size > 0:
        levels.append(PriceLevel(price=price, size=size))
        levels.sort(key=lambda x: -x.price if descending else x.price)


# ---------------------------------------------------------------------------
# Polymarket feed
# ---------------------------------------------------------------------------

async def poly_feed(state: DashboardState):
    # Seed initial orderbook via REST (WS only sends updates on changes)
    try:
        from ..clients.polymarket import PolymarketClient
        pm = PolymarketClient()
        state.poly_ob = pm.get_orderbook(state.poly_token_id)
        state.poly_updates += 1
        state.poly_connected = True
    except Exception:
        pass

    # Then listen for live updates via WS
    ws = PolymarketWebSocket()
    state.poly_connected = True
    try:
        async for msg in ws.subscribe(
            token_ids=[state.poly_token_id],
            reconnect=True,
        ):
            if not isinstance(msg, dict):
                continue
            bids = msg.get("bids")
            asks = msg.get("asks")
            if bids is not None or asks is not None:
                state.poly_ob = orderbook_from_clob(state.poly_token_id, msg)
                state.poly_updates += 1
    except Exception:
        state.poly_connected = False
        raise


async def poly_rest_poller(state: DashboardState, condition_id: str):
    """Poll Polymarket orderbook + trades via REST every 3 seconds as fallback."""
    from ..clients.polymarket import PolymarketClient
    try:
        pm = PolymarketClient()
        seen_ids: set = set()
        while True:
            await asyncio.sleep(3)
            try:
                # Refresh orderbook
                ob = pm.get_orderbook(state.poly_token_id)
                state.poly_ob = ob
                state.poly_updates += 1
            except Exception:
                pass
            try:
                # Poll trades
                if condition_id:
                    for trade in pm.get_trades(condition_id=condition_id, max_items=10):
                        if trade.id not in seen_ids:
                            seen_ids.add(trade.id)
                            state.poly_trades.appendleft(trade)
            except Exception:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def build_ob_table(ob: Optional[Orderbook], label: str, depth: int, max_size: float) -> Table:
    table = Table(title=label, expand=True, show_header=True, header_style="bold",
                  title_style="bold cyan" if "Kalshi" in label else "bold magenta")
    table.add_column("Side", width=4, justify="center")
    table.add_column("Price", width=7, justify="right")
    table.add_column("Size", width=10, justify="right")
    table.add_column("Depth", ratio=1)

    if not ob:
        table.add_row("", "", "[dim]waiting...[/dim]", "")
        return table

    # Asks (reversed so lowest ask is near the spread)
    asks = ob.yes_asks[:depth]
    for lv in reversed(asks):
        bar_len = int(20 * min(lv.size / max_size, 1)) if max_size else 0
        bar = "[red]" + "\u2588" * bar_len + "[/red]"
        table.add_row(
            "[red]ASK[/red]",
            f"[red]{lv.price:.4f}[/red]",
            f"[red]{lv.size:,.0f}[/red]",
            bar,
        )

    # Spread row
    spread = ob.spread
    spread_str = f"{spread * 100:.1f}\u00a2" if spread is not None else "?"
    table.add_row("", f"[dim]\u2500\u2500\u2500[/dim]", f"[dim]spd {spread_str}[/dim]", "")

    # Bids
    bids = ob.yes_bids[:depth]
    for lv in bids:
        bar_len = int(20 * min(lv.size / max_size, 1)) if max_size else 0
        bar = "[green]" + "\u2588" * bar_len + "[/green]"
        table.add_row(
            "[green]BID[/green]",
            f"[green]{lv.price:.4f}[/green]",
            f"[green]{lv.size:,.0f}[/green]",
            bar,
        )

    return table


def build_fair_panel(state: DashboardState) -> Panel:
    fv = fair_value.compute(state.kalshi_ob, state.poly_ob)

    parts = []
    if fv["blend"] is not None:
        parts.append(f"[yellow]Blend: {fv['blend']:.4f}[/yellow]")
    if fv["micro"] is not None:
        parts.append(f"[yellow]Micro: {fv['micro']:.4f}[/yellow]")
    if fv["cross_spread"]:
        parts.append(f"[bold red]Arb: {fv['cross_spread']*100:.1f}\u00a2[/bold red]")
    else:
        parts.append("[dim]Arb: none[/dim]")
    if fv["obi"] is not None:
        color = "green" if fv["obi"] > 0 else "red" if fv["obi"] < 0 else "white"
        parts.append(f"[{color}]OBI: {fv['obi']:+.3f}[/{color}]")

    line1 = "  ".join(parts)

    details = []
    if fv["k_mid"] is not None:
        details.append(f"K mid={fv['k_mid']:.4f}")
    if fv["p_mid"] is not None:
        details.append(f"P mid={fv['p_mid']:.4f}")
    if fv["k_micro"] is not None:
        details.append(f"K\u03bc={fv['k_micro']:.4f}")
    if fv["p_micro"] is not None:
        details.append(f"P\u03bc={fv['p_micro']:.4f}")
    if fv["k_obi"] is not None:
        details.append(f"K obi={fv['k_obi']:+.2f}")
    if fv["p_obi"] is not None:
        details.append(f"P obi={fv['p_obi']:+.2f}")

    line2 = "[dim]  " + "  ".join(details) + "[/dim]"

    return Panel(
        Text.from_markup(line1 + "\n" + line2),
        title="Fair Value",
        title_align="center",
        border_style="yellow",
    )


def build_tape(state: DashboardState) -> Table:
    table = Table(expand=True, show_header=True, header_style="bold", title="Trade Tape")
    table.add_column("Venue", width=2, justify="center")
    table.add_column("Side", width=4, justify="center")
    table.add_column("Size", width=8, justify="right")
    table.add_column("Price", width=7, justify="right")
    table.add_column("Time", width=8, justify="right")

    all_trades = []
    for t in state.kalshi_trades:
        all_trades.append(("K", t))
    for t in state.poly_trades:
        all_trades.append(("P", t))
    all_trades.sort(key=lambda x: x[1].timestamp if x[1].timestamp else datetime.min, reverse=True)

    for venue, t in all_trades[:12]:
        color = "cyan" if venue == "K" else "magenta"
        side_color = "green" if t.side == "yes" else "red"
        ts = t.timestamp.strftime("%H:%M:%S") if t.timestamp else ""
        table.add_row(
            f"[{color}]{venue}[/{color}]",
            f"[{side_color}]{t.side.upper()}[/{side_color}]",
            f"{t.size:,.0f}",
            f"{t.price:.4f}",
            f"[dim]{ts}[/dim]",
        )

    if not all_trades:
        table.add_row("", "", "[dim]waiting...[/dim]", "", "")

    return table


def build_status(state: DashboardState) -> Text:
    k_dot = "[green]\u25cf[/green]" if state.kalshi_connected else "[red]\u25cf[/red]"
    p_dot = "[green]\u25cf[/green]" if state.poly_connected else "[red]\u25cf[/red]"
    now = datetime.now().strftime("%H:%M:%S")
    return Text.from_markup(
        f" K: {k_dot}  ({state.kalshi_updates} upd)  "
        f"P: {p_dot}  ({state.poly_updates} upd)  "
        f"[dim]{now}[/dim]"
    )


def build_layout(state: DashboardState, depth: int) -> Layout:
    layout = Layout()

    # Compute max size across both books for consistent bar scaling
    max_size = 1.0
    for ob in (state.kalshi_ob, state.poly_ob):
        if ob:
            for lv in ob.yes_bids[:depth] + ob.yes_asks[:depth]:
                max_size = max(max_size, lv.size)

    layout.split_column(
        Layout(Panel(Text(state.label or f"{state.kalshi_ticker}", style="bold"), border_style="dim"), size=3, name="header"),
        Layout(name="books", size=depth * 2 + 6),
        Layout(build_fair_panel(state), size=5, name="fair"),
        Layout(build_tape(state), size=min(len(state.kalshi_trades) + len(state.poly_trades), 12) + 4, name="tape"),
        Layout(Panel(build_status(state), style="dim"), size=3, name="status"),
    )

    layout["books"].split_row(
        Layout(build_ob_table(state.kalshi_ob, "Kalshi", depth, max_size)),
        Layout(build_ob_table(state.poly_ob, "Polymarket", depth, max_size)),
    )

    return layout


async def display_loop(state: DashboardState, depth: int, refresh: float):
    console = Console()
    with Live(build_layout(state, depth), console=console, auto_refresh=False, screen=True) as live:
        while True:
            live.update(build_layout(state, depth))
            live.refresh()
            await asyncio.sleep(refresh)


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def parse_kalshi_input(s: str) -> str:
    if "kalshi.com" in s:
        parts = urlparse(s).path.strip("/").split("/")
        return parts[-1].upper().replace("-", "-")
    return s.upper() if s == s.lower() else s


def resolve_poly_input(s: str) -> tuple[str, str]:
    """
    Returns (token_id, condition_id).
    Accepts: token_id directly, condition_id (resolves via CLOB), or URL.
    """
    from ..clients.polymarket import PolymarketClient

    # If it's a URL, extract slug and search
    if "polymarket.com" in s:
        slug = urlparse(s).path.strip("/").split("/")[-1]
        pm = PolymarketClient()
        events = pm.search_events(slug=slug)
        if not events:
            print(f"No Polymarket event found for slug: {slug}", file=sys.stderr)
            sys.exit(1)
        markets = events[0].get("markets", [])
        if not markets:
            print(f"No markets in event: {slug}", file=sys.stderr)
            sys.exit(1)
        cid = markets[0].get("conditionId", markets[0].get("condition_id", ""))
        raw = pm.get_raw_market(cid)
        return raw.yes_token_id(), cid

    # If it starts with 0x, it's likely a condition_id
    if s.startswith("0x"):
        pm = PolymarketClient()
        raw = pm.get_raw_market(s)
        return raw.yes_token_id(), s

    # Otherwise assume it's a token_id directly
    return s, ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_dashboard(
    kalshi_ticker: str,
    poly_token_id: str,
    poly_condition_id: str,
    depth: int,
    refresh: float,
    label: str = "",
):
    state = DashboardState(
        kalshi_ticker=kalshi_ticker,
        poly_token_id=poly_token_id,
        label=label,
    )

    tasks = [
        asyncio.create_task(kalshi_feed(state)),
        asyncio.create_task(poly_feed(state)),
        asyncio.create_task(display_loop(state, depth, refresh)),
    ]

    # REST poller for Poly OB refresh + trades (WS is quiet for illiquid markets)
    tasks.append(asyncio.create_task(poly_rest_poller(state, poly_condition_id)))

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        pass
    finally:
        for t in tasks:
            t.cancel()


def main():
    # Load env vars (KALSHI_API_KEY, KALSHI_PRIVATE_KEY_PATH, etc.)
    import os
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(
        description="Live cross-venue orderbook dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m predx.tools.live_dash KXMARMAD-26-DUKE 71321045123...
  python -m predx.tools.live_dash KXMARMAD-26-DUKE --poly-cid 0xabc123...
  python -m predx.tools.live_dash KXMARMAD-26-DUKE --poly-url https://polymarket.com/event/...
        """,
    )
    parser.add_argument("kalshi", help="Kalshi ticker or URL")
    parser.add_argument("polymarket", nargs="?", help="Polymarket token_id")
    parser.add_argument("--poly-cid", help="Polymarket condition_id (resolved to token_id)")
    parser.add_argument("--poly-url", help="Polymarket URL (resolved to token_id)")
    parser.add_argument("--depth", type=int, default=5, help="Orderbook depth (default: 5)")
    parser.add_argument("--refresh", type=float, default=0.1, help="Refresh interval in seconds (default: 0.1)")
    parser.add_argument("--label", default="", help="Custom label for the dashboard header")
    args = parser.parse_args()

    # Resolve Kalshi
    kalshi_ticker = parse_kalshi_input(args.kalshi)

    # Resolve Polymarket
    poly_input = args.polymarket or args.poly_cid or args.poly_url or ""
    if not poly_input:
        print("Error: provide a Polymarket token_id, --poly-cid, or --poly-url", file=sys.stderr)
        sys.exit(1)

    print(f"Resolving Polymarket input: {poly_input[:40]}...")
    poly_token_id, poly_cid = resolve_poly_input(poly_input)
    if not poly_token_id:
        print("Error: could not resolve Polymarket token_id", file=sys.stderr)
        sys.exit(1)

    print(f"Kalshi: {kalshi_ticker}")
    print(f"Poly token: {poly_token_id[:30]}...")
    print("Starting dashboard... (Ctrl+C to exit)\n")

    try:
        asyncio.run(run_dashboard(
            kalshi_ticker=kalshi_ticker,
            poly_token_id=poly_token_id,
            poly_condition_id=poly_cid,
            depth=args.depth,
            refresh=args.refresh,
            label=args.label or kalshi_ticker,
        ))
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
