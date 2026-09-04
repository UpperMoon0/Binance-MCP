# Binance MCP

A security-first Model Context Protocol server for Binance.

It provides one compact surface instead of mirroring hundreds of Binance endpoints as individual MCP tools:

- `binance_public_request` — GET any public Spot, USDⓈ-M Futures, COIN-M Futures, or Options REST path.
- `binance_account_request` — signed **GET-only** account access using credentials stored on the server.
- `binance_order_request` — standard create/cancel order operations, disabled unless the deployment explicitly enables trading.
- `binance_auth_status` — redacted capability status. It never returns secrets.

The generic public request tool keeps pace with Binance adding new market-data endpoints without forcing a new MCP schema for every endpoint.

## Security model

There are two independent authentication boundaries:

1. **MCP client → Binance MCP** uses the owner-approved OAuth model from the NsTut MCP stack: authorization code flow, PKCE S256, dynamic client registration, short-lived access tokens, rotating refresh tokens, persisted hashed grants, strict redirect validation, and fail-closed MCP access.
2. **Binance MCP → Binance** uses a Binance API key plus HMAC secret, Ed25519 private key, or RSA private key. Credentials stay server-side and are never returned by tools.

Binance Login OAuth2 is not used for ordinary self-hosting because Binance currently limits that program to approved ecosystem partners. If Binance broadens access later, it can be added as another credential provider without changing the MCP OAuth boundary.

### Safe defaults

- MCP refuses protected requests when OAuth is not configured.
- Account access is GET-only.
- Trading is disabled unless `BINANCE_TRADING_ENABLED=true` is set by the deployment owner.
- Withdrawals are intentionally unsupported.
- Full URLs are rejected. Requests are pinned to known Binance API hosts.
- `recvWindow` defaults to 5000 ms and may not exceed 60000 ms.
- Non-ASCII request parameters are percent-encoded before signing, matching current Binance signed-endpoint requirements.
- Secret/key files are excluded by `.gitignore` and `.dockerignore`.

For account keys, prefer a dedicated Binance API key with only the permissions you need and IP restrictions where practical. Binance recommends Ed25519 keys for performance and security.

## Supported API hosts

| Product | Base URL |
| --- | --- |
| Spot and SAPI | `https://api.binance.com` |
| USDⓈ-M Futures | `https://fapi.binance.com` |
| COIN-M Futures | `https://dapi.binance.com` |
| Options | `https://eapi.binance.com` |

Example public call:

```text
product = spot
path = /api/v3/ticker/price
params = {"symbol":"BTCUSDT"}
```

Example account call:

```text
product = spot
path = /api/v3/account
params = {"omitZeroBalances":true}
```

## Configuration

Copy `.env.example` and provide the MCP OAuth values. For Binance account access, configure `BINANCE_API_KEY` and one signer:

- `BINANCE_API_SECRET` for HMAC, or
- `BINANCE_PRIVATE_KEY_PATH` for Ed25519/RSA PEM, optionally with `BINANCE_PRIVATE_KEY_PASSPHRASE`.

Do not paste Binance credentials into a chat conversation or commit them to Git.

## Local run

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
pytest
uvicorn main:app --host 127.0.0.1 --port 8080
```

The MCP endpoint is `/mcp/`; health is `/health`.

## Docker

```bash
docker build -t binance-mcp .
docker run --rm -p 8080:8080 --env-file .env binance-mcp
```

Mount `/app/data` persistently so OAuth grants survive container replacement. If using an asymmetric Binance key, mount the private key read-only and point `BINANCE_PRIVATE_KEY_PATH` at it.

## CI/CD

This public repository intentionally contains **no GitHub Actions workflows and no production deployment configuration**. NsTut's CI/CD lives in the private `UpperMoon0/NsTut-CICD` repository so deployment credentials, runner policy, and infrastructure topology stay outside the public source tree.

## Scope

The public reader intentionally exposes REST GET endpoints rather than WebSocket streams. Account User Data Streams can be added later as bounded MCP resources if a concrete use case needs event streaming.

This project is not affiliated with or endorsed by Binance.
