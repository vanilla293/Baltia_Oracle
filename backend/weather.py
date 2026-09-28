"""ОРАКУЛ // ПИФИЯ — погода (open-meteo, без ключа).

Тянется только для инструментов, где погода реально двигает спрос
(газ/нефть). Считаем простой сигнал отопительного спроса (HDD-подобный):
холоднее нормы → выше спрос на газ. Отдаём текущую температуру, прогноз на
7 дней и грубую оценку «холодно/тепло».
"""
from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger("pythia.weather")


async def _one(region: dict, client: httpx.AsyncClient) -> dict | None:
    try:
        r = await client.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": region["lat"], "longitude": region["lon"],
            "current": "temperature_2m,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min",
            "forecast_days": 7, "timezone": "auto",
        })
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        logger.warning("weather %s failed: %s", region.get("name"), str(e)[:80])
        return None
    cur = d.get("current", {})
    daily = d.get("daily", {})
    tmax = daily.get("temperature_2m_max", []) or []
    tmin = daily.get("temperature_2m_min", []) or []
    avg7 = None
    if tmax and tmin:
        avgs = [(a + b) / 2 for a, b in zip(tmax, tmin)]
        avg7 = round(sum(avgs) / len(avgs), 1)
    # отопительные градусо-дни (база 18°C): сумма max(0, 18 - tavg)
    hdd = None
    if tmax and tmin:
        hdd = round(sum(max(0, 18 - (a + b) / 2) for a, b in zip(tmax, tmin)), 1)
    t_now = cur.get("temperature_2m")
    cold_signal = None
    if avg7 is not None:
        cold_signal = ("очень холодно — пик отопления" if avg7 < 0 else
                       "холодно — повышенный спрос на газ" if avg7 < 8 else
                       "умеренно" if avg7 < 18 else
                       "тепло — спрос на охлаждение")
    return {
        "region": region["name"],
        "temp_now": t_now,
        "wind_now": cur.get("wind_speed_10m"),
        "avg_7d": avg7,
        "min_7d": round(min(tmin), 1) if tmin else None,
        "max_7d": round(max(tmax), 1) if tmax else None,
        "heating_degree_days_7d": hdd,
        "signal": cold_signal,
    }


_http: httpx.AsyncClient | None = None
_http_lock = asyncio.Lock()


async def _client() -> httpx.AsyncClient:
    global _http
    h = _http
    if h is not None and not h.is_closed:
        return h
    async with _http_lock:
        if _http is None or _http.is_closed:
            _http = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                limits=httpx.Limits(max_keepalive_connections=4,
                                    max_connections=8, keepalive_expiry=60.0))
        return _http


async def aclose() -> None:
    global _http
    h, _http = _http, None
    if h is not None and not h.is_closed:
        try:
            await h.aclose()
        except Exception:
            pass


async def fetch_weather(regions: list[dict]) -> list[dict]:
    if not regions:
        return []
    cl = await _client()
    res = await asyncio.gather(*[_one(r, cl) for r in regions],
                               return_exceptions=True)
    return [x for x in res if isinstance(x, dict)]


def render_for_ai(weather: list[dict]) -> str:
    if not weather:
        return ""
    lines = []
    for w in weather:
        lines.append(
            f"• {w['region']}: сейчас {w.get('temp_now')}°C, "
            f"ветер {w.get('wind_now')} м/с, средняя на 7д {w.get('avg_7d')}°C "
            f"(мин {w.get('min_7d')} / макс {w.get('max_7d')}), HDD7={w.get('heating_degree_days_7d')} "
            f"→ {w.get('signal')}")
    return "\n".join(lines)
