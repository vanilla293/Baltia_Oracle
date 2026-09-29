"""Настройки словами: отвечать ли голосом, быстрый/глубокий режим, «забудь разговор»,
а также самонастройка под человека — город (погода + часовой пояс), имя и род обращения.

То же, что команды /voice, /mode и /reset, но словами: «отвечай мне голосом», «думай глубже»,
«давай с чистого листа». Значения — те же ключи kv, что читают обработчики бота:
`tts_mode` (off | mirror | always — bot.handlers.TTS_MODES) и `mode` (fast | deep).

Самонастройка (чтобы бота не пришлось конфигурировать руками): владелец называет город, имя или
понятно, как к нему обращаться, — модель тихо сохраняет это в kv, а конфиг/агент читают эти
переопределения при запуске/на ходу. Ключи kv: `weather_city`, `timezone`, `owner_name`,
`owner_gender`. Часовой пояс применяется после перезапуска (cfg.tz строится при загрузке).
"""
from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from ..persona import is_female
from .base import ToolContext, tool

TTS_MODES = ("off", "mirror", "always")         # = bot.handlers.TTS_MODES
THINKING_MODES = ("fast", "deep")
_TTS_RU = {"off": "только текстом", "mirror": "на голосовые — голосом (и текстом), на текст — текстом",
           "always": "всегда голосом (и текстом)"}
_TTS_ALIASES = {"no": "off", "none": "off", "text": "off", "нет": "off", "выкл": "off",
                "voice": "mirror", "on": "mirror", "да": "mirror", "yes": "mirror", "вкл": "mirror",
                "всегда": "always"}
_MODE_ALIASES = {"quick": "fast", "быстрый": "fast", "normal": "fast",
                 "глубокий": "deep", "think": "deep", "slow": "deep"}


def _pick(value: Any, allowed: tuple[str, ...], aliases: dict[str, str], what: str) -> str:
    v = str(value or "").strip().lower()
    v = aliases.get(v, v)
    if v not in allowed:
        raise ValueError(f"{what}: одно из {', '.join(allowed)}")
    return v


@tool("set_voice_replies",
      "Отвечать ли голосом (озвучка ответов). off — только текстом; mirror — на голосовые голосом, на текст "
      "текстом; always — всегда голосом (текст приходит тоже). «Отвечай голосом» — обычно mirror, "
      "«всегда голосом» — always, «хватит голосом» — off. Только по его прямой просьбе.",
      {"mode": {"type": "string", "enum": list(TTS_MODES)}},
      required=["mode"])
async def t_set_voice_replies(ctx: ToolContext, *, mode: Any) -> dict:
    m = _pick(mode, TTS_MODES, _TTS_ALIASES, "mode")
    await ctx.db.kv_set("tts_mode", m)
    out: dict = {"ok": True, "mode": m, "means": _TTS_RU[m]}
    tts = getattr(ctx.services, "tts", None)
    try:
        ready = tts is not None and tts.available()
    except Exception:
        ready = False
    if m != "off" and not ready:
        out["note"] = ("настройка сохранена, но озвучка сейчас недоступна (не установлен edge-tts или нет "
                       "связи) — отвечать буду текстом; скажи ему об этом")
    return out


@tool("set_thinking_mode",
      "Режим мышления: fast — отвечать за секунды; deep — думать дольше и основательнее (минуты) для всего "
      "разговора. Действует со следующего сообщения. Один сложный вопрос без смены режима — /deep вопрос. "
      "Только по его прямой просьбе.",
      {"mode": {"type": "string", "enum": list(THINKING_MODES)}},
      required=["mode"])
async def t_set_thinking_mode(ctx: ToolContext, *, mode: Any) -> dict:
    m = _pick(mode, THINKING_MODES, _MODE_ALIASES, "mode")
    await ctx.db.kv_set("mode", m)
    return {"ok": True, "mode": m, "note": "со следующего сообщения"}


@tool("reset_conversation",
      "Начать разговор с чистого листа: убрать из контекста текущую переписку. Память о нём, твои позиции, "
      "дневник, напоминания, идеи и проекты остаются. Только когда он прямо просит забыть разговор или "
      "начать заново — не по своей инициативе.")
async def t_reset_conversation(ctx: ToolContext) -> dict:
    # без блокировки агента: инструмент и так выполняется внутри его хода
    n = await ctx.db.execute("UPDATE messages SET summarized=1 WHERE summarized=0")
    return {"ok": True, "dropped": int(n or 0),
            "note": "контекст очищен; память, позиции и дневник на месте"}


