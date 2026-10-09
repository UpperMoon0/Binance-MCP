# Readiness contracts

Version 0.3 implements the source fixes tracked in issues #6–#14. Tracking #15 also requires separately approved operational provisioning, deployment and read-only verification through the actual connected client. Passing tests is not live readiness or evidence of profitability.

## Passive tools and protocol

`binance_execution_status` reads the stored intent only, including its original evidence timestamp. Use the explicitly state-changing `binance_execution_reconcile` to query exchange history and update local accounting/control state. That tool never submits an exchange write or runs automated recovery. Unknown IDs never pause anything. Preview, status, readiness, fee inspection, snapshots and market reads change no financial/control tables on success or failure. Background monitoring remains independently active.

Every registered tool returns explicit MCP structured content. Failures set `isError=true` with a sanitized `error` object: blocker, status, exchange code, retry-after, outcome-unknown, correlation ID and `automaticWriteRetryAllowed=false`. Raw exchange error messages, signed URLs, arbitrary headers and secrets are excluded. Dictionary successes retain their shape; array/scalar successes have `structuredContent.data`, while text content preserves the original JSON value. Inspect `tools/list` rather than assuming tool names.

`binance_readiness(checkExchange=false)` works without any strategy. It lists actual IDs/modes, approved universe, policies, commitments, incidents, runtime source digest and schema digest. A runtime digest identifies the installed package files inspected at the read; package/Python/SDK versions are reported too. It does not attest already-loaded bytecode after an in-place edit, prove a particular Git commit is deployed, or prove this client's tool cache is current. Optional `checkExchange=true` performs a scoped portfolio read and a bounded one-symbol market read, retaining failures. It never provisions, reconciles or triggers recovery. Paper configuration and functional verification are separate. Protected-live readiness remains blocked until a fresh exact-contract trade preflight; a generic healthy flag cannot certify permission/fees for every symbol.

Portfolio `complete` refers only to Spot, Flexible Earn, DI, Spot open orders and linked lists. `accountWideComplete=false` and `excludedCoverage` explicitly exclude Margin, Futures, Options, Portfolio Margin, Funding, Locked Earn, staking and external wallets. Unsupported products are not account-wide zero holdings. Pagination is bounded and incomplete/repeated/contradictory evidence fails closed; LD receipts are not counted twice. Market results name their closed-hourly-candle window, reject gaps/stale candles and identify book timing as local receipt rather than an exchange update timestamp.

## Owner policy and fees

Fresh symbol-specific compatible exit fee bounds are required for each held non-quote asset when evaluating portfolio losses. A new entry's commission rate cannot stand in for another holding's rate; unavailable or incompatible fee evidence blocks the entry.

There are no approved pilot limits in source. Owner provisioning can supply `riskPolicy` and `recoveryPolicy` within an explicitly chosen allocation. Policies persist across restart, do not top up funding, and cannot be changed by tool parameters. Reopening without policy configuration preserves existing policy; contradictory configuration fails. Paper without policy is permitted; live new risk without complete approved policy is blocked. Owned exits/cancels remain possible during an upgrade without new-risk policy, subject to ownership/backing checks. With a policy present, its operation/asset allowlists apply to these paths too; configure the required exit/recovery operations explicitly.

Risk policy version 1 requires all fields:

| Field | Meaning |
| --- | --- |
| `version`, `approved` | Exactly `1`, `true`; deployment owner approval |
| `operations`, `assets` | Explicit permitted families and assets; no research-controlled expansion |
| `maxPositions` | Positive integer cap on entries/unknown commitments, partial positions, orphaned holdings and settled DI locations; OCO siblings count once |
| `maxPositionQuote` | Positive maximum per new commitment including its fee reservation |
| `maxPlannedDownsideQuote` | Positive per-entry planned downside including conservative entry/exit fees and execution allowance; DI charges its entire principal against this bound |
| `lossPauseQuote` | Positive loss threshold using net attributable realized trading P&L plus marked holdings/costs and conservative exit costs |
| `executionAllowanceBps` | Explicit nonnegative conservative allowance, at most 2000 |
| `valuationMaxAgeMs` | Explicit positive valuation age bound, at most 90000 ms |

