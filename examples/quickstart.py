"""
predx quickstart — scan Polymarket, find movers, and export to pandas.
No API keys needed.
"""
from predx.analytics import MarketScanner, to_df

scanner = MarketScanner()

# ── 1. Scan top markets by volume ──
print("=== Top 5 Markets by 24h Volume ===\n")
markets = scanner.scan(limit=5, sort_by="volume")
for m in markets:
    print(f"  ${m.volume_24h:>12,.0f}  {m.question[:60]}")

# ── 2. Find biggest movers (price changes) ──
print("\n=== Biggest Movers ===\n")
movers = scanner.movers(limit=5)
for m in movers:
    change = m.price_change_1d or 0
    print(f"  {change:>+6.2f}  {m.best_ask:.2f}  {m.question[:55]}")

# ── 3. Trending (new markets gaining traction) ──
print("\n=== Trending Markets ===\n")
trending = scanner.trending(limit=5)
for m in trending:
    hours = m.hours_to_expiry
    expiry = f"{hours:.0f}h" if hours and hours < 168 else "long"
    print(f"  ${m.volume_24h:>10,.0f}  [{expiry:>5}]  {m.question[:50]}")

# ── 4. Export to DataFrame ──
print("\n=== DataFrame Preview ===\n")
df = to_df(markets)
print(df[["question", "volume_24h", "best_bid", "best_ask", "spread"]].to_string(index=False))
