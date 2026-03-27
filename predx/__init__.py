from predx._version import __version__
from predx.config import KalshiConfig, PolymarketConfig, load_dotenv_if_present
from predx.clients.kalshi import KalshiClient
from predx.clients.polymarket import PolymarketClient
from predx.models.common import Market, Orderbook, Trade, Position, Order, Exchange, MarketStatus
from predx.exceptions import PredxError, AuthError, RateLimitError, NotFoundError

load_dotenv_if_present()

__all__ = [
    # Clients
    "KalshiClient",
    "PolymarketClient",
    # Config
    "KalshiConfig",
    "PolymarketConfig",
    # Models
    "Market",
    "Orderbook",
    "Trade",
    "Position",
    "Order",
    "Exchange",
    "MarketStatus",
    # Exceptions
    "PredxError",
    "AuthError",
    "RateLimitError",
    "NotFoundError",
    # Version
    "__version__",
]
