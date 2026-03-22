"""
NCAA March Madness reward farmer.
Run: python farmer.py

Reads config from .farmer_markets.json (written by farmer.ipynb Cell 3).
Or uses defaults below if no json found.
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()
# Requires POLYMARKET_PRIVATE_KEY in .env

from predx.tools.reward_farmer import (
    FarmerConfig,
    MarketPair,
    resolve_pair,
    run,
)
from predx import PolymarketClient
from predx.auth.polymarket import PolymarketAuth


def discover_markets(min_reward: float = 7000) -> list[dict]:
    """Fetch all reward-eligible markets, filter by min daily rate."""
    auth = PolymarketAuth(
        os.environ["POLYMARKET_PRIVATE_KEY"],
        "https://clob.polymarket.com",
        137,
    )
    client = auth.clob_client

    cursor = ""
    all_markets = []
    while True:
        try:
            resp = client.get_sampling_markets(next_cursor=cursor)
            items = resp.get("data", [])
            if not items:
                break
            all_markets.extend(items)
            cursor = resp.get("next_cursor")
            if not cursor:
                break
        except Exception:
            break

    results = []
    for m in all_markets:
        if not m.get("active") or m.get("closed"):
            continue
        rate = 0
        rewards = m.get("rewards") or {}
        for ri in rewards.get("rates", []) or []:
            if "2791bca1f2de4661" in ri.get("asset_address", "").lower():
                rate = ri.get("rewards_daily_rate", 0)
        if rate >= min_reward:
            m["_rate"] = rate
            results.append(m)

    results.sort(key=lambda m: -m["_rate"])
    return results


def print_markets(markets: list[dict]) -> None:
    print(f"\n{'Rate':>8}  {'MinSz':>5}  {'Spread':>6}  Question")
    print("─" * 80)
    for m in markets:
        rewards = m.get("rewards") or {}
        print(
            f"${m['_rate']:>7}/day  {rewards.get('min_size',0):>5}  "
            f"{rewards.get('max_spread',0):>5}¢  {m['question'][:50]}"
        )
    print()


def resolve_markets(markets: list[dict]) -> list[MarketPair]:
    pm = PolymarketClient()
    pairs = []
    for m in markets:
        try:
            pair = resolve_pair(pm, m["condition_id"], None)
            tokens = m.get("tokens", [])
            outcomes = " vs ".join(t.get("outcome", "?") for t in tokens)
            print(f"  ✓ {outcomes}")
            pairs.append(pair)
        except Exception as e:
            print(f"  ✗ {m.get('question','?')[:40]}: {e}")
    pm.close()
    return pairs


if __name__ == "__main__":
    # Read config from notebook or use defaults
    json_path = os.path.join(os.path.dirname(__file__), ".farmer_markets.json")
    if os.path.exists(json_path):
        with open(json_path) as f:
            conf = json.load(f)
        cids = conf.get("cids", [])
        dry_run = conf.get("dry_run", True)
        order_size = conf.get("order_size", 1000.0)
        db_path = conf.get("db_path", "trades.db")
        print(f"Loaded {len(cids)} markets from .farmer_markets.json")
    else:
        print("No .farmer_markets.json found — run farmer.ipynb first, or using auto-discover.")
        print("Discovering markets (>= $7000/day)...")
        found = discover_markets(min_reward=7000)
        print_markets(found)
        cids = [m["condition_id"] for m in found]
        dry_run = True
        order_size = 1000.0
        db_path = "trades.db"

    cfg = FarmerConfig(order_size=order_size, dry_run=dry_run, db_path=db_path)

    # Resolve condition IDs → pairs
    pm = PolymarketClient()
    pairs = []
    for cid in cids:
        try:
            pair = resolve_pair(pm, cid, None)
            print(f"  ✓ {pair.label}")
            pairs.append(pair)
        except Exception as e:
            print(f"  ✗ {cid[:20]}...: {e}")
    pm.close()

    if not pairs:
        print("No markets resolved.")
        sys.exit(1)

    try:
        asyncio.run(run(pairs, cfg))
    except KeyboardInterrupt:
        print("\nDone.")
