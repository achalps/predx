"""
predx quickstart — scan, analyze, and export prediction market data.
No API keys needed.
"""
from predx import PolymarketClient
from predx.analytics import MarketScanner, to_df
from predx.analytics.execution import analyze, slippage_curve

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

# ── 5. Execution analytics on the top market ──
print("\n=== Execution Analytics ===\n")
top = markets[0]
pm = PolymarketClient()
raw = pm.get_raw_market(top.condition_id)
ob = pm.get_orderbook(raw.yes_token_id())
pm.close()

report = analyze(ob)
print(report)

print(f"\n{'Size':>8}  {'Avg Price':>9}  {'Slippage':>8}  {'Fillable':>8}")
for pt in slippage_curve(ob, side="buy", sizes=[100, 1000, 5000]):
    print(f"{pt.size:>8,.0f}  {pt.avg_price:>9.4f}  {pt.slippage_bps:>7.0f}bp  {str(pt.fillable):>8}")
