# server.py — WeatherFusion MCP Server (SSE)
# ZERO-CONFIG STANDALONE MODE: when WEATHERFUSION_API_URL is absent, the server
# falls back to direct Open-Meteo calls with no API key required.
# AUTH: verifies Supabase access tokens via the PUBLIC JWKS endpoint when
# SUPABASE_URL is present. When absent, runs in OPEN mode with a loud warning.
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

# ---- Configuration -------------------------------------------------------
WF_BASE = os.environ.get("WEATHERFUSION_API_URL", "").rstrip("/") or None
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/") or None
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")  # optional: legacy HS256 projects only
REQUIRED_SCOPE = "weatherfusion:read"
PORT = int(os.getenv("MCP_PORT", "7860"))

# OPEN-METEO defaults for zero-config mode
OPEN_METEO_BASE = "https://api.open-meteo.com/v1"
GEOCODE_BASE = "https://geocoding-api.open-meteo.com/v1"
NWS_ALERTS_BASE = "https://api.weather.gov/alerts/active"

if WF_BASE:
    logger.info("WeatherFusion API mode: %s", WF_BASE)
else:
    logger.info("Open-Meteo standalone mode (zero-config)")

_jwk_client = None
if SUPABASE_URL:
    JWKS_URL = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
    _jwk_client = jwt.PyJWKClient(JWKS_URL)
else:
    logger.warning("=" * 65)
    logger.warning("SUPABASE_URL NOT SET — running in OPEN AUTH MODE.")
    logger.warning("All requests will be accepted. This is INSECURE for production.")
    logger.warning("Set SUPABASE_URL to enable Supabase JWT verification.")
    logger.warning("=" * 65)

# Holds decoded scopes for the currently-running SSE session task.
session_scopes: ContextVar[set] = ContextVar("session_scopes", default=set())

mcp = Server("weatherfusion")

# ---- Tool schemas --------------------------------------------------------
LOCATION_SCHEMA = {
    "type": "object",
    "properties": {
        "lat": {"type": "number", "description": "Latitude. Provide alongside lon if not using location."},
        "lon": {"type": "number", "description": "Longitude. Provide alongside lat if not using location."},
        "location": {"type": "string", "description": "City/place name. Auto-geocoded via Open-Meteo if lat/lon are omitted."},
    },
    "anyOf": [
        {"required": ["lat", "lon"]},
        {"required": ["location"]},
    ],
}

TOOLS = [
    Tool(
        name="get_current_weather",
        description="Get current weather conditions for a location. Scope: weatherfusion:read",
        inputSchema=LOCATION_SCHEMA,
    ),
    Tool(
        name="get_forecast",
        description="Get weather forecast for a location (up to 7 days in standalone mode). Scope: weatherfusion:read",
        inputSchema={
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "Latitude. Provide alongside lon if not using location."},
                "lon": {"type": "number", "description": "Longitude. Provide alongside lat if not using location."},
                "location": {"type": "string", "description": "City/place name. Auto-geocoded via Open-Meteo if lat/lon are omitted."},
                "days": {"type": "integer", "default": 5, "description": "Number of forecast days (max 7 in standalone mode)."},
            },
            "anyOf": [
                {"required": ["lat", "lon"]},
                {"required": ["location"]},
            ],
        },
    ),
    Tool(
        name="compare_providers",
        description="Compare weather model outputs for a location. Scope: weatherfusion:read",
        inputSchema=LOCATION_SCHEMA,
    ),
    Tool(
        name="get_confidence",
        description="Get confidence / disagreement score for a location. Scope: weatherfusion:read",
        inputSchema=LOCATION_SCHEMA,
    ),
    Tool(
        name="get_alerts",
        description="Get active NWS weather alerts for a location (US-only in standalone mode). Scope: weatherfusion:read",
        inputSchema=LOCATION_SCHEMA,
    ),
]


@mcp.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


# ---- Helpers -------------------------------------------------------------
async def _geocode(location: str) -> tuple[float, float]:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GEOCODE_BASE}/search",
            params={"name": location, "count": 1},
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("results", [])
        if not results:
            raise ValueError(f"Location not found: {location}")
        return float(results[0]["latitude"]), float(results[0]["longitude"])


