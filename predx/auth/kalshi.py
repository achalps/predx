from __future__ import annotations
import base64
import time
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class KalshiSigner:
    """
    RSA-PSS signer for Kalshi API authentication.

    Signature payload: f"{timestamp_ms}{METHOD}{/trade-api/v2}{path}"
    Salt length: SHA-256 digest size (32 bytes) — must match Kalshi's DIGEST_LENGTH spec.
    Key is lazy-loaded on first sign() call.
    """

    def __init__(self, api_key: str, private_key_path: Path):
        self.api_key = api_key
        self._key_path = Path(private_key_path)
        self._private_key = None

    def _load_key(self) -> None:
        if self._private_key is None:
            pem = self._key_path.read_bytes()
            self._private_key = serialization.load_pem_private_key(pem, password=None)

    def sign(self, method: str, path: str) -> dict[str, str]:
        """
        Generate Kalshi auth headers for a single request.
        Call this immediately before each request — timestamp is request-specific.

        Args:
            method: HTTP method ("GET", "POST", "DELETE")
            path: Path without /trade-api/v2 prefix (e.g. "/markets", "/portfolio/orders").
                  To sign a full path as-is (e.g. for WebSocket "/trade-api/ws/v2"),
                  use sign_full_path() instead.

        Returns:
            Dict of headers to merge into the request.
        """
        return self.sign_full_path(method, f"/trade-api/v2{path}")

    def sign_full_path(self, method: str, full_path: str) -> dict[str, str]:
        """
        Generate Kalshi auth headers signing the given full_path directly (no prefix added).
        Use this for WebSocket auth where the path is /trade-api/ws/v2.
        """
        self._load_key()
        timestamp_ms = str(int(time.time() * 1000))
        payload = f"{timestamp_ms}{method.upper()}{full_path}".encode()

        signature = self._private_key.sign(
            payload,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256.digest_size,  # 32 bytes, NOT PSS.MAX_LENGTH
            ),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(signature).decode()

        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
        }
