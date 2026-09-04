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
