# Sentinel MCP authentication

Production MCP sign-in uses Sentinel account authentication with an explicit
account allowlist (account ID 1). The shared `lily-mcp-owner-token` Key Vault
secret is retired. It must not be read, deployed, or used to approve a client.
Kaggle/Discord/Binance credentials and Lily node enrollment credentials remain
separate from user authentication and are never issued as MCP client tokens.

OAuth client registration, PKCE S256, resource-bound access tokens and refresh
rotation remain service-local. Existing clients issued under the retired login
must reconnect and sign in with Sentinel. Minecraft uses dedicated RPC and UI
session credentials; neither falls back to the retired owner secret.

The private NsTut-CICD configuration owns production rollout. Authentication-only
Python image overlays preserve the running application version and data.
