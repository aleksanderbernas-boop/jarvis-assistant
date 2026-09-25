"""Live weather from Open-Meteo (free, no API key)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx

from jarvis.web.client import WebError

_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Heavy freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
    77: "Snow grains", 80: "Light showers", 81: "Showers", 82: "Violent showers", 85: "Snow showers",
    86: "Heavy snow showers", 95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Severe thunderstorm",
}


def describe(code: int | None) -> str:
    return _CODES.get(code if code is not None else -1, "Unknown")


@dataclass
class Location:
    name: str
    latitude: float
    longitude: float


class WeatherService:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, simulated: bool = False) -> None:
        self._client = httpx.AsyncClient(timeout=10.0, transport=transport)
        self._simulated = simulated

    async def close(self) -> None:
        await self._client.aclose()

    async def geocode(self, query: str) -> Location:
        if self._simulated:
            return Location(f"{query} (simulated)", 50.06, 19.94)
        try:
            resp = await self._client.get("https://geocoding-api.open-meteo.com/v1/search",
                                          params={"name": query, "count": 1})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise WebError(f"location lookup failed: {type(exc).__name__}") from exc
        results = resp.json().get("results") or []
        if not results:
            raise WebError(f"I couldn't find a place called '{query}'")
        top = results[0]
        name = ", ".join(p for p in (top.get("name"), top.get("country_code")) if p)
        return Location(name, top["latitude"], top["longitude"])

    async def forecast(self, location: Location, days: int = 4) -> dict[str, Any]:
        if self._simulated:
            return {
                "location": location.name, "simulated": True,
                "current": {"temperature_c": 18.0, "condition": "Partly cloudy", "code": 2, "wind_kmh": 9.0},
                "daily": [{"date": (date.today() + timedelta(days=i)).isoformat(), "max_c": 21 - i, "min_c": 12, "condition": "Partly cloudy",
                           "code": 2, "precipitation_probability": 20} for i in range(days)],
            }
        try:
            resp = await self._client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                    "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "timezone": "auto",
                    "forecast_days": days,
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise WebError(f"weather service unavailable: {type(exc).__name__}") from exc
        data = resp.json()
        current = data.get("current", {})
        daily = data.get("daily", {})
        return {
            "location": location.name,
            "current": {
                "temperature_c": current.get("temperature_2m"),
                "feels_like_c": current.get("apparent_temperature"),
                "condition": describe(current.get("weather_code")),
                "code": current.get("weather_code"),
                "wind_kmh": current.get("wind_speed_10m"),
                "precipitation_mm": current.get("precipitation"),
            },
            "daily": [
                {
                    "date": date,
                    "max_c": daily["temperature_2m_max"][i],
                    "min_c": daily["temperature_2m_min"][i],
                    "condition": describe(daily["weather_code"][i]),
                    "code": daily["weather_code"][i],
                    "precipitation_probability": (daily.get("precipitation_probability_max") or [None] * days)[i],
                }
                for i, date in enumerate(daily.get("time", []))
            ],
        }
