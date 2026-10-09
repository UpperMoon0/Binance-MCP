# Binance MCP

A compact Binance MCP service with server-side credentials, persistent strategy accounting, and guarded Spot and investment workflows.

## Strategy execution

Every financial write requires `strategyId` and a stable `intentId`. The allocation is provisioned by the deployment owner, persisted once in SQLite, and never replenished on a request or restart. Selling and reusing a strategy's own proceeds is allowed; losses reduce the capital available for the next entry. `reinvest=false` moves realized gains into a separate non-spendable profit reserve.

Reservations are shared by ordinary Spot orders, linked orders, Flexible Earn subscriptions/redemptions, and Dual Investment. Amounts use decimal arithmetic. An intent and its complete contract are committed before the exchange request. The Earn-to-DI saga checkpoints its redemption and refreshed DI contract before each write. A repeated intent with the same contract reconciles the original; a different contract is rejected.

After a redemption is accepted, a later read rejection retains the saga checkpoint and reservation. Recovery verifies the unique PAID redemption history and Spot receipt before moving ownership; it never repeats the redemption automatically. A definitive rejection of the first write can release its reservation.

Capital is tracked by strategy, asset and location (`SPOT`, `EARN:productId`, `DI:positionId`, `PROFIT_RESERVE`). Configure separate allocations for an experiment, conservative holdings and an ETH buyback reserve. One strategy cannot sell or redeem another strategy's holdings. The ledger is not a lifetime turnover counter.

Recovery SELL and OCO requests can proceed during a protection pause only after a fresh Spot account read backs the aggregate ownership of the sold asset across every live strategy. This includes non-spendable profit reserves and both free and locked Spot balances; holdings in Earn or DI cannot back a Spot sale. Missing or invalid balance evidence blocks submission. The request must also fit the requesting strategy's unreserved capital and the account's free balance. Cancellation remains available during ownership pauses and account-read failures.

### Configuration

```dotenv
BINANCE_LEDGER_PATH=/app/data/strategy.sqlite
BINANCE_STRATEGIES={}
BINANCE_APPROVED_SYMBOLS=BTCUSDT,ETHUSDT
BINANCE_MONITOR_INTERVAL_SECONDS=30
```

An example owner-provisioned paper allocation:

```json
{"experiment":{"allocation":"100","quote":"USDT","mode":"paper","reinvest":true}}
```

A live deployment can provision independent locations:

```json
{
  "experiment":{"allocation":"100","quote":"USDT","mode":"live","reinvest":true},
  "conservative":{"allocation":"500","quote":"USDT","mode":"live","location":"EARN:USDT001"},
  "eth-buyback":{"allocation":"100","quote":"USDT","mode":"live","reinvest":false}
}
```

These are examples, not automatic allocations. Live allocations must be backed at the configured account location. Existing allocation policies are immutable across restarts. All worker processes must use the same durable SQLite file. Run **one ASGI worker** so the account stream, monitor and execution coordinator have one owner. Multiple ledger connections still enforce atomic reservations; multiple independent monitoring workers are not supported.

Existing accounts must be reconciled before enabling live mode. Keep trading disabled while importing owner-reviewed accounting evidence. Never delete the ledger to recover a timeout or to reset the budget.

### Tools

