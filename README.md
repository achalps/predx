# predx

Python library and reward-farming market-making bot for Polymarket prediction markets, with Kalshi cross-venue support.

## Overview

The bot earns Polymarket liquidity rewards by quoting on both sides of NCAA March Madness game markets. It buys YES tokens on both outcomes (Team A YES + Team B YES), then merges them back into USDC to recycle capital.

### How rewards work
- Polymarket pays makers who keep resting limit orders within a `max_spread` of the midpoint
- Rewards follow a quadratic formula — tighter quotes earn exponentially more
- For most NCAA games: `max_spread = +/-1 cent`, `min_size = 1000 shares`
- Only the inside level earns meaningful rewards (1 tick back = 11% of max score)

## Project Structure

```
predx/
├── predx/                      # Core library (pip install -e .)
│   ├── auth/
│   │   ├── kalshi.py           # Kalshi RSA signer
│   │   └── polymarket.py       # Polymarket L1/L2 auth
│   ├── clients/
│   │   ├── base.py             # Base HTTP client
│   │   ├── kalshi.py           # Kalshi REST API
│   │   └── polymarket.py       # Polymarket CLOB REST API
│   ├── models/
│   │   ├── common.py           # Shared data models
│   │   ├── kalshi.py           # Kalshi-specific models
│   │   └── polymarket.py       # Polymarket-specific models
│   ├── ws/
│   │   ├── kalshi.py           # Kalshi WebSocket client
│   │   └── polymarket.py       # Polymarket WebSocket (book + price_change)
│   ├── tools/
│   │   ├── reward_farmer.py    # Main market-making bot
│   │   ├── fair_value.py       # Fair value: mid blend, microprice, OBI
│   │   └── live_dash.py        # Rich TUI dashboard
│   ├── config.py               # Configuration
│   └── exceptions.py           # Custom exceptions
├── farmer.ipynb                # Bot control panel notebook
├── analysis.ipynb              # Post-session analysis (markouts, timelines)
├── session_analysis.ipynb      # Detailed PnL analysis
├── approve_allowance.py        # On-chain token allowance approval
├── pyproject.toml              # Package config
└── .env                        # API keys (not committed)
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[poly-trading,dev]"
```

Create a `.env` file:
```
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_FUNDER=0x...
```

## Usage

The bot is controlled via `farmer.ipynb`:

1. **Discover markets** — queries Polymarket for reward-eligible games
2. **Select markets** — pick which games to quote on
3. **Start bot** — runs in a background thread
4. **Monitor** — check status, positions, open orders
5. **Adjust** — change OBI thresholds, position caps mid-run
6. **Stop** — graceful shutdown with order cancellation

## Bot Features

- **WS-driven orderbook** — real-time book construction from Polymarket WebSocket
- **Event-driven requoting** — cancel+replace on every book change (back-of-queue to minimize fills)
- **OBI gate** — price-adjusted Order Book Imbalance threshold (strict for favorites, loose for underdogs)
- **Momentum filter** — blocks buy if last N trades are all sells
- **Volatility regime filter** — pulls quotes during fast mid changes, resumes after 2s calm
- **Take-profit** — sell orders at avg cost + 1 tick after fill
- **Stop-loss** — tick-based, dumps at best bid
- **Position cap** — hard cap at 1500 shares per token
- **Skew management** — stops buying heavy side when imbalance exceeds threshold
- **Position sync** — reconciles with on-chain balances every 5 seconds
- **SQLite trade tape** — logs every fill with OBI, mid, and full OB snapshot
- **Stale order cleanup** — cancels orders >2 ticks from inside

## Known Issues

- **Order scoring API** — uses unauthenticated requests, needs L2 auth headers
- **Auto-merge** — relayer fails with `SafeMath: subtraction overflow` (tokens on main wallet, not Safe). Manual merge via Polymarket UI works
- **Cross-session cost basis** — only tracks fills from current session; needs CLOB trade history query on startup

---

## Changelog

### 2026-03-22 — Initial build

**Bot core (`reward_farmer.py`)**
- Built full market-making loop: WS orderbook → fair value → requote cycle
- Implemented both-sides BUY strategy (buy YES on both tokens, merge to recycle USDC)
- Added OBI gate with price-adjusted thresholds (0.30 for underdogs, 0.75 for favorites)
- Added momentum filter (blocks buy if last N trades all sells)
- Added volatility regime filter (pulls quotes on fast mid moves, 2s cooldown)
- Added take-profit sell orders (avg cost + 1 tick)
- Added stop-loss (tick-based, dumps at best bid)
- Added position cap (hard 1500), skew management
- Added 5-second on-chain position sync
- Added SQLite trade tape with full OB snapshot at fill time
- Added stale order cleanup (cancel if >2 ticks from inside)
- Added market discovery via `get_sampling_markets()` reward filter

**Library (`predx/`)**
- Built Polymarket REST client (place/cancel orders, balances, positions)
- Built Polymarket WebSocket client (book snapshots + price_change deltas)
- Built Kalshi REST client (auth, markets, orderbooks, positions)
- Built Kalshi WebSocket client
- Built fair value tools (mid blend, microprice, cross-venue arb, OBI)
- Built Rich TUI live dashboard

**Bug fixes**
- Fixed Kalshi `KalshiSigner` base64 encoding (urlsafe → standard)
- Fixed Kalshi status map ("active" vs "open")
- Fixed Kalshi price parsing (API switched to dollar strings)
- Fixed Kalshi orderbook key (`orderbook_fp`)
- Fixed Kalshi WS auth path doubling bug
- Fixed Polymarket `get_raw_market` 422 (wrong endpoint)
- Fixed Polymarket WS event type ("book" not "orderbook")
- Fixed `neg_risk` parameter in `OrderArgs`
- Fixed bot position tracking drift (added on-chain sync)
- Fixed skew threshold being overridden by rewards floor
- Fixed vol filter not cancelling stale orders during cooldown

**Analysis**
- Created `analysis.ipynb` (markouts, price+fills timeline, fill distributions)
- Created `session_analysis.ipynb` (PnL by market, OBI vs markout scatter, position over time)
- Key finding: cheap/underdog tokens have positive markouts; expensive/favorite tokens get adversely selected
- Key finding: main P&L drag is spread cost (~2.7 cents/pair vs $1.00 settle), not adverse selection
