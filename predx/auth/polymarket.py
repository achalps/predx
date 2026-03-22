from __future__ import annotations
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from py_clob_client.client import ClobClient


class PolymarketAuth:
    """
    Wraps py_clob_client for Polymarket order signing.
    Only instantiated when a private_key is configured.
    Read-only research workflows (Gamma + CLOB reads) need no auth.
    """

    def __init__(
        self,
        private_key: str,
        clob_url: str,
        chain_id: int = 137,
        signature_type: int = 1,
        funder: Optional[str] = None,
    ):
        self._private_key = private_key
        self._clob_url = clob_url
        self._chain_id = chain_id
        self._signature_type = signature_type
        self._funder = funder
        self._client: Optional["ClobClient"] = None

    def _get_client(self) -> "ClobClient":
        if self._client is None:
            try:
                from py_clob_client.client import ClobClient
            except ImportError:
                raise ImportError(
                    "Polymarket trading requires py_clob_client: "
                    "pip install 'predx[poly-trading]'"
                )
            self._client = ClobClient(
                self._clob_url,
                key=self._private_key,
                chain_id=self._chain_id,
                signature_type=self._signature_type,
                funder=self._funder,
            )
            self._client.set_api_creds(self._client.create_or_derive_api_creds())
        return self._client

    @property
    def clob_client(self) -> "ClobClient":
        return self._get_client()