New entries check policy both passively and inside SQLite's reservation transaction. Current marks are required; missing/stale marks block new risk. Loss pauses persist separately from operational pauses and do not automatically reset. Deposits, withdrawals, transfers, Earn rewards and settlements are separately categorized; they are not added to realized trading P&L. Non-quote positive owner attributions require audited receipt cost basis so receipt value is not manufactured as trading profit. Investment writes with open non-quote exposure are currently blocked until compatible trading valuation evidence is supported, rather than assigning zero exit costs. Sale proceeds can be reused subject to current limits; initial funding is not a per-run allowance or lifetime turnover cap. A stop and planned downside do not guarantee a maximum realized loss during gaps or liquidity failures.

`binance_fee_compatibility` exposes current standard/tax/special rates and discount flags. Discounts enabled for account **and** symbol block submission even if BNB appears insufficient: fallback cannot safely be predicted from someone else's balance. Base/quote-only payment remains the supported mode. BNB support, acquisition, attribution and account-wide discount changes are not implemented or authorized. Execution fetches fresh fees; unknown fee assets or actual charges exceeding the verified bound retain uncertainty for owner accounting. Cost/P&L already include fees; the commission disclosure is not deducted again. Commission metadata without explicit discount evidence is blocked; symbol contradictions are rejected.

Spot permission checks require `canTrade=true`, symbol TRADING/Spot availability and valid account permissions. OR applies within a permission set, AND across sets. Only an omitted `permissionSets` permits the legacy literal-SPOT fallback; empty, null or malformed sets fail. A supplied non-SPOT account type fails. `accountType=SPOT` alone is not authority.

## Operational response

Recovery policy requires `{version:1, approved:true, action:"operator_only"|"cancel_attach", maxDelayMs:<1000..90000>}`. Neither recovery nor its notification channel is provisioned by default. New live risk requires both policies and an incident receiver. An operator-only policy relies on the approved operator response; no automatic liquidation follows from it.

The monitor reacts to stream events and uses bounded 5..60-second REST fallback. Heartbeat, successful tick, account reconciliation and protection timestamps are separate. A failed tick cannot refresh successful evidence or clear an owner pause. Per-leg verification checks list/child identity, symbol/side, type, approved price/trigger, state and exact intended quantity against actual owned exposure. Every required sibling must cover its position; larger siblings cannot compensate for an undersized stop. Partial entry fills are unprotected until activation is verified. Actual quote-asset entry fees can leave more base units than a conservative estimate: remaining lot-sized exposure is reserved and repaired in full.

Incidents persist detection time, exposure, deadline, exchange evidence, stable cancellation/replacement IDs, delivery attempts/status and operator acknowledgment. Source queries must verify current evidence before recovery. Before approved `cancel_attach` cancellation, a read-only replacement preview checks current exposure, trigger/price/lot/notional filters, permissions, fee compatibility, ownership, free capital and order limits. Failure preserves the original order list, including any working stop. The disposable planning ledger releases only the source reservation; exchange free balance and order slots receive no cancellation credit, so locked funds or exhausted slots conservatively require an operator. Cancellation is followed by terminal history/fill reconciliation, then an owned fee-adjusted OCO with the original stop/take contract. Post-cancel execution repeats preflight; exchange changes during cancellation can still prevent replacement. Fill-during-cancel is included; ambiguous cancellation/replacement is queried through the original intents and never resubmitted. Stops already crossed, below-minimum residuals, unsupported source ownership and expired deadlines require operator handling. No new recovery mutation begins after the deadline. Existing commits remain reserved until evidence resolves them; no unconditional market liquidation is installed.

Configure `BINANCE_INCIDENT_WEBHOOK_URL` and optional `BINANCE_INCIDENT_WEBHOOK_TOKEN` only for a separately approved private HTTPS receiver. Delivery uses the incident ID as `Idempotency-Key`, bounded timeout and no redirects. The receiver must deduplicate across restart and return HTTP 200 with `{"acknowledged":"<incidentId>"}` to confirm receipt. Receipt is separate from human acknowledgment; owner/offline `incidents.acknowledge` records acknowledgment evidence without resolving exposure or clearing pauses. The tests use a fake sink; no notification smoke test was sent.

