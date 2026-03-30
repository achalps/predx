from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class KalshiConfig:
    api_key: Optional[str] = field(default_factory=lambda: os.environ.get("KALSHI_API_KEY"))
    private_key_path: Optional[Path] = field(
        default_factory=lambda: Path(p) if (p := os.environ.get("KALSHI_PRIVATE_KEY_PATH")) else None
    )
    base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    timeout: float = 10.0
    max_retries: int = 3

    @classmethod
    def with_defaults(cls) -> "KalshiConfig":
        """Create config, pulling from env vars if available but not requiring them."""
        return cls()


@dataclass
class PolymarketConfig:
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    data_url: str = "https://data-api.polymarket.com"
    private_key: Optional[str] = field(
        default_factory=lambda: os.environ.get("POLYMARKET_PRIVATE_KEY")
    )
    funder: Optional[str] = field(
        default_factory=lambda: os.environ.get("POLYMARKET_FUNDER")
    )
    signature_type: int = 1         # 0=EOA, 1=email/Magic wallet, 2=browser proxy
    chain_id: int = 137             # Polygon mainnet
    timeout: float = 10.0



def load_dotenv_if_present() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
