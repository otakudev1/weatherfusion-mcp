# WeatherFusion MCP Server — deploy bundle

Zero-desktop-dependency deploy. The server verifies Supabase JWTs via the **public
JWKS endpoint** — the `SUPABASE_JWT_SECRET` (and the Supabase dashboard visit it
required) is **no longer needed** for projects on asymmetric signing keys.

## Files
| File | Purpose |
|---|---|
| `server.py` | MCP server (SSE, 5 tools, JWKS-first auth, HS256 optional fallback) |
| `deploy.py` | One-command deploy: create Space → upload → set config → restart → health check |
| `Dockerfile` | HF Spaces docker runtime (port 7860) |
| `requirements.txt` | Server deps (incl. `cryptography` for ES256/RS256) |
| `smoke_test.py` | Post-deploy MCP smoke test |
| `.env.example` | Local run reference |

## The ONLY remaining manual step (mobile-feasible)
Create one HuggingFace token with **Write** access: https://huggingface.co/settings/tokens
→ "Create new token" → Write → copy. Then in the fusion-engine space (cloud sandbox run):

```bash
pip install huggingface_hub httpx
HF_TOKEN=<paste-token> python deploy.py \
  --repo-id otakudev1/weatherfusion-mcp \
  --api-url https://<data-layer-api-host> \
  --supabase-url https://<project-ref>.supabase.co
```

That single command does everything the dashboards used to do. Nothing else is manual.

## Notes
- `SUPABASE_URL` and `WEATHERFUSION_API_URL` are plain variables, not secrets.
- Legacy HS256 projects: pass `--jwt-secret` to `deploy.py` (stored as a Space secret).
- 401 on `/sse` without a token = fail-closed auth working as designed.
