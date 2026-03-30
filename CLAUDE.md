# CLAUDE.md

## Project Vision

predx is a Python toolkit for prediction market traders. It provides easy access to Polymarket and Kalshi data and trading APIs.

## Design Philosophy

- **API access over computation** — predx should make it easy to fetch, search, and batch market data from exchanges. Don't build analytics/computation layers — users can do their own math. Focus on making the raw data easy to get.
- **No API keys needed for reads** — read-only access should work without authentication on both exchanges.
- **Lightweight** — minimal dependencies, no persistence layer, no storage.

## Historical / Closed Market Data — Key Constraints

These findings are from live API testing (March 2026):

### Polymarket
- **Price history** (CLOB API `/prices-history`): ~30 day retention. Active markets get 670+ hourly points. Old closed markets return empty.
- **Trade history** (Data API `/trades`): More durable than price history for closed markets. Recently closed markets keep their trades.
- **Recurring crypto markets**: "Bitcoin/Ethereum above X on DATE, TIME ET?" — ~140 markets per time slot (70 BTC + 70 ETH), ~5 slots/day. Grouped by event slug: `{asset}-above-on-{date}-{time}-et`. Low individual volume ($100-$2K).
- **MarketScanner** is hardcoded to `active=True` — cannot find closed markets. Use `PolymarketClient.get_markets(closed=True)` directly.
- **Gamma API `slug_contains`** is client-side filtering only (line 80-81 of `polymarket.py`).
- **Gamma API `active` flag**: Recently resolved markets have `active=True` AND `closed=True`. Don't filter `active=False` when looking for closed markets — use `active=None` to skip the filter.
- **Gamma API default ordering** returns oldest markets first. Always pass `order=createdAt` descending to get recent results.

### Kalshi
- **Reads don't require auth** (contrary to what predx currently assumes in the client). The Kalshi REST API returns data for unauthenticated requests.
- **Series tickers** for crypto: `KXBTC`, `KXETH`, `KXBTCD`
- **No dedicated price history endpoint** — must reconstruct from trades.
- **Crypto volume is near-zero** on Kalshi vs Polymarket.

## Development Notes

- Tests in `tests/` — some hit live APIs (smoke tests), some use synthetic data.
- Dependencies: httpx, cryptography, websockets, python-dotenv, pandas.
