from __future__ import annotations

import base64
import hashlib
import hmac
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from .config import BASE_URLS, BinanceConfig, Product

Scalar = str | int | float | bool

class BinanceClientError(RuntimeError):
    pass

class BinanceClient:
    def __init__(self, config: BinanceConfig | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config or BinanceConfig.from_env()
        self._transport = transport
        self._private_key: Any | None = None

    @staticmethod
    def _validate_path(path: str) -> str:
        path = path.strip()
        parsed = urlsplit(path)
        if not path.startswith("/") or parsed.scheme or parsed.netloc or parsed.fragment:
            raise BinanceClientError("path must be an absolute Binance API path such as /api/v3/ticker/price")
        if ".." in parsed.path.split("/"):
            raise BinanceClientError("path traversal is not allowed")
        if parsed.query:
            raise BinanceClientError("put query parameters in params, not in path")
        return parsed.path

    def auth_status(self) -> dict[str, Any]:
        signer = "none"
        if self.config.private_key_path:
            signer = "asymmetric"
        elif self.config.api_secret:
            signer = "hmac"
        return {
            "api_key_configured": bool(self.config.api_key),
            "signer": signer,
            "account_access_ready": bool(self.config.api_key and (self.config.api_secret or self.config.private_key_path)),
            "trading_enabled": self.config.trading_enabled,
            "withdrawals_supported": False,
        }

    async def public_get(self, product: Product, path: str, params: dict[str, Scalar] | None = None) -> Any:
        url = BASE_URLS[product] + self._validate_path(path)
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds, transport=self._transport) as client:
            response = await client.get(url, params=params or {})
        return self._decode(response)

    async def signed_get(self, product: Product, path: str, params: dict[str, Scalar] | None = None) -> Any:
        return await self._signed_request("GET", product, path, params or {})

    async def order(self, product: Product, path: str, action: str, params: dict[str, Scalar]) -> Any:
        if not self.config.trading_enabled:
            raise BinanceClientError("trading is disabled by deployment policy")
        method = {"create": "POST", "cancel": "DELETE"}.get(action)
        if not method:
            raise BinanceClientError("action must be create or cancel")
        return await self._signed_request(method, product, path, params)

    async def _signed_request(self, method: str, product: Product, path: str, params: dict[str, Scalar]) -> Any:
        if not self.config.api_key:
            raise BinanceClientError("BINANCE_API_KEY is not configured")
        if not (self.config.api_secret or self.config.private_key_path):
            raise BinanceClientError("no Binance signing credential is configured")
        signed_params = dict(params)
        signed_params.setdefault("recvWindow", self.config.recv_window_ms)
        signed_params.setdefault("timestamp", int(time.time() * 1000))
        payload = urlencode(signed_params, encoding="utf-8", safe="")
        signature = self._sign(payload.encode("ascii"))
        url = BASE_URLS[product] + self._validate_path(path)
        signed_url = f"{url}?{payload}&signature={quote(signature, safe='')}"
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds, transport=self._transport) as client:
            response = await client.request(method, signed_url, headers={"X-MBX-APIKEY": self.config.api_key})
        return self._decode(response)

    def _sign(self, payload: bytes) -> str:
        if self.config.private_key_path:
            key = self._load_private_key()
            if isinstance(key, ed25519.Ed25519PrivateKey):
                raw = key.sign(payload)
            elif isinstance(key, rsa.RSAPrivateKey):
                raw = key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
            else:
                raise BinanceClientError("private key must be Ed25519 or RSA")
            return base64.b64encode(raw).decode("ascii")
        return hmac.new(self.config.api_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    def _load_private_key(self) -> Any:
        if self._private_key is not None:
            return self._private_key
        try:
            raw = Path(self.config.private_key_path).read_bytes()
            password = self.config.private_key_passphrase.encode() if self.config.private_key_passphrase else None
            self._private_key = serialization.load_pem_private_key(raw, password=password)
        except (OSError, TypeError, ValueError) as exc:
            raise BinanceClientError("could not load Binance private key") from exc
        return self._private_key

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        try:
            data = response.json()
        except ValueError:
            data = {"text": response.text}
        if response.is_error:
            raise BinanceClientError(f"Binance HTTP {response.status_code}: {data}")
        return data