async def _resolve_lat_lon(arguments: dict) -> tuple[float, float]:
    if arguments.get("location"):
        return await _geocode(arguments["location"])
    lat = arguments.get("lat")
    lon = arguments.get("lon")
    if lat is None or lon is None:
        raise ValueError("Either 'location' or both 'lat' and 'lon' must be provided.")
    return float(lat), float(lon)


async def _wf_api_get(endpoint: str, params: dict) -> dict:
    """Thin wrapper around the WeatherFusion Data Layer API."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{WF_BASE}{endpoint}", params=params, timeout=30.0)
        r.raise_for_status()
        return r.json()


# ---- Open-Meteo standalone handlers ------------------------------------
async def _om_current(lat: float, lon: float) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,precipitation,rain,showers,snowfall,cloud_cover,pressure_msl",
        "timezone": "auto",
    }
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{OPEN_METEO_BASE}/forecast", params=params, timeout=15.0)
        r.raise_for_status()
        data = r.json()

    current = data.get("current", {})
    return {
        "location": {"lat": lat, "lon": lon},
        "current": {
            "temperature": current.get("temperature_2m"),
            "humidity": current.get("relative_humidity_2m"),
            "feels_like": current.get("apparent_temperature"),
            "weather_code": current.get("weather_code"),
            "wind_speed": current.get("wind_speed_10m"),
            "wind_direction": current.get("wind_direction_10m"),
            "precipitation": current.get("precipitation"),
            "rain": current.get("rain"),
            "showers": current.get("showers"),
            "snowfall": current.get("snowfall"),
            "cloud_cover": current.get("cloud_cover"),
            "pressure": current.get("pressure_msl"),
        },
        "units": data.get("current_units", {}),
        "source": "open-meteo",
    }


async def _om_forecast(lat: float, lon: float, days: int) -> dict:
    days = min(max(days, 1), 7)  # cap 1-7 in standalone mode
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code,wind_speed_10m_max",
        "timezone": "auto",
        "forecast_days": days,
    }
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{OPEN_METEO_BASE}/forecast", params=params, timeout=15.0)
        r.raise_for_status()
        data = r.json()

    daily_raw = data.get("daily", {})
    dates = daily_raw.get("time", [])
    daily_list = []
    for i, date in enumerate(dates):
        daily_list.append({
            "date": date,
            "temp_max": daily_raw.get("temperature_2m_max", [])[i] if i < len(daily_raw.get("temperature_2m_max", [])) else None,
            "temp_min": daily_raw.get("temperature_2m_min", [])[i] if i < len(daily_raw.get("temperature_2m_min", [])) else None,
            "precipitation": daily_raw.get("precipitation_sum", [])[i] if i < len(daily_raw.get("precipitation_sum", [])) else None,
            "weather_code": daily_raw.get("weather_code", [])[i] if i < len(daily_raw.get("weather_code", [])) else None,
            "wind_speed_max": daily_raw.get("wind_speed_10m_max", [])[i] if i < len(daily_raw.get("wind_speed_10m_max", [])) else None,
        })

    return {
        "location": {"lat": lat, "lon": lon},
        "daily": daily_list,
        "days": days,
        "source": "open-meteo",
    }


async def _om_compare(lat: float, lon: float) -> dict:
    models = ["best_match", "gfs_seamless", "icon_seamless"]
    providers = []
    temps = []
    async with httpx.AsyncClient() as client:
        for model in models:
            r = await client.get(
                f"{OPEN_METEO_BASE}/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m",
                    "models": model,
                    "timezone": "auto",
                },
                timeout=15.0,
            )
            r.raise_for_status()
            data = r.json()
            temp = data.get("current", {}).get("temperature_2m")
            providers.append({"model": model, "temperature": temp})
            if temp is not None:
                temps.append(temp)

    spread = None
    if temps:
        spread = {"min": min(temps), "max": max(temps), "delta": round(max(temps) - min(temps), 2)}

    return {
        "location": {"lat": lat, "lon": lon},
        "providers": providers,
        "spread": spread,
        "source": "open-meteo",
    }


async def _om_confidence(lat: float, lon: float) -> dict:
    compare = await _om_compare(lat, lon)
    spread = compare.get("spread")
    if spread and spread.get("delta") is not None:
        delta = spread["delta"]
        score = max(0.0, min(100.0, 100.0 - delta))
    else:
        score = 0.0
        delta = None
    return {
        "location": {"lat": lat, "lon": lon},
        "confidence": round(score, 1),
        "spread_delta": delta,
        "source": "open-meteo",
    }


async def _om_alerts(lat: float, lon: float) -> dict:
    # Rough US bounding box (continental + AK + HI + territories)
    in_us = (15.0 <= lat <= 75.0) and (-180.0 <= lon <= -64.0)
    if not in_us:
        return {"alerts": [], "note": "US-only coverage", "count": 0, "source": "nws"}

    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{NWS_ALERTS_BASE}",
            params={"point": f"{lat},{lon}"},
            headers={"User-Agent": "WeatherFusion-MCP/1.0 (github.com/otakudev1/weatherfusion-mcp)"},
            timeout=15.0,
        )
        if r.status_code == 404:
            return {"alerts": [], "note": "No active alerts for this point", "count": 0, "source": "nws"}
        r.raise_for_status()
        data = r.json()

    features = data.get("features", [])
    alerts = []
    for f in features:
        props = f.get("properties", {})
        alerts.append({
            "event": props.get("event"),
            "severity": props.get("severity"),
            "certainty": props.get("certainty"),
            "urgency": props.get("urgency"),
            "headline": props.get("headline"),
            "description": props.get("description"),
            "effective": props.get("effective"),
            "expires": props.get("expires"),
        })

    return {
        "alerts": alerts,
        "count": len(alerts),
        "note": None,
        "source": "nws",
    }


# ---- Tool dispatcher -----------------------------------------------------
@mcp.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    scopes = session_scopes.get()
    if REQUIRED_SCOPE not in scopes:
        raise RuntimeError(f"Forbidden: missing required scope '{REQUIRED_SCOPE}'")

    lat, lon = await _resolve_lat_lon(arguments)

    try:
        # Standalone (Open-Meteo) path
        if not WF_BASE:
            if name == "get_current_weather":
                data = await _om_current(lat, lon)
            elif name == "get_forecast":
                days = arguments.get("days", 5)
                data = await _om_forecast(lat, lon, days)
            elif name == "compare_providers":
                data = await _om_compare(lat, lon)
            elif name == "get_confidence":
                data = await _om_confidence(lat, lon)
            elif name == "get_alerts":
                data = await _om_alerts(lat, lon)
            else:
                raise ValueError(f"Unknown tool: {name}")
            return [TextContent(type="text", text=json.dumps(data, default=str))]

        # Proxied WeatherFusion API path
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


# ---- Auth ---------------------------------------------------------------
def verify_token(token: str) -> dict:
    if _jwk_client is None:
        # OPEN mode: this function is bypassed in the handler, but keep a safe fallback.
        return {"scopes": [REQUIRED_SCOPE]}

    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(token)
        return jwt.decode(token, signing_key.key, algorithms=["ES256", "RS256"], audience="authenticated")
    except jwt.PyJWKClientError:
        if not SUPABASE_JWT_SECRET:
            raise jwt.InvalidTokenError(
                "Token not verifiable via JWKS and no legacy SUPABASE_JWT_SECRET configured"
            )
    if SUPABASE_JWT_SECRET:
        return jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="authenticated")
    raise jwt.InvalidTokenError("No verification method available")


# ---- Transport ----------------------------------------------------------
sse = SseServerTransport("/messages/")


async def handle_sse(request: Request):
    # OPEN auth mode: loud warning already emitted at startup.
    if not SUPABASE_URL:
        logger.warning("OPEN MODE: accepting unauthenticated SSE connection (client=%s)", request.client.host if request.client else "unknown")
        session_scopes.set({REQUIRED_SCOPE})
        async with sse.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
            await mcp.run(read_stream, write_stream, mcp.create_initialization_options())
        return

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

    async with sse.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
        await mcp.run(read_stream, write_stream, mcp.create_initialization_options())


routes = [
    Route("/health", lambda request: JSONResponse({"status": "ok", "service": "weatherfusion-mcp"})),
    Route("/sse", endpoint=handle_sse),
    Mount("/messages", app=sse.handle_post_message),
]

app = Starlette(routes=routes)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
