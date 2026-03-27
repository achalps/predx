"""Smoke tests for Polymarket public read-only endpoints (no API keys needed)."""
import pytest
from predx import PolymarketClient, Market, Orderbook, Exchange, MarketStatus


@pytest.fixture(scope="module")
def pm():
    with PolymarketClient() as client:
        yield client


@pytest.fixture(scope="module")
def active_market(pm):
    """Grab one active market for downstream tests."""
    markets = list(pm.get_markets(active=True, max_items=3))
    assert markets, "No active markets found on Polymarket"
    return markets[0]


@pytest.fixture(scope="module")
def token_id(pm, active_market):
    """Resolve a YES token ID from an active market."""
    raw = pm.get_raw_market(active_market.id)
    tid = raw.yes_token_id()
    assert tid, f"No YES token ID for market {active_market.id}"
    return tid


# -- Market discovery (Gamma API) --

def test_get_markets(pm):
    markets = list(pm.get_markets(active=True, max_items=5))
    assert len(markets) >= 1
    m = markets[0]
    assert isinstance(m, Market)
    assert m.exchange == Exchange.POLYMARKET
    assert m.title


def test_get_market(pm, active_market):
    m = pm.get_market(active_market.id)
    assert isinstance(m, Market)
    assert m.id == active_market.id


def test_get_raw_market(pm, active_market):
    raw = pm.get_raw_market(active_market.id)
    assert raw.condition_id == active_market.id
    assert raw.yes_token_id()


def test_search_events(pm):
    events = pm.search_events(active=True, limit=3)
    assert isinstance(events, list)
    assert len(events) >= 1


# -- Orderbook & Prices (CLOB API) --

def test_get_orderbook(pm, token_id):
    ob = pm.get_orderbook(token_id)
    assert isinstance(ob, Orderbook)
    assert ob.exchange == Exchange.POLYMARKET
    assert ob.market_id == token_id


def test_get_midpoint(pm, token_id):
    mid = pm.get_midpoint(token_id)
    if mid is not None:
        assert 0.0 <= mid <= 1.0


def test_get_best_price(pm, token_id):
    price = pm.get_best_price(token_id, side="buy")
    if price is not None:
        assert 0.0 <= price <= 1.0


def test_get_price_history(pm, token_id):
    history = pm.get_price_history(token_id, interval="1d")
    assert isinstance(history, list)


# -- Trade history (Data API) --

def test_get_trades(pm):
    trades = list(pm.get_trades(max_items=5))
    assert isinstance(trades, list)
