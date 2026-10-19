# server.py — WeatherFusion MCP Server (SSE) — zero-secret auth edition
# Auth: verifies Supabase access tokens via the PUBLIC JWKS endpoint (asymmetric
# signing keys). No shared JWT secret required. Legacy HS256 fallback is optional.
import os
import json
import logging
from contextvars import ContextVar

from dotenv import load_dotenv
import httpx
import jwt
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wf-mcp")

WF_BASE = os.environ["WEATHERFUSION_API_URL"].rstrip("/")
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")  # optional: legacy HS256 projects only
REQUIRED_SCOPE = "weatherfusion:read"
PORT = int(os.getenv("MCP_PORT", "7860"))

JWKS_URL = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
_jwk_client = jwt.PyJWKClient(JWKS_URL)

# Holds decoded scopes for the currently-running SSE session task.
# Safe for single-tenant personal use; multi-tenant concurrency -> session-keyed map in Phase 4.
session_scopes: ContextVar[set] = ContextVar("session_scopes", default=set())

mcp = Server("weatherfusion")

TOOLS = [
    Tool(
        name="get_current_weather",
        description="Get current weather conditions for a location. Requires scope: weatherfusion:read",
        inputSchema={
            "type": "object",
            "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}},
            "required": ["lat", "lon"],
        },
    ),
    Tool(
        name="get_forecast",
        description="Get weather forecast for a location. Requires scope: weatherfusion:read",
        inputSchema={
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "days": {"type": "integer", "default": 5},
            },
            "required": ["lat", "lon"],
        },
    ),
    Tool(
        name="compare_providers",
        description="Compare weather provider outputs for a location. Requires scope: weatherfusion:read",
        inputSchema={
            "type": "object",
            "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}},
            "required": ["lat", "lon"],
        },
    ),
    Tool(
        name="get_confidence",
        description="Get confidence / disagreement score for a location. Requires scope: weatherfusion:read",
        inputSchema={
            "type": "object",
            "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}},
            "required": ["lat", "lon"],
        },
    ),
    Tool(
        name="get_alerts",
        description="Get active NWS weather alerts for a location. Requires scope: weatherfusion:read",
        inputSchema={
            "type": "object",
            "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}},
            "required": ["lat", "lon"],
        },
    ),
]


@mcp.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


async def _wf_api_get(endpoint: str, params: dict) -> dict:
    """Thin wrapper — no caching, no business logic duplication."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{WF_BASE}{endpoint}", params=params, timeout=30.0)
        r.raise_for_status()
        return r.json()


@mcp.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    scopes = session_scopes.get()
    if REQUIRED_SCOPE not in scopes:
        raise RuntimeError(f"Forbidden: missing required scope '{REQUIRED_SCOPE}'")

    lat, lon = arguments.get("lat"), arguments.get("lon")
    try:
        if name == "get_current_weather":
            data = await _wf_api_get("/v1/weather/current", {"lat": lat, "lon": lon})
        elif name == "get_forecast":
            data = await _wf_api_get("/v1/weather/forecast", {"lat": lat, "lon": lon, "days": arguments.get("days", 5)})
        elif name == "compare_providers":
            data = await _wf_api_get("/v1/weather/compare", {"lat": lat, "lon": lon})
        elif name == "get_confidence":
            data = await _wf_api_get("/v1/weather/confidence", {"lat": lat, "lon": lon})
        elif name == "get_alerts":
            data = await _wf_api_get("/v1/alerts", {"lat": lat, "lon": lon})
        else:
            raise ValueError(f"Unknown tool: {name}")
        return [TextContent(type="text", text=json.dumps(data, default=str))]
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"Upstream API error {e.response.status_code}: {e.response.text}")
    except Exception as e:
        raise RuntimeError(f"Tool error: {e}")


def verify_token(token: str) -> dict:
    """JWKS-first verification (publickeys, no secret). HS256 fallback only for
    legacy Supabase projects where SUPABASE_JWT_SECRET is explicitly provided."""
    try:
        signing_key = _jkw_client.get_signing_key_from_jwt(token)
        return jwt.decode(token, signing_key.key, algorithms=["ES256", "RS256"], audience="authenticated")
    except jwt.PyJWKClientError:
        if not SUPABASE_JWT_SECRET:
            raise jwt.InvalidTokenError(
                "Token not verifiable via JWKS and no legacy SUPABASE_JWT_SECRET configured"
            )
    if SUPABASE_JWT_SECRET:
        return jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=[HS256"], audience="authenticated")
    raise jwt.InvalidTokenError("No verification method available")


# ------ Transport + Auth -------------------------------------------------
sse = SseServerTransport("/messages")


async def handle_sse(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse({"error": "Missing Bearer token"}, status_code=401)
    try:
        payload = verify_token(auth[7:])
    except jwt.InvalidTokenError as exc:
        logger.warning("JWT validation failed: %s", exc)
        return JSONResponse({"error": "Invalid token"}, status_code=401)

    scopes = set(payload.get("scopes", []))
    if REQUIRED_SCOPE not in scopes:
        return JSONResponse({"error": f"Forbidden: missing scope '{REQUIRED_SCOPE}'"}, status_code=403)

    session_scopes.set(scopes)
    logger.info("SSE session established; scopes=%s", scopes)

    async with sse.connect_session(request.scope, request.receive, request.send) as session:
        await mcp.run(session)


routes = [
    Route("/sse", endpoint=handle_sse),
    Mount("/messages", app=sse.handle_post_message),
]

app = Starlette(routes=routes)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
