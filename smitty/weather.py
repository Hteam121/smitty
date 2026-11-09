"""Weather via Open-Meteo (free, no API key). Geocodes a place name, caches it."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "icy fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
       66: "freezing rain", 67: "heavy freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
       77: "snow grains", 80: "light showers", 81: "showers", 82: "violent showers", 85: "snow showers",
       86: "heavy snow showers", 95: "thunderstorms", 96: "thunderstorms with hail", 99: "severe thunderstorms with hail"}


class Weather:
    def __init__(self, state_file: Path, units: str = "f"):
        self.state_file = Path(state_file).expanduser()
        self.units = units
        self._state = {}
        if self.state_file.exists():
            try:
                self._state = json.loads(self.state_file.read_text())
            except Exception:
                pass

    @property
    def default_location(self) -> str | None:
        return self._state.get("location", {}).get("name")

    async def _geocode(self, session: aiohttp.ClientSession, name: str) -> dict:
        cached = self._state.get("location")
        if cached and cached["name"].lower() == name.lower():
            return cached
        async with session.get("https://geocoding-api.open-meteo.com/v1/search",
                               params={"name": name, "count": 1, "language": "en"}) as r:
            data = await r.json()
        hits = data.get("results") or []
        if not hits:
            raise ValueError(f"no place called {name!r}")
        h = hits[0]
        loc = {"name": name, "resolved": ", ".join(x for x in (h.get("name"), h.get("admin1"), h.get("country")) if x),
               "lat": h["latitude"], "lon": h["longitude"]}
        self._state["location"] = loc
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self._state, indent=2))
        return loc

    async def get(self, location: str | None = None) -> dict:
        name = location or self.default_location
        if not name:
            raise ValueError("no location known yet; ask the user where they are")
        temp_unit = "fahrenheit" if self.units == "f" else "celsius"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            loc = await self._geocode(s, name)
            async with s.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto", "forecast_days": 2,
                "temperature_unit": temp_unit, "wind_speed_unit": "mph" if self.units == "f" else "kmh",
                "current": "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,relative_humidity_2m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code,sunrise,sunset",
            }) as r:
                d = await r.json()
        cur, day = d["current"], d["daily"]
        u = "°F" if self.units == "f" else "°C"
        return {
            "location": loc["resolved"],
            "now": {"temp": f"{round(cur['temperature_2m'])}{u}", "feels_like": f"{round(cur['apparent_temperature'])}{u}",
                    "sky": WMO.get(cur["weather_code"], "unknown"), "wind": f"{round(cur['wind_speed_10m'])} {'mph' if self.units == 'f' else 'km/h'}",
                    "humidity": f"{cur['relative_humidity_2m']}%"},
            "today": {"high": f"{round(day['temperature_2m_max'][0])}{u}", "low": f"{round(day['temperature_2m_min'][0])}{u}",
                      "rain_chance": f"{day['precipitation_probability_max'][0]}%", "sky": WMO.get(day["weather_code"][0], "unknown"),
                      "sunrise": day["sunrise"][0][-5:], "sunset": day["sunset"][0][-5:]},
            "tomorrow": {"high": f"{round(day['temperature_2m_max'][1])}{u}", "low": f"{round(day['temperature_2m_min'][1])}{u}",
                         "rain_chance": f"{day['precipitation_probability_max'][1]}%", "sky": WMO.get(day["weather_code"][1], "unknown")},
        }