## Legacy migration

Stop the service and leave trading disabled while the owner prepares evidence. Default planning opens an existing ledger **read-only**, does no schema initialization, and applies candidate changes only to an in-memory clone:

```bash
python -m binance_mcp.migration owner-evidence.json
```

The plan reports wallet/product holdings, receipt representations, current earmarks, open/partial/unknown commitments and unsupported compatibility decisions. It does not infer remaining reserve from its original allowance, automatically fund a strategy or change auto-subscription. Missing ledger/evidence/identity/cost basis fails closed. Private exports/plans/backups stay outside this public repository.

An owner audit needs a stable `id`, evidence references, `ledgerRevision` from a fresh plan, and `sourceRevision` equal to the runtime `sourceSha256`. Existing `accountDeltas`/`adjustments`/`resolveIntents` retain the owner-audit contract. A `legacyOrders` entry supplies strategy ID, exact observed symbol/orderId/clientOrderId/side/type/timeInForce/price/origQty/executedQty, base/quote assets, cost-basis evidence and historical-fill evidence. The observer independently fetches symbol, fee and complete trade history. Only unlinked live SELL LIMIT GTC orders with known attribution and receipt cost basis are currently importable. Remaining quantities reserve that strategy's recorded Spot holdings. Original historical IDs are preserved; imported history is fingerprinted and excluded from new fill accounting. Later fills and commissions are followed once across restart. No fake `mcp_` identity is assigned to the exchange order.

Apply is explicit, checks the exact ledger/source revision and fresh complete scoped observations, creates a new private SQLite backup, then imports atomically:

```bash
python -m binance_mcp.migration owner-evidence.json --apply --backup /private/new-ledger-backup.sqlite
```

The revision fingerprints every metadata row and all incidents as well as strategies, balances, intents and events; safety changes after approval invalidate the plan. Audits preserve existing pauses, including owner emergency halts.

Duplicate evidence cannot top up balances or reset reserve history; changed identities/history, contradictory locations, missing cost basis and unmanaged orders roll back the import. Existing audited rewards/settlements/manual movements remain separately categorized. Imports do not cancel/reprice orders or confer experiment authority over conservative/reserve holdings. Unprotected legacy non-quote holdings still block protected new entries until independently resolved.

Legacy IOC buybacks, conversion, same-asset non-quote Earn and auto-subscription remain unsupported or require an explicit owner audit/configuration decision. A compatibility report is not permission to replace those strategies with OTOCO. Paper funds stay separate and unbacked by real funds. No migration was applied to production.

## Verification gates

The isolated suite includes the five failed readiness regressions and the historical DI-504/saga/pagination/ownership/idempotency cases. Real ASGI `tools/list` and `tools/call` tests cover passive success/failure, missing IDs, paper, blocked live, scoped diagnostics and sanitized errors. They compare all persistent tables, exercise concurrent reservations/restarts, fake cancellation races and fake notification failure/recovery. Outbound network connections are blocked and credentials cleared. Numeric loopback and local IPC remain available for asyncio socketpair, including the Windows event loop. Candle simulation is conservative evidence of software behavior, not live execution quality or profit.

After a separately approved deployment, an owner can run:

```bash
# Supply OAuth bearer credentials only through MCP_READINESS_TOKEN in the environment.
python scripts/readiness_smoke.py https://your-approved-host/mcp/
```

This lists actual schemas, checks passive annotations and calls passive readiness with fresh bounded reads. It prints a sanitized scoped result, source/schema identity and blockers. It does not allocate, place test orders, reconcile, reconnect/redeploy, change settings/schedules or send notifications. Confirm the same discovery/read results in the intended ChatGPT conversation; HTTP smoke results alone do not prove that client's exposure. Research, paper validation and protected live are separate decisions. Private CI/CD remains outside this public source tree; no untrusted fork code should run on private runners.
