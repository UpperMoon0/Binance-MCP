# Security Policy

## Secrets

Never report Binance API keys, HMAC secrets, private keys, MCP owner tokens, OAuth access tokens, or refresh tokens in an issue.

If a secret is exposed, revoke/rotate it before filing a report. Use GitHub's private vulnerability reporting mechanism when available.

## Deployment guidance

- Keep MCP OAuth enabled on every internet-facing deployment.
- Store OAuth state and asymmetric keys with owner-only filesystem permissions.
- Use a Binance API key dedicated to this service.
- Enable only the Binance permissions the deployment needs.
- Keep `BINANCE_TRADING_ENABLED=false` unless order mutation is explicitly required.
- Prefer IP restrictions on Binance API keys where the deployment has a stable egress IP.
- Withdrawals are deliberately not implemented.
- Never run untrusted public-PR code on a privileged self-hosted runner.

## Execution state

- Persist and back up the SQLite execution ledger alongside OAuth state. Never reset it to release an uncertain order.
- Provision strategy allocations and accounting audits only through trusted deployment-owner access. Neither is an MCP caller permission.
- Use one ASGI worker for the monitor and execution coordinator; independent deployments must not use separate ledgers for the same account capital.
- Live entries require a current User Data Stream connection, complete account/location coverage and reconciled protection.
- Paper mode is a conservative simulation and does not certify real-exchange execution.
- Import reward, deposit, withdrawal and settlement attribution only from reviewed exchange evidence. Stop the service while importing owner audits.
