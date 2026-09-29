"""Настройки голосом: отвечать ли голосом, быстрый/глубокий режим, «забудь разговор».

То же, что команды /voice, /mode и /reset, но словами: «отвечай мне голосом», «думай глубже»,
«давай с чистого листа». Значения — те же ключи kv, что читают обработчики бота:
`tts_mode` (off | mirror | always — bot.handlers.TTS_MODES) и `mode` (fast | deep).
"""
from __future__ import annotations

from typing import Any

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
