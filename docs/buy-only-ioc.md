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
exit parameters are rejected. The exchange must explicitly support
`EXPIRE_TAKER` self-trade prevention. The request pins that mode and reconciliation
checks it, so crossing the account's own resting ladder expires the taker remainder
instead of filling, expiring or reducing the maker. Missing support fails closed;
exchange defaults such as `EXPIRE_MAKER`, `EXPIRE_BOTH`, `DECREMENT` or `NONE` are
never inherited. See the [official STP contract](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/stp_faq.md).

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
bound fail closed; they do not silently debit another holding. Owner recovery for
base/quote fee deviations uses the exact-fill audit described below. Third-asset
fees remain unsupported by that recovery path.

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

Non-quote subscriptions require owned, verified buy-and-hold units in Spot.
The acquired subset is rechecked and reserved inside the same immediate SQLite
transaction as ordinary financial ownership. Concurrent/stale previews cannot
borrow unrelated units. Reconciliation requires the full exact attributed amount;
a partial `min(owned, amount)` movement is not accepted. They
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

## Exact ownership overlap and owner IOC recovery

Each outgoing intent records its reserved hold subset. Owned SELL/OCO reservations
use available non-hold units first, then an exact hold portion when needed. Fresh
protection counts only that reservation's non-hold portion in addition to the full
intentional hold subset. Disjoint protected and held inventory both count, while
an exit attached to a hold cannot also cover unrelated inventory. Partial sells
consume the recorded non-hold principal first and reduce only their own hold
attribution. Buy-and-hold budgets also reserve a separate conservative base-fee
bucket for new SELL/OCO exits. Each partial fill releases principal and fee quota
separately: actual base fees consume the fee bucket's attributed units, while
quote fees release the unused base allowance. Strategy reports expose these
`sellFeeReservations`. Older exits without that bucket can account base fees only
from currently uncommitted units; they cannot borrow another intent's backing. Earn cannot subscribe or redeem another intent's committed hold.

A generic accounting `resolveIntents` audit cannot terminate a `buy_ioc` or book
its pending base/quote balances independently. Doing so would release the allowance
without recording the purchase. Use a standalone owner-only `iocRecoveries` audit
for supported base/quote commission deviations, containing:

- the existing audit `id`, owner `evidence` and exact complete `accountDeltas`
- for each recovery, `intentId`, `approveActualFees=true`, fresh `observedAt`,
  exact terminal `order`, complete `trades` and `openOrders` excluding its remainder
- no generic `adjustments` or `resolveIntents` in the same audit

The existing offline audit command obtains complete read-only account totals.
Recovery checks original order identity/contract/STP, quantities, valid unique trade IDs and each trade’s exact parent order ID,
commission assets, quote totals and no live remainder. It books actual fills,
commissions, quote debit, finite allowance consumption and acquired holdings
atomically before releasing the reservation. Derived account deltas must explain
all observed totals or every table rolls back. Replaying the same audit is
idempotent; changing its payload fails. Actual past fees can exhaust or exceed the
allowance, but never reset it or authorize another buy. The original immutable
request and owner pauses remain unchanged. This recovery sends no exchange write.

## Owner recovery for uncertain acquired Earn transfers

Generic balance adjustments plus terminal `resolveIntents` are rejected for
outstanding acquired-asset Earn transfers, including separate adjustments that
could be booked twice later. A standalone owner-only `earnRecoveries` audit must
contain no generic adjustments, terminal resolutions, IOC recoveries, or account
deltas: these are transfers of existing underlying assets, not new funding.
Each entry contains `intentId`, `approveAttribution=true`, a fresh `observedAt`,
and the exact exchange history `record`, including its receipt ID and `time`.

The receipt must match operation-specific terminal status (SUCCESS subscription,
PAID redemption), product, asset, amount, any exposed Spot account fields, and the
intent's time window. A known accepted ID cannot change. Owner approval binds a
previously unknown receipt to that intent; the shared receipt-claim journal rejects
reuse for any other intent, including receipts already reconciled online.

Recovery uses the same transactional exact transfer as normal reconciliation,
moving the full reserved acquired subset and proportional cost between Spot and
its exact Earn product. Identity claims, balances, cost, attribution and terminal
state either commit together or all roll back. Replays of an identical audit are
idempotent. Both subscription and redemption timeouts are supported without
reissuing either exchange write. Existing pauses remain; unresolved account or
location drift requires independent owner review.

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
