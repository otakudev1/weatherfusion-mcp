# WeatherFusion MCP Server

Zero-config standalone weather MCP server. Runs anywhere with **no API key** in standalone mode, or proxies to a WeatherFusion Data Layer when `WEATHERFUSION_API_URL` is set.

## Quick start (zero-config)

```bash
pip install mcp httpx pyjwt cryptography python-dotenv starlette uvicorn
python server.py
```

Then point your MCP client at `http://localhost:7860/sse`.

That's it. No API keys, no Supabase project, no accounts.

## Two modes

| Mode | Trigger | Auth | Data source | Best for |
|---|---|---|---|---|
| **Standalone** (default) | `WEATHERFUSION_API_URL` **unset** | Open or Supabase | Direct Open-Meteo + NWS | Local dev, quick tests, any host |
| **Proxied** | `WEATHERFUSION_API_URL` **set** | Supabase JWKS (or legacy HS256) | WeatherFusion Data Layer | Production with aggregated intelligence |

## Environment variables

| Variable | Required? | Mode | Description |
|---|---|---|---|
| `WEATHERFUSION_API_URL` | No | Proxied only | Base URL of the WeatherFusion Data Layer API |
| `SUPABASE_URL` | No | Both | Enables JWT verification via JWKS. **If omitted, server runs in OPEN mode (all tokens accepted).** |
| `SUPABASE_JWT_SECRET` | No | Proxied / legacy HS256 | Legacy shared-secret fallback for old Supabase projects |
| `MCP_PORT` | No | Both | HTTP port for the SSE endpoint (default 7860) |

## Auth details

### Standalone + no SUPABASE_URL (OPEN mode)
The server accepts **any** request. A loud warning is logged at startup. Fine for local development — **do not expose to the internet** without a reverse-proxy auth layer.

### Standalone + SUPABASE_URL set
Tokens are verified against your Supabase project's public JWKS endpoint. The `scopes` claim must include `weatherfusion:read`.

### Proxied mode
Same JWT verification as above. All weather queries are forwarded to `WEATHERFUSION_API_URL`.

## Tools

| Tool | Standalone behavior | Proxied behavior |
|---|---|---|
| `get_current_weather` | Open-Meteo current conditions | Proxy to Data Layer |
| `get_forecast` | Open-Meteo daily forecast (max 7 days) | Proxy to Data Layer |
| `compare_providers` | Compare `best_match`, `gfs_seamless`, `icon_seamless` models | Proxy to Data Layer |
| `get_confidence` | `100 - delta` across models, clipped 0-100 | Proxy to Data Layer |
| `get_alerts` | NWS alerts (US bounding-box check) | Proxy to Data Layer |

All tools accept either `lat` + `lon` **or** a `location` string (auto-geocoded via Open-Meteo).

## Deploy anywhere

> **HuggingFace Spaces deploy has been retired.** The project's HF account was banned. Use any of the options below instead.

- **Local / homelab**: `python server.py`
- **Railway / Render / Fly**: Dockerize and push (`Dockerfile` in repo)
- **VPS**: `git clone`, `pip install -r requirements.txt`, `python server.py` behind nginx/Caddy
- **Docker**: `docker build -t weatherfusion-mcp . && docker run -p 7860:7860 weatherfusion-mcp`

## Files

| File | Purpose |
|---|---|
| `server.py` | MCP server (SSE, 5 tools, dual-mode auth/standalone) |
| `Dockerfile` | Container runtime (port 7860) |
| `requirements.txt` | Python dependencies |
| `smoke_test.py` | Post-deploy MCP smoke test |
| `.env.example` | Local run reference |

## Notes
- Open-Meteo data is free, no key required, generous rate limits.
- NWS alerts are US-only in standalone mode; outside the US bounding box an empty list is returned with a note.
- 401 on `/sse` without a token in Supabase mode = fail-closed auth working as designed.
