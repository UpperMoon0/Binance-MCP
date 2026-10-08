from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from .config import BASE_URLS, BinanceConfig, Product

# HTTPX's INFO request log includes the complete URL. Signed Binance endpoints
# carry the signature in that URL, so never allow the transport logger to emit
# those request lines. Errors are still surfaced through BinanceClientError.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

Scalar = str | int | float | bool


class BinanceClientError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: int | None = None,
                 retry_after: float | None = None, outcome_unknown: bool = False,
                 headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.outcome_unknown = outcome_unknown
        self.headers = headers or {}

    @property
    def definitive_rejection(self) -> bool:
        # Only authoritative exchange validation/permission rejections release capital.
        # Gateway 4xx, 408, rate-limit responses without a Binance code and malformed
        # responses remain uncertain once a mutation may have reached Binance.
        codes = {-1013, -1021, -1022, -1100, -1101, -1102, -1103, -1104, -1105, -1106,
                 -1111, -1112, -1114, -1115, -1116, -1117, -1118, -1119, -1120, -1121,
                 -1130, -2010, -2014, -2015}
        return self.status in (400, 401, 403) and self.code in codes and not self.outcome_unknown

    def metadata(self) -> dict[str, Any]:
        return {"message": str(self), "status": self.status, "code": self.code,
                "retryAfter": self.retry_after, "outcomeUnknown": self.outcome_unknown,
                "rateLimitHeaders": self.headers}



class BinanceClient:
    def __init__(self, config: BinanceConfig | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config or BinanceConfig.from_env()
        self._transport = transport
        self._private_key: Any | None = None
        self._http: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(4)
        self._offsets: dict[str, int] = {}
        self._blocked_until = 0.0
        self.rate_limit_headers: dict[str, str] = {}
        self._cache: dict[str, tuple[float, Any]] = {}

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        async with self._semaphore:
            if time.monotonic() < self._blocked_until:
                raise BinanceClientError("Binance rate-limit cooldown active", status=429,
                                         retry_after=self._blocked_until - time.monotonic())
            if self._http is None:
                self._http = httpx.AsyncClient(timeout=self.config.timeout_seconds, transport=self._transport,
                                               follow_redirects=False)
            try:
                response = await self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                # Never include the signed request URL in errors or logs.
                raise BinanceClientError("Binance transport failure", outcome_unknown=method != "GET") from None
            self.rate_limit_headers = {k: v for k, v in response.headers.items()
                                       if k.startswith("x-mbx-") or k == "retry-after"}
            try:
                return self._decode(response)
            except BinanceClientError as exc:
                if exc.status in (418, 429):
                    self._blocked_until = time.monotonic() + (exc.retry_after or 60)
                raise

    async def sync_time(self, product: Product = "spot") -> int:
        path = {"spot": "/api/v3/time", "usds_futures": "/fapi/v1/time",
                "coin_futures": "/dapi/v1/time"}.get(product)
        if path is None:
            raise BinanceClientError("clock synchronization unsupported for this product")
        before = int(time.time() * 1000)
        result = await self.public_get(product, path)
        self._offsets[product] = int(result["serverTime"]) - (before + int(time.time() * 1000)) // 2
        return self._offsets[product]


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

    @staticmethod
    def _normalize_scalar(value: Scalar) -> str | int | float:
        if isinstance(value, bool):
            return "true" if value else "false"
        return value

    @classmethod
    def _normalize_params(cls, params: dict[str, Scalar]) -> dict[str, str | int | float]:
        return {key: cls._normalize_scalar(value) for key, value in params.items()}

    def _require_trading(self) -> None:
        if not self.config.trading_enabled:
            raise BinanceClientError("trading is disabled by deployment policy")

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
        normalized = self._normalize_params(params or {})
        key = url + "?" + urlencode(normalized)
        # Only public, slowly changing metadata is cached. Preflight bypasses it.
        if path.endswith("/exchangeInfo") and key in self._cache:
            expiry, value = self._cache[key]
            if time.monotonic() < expiry:
                return value
        value = await self._request("GET", url, params=normalized)
        if path.endswith("/exchangeInfo"):
            self._cache[key] = (time.monotonic() + 30, value)
        return value

    async def signed_get(self, product: Product, path: str, params: dict[str, Scalar] | None = None) -> Any:
        return await self._signed_request("GET", product, path, params or {})

    async def order(self, product: Product, path: str, action: str, params: dict[str, Scalar]) -> Any:
        self._require_trading()
        method = {"create": "POST", "cancel": "DELETE"}.get(action)
        if not method:
            raise BinanceClientError("action must be create or cancel")
        return await self._signed_request(method, product, path, params)

    async def simple_earn_subscribe(
        self,
        product_id: str,
        amount: str,
        auto_subscribe: bool = True,
        source_account: str = "SPOT",
    ) -> Any:
        self._require_trading()
        return await self._signed_request(
            "POST",
            "spot",
            "/sapi/v1/simple-earn/flexible/subscribe",
            {
                "productId": product_id,
                "amount": amount,
                "autoSubscribe": auto_subscribe,
                "sourceAccount": source_account,
            },
        )

    async def simple_earn_redeem(self, product_id: str, amount: str, dest_account: str = "SPOT") -> Any:
        self._require_trading()
        return await self._signed_request(
            "POST",
            "spot",
            "/sapi/v1/simple-earn/flexible/redeem",
            {"productId": product_id, "amount": amount, "destAccount": dest_account},
        )

    async def dual_investment_subscribe(
        self,
        product_id: str,
        order_id: int,
        deposit_amount: str,
        auto_compound_plan: str = "NONE",
    ) -> Any:
        self._require_trading()
        return await self._signed_request(
            "POST",
            "spot",
            "/sapi/v1/dci/product/subscribe",
            {
                "id": product_id,
                "orderId": order_id,
                "depositAmount": deposit_amount,
                "autoCompoundPlan": auto_compound_plan,
            },
        )

    async def _signed_request(self, method: str, product: Product, path: str, params: dict[str, Scalar]) -> Any:
        if not self.config.api_key:
            raise BinanceClientError("BINANCE_API_KEY is not configured")
        if not (self.config.api_secret or self.config.private_key_path):
            raise BinanceClientError("no Binance signing credential is configured")
        signed_params = self._normalize_params(dict(params))
        signed_params.setdefault("recvWindow", self.config.recv_window_ms)
        signed_params.setdefault("timestamp", int(time.time() * 1000) + self._offsets.get(product, 0))
        payload = urlencode(signed_params, encoding="utf-8", safe="")
        signature = self._sign(payload.encode("ascii"))
        url = BASE_URLS[product] + self._validate_path(path)
        signed_url = f"{url}?{payload}&signature={quote(signature, safe='')}"
        return await self._request(method, signed_url, headers={"X-MBX-APIKEY": self.config.api_key})

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
            code = data.get("code") if isinstance(data, dict) else None
            headers = {k: v for k, v in response.headers.items() if k.startswith("x-mbx-") or k == "retry-after"}
            try:
                retry_after = float(response.headers["retry-after"])
            except (KeyError, ValueError):
                retry_after = None
            # Binance error messages can reflect submitted parameters. Expose code,
            # status and a stable safe message rather than signed URLs or raw bodies.
            raise BinanceClientError(f"Binance HTTP {response.status_code} (code {code})",
                                     status=response.status_code, code=code, headers=headers,
                                     retry_after=retry_after,
                                     outcome_unknown=response.status_code >= 500 or response.status_code in (408, 409)
                                     or code in (-1006, -1007))
        return data
