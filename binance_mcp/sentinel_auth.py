from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx


class SentinelAuthError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SentinelIdentity:
    account_id: int
    email: str


@dataclass(frozen=True, slots=True)
class SentinelConfig:
    base_url: str
    allowed_account_ids: frozenset[int]
    timeout_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> "SentinelConfig":
        base_url = os.getenv("MCP_SENTINEL_URL", "").strip().rstrip("/")
        if not base_url:
            raise SentinelAuthError(503, "Sentinel authentication is not configured")
        raw_ids = os.getenv("MCP_SENTINEL_ALLOWED_ACCOUNT_IDS", "").strip()
        if not raw_ids:
            raise SentinelAuthError(503, "Sentinel account allowlist is not configured")
        try:
            allowed = frozenset(int(value.strip()) for value in raw_ids.split(",") if value.strip())
        except ValueError as exc:
            raise SentinelAuthError(503, "Sentinel account allowlist is invalid") from exc
        if not allowed or any(value <= 0 for value in allowed):
            raise SentinelAuthError(503, "Sentinel account allowlist is invalid")
        try:
            timeout = float(os.getenv("MCP_SENTINEL_TIMEOUT_SECONDS", "5").strip() or "5")
        except ValueError as exc:
            raise SentinelAuthError(503, "Sentinel timeout configuration is invalid") from exc
        if timeout <= 0 or timeout > 30:
            raise SentinelAuthError(503, "Sentinel timeout configuration is invalid")
        return cls(base_url=base_url, allowed_account_ids=allowed, timeout_seconds=timeout)


class SentinelClient:
    def __init__(self, config: SentinelConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.transport = transport

    async def authenticate(self, email: str, password: str) -> SentinelIdentity:
        email = email.strip().lower()
        if not email or not password:
            raise SentinelAuthError(400, "email and password are required")
        if len(email) > 320 or len(password) > 4096:
            raise SentinelAuthError(400, "credentials are too long")

        try:
            async with httpx.AsyncClient(
                base_url=self.config.base_url,
                timeout=self.config.timeout_seconds,
                transport=self.transport,
                trust_env=False,
            ) as client:
                response = await client.post("/auth/login", json={"email": email, "password": password})
                if response.status_code in {401, 403}:
                    raise SentinelAuthError(401, "invalid email or password")
                if response.status_code < 200 or response.status_code >= 300:
                    raise SentinelAuthError(502, "Sentinel authentication is unavailable")
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise SentinelAuthError(502, "Sentinel returned an invalid login response") from exc
                if not isinstance(payload, dict):
                    raise SentinelAuthError(502, "Sentinel returned an invalid login response")

                account_id = self._account_id(payload)
                token = str(payload.get("token") or "")
                response_email = str(payload.get("email") or "").strip().lower()
                if not token or not response_email:
                    await self._logout(client, token, account_id)
                    raise SentinelAuthError(502, "Sentinel returned an incomplete login response")

                try:
                    validation = await client.get(
                        "/auth/validate",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    if validation.status_code != 200:
                        raise SentinelAuthError(502, "Sentinel could not validate the login token")
                    try:
                        validation_payload = validation.json()
                    except ValueError as exc:
                        raise SentinelAuthError(502, "Sentinel could not validate the login token") from exc
                    if not isinstance(validation_payload, dict) or validation_payload.get("valid") is not True:
                        raise SentinelAuthError(502, "Sentinel could not validate the login token")
                    try:
                        validated_id = int(validation_payload.get("accountId"))
                    except (TypeError, ValueError) as exc:
                        raise SentinelAuthError(502, "Sentinel login identity mismatch") from exc
                    validated_email = str(validation_payload.get("email") or "").strip().lower()
                    if validated_id != account_id or validated_email != response_email:
                        raise SentinelAuthError(502, "Sentinel login identity mismatch")
                    if account_id not in self.config.allowed_account_ids:
                        raise SentinelAuthError(403, "this Sentinel account is not allowed to access Binance MCP")
                    return SentinelIdentity(account_id=account_id, email=response_email)
                finally:
                    # Binance MCP uses its own short-lived HttpOnly UI session after Sentinel
                    # verifies the credentials. Revoke the temporary Sentinel refresh token so a
                    # viewer login does not leave another long-lived credential behind.
                    await self._logout(client, token, account_id)
        except SentinelAuthError:
            raise
        except httpx.HTTPError as exc:
            raise SentinelAuthError(502, "Sentinel authentication is unavailable") from exc

    @staticmethod
    def _account_id(payload: dict[str, Any]) -> int:
        try:
            account_id = int(payload.get("id"))
        except (TypeError, ValueError) as exc:
            raise SentinelAuthError(502, "Sentinel returned an invalid account id") from exc
        if account_id <= 0:
            raise SentinelAuthError(502, "Sentinel returned an invalid account id")
        return account_id

    @staticmethod
    async def _logout(client: httpx.AsyncClient, token: str, account_id: int) -> None:
        if not token or account_id <= 0:
            return
        try:
            await client.post(
                "/auth/logout",
                json={"accountId": account_id},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError:
            # Login succeeded and refresh-token cleanup is best-effort. The access token is still
            # bounded by Sentinel's own expiration and is never stored by Binance MCP.
            pass


async def authenticate_from_env(email: str, password: str) -> SentinelIdentity:
    return await SentinelClient(SentinelConfig.from_env()).authenticate(email, password)