| Tool | Purpose |
| --- | --- |
| `binance_public_request` | Public REST GETs on the approved Binance hosts, including derivatives reads |
| `binance_account_request` | Signed GET-only account reads |
| `binance_trade_preview` | Current entry/exit filters, fee bounds, spread, slippage, permissions and available strategy capital |
| `binance_strategy_execution` | Narrow `otoco`, `oco`, `order`, or `cancel` workflows |
| `binance_execution_status` | Reconcile the original journal intent without sending another exchange write |
| `binance_strategy_status` | Cash, ownership, commitments, categorized P&L, unrealized valuation and monitor status |
| `binance_portfolio_snapshot` | Spot, Flexible Earn, DI, open orders, linked lists and reservations with coverage/timestamps |
| `binance_market_scan` | Closed candles, volume, volatility, spread and optional shortlisted depth |
| `binance_simple_earn_subscribe` / `binance_simple_earn_redeem` | Allocation-owned Spot-to-Earn transfers and redemptions |
| `binance_dual_investment_subscribe` | Exact-contract DI subscription with live guards |
| `binance_dual_investment_from_flexible_earn` | Allocation-owned Earn redemption followed by exact DI subscription |
| `binance_dual_investment_positions` | Normalized page-based DI reads; semantic workflows paginate internally |
| `binance_order_request` | Compatibility tool for guarded Spot LIMIT SELL or recorded-intent cancellation |
| `binance_auth_status` | Redacted signing/trading capability |

**Version 0.2 breaks unbudgeted financial calls:** every existing financial tool now requires both IDs. Derivative mutations, arbitrary order parameters, Funding-account movements, automatic Earn subscriptions and DI compounding are disabled in managed workflows. Read access remains available. Ordinary BUY orders must use protected `otoco`; individual raw stop parameters are no longer a bypass around the guarded linked workflow.

### Protected Spot entry example

Call `binance_trade_preview` with:

```json
{
  "strategyId":"experiment",
  "operation":"otoco",
  "params":{
    "symbol":"BTCUSDT",
    "quantity":"0.001",
    "price":"80000",
    "takeProfit":"84000",
    "stopPrice":"76000"
  }
}
```

These prices are illustrative; use current observations. Execute the same parameters with `binance_strategy_execution`, adding an intent such as `experiment-2026-10-09-entry-001`. The server revalidates immediately before reserving and submitting. It generates stable exchange client IDs itself.

OTOCO uses a LIMIT BUY entry, a fee-adjusted LIMIT_MAKER take-profit and a STOP_LOSS exit. Both entry and exits must satisfy quantity steps, price ticks, notional limits and supported percent-price filters. Permission, maximum-position and order-count checks are also applied. Unsupported percent-price averaging windows fail closed. Default spread cap is 20 bps (owner-bounded maximum 100); entry limit-distance/slippage cap is 100 bps (maximum 500); stop distance is 10..2000 bps. The quote fee reserve uses the conservative undiscounted maker/taker, buyer/seller, tax and special commission bound.

OTOCO acceptance **does not mean a partial entry is protected**. Pending exits activate only after the working order fully fills. Reconciliation verifies each child by its stable client ID and books actual trade history/commissions exactly once. Partial fills, rejected/pending exits, history gaps and insufficient sellable quantity pause entries. To repair a partial entry:

1. Cancel its list using `operation=cancel`, `params={"targetIntentId":"original-entry-intent"}` and a new recovery intent.
2. Reconcile until every child is terminal and all fills are accounted for.
3. Preview and submit an owned `oco` using the fee-adjusted quantity from strategy accounting and current exit prices.

Cancellation, owned OCO repairs and owned LIMIT SELL exits remain available while entries are paused. A replacement cannot reserve quantities still committed to the previous list. There is no blind cancel/replace retry. A step-size remainder is reported as owned dust; it is not labelled protected or counted as available quote cash.

STOP_LOSS exits are market orders after activation. A future gap can exceed the preview's current spread/slippage; the preview cannot guarantee a future execution price. Paper mode uses the adverse stop outcome if both exit levels occur in the same candle.

Live fee payments in a third asset (for example BNB discount) are refused at preflight. Disable that discount in Binance for managed trading; an unexpected third-asset commission during reconciliation pauses accounting instead of charging an unrelated portfolio. Live runtime validation is still required before funding an experiment.

### Uncertain writes and DI verification

Transport failures, timeouts, server errors and non-authoritative error responses are `OUTCOME_UNKNOWN`. Their reservations survive restarts. Only recognized Binance validation/permission rejections release a reservation. Absence from a history page is not proof of rejection. No financial write is automatically retried, swept into Earn, or substituted with another product.

