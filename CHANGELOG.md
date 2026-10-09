# Changelog

## 0.3.0

- Verify each OCO/OTOCO child against the persisted identity, list, side, type, price/trigger, status and actual fee-adjusted exposure. Keep mutually exclusive reservations single-counted.
- Evaluate fresh symbol permission sets with AND across sets and OR within each set. Retain a documented literal-SPOT fallback only when the field is omitted.
- Make preview/status truly passive; split explicit accounting/control reconciliation into `binance_execution_reconcile`.
- Reject contradictory DI product IDs while retaining exact verification for API versions omitting that field.
- Add durable, deduplicated operator incidents, acknowledgment/delivery state, deadlines and owner-approved cancel/reconcile/owned-OCO recovery. Ambiguity never authorizes resubmission; expired/crossed/below-minimum recovery requires the operator.
- Enforce immutable owner-approved risk policy and atomic position/value/downside/loss controls. Live entries require an operational policy/channel; no pilot settings or funding are installed automatically.
- Add passive owner migration planning on a read-only ledger, evidence-backed legacy GTC SELL imports preserving historical IDs/remaining commitments, backups and exact revision checks. Unsupported legacy actions remain blocked.
- Expose passive fee compatibility; BNB discounts remain unsupported and settings are never changed. Unexpected assets/rate increases fail closed.
- Add structured sanitized MCP errors, runtime/schema identity, explicitly scoped coverage/freshness and a passive post-deployment smoke script.
- Breaking: `binance_execution_status` no longer reconciles. Use `binance_execution_reconcile` explicitly. Live funding alone no longer enables new risk.

## 0.2.0

- Require fresh aggregate Spot backing across live strategies, including profit reserves, before recovery SELL/OCO submission; cancellation remains available during ownership pauses.
- Keep accepted Earn redemption checkpoints and reservations recoverable when a later saga read is rejected.
- Persist the active paper OCO/OTOCO exit leg; partial fills cannot execute the canceled sibling, and activated stops remain market orders.
- Reject truncated pagination and changing advertised totals in account snapshots and Dual Investment reads.

- Correct Flexible Earn redemption history verification to require PAID while subscriptions require SUCCESS.
- Preflight new Flexible Earn subscriptions from live product metadata, retaining owned-position checks for redemptions.
- Preserve operational pauses and the last successful monitor timestamp when a live reconciliation tick fails.

- Fix unknown DI subscription outcomes being reported as verified Spot failures.
- Verify complete newly created DI contracts, returned identifiers, statuses and all pages.
- Add persistent decimal strategy allocations, shared reservations, idempotent intent journal, saga checkpoints and categorized P&L.
- Add narrow OCO/OTOCO workflows, linked cancellation, actual fill accounting and protected quantity checks.
- Add current trade preflight, server revalidation, approved market universe and fee/entry/exit sizing guards.
- Add event-driven User Data Stream monitoring, account/location reconciliation and stale/protection pauses.
- Add conservative paper Spot lifecycle and owner-only evidence-backed accounting imports.
- Add semantic portfolio and bounded market snapshots with raw observations, coverage and timestamps.
- Reuse HTTP connections, bound concurrency, synchronize clock offsets and preserve sanitized structured errors/rate-limit cooldown.
- Breaking: all financial tools require strategyId/intentId; derivatives writes, unprotected buys, Funding movements, auto-subscribe and compounding are disabled in managed workflows.
- Deployment: persist the ledger, explicitly provision immutable allocations, use one ASGI worker, and reconcile pre-existing account state before live use.