# ── самонастройка: город, имя, род обращения ──────────────────────────────────
# Маленькая таблица «город → IANA-пояс», чтобы часовой пояс подхватывался, даже когда модель его не
# передала. Список нарочно короткий и только по бесспорным городам: кривой пояс хуже, чем никакой.
_CITY_TZ = {
    "москва": "Europe/Moscow", "moscow": "Europe/Moscow", "мск": "Europe/Moscow",
    "питер": "Europe/Moscow", "санкт-петербург": "Europe/Moscow", "спб": "Europe/Moscow",
    "saint petersburg": "Europe/Moscow", "st petersburg": "Europe/Moscow",
    "калининград": "Europe/Kaliningrad", "самара": "Europe/Samara",
    "екатеринбург": "Asia/Yekaterinburg", "екб": "Asia/Yekaterinburg",
    "омск": "Asia/Omsk", "новосибирск": "Asia/Novosibirsk", "нск": "Asia/Novosibirsk",
    "красноярск": "Asia/Krasnoyarsk", "иркутск": "Asia/Irkutsk", "якутск": "Asia/Yakutsk",
    "владивосток": "Asia/Vladivostok",
    "минск": "Europe/Minsk", "minsk": "Europe/Minsk",
    "киев": "Europe/Kyiv", "київ": "Europe/Kyiv", "kyiv": "Europe/Kyiv", "kiev": "Europe/Kyiv",
    "алматы": "Asia/Almaty", "астана": "Asia/Almaty", "нур-султан": "Asia/Almaty",
    "ташкент": "Asia/Tashkent", "тбилиси": "Asia/Tbilisi", "ереван": "Asia/Yerevan", "баку": "Asia/Baku",
    "лондон": "Europe/London", "london": "Europe/London",
    "берлин": "Europe/Berlin", "berlin": "Europe/Berlin",
    "париж": "Europe/Paris", "paris": "Europe/Paris",
    "нью-йорк": "America/New_York", "new york": "America/New_York",
    "дубай": "Asia/Dubai", "dubai": "Asia/Dubai",
}

_GENDER_M = {"m", "м", "male", "man", "муж", "мужской", "мужчина", "he", "его"}


@tool("set_location",
      "Запомнить его город: по нему берётся погода и, если получится, часовой пояс. Зови, когда он называет свой "
      "город, «мой город …» или переезжает. city — название города; tz — необязательно IANA-имя пояса "
      "(«Europe/Moscow»), если знаешь его точно. Часовой пояс применится после перезапуска. Только по его словам.",
      {"city": {"type": "string"},
       "tz": {"type": "string", "description": "IANA-пояс, напр. Europe/Moscow (необязательно)"}},
      required=["city"])
async def t_set_location(ctx: ToolContext, *, city: Any, tz: Any = None) -> dict:
    name = str(city or "").strip()
    if not name:
        raise ValueError("город: назови непустое название")
    await ctx.db.kv_set("weather_city", name)          # погода — всегда по городу, что бы ни было с поясом
    out: dict = {"ok": True, "city": name, "kv": ["weather_city"]}
    zone = str(tz or "").strip()
    guessed = False
    if not zone:                                        # модель пояс не дала — попробуем угадать по городу
        zone = _CITY_TZ.get(name.lower(), "")
        guessed = bool(zone)
    if zone:
        try:
            ZoneInfo(zone)
        except Exception:                              # город уже сохранён — не роняем погоду из-за кривого пояса
            raise ValueError(f"город «{name}» запомнил, но часовой пояс «{zone}» не распознан — "
                             f"нужен IANA-формат, напр. Europe/Moscow")
        await ctx.db.kv_set("timezone", zone)
        out["timezone"] = zone
        out["kv"].append("timezone")
        out["note"] = (f"город «{name}» и часовой пояс {zone} записаны; пояс применится после перезапуска бота — "
                       f"скажи ему об этом" + (" (пояс угадал по городу)" if guessed else ""))
    else:
        out["note"] = f"город «{name}» записан, погоду беру по нему; часовой пояс не задан (не назвал и угадать не смог)"
    return out


@tool("set_owner_name",
      "Запомнить, как его зовут (имя в именительном: «Андрей»). Зови, когда он представился или сам назвал своё "
      "имя. В обращении применится после перезапуска. Только по его словам.",
      {"name": {"type": "string"}}, required=["name"])
async def t_set_owner_name(ctx: ToolContext, *, name: Any) -> dict:
    n = " ".join(str(name or "").split())
    if not n or len(n) > 64:
        raise ValueError("имя: одно короткое имя")
    await ctx.db.kv_set("owner_name", n)
    return {"ok": True, "owner_name": n, "kv": ["owner_name"],
            "note": f"запомнил имя: {n}; в обращении применится после перезапуска"}


@tool("set_owner_gender",
      "Запомнить его пол для рода обращения: m — мужской, f — женский. Зови, когда это ясно из разговора. "
      "Только по его словам, навязчиво не угадывай.",
      {"gender": {"type": "string", "enum": ["m", "f"]}}, required=["gender"])
async def t_set_owner_gender(ctx: ToolContext, *, gender: Any) -> dict:
    g = str(gender or "").strip().lower()
    if is_female(g):
        val = "f"
    elif g in _GENDER_M:
        val = "m"
    else:
        raise ValueError("пол: m (мужской) или f (женский)")
    await ctx.db.kv_set("owner_gender", val)
    return {"ok": True, "owner_gender": val, "kv": ["owner_gender"],
            "note": ("буду обращаться в женском роде" if val == "f" else "буду обращаться в мужском роде")
                    + "; применится после перезапуска"}
