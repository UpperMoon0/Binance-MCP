from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

Product = Literal["spot", "usds_futures", "coin_futures", "options"]

BASE_URLS: dict[str, str] = {
    "spot": "https://api.binance.com",
    "usds_futures": "https://fapi.binance.com",
    "coin_futures": "https://dapi.binance.com",
    "options": "https://eapi.binance.com",
}

ORDER_PATHS: dict[str, str] = {
    "spot": "/api/v3/order",
    "usds_futures": "/fapi/v1/order",
    "coin_futures": "/dapi/v1/order",
    "options": "/eapi/v1/order",
}

@dataclass(frozen=True, slots=True)
class BinanceConfig:
    api_key: str
    api_secret: str
    private_key_path: str
    private_key_passphrase: str
    trading_enabled: bool
    timeout_seconds: float
    recv_window_ms: int

    @classmethod
    def from_env(cls) -> "BinanceConfig":
        recv_window = int(os.getenv("BINANCE_RECV_WINDOW_MS", "5000"))
        if not 1 <= recv_window <= 60000:
            raise ValueError("BINANCE_RECV_WINDOW_MS must be between 1 and 60000")
        timeout = float(os.getenv("BINANCE_HTTP_TIMEOUT_SECONDS", "15"))
        if timeout <= 0 or timeout > 120:
            raise ValueError("BINANCE_HTTP_TIMEOUT_SECONDS must be >0 and <=120")
        return cls(
            api_key=os.getenv("BINANCE_API_KEY", "").strip(),
            api_secret=os.getenv("BINANCE_API_SECRET", "").strip(),
            private_key_path=os.getenv("BINANCE_PRIVATE_KEY_PATH", "").strip(),
            private_key_passphrase=os.getenv("BINANCE_PRIVATE_KEY_PASSPHRASE", ""),
            trading_enabled=os.getenv("BINANCE_TRADING_ENABLED", "false").lower() == "true",
            timeout_seconds=timeout,
            recv_window_ms=recv_window,
        )
