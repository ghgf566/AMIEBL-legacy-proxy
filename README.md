# llama-vscode-proxy v2

A localhost compatibility/cancellation proxy between VS Code BYOK Agent and `llama-server`.

Default topology:

- VS Code -> `http://127.0.0.1:8080`
- Proxy -> `http://127.0.0.1:8081`

## What v2 fixes

- Registers chat requests as ACTIVE **before** waiting for upstream response headers.
- Watches downstream disconnects independently of upstream SSE output.
- Watches the current VS Code Agent/Copilot log for Stop/Abort events.
- A VS Code abort cancels only the **newest** active conversation, not every slot.
- Explicit `DELETE /v1/stream?conv_id=...` cancellation retries on failure.
- Cancellation is only marked confirmed after an upstream `2xx` response.
- VS Code log discovery runs at a slower interval; the selected log is tailed quickly.
- New/rotated log files replay a small tail to avoid missing an abort written before discovery.
- `/health` reports watcher/cancellation status.
- Full request-body logging is **OFF by default**.
- Admin endpoints require loopback access plus `X-Proxy-Admin-Token`.

## Environment variables

Common settings:

```powershell
$env:PROXY_HOST = "127.0.0.1"
$env:PROXY_PORT = "8080"
$env:UPSTREAM_BASE = "http://127.0.0.1:8081"
```

Full request body logging is intentionally disabled by default:

```powershell
$env:LOG_REQUEST_BODIES = "1"   # only enable while debugging
```

Optional stable admin token:

```powershell
$env:PROXY_ADMIN_TOKEN = "choose-a-long-random-token"
```

If `PROXY_ADMIN_TOKEN` is unset, the proxy generates a random token for that run and prints it at startup.

## Health

No admin token is required:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health
```

The health response includes the active request list, retry state, watched VS Code log, and last detected Abort event.

## Admin endpoints

Admin endpoints require the token:

```powershell
$headers = @{ "X-Proxy-Admin-Token" = "your-token" }

Invoke-RestMethod `
  -Headers $headers `
  http://127.0.0.1:8080/__proxy/active

Invoke-RestMethod `
  -Method Post `
  -Headers $headers `
  http://127.0.0.1:8080/__proxy/cancel-newest

Invoke-RestMethod `
  -Method Post `
  -Headers $headers `
  http://127.0.0.1:8080/__proxy/cancel-all
```

## Cancellation behavior

Normal socket disconnect:

```text
VS Code socket closes
-> downstream watcher notices it
-> proxy sends DELETE /v1/stream?conv_id=...
```

VS Code Agent Stop when the BYOK HTTP request remains open:

```text
Agent Host writes an Abort/Cancel log line
-> proxy log watcher sees it
-> newest active conversation is selected
-> proxy sends DELETE /v1/stream?conv_id=...
```

This is deliberately a fallback because the Agent log currently does not expose a reliable session -> conversation ID mapping. If a future VS Code version exposes that mapping, the proxy can cancel the exact request instead of the newest one.

## Important llama.cpp note

The proxy can deliver a valid explicit cancellation to llama.cpp. Whether a specific llama.cpp build reacts immediately during pre-first-token prompt processing is a separate server-side behavior.
