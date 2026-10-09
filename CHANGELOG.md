# Changelog

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
