"""Настройки из .env / переменных окружения.

Всё, что нужно боту, читается один раз при старте в `Settings`. Секреты не логируются.
Пустое значение = «не задано» (берётся умолчание).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_FEEDS = (
    # разные лагеря — чтобы сравнивать подачу, а не верить одному источнику
    "https://tass.ru/rss/v2.xml",
    "https://ria.ru/export/rss2/archive/index.xml",
    "https://www.interfax.ru/rss.asp",
    "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
    "https://www.kommersant.ru/RSS/news.xml",
    "https://meduza.io/rss/all",
    "https://feeds.bbci.co.uk/russian/rss.xml",
    "https://rss.dw.com/rdf/rss-ru-all",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.theguardian.com/world/rss",
)


def _get(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip()
    return v if v else default


def _bool(name: str, default: bool) -> bool:
    v = _get(name, "")
    if not v:
        return default
    return v.lower() in {"1", "true", "yes", "on", "да", "y"}


def _int(name: str, default: int) -> int:
    v = _get(name, "")
    try:
        return int(v) if v else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    v = _get(name, "")
    try:
        return float(v.replace(",", ".")) if v else default
    except ValueError:
        return default


def _opt_float(name: str) -> float | None:
    v = _get(name, "")
    try:
        return float(v.replace(",", ".")) if v else None
    except ValueError:
        return None


def _list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    v = _get(name, "")
    if not v:
        return default
    return tuple(x.strip() for x in v.replace("\n", ",").split(",") if x.strip())


def _hhmm(name: str, default: str) -> str:
    """Время дня «HH:MM»; пусто или «off» → "" (задача выключена)."""
    v = _get(name, default)
    if v.lower() in {"off", "no", "0", "нет", "-"}:
        return ""
    try:
        h, m = v.split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return f"{h:02d}:{m:02d}"
    except ValueError:
        pass
    return default


@dataclass(frozen=True)
class Settings:
    # Telegram
    bot_token: str = ""
    owner_id: int = 0
    bot_name: str = "Оракул"
    owner_name: str = ""

    # LLM (OpenAI-совместимый API; по умолчанию DeepSeek)
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"          # быстрый режим: обычный диалог и инструменты
    llm_model_deep: str = "deepseek-flash"     # глубокий режим: с размышлением
    llm_fast_thinking: str = "off"             # off | low | high — размышление в быстром режиме
    llm_deep_effort: str = "high"              # low | high | max — глубина в глубоком режиме
    llm_temperature: float = 1.0
    llm_fast_max_tokens: int = 8192
    llm_deep_max_tokens: int = 65536
    llm_fast_timeout: float = 120.0            # секунд на один ответ модели
    llm_deep_timeout: float = 600.0
    llm_max_steps: int = 8                     # шагов «модель → инструменты» на одно сообщение

    # время
    timezone: str = "Europe/Moscow"

    # память и диалог
    history_messages: int = 30                 # сколько последних реплик идёт в контекст
    summary_chunk: int = 20                    # сколько старых реплик сворачивать в конспект за раз

    # голос
    stt_provider: str = "auto"                 # auto | groq | openai | local | off
    stt_language: str = "ru"
    groq_api_key: str = ""
    groq_stt_model: str = "whisper-large-v3-turbo"
    openai_api_key: str = ""
    openai_stt_model: str = "whisper-1"
    whisper_model: str = "small"               # для local (faster-whisper): tiny/base/small/medium/large-v3
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    show_transcript: bool = True
    tts_voice: str = "ru-RU-DmitryNeural"
    tts_default: str = "mirror"                # off | mirror (голосом на голос) | always
    tts_max_chars: int = 1500

    # расписание (локальное время, "" — выключено)
    morning_brief_time: str = "08:00"
    news_digest_time: str = "09:00"
    reflection_time: str = "03:30"
    birthday_time: str = "09:00"

    # напоминания
    nag_interval_min: int = 3
    nag_max: int = 20
    wake_challenge: bool = True

    # новости и веб
    news_feeds: tuple[str, ...] = DEFAULT_FEEDS
    web_search: bool = True
    weather_city: str = ""
    weather_lat: float | None = None
    weather_lon: float | None = None

    # userbot (чтение своих чатов, черновики ответов)
    userbot_enabled: bool = False
    tg_api_id: int = 0
    tg_api_hash: str = ""
    userbot_session: str = "data/userbot"
    userbot_notify: bool = False               # сообщать о новых личных сообщениях

    # хранилище
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    db_path: Path = field(default_factory=lambda: ROOT / "data" / "oracle.db")
    log_level: str = "INFO"

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    @property
    def is_deepseek(self) -> bool:
        return "deepseek" in self.llm_base_url.lower()

    def problems(self) -> list[str]:
        """Что не настроено — человеческим текстом (пусто = всё в порядке)."""
        out = []
        if not self.bot_token:
            out.append("нет BOT_TOKEN — возьми у @BotFather и впиши в .env")
        if not self.llm_api_key:
            out.append("нет DEEPSEEK_API_KEY — ключ с platform.deepseek.com")
        if not self.owner_id:
            out.append("нет OWNER_ID — напиши боту /start, он пришлёт твой id, впиши его в .env")
        return out


def load(env_file: str | os.PathLike | None = None) -> Settings:
    """Прочитать .env (если есть) и окружение → Settings."""
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file or ROOT / ".env", override=False)
    except ImportError:  # python-dotenv не обязателен, если переменные заданы окружением
        pass

    data_dir = Path(_get("DATA_DIR", str(ROOT / "data")))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    session = _get("USERBOT_SESSION", str(data_dir / "userbot"))

    return Settings(
        bot_token=_get("BOT_TOKEN"),
        owner_id=_int("OWNER_ID", 0),
        bot_name=_get("BOT_NAME", "Оракул"),
        owner_name=_get("OWNER_NAME", ""),
        llm_api_key=_get("DEEPSEEK_API_KEY") or _get("LLM_API_KEY"),
        llm_base_url=_get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        llm_model=_get("LLM_MODEL", "deepseek-flash"),
        llm_model_deep=_get("LLM_MODEL_DEEP", _get("LLM_MODEL", "deepseek-flash")),
        llm_fast_thinking=_get("LLM_FAST_THINKING", "off").lower(),
        llm_deep_effort=_get("LLM_DEEP_EFFORT", "high").lower(),
        llm_temperature=_float("LLM_TEMPERATURE", 1.0),
        llm_fast_max_tokens=_int("LLM_FAST_MAX_TOKENS", 8192),
        llm_deep_max_tokens=_int("LLM_DEEP_MAX_TOKENS", 65536),
        llm_fast_timeout=_float("LLM_FAST_TIMEOUT", 120.0),
        llm_deep_timeout=_float("LLM_DEEP_TIMEOUT", 600.0),
        llm_max_steps=max(1, _int("LLM_MAX_STEPS", 8)),
        timezone=_get("TIMEZONE", "Europe/Moscow"),
        history_messages=max(4, _int("HISTORY_MESSAGES", 30)),
        summary_chunk=max(4, _int("SUMMARY_CHUNK", 20)),
        stt_provider=_get("STT_PROVIDER", "auto").lower(),
        stt_language=_get("STT_LANGUAGE", "ru"),
        groq_api_key=_get("GROQ_API_KEY"),
        groq_stt_model=_get("GROQ_STT_MODEL", "whisper-large-v3-turbo"),
        openai_api_key=_get("OPENAI_API_KEY"),
        openai_stt_model=_get("OPENAI_STT_MODEL", "whisper-1"),
        whisper_model=_get("WHISPER_MODEL", "small"),
        whisper_device=_get("WHISPER_DEVICE", "cpu"),
        whisper_compute_type=_get("WHISPER_COMPUTE_TYPE", "int8"),
        show_transcript=_bool("SHOW_TRANSCRIPT", True),
        tts_voice=_get("TTS_VOICE", "ru-RU-DmitryNeural"),
        tts_default=_get("TTS_DEFAULT", "mirror").lower(),
        tts_max_chars=_int("TTS_MAX_CHARS", 1500),
        morning_brief_time=_hhmm("MORNING_BRIEF_TIME", "08:00"),
        news_digest_time=_hhmm("NEWS_DIGEST_TIME", "09:00"),
        reflection_time=_hhmm("REFLECTION_TIME", "03:30"),
        birthday_time=_hhmm("BIRTHDAY_TIME", "09:00"),
        nag_interval_min=max(1, _int("NAG_INTERVAL_MIN", 3)),
        nag_max=max(1, _int("NAG_MAX", 20)),
        wake_challenge=_bool("WAKE_CHALLENGE", True),
        news_feeds=_list("NEWS_FEEDS", DEFAULT_FEEDS),
        web_search=_bool("WEB_SEARCH", True),
        weather_city=_get("WEATHER_CITY", ""),
        weather_lat=_opt_float("WEATHER_LAT"),
        weather_lon=_opt_float("WEATHER_LON"),
        userbot_enabled=_bool("USERBOT_ENABLED", False),
        tg_api_id=_int("TG_API_ID", 0),
        tg_api_hash=_get("TG_API_HASH"),
        userbot_session=session,
        userbot_notify=_bool("USERBOT_NOTIFY", False),
        data_dir=data_dir,
        db_path=Path(_get("DB_PATH", str(data_dir / "oracle.db"))),
        log_level=_get("LOG_LEVEL", "INFO").upper(),
    )