Flexible Earn subscriptions preflight the live product catalog, so a first subscription or rotation does not require an existing account position. Metadata must confirm a matching asset, purchasable status, availability and the minimum/start-time guards. Redemptions still require an owned redeemable position. Exchange history verifies `SUCCESS` for subscriptions and `PAID` for completed redemptions, including partial Earn-to-DI recovery.

DI verification requires an acceptable `PURCHASE_SUCCESS` status, the returned position ID when present, a newly visible position excluded from the pre-request position set, and matching deposit, assets, direction, strike, APR, settlement and compounding plan. Numeric decimals are compared numerically. All pages are read within a bounded limit; incomplete or repeated pagination fails closed. Binance's explicit `NULL` plan is normalized as no compounding; a missing plan is not proof.

An advertised pagination total remains required on later pages even if they omit it. Empty pages before that total, changing totals, or excess records report incomplete coverage rather than publishing partial holdings as complete.

The Earn-to-DI workflow preserves `preserveEarnAmount`, verifies Spot receipt and re-fetches the same product after redemption. If the DI request then times out, the result never claims the pre-request Spot balance is verified. Exact recovery uses DI positions and Earn history; ambiguous records without a unique write identifier require owner reconciliation. A safe partial completion leaves the funds owned by the same strategy, never spends them as another strategy's cash.

### Operational monitoring

The app lifespan starts a small monitor independently of any research cycle. It subscribes to Binance's signed `userDataStream.subscribe.signature` WebSocket API, reacts to account/order/list events and reconciles with REST every 30 seconds (configurable 5..60). It synchronizes server time, detects outside activity, verifies protection, and checks account coverage. Stream disconnects, stale monitor state over 90 seconds and unresolved executions block entries. Recovery never resends a financial write.

The monitor clears only operational pauses after all corresponding checks pass. A reconciliation failure anywhere in the live tick retains the pause and last successful monitor timestamp, even if the journal previously recorded active protective orders. Outside/manual account changes, rewards and DI settlement mismatches require an attributed owner audit. Health and strategy status expose readiness; transport/authentication health alone does not imply funds are reconciled or an entry is protected.

### Accounting audits

There is no MCP tool for increasing allocations or releasing ambiguous reservations. The deployment owner can stop the service and import reviewed exchange evidence with:

```bash
python -m binance_mcp.accounting evidence.json
```

Example reward attribution:

```json
{
  "id":"earn-reward-2026-10-09",
  "evidence":["exchange-history-record-id-and-export-reference"],
  "accountDeltas":{"USDT":"0.25"},
  "adjustments":[{
    "strategyId":"conservative",
    "category":"earn_reward",
    "asset":"USDT",
    "location":"EARN:USDT001",
    "quantityDelta":"0.25",
    "quoteValue":"0.25"
  }]
}
```

Supported categories are `deposit`, `withdrawal`, `earn_reward`, `settlement`, `commission`, `loss` and `transfer`. Quote cost changes can use `costDelta`. Owner-attributed settlement imports debit the DI location and credit the actual received asset/location. The import verifies complete observed account totals against explicit account deltas, prevents total strategy ownership exceeding exchange capital, and records idempotent evidence hashes. It can include `resolveIntents` with `{intentId,state}` after the owner has proved the original outcome. Attribution is privileged owner input; account totals alone cannot prove which strategy caused a movement. Restart monitoring to verify account locations and protective orders before entries resume.

P&L separates these categories from fill/realized/commission events. Realized P&L is net of booked fees; the commission category is a separate disclosure and must not be subtracted twice. Missing asset valuations produce `unrealizedQuote=null` with explicit missing assets.

### Paper mode

