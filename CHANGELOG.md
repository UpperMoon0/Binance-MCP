# Changelog

## 0.2.0

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
