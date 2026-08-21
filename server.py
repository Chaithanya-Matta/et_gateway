"""FastMCP tools that return concise weather and entertainment information.

The server calls public APIs asynchronously, removes fields an AI agent does not
need, and returns small structured dictionaries suitable for spoken responses.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import AsyncIterator, Mapping
from typing import Any, Literal

import httpx
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan
from mcp.types import ToolAnnotations
from typing_extensions import TypedDict


logger = logging.getLogger(__name__)

# Public, no-account API endpoints used by the MCP tools.
TVMAZE_SEARCH_URL = "https://api.tvmaze.com/singlesearch/shows"
OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

MAX_QUERY_LENGTH = 200
HTML_TAG_RE = re.compile(r"<[^>]*>")
WHITESPACE_RE = re.compile(r"\s+")
CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")


# TypedDicts give FastMCP exact JSON output schemas for both tools.
class ShowInfo(TypedDict):
    title: str
    genres: list[str]
    rating: str | float
    language: str
    summary: str


class WeatherInfo(TypedDict):
    location: str
    observed_at: str
    temperature: float
    temperature_unit: str
    feels_like: float
    conditions: str
    humidity_percent: int
    wind_speed: float
    wind_speed_unit: str
    today_high: float
    today_low: float
    precipitation_probability_percent: int
    timezone: str


@lifespan
async def app_lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Create one connection-pooled HTTP client for the server process."""
    # Timeouts prevent slow upstream APIs from holding an MCP request indefinitely.
    timeout = httpx.Timeout(10.0, connect=3.0)
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
    headers = {
        "Accept": "application/json",
        "User-Agent": "weather-entertainment-mcp/1.0",
    }

    async with httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        headers=headers,
        follow_redirects=True,
    ) as client:
        # Tools retrieve this shared client through their injected Context argument.
        yield {"http_client": client}


# Mask unexpected exception details while allowing deliberate ToolError messages.
mcp = FastMCP(
    name="Weather and Entertainment Info",
    lifespan=app_lifespan,
    mask_error_details=True,
    strict_input_validation=True,
)

# Tell MCP clients that these tools only read data from external services.
READ_ONLY_EXTERNAL_TOOL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _normalize_query(value: str, field_name: str) -> str:
    """Validate and normalize a short, human-readable search query."""
    if not isinstance(value, str):
        raise ToolError(f"{field_name} must be a string.")

    normalized = WHITESPACE_RE.sub(" ", value).strip()
    if not normalized:
        raise ToolError(f"{field_name} cannot be empty.")
    if len(normalized) > MAX_QUERY_LENGTH:
        raise ToolError(
            f"{field_name} must be {MAX_QUERY_LENGTH} characters or fewer."
        )
    if CONTROL_CHARACTER_RE.search(normalized):
        raise ToolError(f"{field_name} contains unsupported control characters.")

    return normalized


def _strip_html(value: object) -> str:
    """Remove HTML tags and normalize entities and spacing for spoken output."""
    if not isinstance(value, str):
        return ""

    without_tags = HTML_TAG_RE.sub(" ", value)
    decoded = html.unescape(without_tags)
    return WHITESPACE_RE.sub(" ", decoded).strip()


def _weather_condition(code: object) -> str:
    """Convert an Open-Meteo WMO weather code into short spoken text."""
    if not isinstance(code, (int, float)) or isinstance(code, bool):
        return "Unknown conditions"

    descriptions = {
        0: "Clear sky",
        1: "Mainly clear",
        2: "Partly cloudy",
        3: "Overcast",
        45: "Fog",
        48: "Rime fog",
        51: "Light drizzle",
        53: "Moderate drizzle",
        55: "Dense drizzle",
        56: "Light freezing drizzle",
        57: "Dense freezing drizzle",
        61: "Light rain",
        63: "Moderate rain",
        65: "Heavy rain",
        66: "Light freezing rain",
        67: "Heavy freezing rain",
        71: "Light snowfall",
        73: "Moderate snowfall",
        75: "Heavy snowfall",
        77: "Snow grains",
        80: "Light rain showers",
        81: "Moderate rain showers",
        82: "Violent rain showers",
        85: "Light snow showers",
        86: "Heavy snow showers",
        95: "Thunderstorm",
        96: "Thunderstorm with light hail",
        99: "Thunderstorm with heavy hail",
    }
    return descriptions.get(int(code), "Unknown conditions")


