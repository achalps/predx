# predx

The Python toolkit for prediction market traders.

Screen markets, analyze microstructure, and build strategies on Polymarket and Kalshi — all from Python.

## Install

```bash
pip install predx
```

No API keys needed for market data. Only trading requires authentication.

## Quick Start

```python
from predx.analytics import MarketScanner, to_df

scanner = MarketScanner()

# Find high-volume, tight-spread markets
markets = scanner.scan(min_volume_24h=50_000, max_spread=0.03)

# What moved the most today?
movers = scanner.movers(period="1d", limit=10)

# New markets gaining traction
trending = scanner.trending(max_age_hours=72)

# Drop into pandas
df = to_df(markets)
df[["question", "yes_price", "spread", "volume_24h", "price_change_1d"]].head()
```

## Features

### Market Discovery

```python
from predx.analytics import MarketScanner, to_df

scanner = MarketScanner()

# Filter by volume, liquidity, spread, expiry, rewards eligibility
markets = scanner.scan(
    min_volume_24h=10_000,
    min_liquidity=50_000,
    max_spread=0.05,
    rewards_only=True,
    sort_by="volume_24h",
)

# Enrich with live orderbook depth
scanner.enrich(markets[:10])

# Export to DataFrame
df = to_df(markets)
```

**Available filters:** `min_volume_24h`, `min_volume_1w`, `min_liquidity`, `min_open_interest`, `max_spread`, `min_best_bid`, `max_hours_to_expiry`, `min_hours_to_expiry`, `category`, `rewards_only`, `neg_risk`

**Sort options:** `volume_24h`, `volume_1w`, `volume_total`, `liquidity`, `spread`, `competitive`, `open_interest`, `price_change_1h`, `price_change_1d`, `price_change_1w`, `created_at`

### API Clients

```python
from predx import PolymarketClient

# No auth needed for read-only access
with PolymarketClient() as pm:
    # Market discovery
    for market in pm.get_markets(active=True, max_items=50):
        print(market.title, market.yes_price)

    # Orderbook
    raw = pm.get_raw_market("condition-id-here")
    ob = pm.get_orderbook(raw.yes_token_id())
    print(ob.best_bid, ob.best_ask, ob.spread)

    # Price history, midpoint, trades
    mid = pm.get_midpoint(raw.yes_token_id())
    history = pm.get_price_history(raw.yes_token_id(), interval="1d")
    trades = list(pm.get_trades(max_items=100))
```

**Polymarket** — full read-only access with no API keys. Trading requires `POLYMARKET_PRIVATE_KEY`.

**Kalshi** — all endpoints require `KALSHI_API_KEY` + `KALSHI_PRIVATE_KEY_PATH` (RSA).

### Data Models

All data is normalized into shared models that work across exchanges:

- `Market` — price, volume, open interest, status, close time
- `Orderbook` — bids/asks with `best_bid`, `best_ask`, `mid`, `spread`, `depth()`
- `Trade` — price, size, side, timestamp
- `Position` / `Order` — portfolio tracking

## Project Structure

```
predx/
├── analytics/
│   └── discovery.py        # MarketScanner, movers, trending, to_df
├── clients/
│   ├── kalshi.py           # Kalshi REST API
│   └── polymarket.py       # Polymarket REST API (Gamma + CLOB + Data)
├── models/
│   └── common.py           # Shared data models (Market, Orderbook, Trade)
├── auth/                   # Exchange authentication
├── ws/                     # WebSocket clients
└── config.py               # Configuration
```

For example applications and strategies, see [predx-apps](https://github.com/achalps/predx-apps).

## Development

```bash
git clone https://github.com/achalps/predx.git
cd predx
pip install -e ".[dev]"
pytest tests/ -v
```

## Auth Setup

Only needed for trading / Kalshi access:

```bash
# .env file
POLYMARKET_PRIVATE_KEY=0x...    # For placing orders on Polymarket
POLYMARKET_FUNDER=0x...         # Optional, for proxy wallets
KALSHI_API_KEY=...              # For any Kalshi access
KALSHI_PRIVATE_KEY_PATH=...     # Path to RSA private key
```
