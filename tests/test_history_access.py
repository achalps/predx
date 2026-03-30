"""Smoke tests for historical market access (no API keys needed)."""
import pytest
from predx import PolymarketClient, KalshiClient, Market
from predx.models.polymarket import PolymarketMarket


@pytest.fixture(scope="module")
def pm():
    with PolymarketClient() as client:
        yield client


@pytest.fixture(scope="module")
def active_market(pm):
    markets = list(pm.get_markets(active=True, max_items=3))
    assert markets, "No active markets found"
    return markets[0]


# -- Polymarket: get_closed_markets --

def test_get_closed_markets(pm):
    closed = list(pm.get_closed_markets(max_items=5))
    assert len(closed) >= 1
    assert all(isinstance(m, Market) for m in closed)


# -- Polymarket: get_event_markets --

def test_get_event_markets_returns_list(pm):
    # Try a few events to find one with a valid slug
    events = pm.search_events(active=True, limit=10)
    if not events:
        pytest.skip("No events found")
    for event in events:
        slug = event.get("slug")
        if not slug:
            continue
        try:
            markets = pm.get_event_markets(slug)
        except Exception:
            continue
        assert isinstance(markets, list)
        if markets:
            assert all(isinstance(m, PolymarketMarket) for m in markets)
        return
    pytest.skip("No valid event slugs found")


# -- Polymarket: get_market_history --

def test_get_market_history(pm, active_market):
    data = pm.get_market_history(active_market.id, include_trades=True, max_trades=5)
    assert "market" in data
    assert "price_history" in data
    assert "trades" in data
    assert isinstance(data["market"], PolymarketMarket)
    assert isinstance(data["price_history"], list)
    assert isinstance(data["trades"], list)


def test_get_market_history_no_trades(pm, active_market):
    data = pm.get_market_history(active_market.id, include_trades=False)
    assert "market" in data
    assert "price_history" in data
    assert "trades" not in data


# -- Polymarket: batch_histories --

def test_batch_histories(pm):
    markets = list(pm.get_markets(active=True, max_items=3))
    assert markets
    ids = [m.id for m in markets]
    results = pm.batch_histories(ids, include_trades=False)
    assert isinstance(results, list)
    assert len(results) >= 1
    for r in results:
        assert "market_id" in r
        assert "market" in r
        assert "price_history" in r


def test_batch_histories_skips_invalid(pm):
    results = pm.batch_histories(["invalid-condition-id-xyz"])
    assert results == []


# -- Kalshi: reads without auth --

def test_kalshi_client_no_auth():
    """KalshiClient should instantiate without API keys."""
    k = KalshiClient()
    k.close()


def test_kalshi_get_markets_no_auth():
    with KalshiClient() as k:
        markets = list(k.get_markets(max_items=3))
        assert isinstance(markets, list)
        if markets:
            assert isinstance(markets[0], Market)


def test_kalshi_get_trades_no_auth():
    with KalshiClient() as k:
        trades = list(k.get_trades(max_items=3))
        assert isinstance(trades, list)


def test_kalshi_portfolio_requires_auth():
    with KalshiClient() as k:
        with pytest.raises(ValueError, match="Auth required"):
            k.get_balance()