def _required_number(value: object, field_name: str) -> float:
    """Return a numeric weather field or raise a safe incomplete-data error."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ToolError(
            f"The weather service did not provide a valid {field_name} value."
        )
    return float(value)


def _first_number(values: object, field_name: str) -> float:
    """Read the first numeric value from a daily forecast array."""
    if not isinstance(values, list) or not values:
        raise ToolError(
            f"The weather service did not provide a valid {field_name} value."
        )
    return _required_number(values[0], field_name)


def _format_location(location_data: dict[str, Any], fallback: str) -> str:
    """Build a readable city, state/region, country label without duplicates."""
    parts: list[str] = []
    seen: set[str] = set()
    for key in ("name", "admin1", "country"):
        value = location_data.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        cleaned = value.strip()
        normalized = cleaned.casefold()
        if normalized not in seen:
            parts.append(cleaned)
            seen.add(normalized)
    return ", ".join(parts) if parts else fallback


def _get_http_client(ctx: Context) -> httpx.AsyncClient:
    """Read the shared HTTP client from the FastMCP lifespan context."""
    client = ctx.lifespan_context.get("http_client")
    if not isinstance(client, httpx.AsyncClient):
        raise ToolError("The upstream API client is not available. Try again shortly.")
    return client


async def _request_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: Mapping[str, str],
    resource_name: str,
) -> Any:
    """Fetch JSON while translating transport failures into safe MCP errors."""
    try:
        response = await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        logger.warning("Timed out while requesting %s: %s", resource_name, exc)
        raise ToolError(
            f"The {resource_name} service took too long to respond. Try again."
        ) from exc
    except httpx.RequestError as exc:
        logger.warning("Network error while requesting %s: %s", resource_name, exc)
        raise ToolError(
            f"The {resource_name} service is temporarily unreachable. Try again."
        ) from exc

    # A missing record is more useful to the agent than a generic HTTP 404.
    if response.status_code == 404:
        raise ToolError(f"No matching {resource_name} was found.")

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        logger.warning(
            "Upstream %s request failed with HTTP %s",
            resource_name,
            status_code,
        )
        if status_code == 429:
            message = f"The {resource_name} service is busy. Try again shortly."
        elif status_code >= 500:
            message = f"The {resource_name} service is temporarily unavailable."
        else:
            message = f"The {resource_name} service could not complete the request."
        raise ToolError(message) from exc

    try:
        return response.json()
    except ValueError as exc:
        logger.warning("Invalid JSON returned by the %s service", resource_name)
        raise ToolError(
            f"The {resource_name} service returned an invalid response."
        ) from exc


@mcp.tool(
    annotations=READ_ONLY_EXTERNAL_TOOL,
    timeout=12.0,
)
async def get_show_or_movie_info(title: str, ctx: Context) -> ShowInfo:
    """Get concise details for a movie or TV show.

    Args:
        title: Movie or television show title to search for.
    """
    normalized_title = _normalize_query(title, "title")
    payload = await _request_json(
        _get_http_client(ctx),
        TVMAZE_SEARCH_URL,
        params={"q": normalized_title},
        resource_name="movie or show",
    )

    if not isinstance(payload, dict):
        raise ToolError("The movie or show service returned an invalid response.")

    # Read only the five fields exposed by the MCP tool's compact output contract.
    raw_name = payload.get("name")
    show_title = raw_name.strip() if isinstance(raw_name, str) else normalized_title

    raw_genres = payload.get("genres")
    genres = (
        [
            genre.strip()
            for genre in raw_genres
            if isinstance(genre, str) and genre.strip()
        ]
        if isinstance(raw_genres, list)
        else []
    )

    raw_rating = payload.get("rating")
    average = raw_rating.get("average") if isinstance(raw_rating, dict) else None
    rating: str | float = (
        float(average)
        if isinstance(average, (int, float)) and not isinstance(average, bool)
        else "N/A"
    )

    raw_language = payload.get("language")
    language = (
        raw_language.strip()
        if isinstance(raw_language, str) and raw_language.strip()
        else "N/A"
    )

    return {
        "title": show_title,
        "genres": genres,
        "rating": rating,
        "language": language,
        "summary": _strip_html(payload.get("summary")),
    }


@mcp.tool(
    annotations=READ_ONLY_EXTERNAL_TOOL,
    timeout=12.0,
)
async def get_weather_info(
    location: str,
    ctx: Context,
    temperature_unit: Literal["celsius", "fahrenheit"] = "celsius",
) -> WeatherInfo:
    """Get current conditions and today's forecast for a location.

    Args:
        location: City, postal code, or city and region/country to search for.
        temperature_unit: Return temperatures in celsius or fahrenheit.
    """
    normalized_location = _normalize_query(location, "location")
    client = _get_http_client(ctx)

    # Resolve a spoken place name to the coordinates required by the forecast API.
    geocoding_payload = await _request_json(
        client,
        OPEN_METEO_GEOCODING_URL,
        params={
            "name": normalized_location,
            "count": "1",
            "language": "en",
            "format": "json",
        },
        resource_name="location",
    )

    if not isinstance(geocoding_payload, dict):
        raise ToolError("The location service returned an invalid response.")
    results = geocoding_payload.get("results")
    if not isinstance(results, list) or not results:
        raise ToolError(f'No matching location was found for "{normalized_location}".')
    if not isinstance(results[0], dict):
        raise ToolError("The location service returned an invalid response.")

    location_data = results[0]
    latitude = _required_number(location_data.get("latitude"), "latitude")
    longitude = _required_number(location_data.get("longitude"), "longitude")
    display_location = _format_location(location_data, normalized_location)

    # Open-Meteo uses coordinates and can return local time plus requested units.
    wind_speed_unit = "mph" if temperature_unit == "fahrenheit" else "kmh"
    weather_payload = await _request_json(
        client,
        OPEN_METEO_FORECAST_URL,
        params={
            "latitude": str(latitude),
            "longitude": str(longitude),
            "current": (
                "temperature_2m,apparent_temperature,relative_humidity_2m,"
                "weather_code,wind_speed_10m"
            ),
            "daily": (
                "temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max"
            ),
            "temperature_unit": temperature_unit,
            "wind_speed_unit": wind_speed_unit,
            "timezone": "auto",
            "forecast_days": "1",
        },
        resource_name="weather",
    )

    if not isinstance(weather_payload, dict):
        raise ToolError("The weather service returned an invalid response.")
    current = weather_payload.get("current")
    current_units = weather_payload.get("current_units")
    daily = weather_payload.get("daily")
    if (
        not isinstance(current, dict)
        or not isinstance(current_units, dict)
        or not isinstance(daily, dict)
    ):
        raise ToolError("The weather service returned an incomplete response.")

    observed_at = current.get("time")
    timezone = weather_payload.get("timezone")
    temperature_symbol = current_units.get("temperature_2m")
    wind_symbol = current_units.get("wind_speed_10m")

    humidity = round(
        _required_number(current.get("relative_humidity_2m"), "humidity")
    )
    precipitation_probability = round(
        _first_number(
            daily.get("precipitation_probability_max"),
            "precipitation probability",
        )
    )

    return {
        "location": display_location,
        "observed_at": observed_at if isinstance(observed_at, str) else "N/A",
        "temperature": _required_number(current.get("temperature_2m"), "temperature"),
        "temperature_unit": (
            temperature_symbol if isinstance(temperature_symbol, str) else "°C"
        ),
        "feels_like": _required_number(
            current.get("apparent_temperature"),
            "apparent temperature",
        ),
        "conditions": _weather_condition(current.get("weather_code")),
        "humidity_percent": humidity,
        "wind_speed": _required_number(current.get("wind_speed_10m"), "wind speed"),
        "wind_speed_unit": (
            wind_symbol if isinstance(wind_symbol, str) else wind_speed_unit
        ),
        "today_high": _first_number(daily.get("temperature_2m_max"), "daily high"),
        "today_low": _first_number(daily.get("temperature_2m_min"), "daily low"),
        "precipitation_probability_percent": precipitation_probability,
        "timezone": timezone if isinstance(timezone, str) else "N/A",
    }


if __name__ == "__main__":
    # Bind to every container interface on Cloud Run's expected port using SSE.
    mcp.run(transport="sse", host="0.0.0.0", port=8080)
