# Buy-only IOC and acquired-asset Flexible Earn

This implements the source contract for [issue #19](https://github.com/UpperMoon0/Binance-MCP/issues/19).
A buy-only entry is **unprotected market exposure**. There is no stop loss,
attached exit, implicit sell, conversion, discount change, or symbol-universe expansion.
Protected OTOCO remains the default entry workflow; omitted exits do not authorize a hold.

## Owner policy and funding

The owner must provision both an approved risk policy permitting `buy_ioc` and a
separate immutable `buyHoldPolicy` within the selected strategy configuration:

```json
{
  "version": 1,
  "approved": true,
  "mode": "buy_and_hold",
  "symbols": ["ETHUSDT"],
  "maxCumulativeQuote": "100"
}
```

These are illustrative settings, not an installed approval. Symbols must also be
in `BINANCE_APPROVED_SYMBOLS`, and the base/quote assets must be permitted by the
risk policy. Each IOC's entire fee-bounded principal must fit
`maxPositionQuote` and `maxPlannedDownsideQuote`; normal position, loss, valuation,
recovery, account-backing, monitor, stream, incident and execution-pause gates remain.
Existing immutable policies cannot be broadened by a tool call or silently replaced
on restart. An owner-reviewed offline policy migration is required to change one.

The existing shared-account `fundingSource=shared_account` / `budgetCap` contract
and `BINANCE_DEFAULT_STRATEGY_ID` work unchanged. No second deposit or wallet is
needed. Omitted `strategyId` selects only the configured default. An explicit
unknown ID always fails.

`maxCumulativeQuote` is a **non-replenishing** allowance, distinct from reusable
strategy cash. Verified purchase notional plus commission value consumes it.
Sales, rewards, deposits, redemptions and restarts never refill this allowance.
Outstanding IOC commitments count against it atomically in the same SQLite
reservation transaction as normal capital. Other reservations, profit reserves
and imported legacy sell ownership remain intact.

## Preview and execution

Call `binance_trade_preview` with `operation="buy_ioc"` and exact params:

```json
{
  "symbol": "ETHUSDT",
  "quantity": "0.01",
  "price": "2000",
  "maxQuoteSpend": "20.04"
}
```

The preview is passive, including failures. It re-fetches symbol/permission,
commission, book, average-price, order-count and account evidence and validates
quantity/price filters, spread/slippage and backing. Quantities and prices are
never silently rounded. `maxQuoteSpend` is mandatory and includes the conservative
fee bound. An IOC does not need OCO/OTO availability or valid exit legs. Optional
`side`, `type`, and `timeInForce` must be `BUY`, `LIMIT`, and `IOC`, respectively;
exit parameters are rejected.

Submit the same operation/params through `binance_strategy_execution`, with a
stable unique `intentId`. Execution performs fresh validation again. A successful
preview alone does not certify operational readiness or authorize funding changes.
The request creates exactly one plain `LIMIT BUY / IOC` using a deterministic
client-order identity. No sell or cancellation is sent by this workflow.

The intent and reservation are durably committed before submission. Reusing an
ID with changed params or another budget fails. Reusing the original ID reconciles
that original order only, including after restart. Timeout/5xx, absent history,
nonterminal IOC status, contradictory fills or a live remainder retain the
commitment and fail closed. A missing order is never grounds for a replacement BUY.

Resolution requires an exact terminal order, complete deduplicated trade history,
and a read-only open-order check excluding a live BUY remainder. Zero, partial,
and full fills are supported. Unfilled commitments are released only then (or
on an authoritative first-write rejection). Fill accounting and terminal release
are atomic. Unexpected third-asset commissions or commissions beyond the verified
bound require owner accounting; they do not silently debit another holding.

Results distinguish:

- `actualQuoteSpent`: sum of exchange trade quote quantities, excluding commissions
- `quoteDebit`: actual quote-currency debit, including only quote-currency commissions
- `commissionsByAsset` and `feeValueQuote`: reporting of all commissions; base fees
  reduce acquired units and their quote value is **not** debited from quote again
- `netAcquiredQuantity`: filled base quantity minus base commissions
- `effectiveEntry`: actual quote debit / net acquired units, or null for zero fills
- `terminalStatus`, `liveBuyRemainder=false`, `protected=false`, and
  `protection=UNPROTECTED_BUY_AND_HOLD`
- `buybackBudget` and `remainingStrategyQuote`: cumulative allowance versus reusable cash

`buybackBudget.spentQuoteIncludingFeeValue` deliberately counts notional plus all
fee value conservatively, including base-fee value. It is an allowance metric,
not an additional account debit or P&L charge. Historical intent results are a
snapshot; strategy status/readiness expose the current budget.

Paper IOC execution is explicitly a conservative zero-fill/EXPIRED simulation,
not a promise of fills or a persistent GTC substitute. Isolated fake-exchange
tests exercise real IOC fill and timeout paths without exchange access.

## Explicit acquired-asset Earn step

There is **no redeem → buy → Earn orchestration** in this change. Each financial
step requires its own explicit request and stable intent ID. Do not redeem quote
funds assuming later steps will succeed; inspect scoped readiness and support first.

After a resolved IOC, `binance_earn_preview` accepts `operation="earn_subscribe"`
and `params={"asset":"ETH","productId":"ETH001","amount":"<net amount>"}`.
The write is `binance_simple_earn_subscribe` with the same explicit asset, exact
product/amount, a new intent ID and the selected/default budget. Omitting `asset`
preserves the prior quote-asset behavior.

Non-quote subscriptions require owned, verified buy-and-hold units in Spot. They
cannot borrow unrelated account assets or legacy holdings. Current product asset,
availability, minimum, start time, fee/valuation, exchange free balance and
aggregate backing are checked. Dust stays in Spot. An unavailable product causes
no alternative subscription or compensating sale. Existing matching positions
must explicitly report auto-subscription disabled; true or unknown settings block
rather than changing them. No auto-subscription settings endpoint is called.

Exact successful subscription/redemption history moves the owned units and their
cost basis between `SPOT` and `EARN:<productId>`. `binance_simple_earn_redeem` can
subsequently redeem only that owned amount; the returned product asset must match
its ownership. Attribution is a subset of ledger balances, never an extra asset.
Earn receipt tokens continue to be excluded from underlying funding.

Uncertain Earn writes retain the reservation and report
`SPOT_OR_EARN_UNVERIFIED`. When no unique accepted history ID is available,
operator reconciliation is required; a repeated request never re-subscribes or
re-redeems. A definitive subscription rejection leaves the asset in Spot. Verified
steps report `fundsLocation`. Independent purchases are never rolled back by sales.

Rewards and external auto-subscription changes do not become new funding. The
existing owner-only audited reconciliation handles reward attribution. An
`earn_reward` adjustment can include `buyHoldReward=true` only for positive,
evidenced rewards on already owned Earn principal with a receipt cost basis.
The audit preserves the reward category and hold attribution without resetting
purchase allowance. Ambiguous/unattributed rewards or location drift pause new
execution; automatic pro-rata attribution across unrelated holdings is not assumed.

## Readiness and release evidence

`binance_readiness` separately reports `buyOnlyIoc` and `acquiredAssetEarn`,
including operation permissions, policy/allowance and exact-product preflight
requirements. It never describes intentional holds as protected. Implementation
does not clear pauses, incidents, stale monitoring or missing policies.

Release milestones remain independent:

1. Implemented and synthetic tests verified in the feature PR
2. Independently reviewed and merged (separate approval)
3. Deployed to the intended service (separate approval)
4. New operation enum and Earn preview exposed in the actual connector discovery
5. Read-only functional previews checked with the intended configured policies

Do not use a real trade, cancellation or Earn transfer as a smoke test. This PR
neither installs production policy nor authorizes deployment, funding or trading.
