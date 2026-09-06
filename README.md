# Binance MCP

A security-first Model Context Protocol server for Binance.

It provides a compact surface instead of mirroring hundreds of Binance endpoints as individual MCP tools:

- `binance_public_request` — GET any public Spot, USDⓈ-M Futures, COIN-M Futures, Options, or Portfolio Margin REST path.
- `binance_account_request` — signed **GET-only** account access using credentials stored on the server.
- `binance_order_request` — standard create/cancel Spot, Futures, and Options order operations, disabled unless the deployment explicitly enables trading.
- `binance_simple_earn_redeem` — redeem a specified Simple Earn Flexible amount to Spot or Funding.
- `binance_dual_investment_subscribe` — subscribe Spot funds into one exact Dual Investment product after live product revalidation and optional quote guards.
- `binance_dual_investment_positions` — read normalized Dual Investment positions.
- `binance_dual_investment_from_flexible_earn` — guarded orchestration from Flexible Earn → Spot → one exact Dual Investment product while preserving a configured Earn balance.
- `binance_auth_status` — redacted capability status. It never returns secrets.

The generic public request tool keeps pace with Binance adding new market-data endpoints without forcing a new MCP schema for every endpoint. Financial writes deliberately do **not** use a generic signed POST tool: each supported write is a named, purpose-specific action.

## Security model

There are two independent authentication boundaries:

1. **MCP client → Binance MCP** uses the owner-approved OAuth model from the NsTut MCP stack: authorization code flow, PKCE S256, dynamic client registration, short-lived access tokens, rotating refresh tokens, persisted hashed grants, strict redirect validation, and fail-closed MCP access.
2. **Binance MCP → Binance** uses a Binance API key plus HMAC secret, Ed25519 private key, or RSA private key. Credentials, signatures, and private keys stay server-side and are never returned by tools.

Binance Login OAuth2 is not used for ordinary self-hosting because Binance currently limits that program to approved ecosystem partners. If Binance broadens access later, it can be added as another credential provider without changing the MCP OAuth boundary.

### Safe defaults

- MCP refuses protected requests when OAuth is not configured.
- Generic account access is GET-only.
- Trading and financial writes are disabled unless `BINANCE_TRADING_ENABLED=true` is set by the deployment owner.
- Withdrawals are intentionally unsupported.
- Full URLs are rejected. Requests are pinned to known Binance API hosts.
- `recvWindow` defaults to 5000 ms and may not exceed 60000 ms.
- Boolean query parameters are normalized to Binance-compatible lowercase `true` / `false` before encoding and signing.
- Non-ASCII request parameters are percent-encoded before signing, matching current Binance signed-endpoint requirements.
- Dual Investment auto-compounding defaults to `NONE`; Standard or Advanced compounding is never silently enabled.
- Dual Investment writes re-fetch the exact product before subscribing and can enforce `minimumApr` and `minimumStrikeDistancePercent`.
- The Earn → Dual Investment orchestration supports `preserveEarnAmount` and refuses to redeem funds that would push the remaining Flexible Earn balance below that floor.
- After redemption, the orchestration re-fetches the exact product and re-runs quote guards. If the product disappears or a guard fails, it leaves the redeemed funds in Spot and does not substitute another product.
- Secret/key files are excluded by `.gitignore` and `.dockerignore`.

For account keys, prefer a dedicated Binance API key with only the permissions you need and IP restrictions where practical. Binance recommends Ed25519 keys for performance and security.

## Supported API hosts

| Product | Base URL |
| --- | --- |
| Spot and SAPI | `https://api.binance.com` |
| USDⓈ-M Futures | `https://fapi.binance.com` |
| COIN-M Futures | `https://dapi.binance.com` |
| Options | `https://eapi.binance.com` |
| Portfolio Margin | `https://papi.binance.com` |

Portfolio Margin reads use `product = portfolio_margin`. Its UM, CM, and margin order families use different paths, so they are deliberately not collapsed into the standard `binance_order_request` tool.

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

## Financial write tools

These actions can move funds. They all require `BINANCE_TRADING_ENABLED=true` and a Binance API key with the permissions required by Binance.

### Redeem Simple Earn Flexible

`binance_simple_earn_redeem` accepts a Flexible Earn `productId`, a positive decimal `amount`, and an optional `destAccount`. The default destination is `SPOT`.

### Subscribe Dual Investment

`binance_dual_investment_subscribe` accepts the exact product id plus the explicit investment pair and direction (`investCoin`, `exercisedCoin`, `optionType`). The server re-fetches the live product and obtains the Binance `orderId` itself, preventing callers from supplying stale or arbitrary order ids.

Optional quote guards:

- `minimumApr`: decimal APR floor, e.g. `0.18` means 18%.
- `minimumStrikeDistancePercent`: minimum live distance between spot and strike, e.g. `2.0`.
- `autoCompoundPlan`: defaults to `NONE`; `STANDARD` or `ADVANCED` are accepted only when the selected live product advertises that plan.

### Read Dual Investment positions

`binance_dual_investment_positions` wraps Binance's signed position endpoint and normalizes position/order id, available product id, invest/exercised assets, deposit amount, strike, APR, settlement date, purchase status, direction, and auto-compound plan.

### Flexible Earn → Dual Investment orchestration

`binance_dual_investment_from_flexible_earn` is the preferred user-facing workflow when the source funds are in Simple Earn Flexible:

1. Read the exact Flexible Earn position.
2. Confirm it is redeemable and the requested amount is available.
3. Enforce `preserveEarnAmount`.
4. Fetch and validate the exact Dual Investment product and optional quote guards.
5. Record the Spot free balance.
6. Redeem the requested amount to Spot.
7. Verify Spot received the funds.
8. Re-fetch the exact Dual Investment product and re-run all guards.
9. Subscribe only if the same product still passes.
10. Read positions and verify the resulting subscription.

If the product becomes unavailable or fails a guard after redemption, the workflow returns a partial-completion result with `subscribed=false` and leaves the funds in Spot. It does not automatically choose a substitute product or re-subscribe the funds to Simple Earn.

## Configuration

Copy `.env.example` and provide the MCP OAuth values. For Binance account access, configure `BINANCE_API_KEY` and one signer:

- `BINANCE_API_SECRET` for HMAC, or
- `BINANCE_PRIVATE_KEY_PATH` for Ed25519/RSA PEM, optionally with `BINANCE_PRIVATE_KEY_PASSPHRASE`.

Set `BINANCE_TRADING_ENABLED=true` only on deployments where named financial write tools should be enabled.

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