Paper Spot strategies use the same symbol/filter, sizing, reservation, intent and lifecycle rules, with simulated 0.1% undiscounted fees. They need no Binance signing credentials, send no exchange writes, and never connect to an authenticated user stream. New LIMIT orders wait for subsequent closed one-minute candle evidence. Entry fees reduce sellable quantity; exits book actual simulated proceeds and losses. Both-level candles take the adverse stop outcome, gap stops use the worse open, and missing candle coverage leaves the intent unresolved. Simulated fills are capped at 10% of candle base volume and rounded to the quantity step; thin-volume entries remain partial and cannot be labelled protected. The first take-profit fill or stop activation persists the active exit leg and cancels its sibling across restarts. Remaining take-profit quantity waits for its limit; remaining activated stop quantity trades at subsequent candle opens. Legacy partial exits without a recorded active leg require reconciliation and cannot resume automatic fills. Paper mode is a conservative candle simulation, not an order-book queue simulator. Investment settlement simulation is unsupported; investment calls fail closed in paper mode.

### Semantic snapshots and research boundary

Portfolio snapshots paginate Flexible Earn and DI internally, report individual observation times, missing data and reconciliation state. `LD*` Earn receipt representations are reported separately and excluded from underlying asset totals. Reservations are included with their owning strategies; account totals are not a strategy's spendable budget. Coverage is limited to Spot, Flexible Earn and DI, not Funding, Locked Earn, staking or derivatives.

Market scans use an owner-approved universe, 1..20 unique symbols, closed hourly candles, quote volume, return volatility and spread. They return raw observations rather than a buy score. Detailed order books are fetched only for a requested subset of at most five candidates. News/social research remains outside this credential-holding service. Preserve source, publication time, event time and uncertainty in research records; never treat those records as execution commands.

## Authentication and transport

MCP access uses the existing owner-approved OAuth boundary: PKCE, dynamic registration, short-lived tokens, rotating refresh tokens, persisted hashed grants and strict redirects. Unconfigured OAuth fails closed.

Binance uses server-side HMAC, Ed25519 or RSA credentials. Configure `BINANCE_API_KEY` and either `BINANCE_API_SECRET` or `BINANCE_PRIVATE_KEY_PATH` (optional passphrase). Live financial writes additionally require `BINANCE_TRADING_ENABLED=true`. Withdrawals are unsupported. Full URLs and traversal paths are rejected; hosts stay pinned to Binance. Secrets, signed URLs and private keys are never returned by tools or structured errors.

The reusable HTTP client has four-request bounded concurrency, 30-second public exchange metadata caching (preflight bypasses it), clock-offset synchronization, sanitized structured errors and rate-limit header retention. `Retry-After` cooldown blocks further requests. Writes have no generic retry policy. `recvWindow` defaults to 5000 ms, maximum 60000. Boolean parameters use lowercase values; non-ASCII parameters are percent-encoded before signing.

## Run and test

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
pytest
uvicorn main:app --host 127.0.0.1 --port 8080
```

The MCP endpoint is `/mcp/`; health is `/health`. Configuration values must be exported to the process. Copy `.env.example` for Docker/environment configuration; do not commit credentials or paste them into chat.

```bash
docker build -t binance-mcp .
docker run --rm -p 8080:8080 --env-file .env -v binance-data:/app/data binance-mcp
```

Persist `/app/data` for both OAuth grants and the execution ledger. Mount asymmetric private keys read-only. SQLite state contains financial history and must receive appropriate owner-only filesystem access and backups.

Tests use fake exchanges/HTTP transports only; they never place live Binance orders. See [CHANGELOG.md](CHANGELOG.md) for the 0.2 migration.

## API references

- [Binance Spot REST API](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md): timeout semantics, OCO/OTOCO, account commission and filters.
- [Binance WebSocket API](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-api.md): signed User Data Stream subscription.
- [Official Binance Python connector models](https://github.com/binance/binance-connector-python): Dual Investment position identity/status and Simple Earn history field names.

## CI/CD

This public repository intentionally contains no GitHub Actions workflows or production deployment configuration. NsTut's CI/CD remains in private `UpperMoon0/NsTut-CICD`; infrastructure and deployment credentials stay outside this source tree.

This project is not affiliated with or endorsed by Binance.
