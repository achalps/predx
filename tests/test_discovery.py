"""Smoke tests for MarketScanner (live Polymarket APIs, no auth needed)."""
import pytest
from predx.analytics.discovery import MarketScanner, MarketSnapshot, ScanFilters


@pytest.fixture(scope="module")
def scanner():
    with MarketScanner() as s:
        yield s


@pytest.fixture(scope="module")
def all_markets(scanner):
    """Fetch a baseline set of active markets."""
    return scanner.scan(limit=20)


# -- Basic scan --

def test_scan_returns_markets(all_markets):
    assert len(all_markets) >= 1
    assert all(isinstance(m, MarketSnapshot) for m in all_markets)


def test_market_fields_populated(all_markets):
    m = all_markets[0]
    assert m.condition_id
    assert m.question
    assert m.active is True
    assert m.volume_total > 0


def test_sorted_by_volume_24h(scanner):
    markets = scanner.scan(sort_by="volume_24h", limit=10)
    vols = [m.volume_24h for m in markets]
    assert vols == sorted(vols, reverse=True)


# -- Filters --

def test_filter_min_volume_24h(scanner):
    markets = scanner.scan(min_volume_24h=50_000, limit=10)
    assert all(m.volume_24h >= 50_000 for m in markets)


def test_filter_min_liquidity(scanner):
    markets = scanner.scan(min_liquidity=100_000, limit=10)
    assert all(m.liquidity >= 100_000 for m in markets)


def test_filter_max_spread(scanner):
    markets = scanner.scan(max_spread=0.03, limit=20)
    assert all(m.spread <= 0.03 for m in markets)


def test_filter_rewards_only(scanner):
    markets = scanner.scan(rewards_only=True, limit=10)
    if markets:  # rewards may not always be available
        assert all(m.has_rewards for m in markets)


# -- Properties --

def test_midpoint_property(all_markets):
    for m in all_markets:
        if m.best_bid > 0 and m.best_ask > 0:
            assert 0 < m.midpoint < 1
            break


def test_hours_to_expiry(all_markets):
    for m in all_markets:
        if m.end_date is not None:
            hours = m.hours_to_expiry
            assert hours is not None
            assert hours >= 0
            break


def test_spread_bps(all_markets):
    for m in all_markets:
        if m.spread > 0 and m.midpoint > 0:
            assert m.spread_bps > 0
            break


# -- Enrich --

def test_enrich_adds_depth(scanner, all_markets):
    subset = all_markets[:2]
    scanner.enrich(subset)
    enriched = [m for m in subset if m.orderbook_depth is not None]
    assert len(enriched) >= 1
    d = enriched[0].orderbook_depth
    assert "bid_volume" in d
    assert "ask_volume" in d
    assert "imbalance" in d
    assert -1 <= d["imbalance"] <= 1


# -- Sort options --

def test_sort_by_liquidity(scanner):
    markets = scanner.scan(sort_by="liquidity", limit=10)
    liqs = [m.liquidity for m in markets]
    assert liqs == sorted(liqs, reverse=True)
