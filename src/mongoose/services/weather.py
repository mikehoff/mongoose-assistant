from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from zoneinfo import ZoneInfo

from ..db import audit, connect, init_db, utc_now_iso
from ..settings import get_settings


def _configured_tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().timezone)
    except Exception:
        return ZoneInfo("UTC")


def _local_now() -> datetime:
    return datetime.now(_configured_tz())


def _as_configured_time(value: str) -> datetime:
    tz = _configured_tz()
    parsed = _parse_iso(value)
    if parsed.tzinfo is None:
        # Open-Meteo returns naive local timestamps when a named timezone is requested.
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _parse_iso(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _expires(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _weather_code_label(code: int | None) -> str:
    if code is None:
        return "unknown"
    if code == 0:
        return "clear"
    if code in {1, 2}:
        return "partly_cloudy"
    if code == 3:
        return "cloudy"
    if code in {45, 48}:
        return "fog"
    if code in {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}:
        return "rain"
    if code in {71, 73, 75, 77, 85, 86}:
        return "snow"
    if code in {95, 96, 99}:
        return "storm"
    return "mixed"


@dataclass
class ProviderResult:
    provider: str
    payload: dict[str, Any]
    has_minutely: bool = False


class OpenMeteoProvider:
    name = "open_meteo"

    async def fetch(self, lat: float, lon: float, timezone_name: str) -> ProviderResult:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "timezone": timezone_name,
            "current": "temperature_2m,apparent_temperature,precipitation,rain,showers,weather_code,wind_speed_10m,wind_gusts_10m",
            "hourly": "temperature_2m,apparent_temperature,precipitation,precipitation_probability,rain,showers,uv_index,wind_speed_10m,wind_gusts_10m,weather_code",
            "forecast_days": 2,
        }
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return ProviderResult(self.name, response.json(), has_minutely=False)


class TomorrowProvider:
    name = "tomorrow"

    async def fetch(self, lat: float, lon: float, timezone_name: str) -> ProviderResult | None:
        settings = get_settings()
        if not settings.tomorrow_api_key:
            return None
        url = "https://api.tomorrow.io/v4/weather/forecast"
        params = {
            "location": f"{lat},{lon}",
            "apikey": settings.tomorrow_api_key,
            "timesteps": "1m,1h,1d",
            "units": "metric",
        }
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return ProviderResult(self.name, response.json(), has_minutely=True)


class PlaceholderProvider:
    def __init__(self, name: str):
        self.name = name

    async def fetch(self, lat: float, lon: float, timezone_name: str) -> ProviderResult | None:
        return None


def providers() -> dict[str, Any]:
    return {
        "open_meteo": OpenMeteoProvider(),
        "tomorrow": TomorrowProvider(),
        "metoffice": PlaceholderProvider("metoffice"),
        "weatherkit": PlaceholderProvider("weatherkit"),
    }


def _cache_key(provider: str, lat: float, lon: float) -> str:
    return f"weather:{provider}:{lat:.4f}:{lon:.4f}"


def _load_cached_provider(provider: str, lat: float, lon: float) -> ProviderResult | None:
    key = _cache_key(provider, lat, lon)
    with connect() as con:
        row = con.execute("SELECT * FROM weather_cache WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        if _parse_iso(row["expires_at"]) < datetime.now(timezone.utc):
            return None
        return ProviderResult(provider=row["provider"], payload=json.loads(row["payload_json"]), has_minutely=provider == "tomorrow")


def _store_provider(result: ProviderResult, lat: float, lon: float, ttl_seconds: int) -> None:
    key = _cache_key(result.provider, lat, lon)
    with connect() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO weather_cache(key, provider, payload_json, fetched_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (key, result.provider, json.dumps(result.payload), utc_now_iso(), _expires(ttl_seconds)),
        )


async def fetch_weather_sources(force: bool = False) -> list[ProviderResult]:
    settings = get_settings()
    results: list[ProviderResult] = []
    available = providers()
    for provider_name in settings.provider_names:
        provider = available.get(provider_name)
        if not provider:
            continue
        cached = None if force else _load_cached_provider(provider_name, settings.home_lat, settings.home_lon)
        if cached:
            results.append(cached)
            continue
        try:
            result = await provider.fetch(settings.home_lat, settings.home_lon, settings.timezone)
        except Exception as exc:
            audit("weather", "fetch_provider", False, route=provider_name, reason=str(exc))
            continue
        if result:
            ttl = settings.weather_minutely_ttl_seconds if result.has_minutely else settings.weather_hourly_ttl_seconds
            _store_provider(result, settings.home_lat, settings.home_lon, ttl)
            results.append(result)
    return results


def _normalise_open_meteo(payload: dict[str, Any]) -> dict[str, Any]:
    current = payload.get("current", {})
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    feels = hourly.get("apparent_temperature", [])
    precip_prob = hourly.get("precipitation_probability", [])
    precip = hourly.get("precipitation", [])
    uv = hourly.get("uv_index", [])
    wind = hourly.get("wind_speed_10m", [])
    gusts = hourly.get("wind_gusts_10m", [])

    now = _local_now().replace(minute=0, second=0, microsecond=0)
    today_prefix = now.date().isoformat()

    uv_peak = None
    uv_peak_time = None
    for t, v in zip(times, uv):
        if not str(t).startswith(today_prefix):
            continue
        if v is not None and (uv_peak is None or v > uv_peak):
            uv_peak = v
            uv_peak_time = t

    next_rain = None
    rain_confidence = "low"
    for t, p, mm in zip(times, precip_prob, precip):
        try:
            tt = _as_configured_time(str(t))
        except ValueError:
            continue
        if tt < now:
            continue
        probability = p or 0
        amount = mm or 0
        if probability >= 50 or amount >= 0.2:
            next_rain = t
            rain_confidence = "medium" if probability < 70 else "high"
            break

    return {
        "provider": "open_meteo",
        "current": {
            "temperature_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "precipitation_mm": current.get("precipitation"),
            "weather_code": current.get("weather_code"),
            "condition": _weather_code_label(current.get("weather_code")),
            "wind_kmh": current.get("wind_speed_10m"),
            "gust_kmh": current.get("wind_gusts_10m"),
        },
        "uv": {
            "peak": uv_peak,
            "peak_time": uv_peak_time,
        },
        "rain": {
            "next_expected": next_rain,
            "confidence": rain_confidence,
            "minute_precision": False,
        },
        "hourly_sample": [
            {
                "time": t,
                "temperature_c": temp,
                "feels_like_c": feel,
                "precip_probability": prob,
                "uv_index": uvi,
                "wind_kmh": w,
                "gust_kmh": g,
            }
            for t, temp, feel, prob, uvi, w, g in list(zip(times, temps, feels, precip_prob, uv, wind, gusts))[:36]
        ],
    }


def _normalise_tomorrow(payload: dict[str, Any]) -> dict[str, Any]:
    timelines = payload.get("timelines", {})
    minutely = timelines.get("minutely", []) or timelines.get("1m", [])
    hourly = timelines.get("hourly", []) or timelines.get("1h", [])

    next_rain = None
    confidence = "low"
    for item in minutely:
        values = item.get("values", {})
        probability = values.get("precipitationProbability", 0) or 0
        intensity = values.get("rainIntensity", values.get("precipitationIntensity", 0)) or 0
        if probability >= 45 or intensity > 0:
            next_rain = item.get("time")
            confidence = "medium" if probability < 70 else "high"
            break

    uv_peak = None
    uv_peak_time = None
    today = _local_now().date()
    for item in hourly:
        values = item.get("values", {})
        time_raw = item.get("time")
        if not time_raw:
            continue
        try:
            date = _as_configured_time(str(time_raw)).date()
        except Exception:
            continue
        if date != today:
            continue
        uvi = values.get("uvIndex")
        if uvi is not None and (uv_peak is None or uvi > uv_peak):
            uv_peak = uvi
            uv_peak_time = time_raw

    return {
        "provider": "tomorrow",
        "rain": {
            "next_expected": next_rain,
            "confidence": confidence,
            "minute_precision": bool(next_rain),
        },
        "uv": {
            "peak": uv_peak,
            "peak_time": uv_peak_time,
        },
    }


def build_consensus(results: list[ProviderResult]) -> dict[str, Any]:
    normalised: list[dict[str, Any]] = []
    for result in results:
        if result.provider == "open_meteo":
            normalised.append(_normalise_open_meteo(result.payload))
        elif result.provider == "tomorrow":
            normalised.append(_normalise_tomorrow(result.payload))

    base = next((item for item in normalised if item.get("current")), {})
    current = base.get("current", {})

    rain_candidates = [item.get("rain", {}) for item in normalised if item.get("rain")]
    rain = next((r for r in rain_candidates if r.get("minute_precision")), None) or (rain_candidates[0] if rain_candidates else {})

    uv_candidates = [item.get("uv", {}) for item in normalised if item.get("uv")]
    uv = max(uv_candidates, key=lambda x: x.get("peak") or -1) if uv_candidates else {}

    consensus = {
        "updated_at": utc_now_iso(),
        "sources": [r.provider for r in results],
        "current": current,
        "rain": rain,
        "uv": uv,
        "confidence": "high" if len(results) >= 2 else "medium" if results else "low",
        "normalised": normalised,
    }
    consensus["voice"] = make_voice_summary(consensus)
    return consensus


def _recent_phrases(category: str) -> set[str]:
    init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
    with connect() as con:
        rows = con.execute(
            "SELECT phrase FROM phrase_history WHERE category = ? AND used_at > ?",
            (category, cutoff),
        ).fetchall()
        return {row["phrase"] for row in rows}


def _remember_phrase(category: str, phrase: str) -> None:
    init_db()
    with connect() as con:
        con.execute(
            "INSERT INTO phrase_history(category, phrase, used_at) VALUES (?, ?, ?)",
            (category, phrase, utc_now_iso()),
        )


def _choose_phrase(category: str) -> str:
    bank = {
        "sunny_warm": [
            "Have you found your summer shorts yet?",
            "Shorts are cleared for deployment.",
            "Sunny. Britain is briefly pretending to be generous.",
            "Leg weather, frankly.",
        ],
        "rain": [
            "Meh. Wet nonsense.",
            "Umbrella if you’re being sensible.",
            "Not romantic rain. Just wet.",
            "Admin rain.",
        ],
        "grey": [
            "Grey but functional.",
            "Not glamorous, but workable.",
            "Bit murky, then fine.",
            "Cardigan-level uncertainty.",
        ],
        "hot": [
            "Hydrate. Shade. Don’t crisp.",
            "Lovely, but slightly aggressive.",
            "Hot. Behave accordingly.",
        ],
        "cold": [
            "Big coat, no drama.",
            "Cold, but manageable.",
            "Layers are not a personality flaw today.",
        ],
    }
    options = bank.get(category, ["Fine once it gets going."])
    recent = _recent_phrases(category)
    fresh = [option for option in options if option not in recent] or options
    seed = hashlib.sha256(f"{category}:{datetime.now().date()}:{datetime.now().hour}".encode()).hexdigest()
    rng = random.Random(seed)
    phrase = rng.choice(fresh)
    _remember_phrase(category, phrase)
    return phrase


def _format_hour(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = _parse_iso(value)
        return dt.strftime("%-I%p").lower()
    except Exception:
        try:
            dt = datetime.fromisoformat(value)
            return dt.strftime("%-I%p").lower()
        except Exception:
            return None


def make_voice_summary(consensus: dict[str, Any]) -> str:
    # Precision before personality: with no live sources there is NO forecast, and saying
    # "probably dry" from nothing is fake confidence (the external audit caught exactly that).
    # Say what we know: nothing current, plus how to judge any cached remainder.
    if not consensus.get("sources") and not (consensus.get("current") or {}):
        return ("I don't have live weather data right now (no providers answered). "
                "I won't guess; ask again in a minute or check providers in settings.")
    current = consensus.get("current", {})
    rain = consensus.get("rain", {})
    uv = consensus.get("uv", {})

    temp = current.get("temperature_c")
    feels = current.get("feels_like_c")
    condition = current.get("condition", "unknown")
    uv_peak = uv.get("peak")
    uv_time = _format_hour(uv.get("peak_time"))
    next_rain = _format_hour(rain.get("next_expected"))
    minute_precision = rain.get("minute_precision", False)

    category = "grey"
    if temp is not None and temp >= 26:
        category = "hot"
    elif condition in {"clear", "partly_cloudy"} and temp is not None and temp >= 18:
        category = "sunny_warm"
    elif condition == "rain" or next_rain:
        category = "rain"
    elif temp is not None and temp <= 7:
        category = "cold"

    bits: list[str] = []
    if temp is not None:
        if feels is not None and round(feels) != round(temp):
            bits.append(f"{round(temp)} degrees, feels like {round(feels)}")
        else:
            bits.append(f"{round(temp)} degrees")

    if next_rain:
        if minute_precision:
            bits.append(f"rain around {next_rain}")
        else:
            bits.append(f"rain risk around {next_rain}")
    elif condition == "rain":
        bits.append("rain about")
    elif temp is not None or condition != "unknown":
        bits.append("probably dry")
    else:
        bits.append("no current reading, so no rain call")

    if uv_peak is not None:
        if uv_time:
            bits.append(f"UV peaks at {round(uv_peak)} around {uv_time}")
        else:
            bits.append(f"UV peaks at {round(uv_peak)}")

    phrase = _choose_phrase(category)
    return f"{', '.join(bits)}. {phrase}"


def _store_consensus(location_key: str, consensus: dict[str, Any]) -> None:
    settings = get_settings()
    ttl = settings.weather_current_ttl_seconds
    with connect() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO weather_consensus(location_key, payload_json, updated_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (location_key, json.dumps(consensus), utc_now_iso(), _expires(ttl)),
        )


def _load_consensus(location_key: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM weather_consensus WHERE location_key = ?", (location_key,)).fetchone()
        if not row:
            return None
        if _parse_iso(row["expires_at"]) < datetime.now(timezone.utc):
            return None
        return json.loads(row["payload_json"])


async def weather_summary(force: bool = False) -> dict[str, Any]:
    location_key = "home"
    if not force:
        cached = _load_consensus(location_key)
        if cached:
            # FIX (external audit, additional findings #9): this returned the stored payload
            # verbatim, so a 45-minute-old cached reading answered with the SAME voice string
            # written when it was fresh: no hint it wasn't live. Precision-before-personality
            # already applies to "no data at all" (see the no-sources guard in
            # make_voice_summary); a stale-but-not-yet-expired cache is the same principle at
            # a smaller scale, especially for rain timing and UV where "confidence" matters.
            # Below ~10 minutes the age is immaterial and adding a disclosure would just be
            # noise; past that, say so plainly rather than let a cached answer read as live.
            age_minutes = None
            try:
                updated = _parse_iso(cached.get("updated_at", ""))
                age_minutes = int((datetime.now(timezone.utc) - updated).total_seconds() // 60)
            except Exception:
                pass
            cached = dict(cached)
            cached["served_from_cache"] = True
            cached["cache_age_minutes"] = age_minutes
            if age_minutes is not None and age_minutes >= 10:
                voice = cached.get("voice", "")
                cached["voice"] = f"Using weather from about {age_minutes} minutes ago: {voice}"
            return cached
    results = await fetch_weather_sources(force=force)
    consensus = build_consensus(results)
    _store_consensus(location_key, consensus)
    audit("weather", "summary", bool(results), route="provider_pool", payload={"sources": consensus.get("sources", [])})
    consensus["served_from_cache"] = False
    consensus["cache_age_minutes"] = 0
    return consensus
