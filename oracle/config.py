"""Настройки из .env / переменных окружения.

Всё, что нужно боту, читается один раз при старте в `Settings`. Секреты не логируются.
Пустое значение = «не задано» (берётся умолчание).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

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


# ── размышление DeepSeek: API понимает только low | high | max ────────────────
EFFORTS = ("low", "high", "max")
_OFF = {"", "off", "none", "disabled", "disable", "0", "false", "no", "нет", "выкл", "выключено"}
_ON = {"on", "true", "yes", "1", "да", "вкл", "включено", "enabled", "enable"}
_EFFORT_ALIASES = {"minimal": "low", "min": "low", "мало": "low", "medium": "high", "mid": "high",
                   "middle": "high", "normal": "high", "средне": "high", "xhigh": "max", "maximum": "max",
                   "максимум": "max"}


def clean_effort(value: str, *, fast: bool) -> tuple[str, bool]:
    """LLM_FAST_THINKING / LLM_DEEP_EFFORT → значение, которое примет API, и понято ли оно.

    Быстрый режим: off | low | high | max («on»/«true» → low). Глубокий: low | high | max
    или auto (не слать reasoning_effort — пусть решает модель). Незнакомое → умолчание (off / high).
    """
    s = (value or "").strip().lower()
    if fast and s in _OFF:
        return "off", True
    if s in _ON:
        return ("low" if fast else "high"), True
    s = _EFFORT_ALIASES.get(s, s)
    if s in EFFORTS:
        return s, True
    if not fast and s in {"auto", "default"}:
        return s, True
    return ("off" if fast else "high"), False


# ── часовой пояс: IANA-имя; частые вольности («MSK», «UTC+3», «europe/moscow») понимаем ──
_TZ_ALIASES = {
    "msk": "Europe/Moscow", "мск": "Europe/Moscow", "moscow": "Europe/Moscow", "москва": "Europe/Moscow",
    "riga": "Europe/Riga", "рига": "Europe/Riga", "kaliningrad": "Europe/Kaliningrad",
    "калининград": "Europe/Kaliningrad", "minsk": "Europe/Minsk", "минск": "Europe/Minsk",
    "kyiv": "Europe/Kyiv", "kiev": "Europe/Kyiv", "киев": "Europe/Kyiv", "київ": "Europe/Kyiv",
    "vilnius": "Europe/Vilnius", "вильнюс": "Europe/Vilnius", "tallinn": "Europe/Tallinn",
    "таллин": "Europe/Tallinn", "таллинн": "Europe/Tallinn", "utc": "UTC", "gmt": "UTC", "z": "UTC",
}
_TZ_OFFSET = re.compile(r"^(?:utc|gmt)?\s*([+-])\s*(\d{1,2})(?::?00)?$")


def _zone_ok(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def resolve_timezone(name: str) -> str | None:
    """TIMEZONE из .env → IANA-имя, которое понимает zoneinfo, или None (не распознан).

    «UTC+3»/«GMT+3»/«+3» → Etc/GMT-3: у зон Etc знак обратный (POSIX), наивное Etc/GMT+3 — это UTC−3."""
    raw = (name or "").strip()
    if not raw:
        return None
    if "/" in raw or raw.upper() == "UTC":
        if _zone_ok(raw):
            return raw
    alias = _TZ_ALIASES.get(raw.lower())
    if alias:
        return alias
    m = _TZ_OFFSET.match(raw.lower())
    if m:
        hours = int(m.group(2))
        if hours == 0:
            return "UTC"
        if hours <= 14:
            cand = f"Etc/GMT{'-' if m.group(1) == '+' else '+'}{hours}"
            if _zone_ok(cand):
                return cand
    if _zone_ok(raw):
        return raw
    try:
        for z in available_timezones():                 # «europe/moscow» → Europe/Moscow
            if z.lower() == raw.lower():
                return z
    except Exception:                                   # нет базы зон (Windows без tzdata)
        pass
    return None


@dataclass(frozen=True)
class Settings:
    # Telegram
    bot_token: str = ""
    owner_id: int = 0
    telegram_api_url: str = ""                 # свой Bot API сервер; пусто — api.telegram.org
    bot_name: str = "Оракул"
    owner_name: str = ""
    owner_gender: str = "m"                    # m | f — род, в котором бот говорит о владельце и пишет от него

    # LLM (OpenAI-совместимый API; по умолчанию DeepSeek)
    llm_api_key: str = ""
    llm_api_keys: tuple[str, ...] = ()        # запасные ключи: DEEPSEEK_API_KEY=ключ1,ключ2
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"          # быстрый режим: обычный диалог и инструменты
    llm_model_deep: str = "deepseek-v4-pro"    # глубокий режим: с размышлением (не принят API — уходим на llm_model)
    llm_auto_deep: bool = True                 # быстрая модель сама переключается на глубокую на сложном
    llm_fast_thinking: str = "off"             # off | low | high — размышление в быстром режиме
    llm_deep_effort: str = "high"              # low | high | max — глубина в глубоком режиме
    llm_temperature: float = 1.0
    llm_fast_max_tokens: int = 8192
    llm_deep_max_tokens: int = 65536
    llm_fast_timeout: float = 120.0            # секунд на один ответ модели
    llm_deep_timeout: float = 600.0
    llm_turn_budget: float = 150.0             # секунд на весь ход (все шаги) в быстром режиме
    llm_deep_turn_budget: float = 900.0        # и в глубоком
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
    tts_default: str = "off"                   # off | mirror (голосом на голос) | always
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
    # что в .env не понято и заменено умолчанием (заполняет load(); показывается в problems())
    warnings: tuple[str, ...] = ()

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return ZoneInfo("UTC")

    @property
    def tz_ok(self) -> bool:
        """TIMEZONE распознан (иначе cfg.tz молча стал UTC и все будильники съедут)."""
        return _zone_ok(self.timezone)

    def turn_budget(self, deep: bool) -> float:
        """Сколько секунд может занять весь ход агента (все шаги «модель → инструменты»)."""
        budget = self.llm_deep_turn_budget if deep else self.llm_turn_budget
        limit = self.llm_deep_timeout if deep else self.llm_fast_timeout
        return float(budget if budget and budget > 0 else 1.5 * limit)

    @property
    def api_keys(self) -> tuple[str, ...]:
        """Все ключи модели по порядку: основной первым, дальше запасные."""
        return self.llm_api_keys or ((self.llm_api_key,) if self.llm_api_key else ())

    @property
    def is_deepseek(self) -> bool:
        return "deepseek" in self.llm_base_url.lower()

    def problems(self) -> list[str]:
        """Что не настроено — человеческим текстом (пусто = всё в порядке)."""
        out = []
        if not self.bot_token:
            out.append("нет BOT_TOKEN — возьми у @BotFather и впиши в .env")
        if not self.llm_api_key:
            out.append("нет DEEPSEEK_API_KEY — ключ с platform.deepseek.com" if self.is_deepseek else
                       "нет LLM_API_KEY — ключ провайдера из LLM_BASE_URL (для Ollama / LM Studio — любое слово)")
        if not self.owner_id:
            out.append("нет OWNER_ID — напиши боту /start, он пришлёт твой id, впиши его в .env")
        if not self.tz_ok:
            out.append(f"TIMEZONE={self.timezone!r} не распознан — сейчас работаю по UTC, и будильники "
                       "съедут на часы; нужен вид Europe/Moscow (или MSK, UTC+3)")
        out.extend(self.warnings)
        return out


def _gender(v: str) -> str:
    """OWNER_GENDER: f / ж / female → "f", остальное → "m"."""
    return "f" if str(v or "").strip().lower() in ("f", "ж", "female", "woman", "w", "жен", "женщина") else "m"


def _rooted(p: str) -> Path:
    """Относительный путь из .env — от папки проекта, а не от текущего каталога запуска."""
    path = Path(p).expanduser()
    return path if path.is_absolute() else ROOT / path


def load(env_file: str | os.PathLike | None = None) -> Settings:
    """Прочитать .env (если есть) и окружение → Settings."""
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file or os.environ.get("ENV_FILE") or ROOT / ".env", override=False)
    except ImportError:  # python-dotenv не обязателен, если переменные заданы окружением
        pass

    data_dir = _rooted(_get("DATA_DIR", str(ROOT / "data")))
    session = str(_rooted(_get("USERBOT_SESSION", str(data_dir / "userbot"))))
    # ключ — под адрес: DEEPSEEK_API_KEY не должен уйти стороннему сервису, если тот сменили в LLM_BASE_URL
    base_url = _get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    ds_key, other_key = _get("DEEPSEEK_API_KEY"), _get("LLM_API_KEY")
    api_key = (ds_key or other_key) if "deepseek" in base_url.lower() else (other_key or ds_key)
    # несколько ключей через запятую: основной + запасные (кончились деньги / отозван — берём следующий)
    api_keys = tuple(dict.fromkeys(k.strip() for k in re.split(r"[,;\s]+", api_key) if k.strip()))
    api_key = api_keys[0] if api_keys else ""

    warnings: list[str] = []
    raw_fast = _get("LLM_FAST_THINKING", "off")
    fast_thinking, ok = clean_effort(raw_fast, fast=True)
    if not ok:
        warnings.append(f"LLM_FAST_THINKING={raw_fast!r} не понял — размышление в быстром режиме выключено "
                        "(бывает: off | low | high | max)")
    raw_deep = _get("LLM_DEEP_EFFORT", "high")
    deep_effort, ok = clean_effort(raw_deep, fast=False)
    if not ok:
        warnings.append(f"LLM_DEEP_EFFORT={raw_deep!r} не понял — беру high (бывает: low | high | max)")
    fast_timeout = _float("LLM_FAST_TIMEOUT", 120.0)
    deep_timeout = _float("LLM_DEEP_TIMEOUT", 600.0)
    raw_tz = _get("TIMEZONE", "Europe/Moscow")

    return Settings(
        bot_token=_get("BOT_TOKEN"),
        owner_id=_int("OWNER_ID", 0),
        telegram_api_url=_get("TELEGRAM_API_URL", "").rstrip("/"),
        bot_name=_get("BOT_NAME", "Оракул"),
        owner_name=_get("OWNER_NAME", ""),
        owner_gender=_gender(_get("OWNER_GENDER", "m")),
        llm_api_key=api_key,
        llm_api_keys=api_keys,
        llm_base_url=base_url,
        llm_model=_get("LLM_MODEL", "deepseek-flash"),
        # по умолчанию у DeepSeek глубокая — Pro; у другого провайдера — та же, что быстрая
        llm_model_deep=_get("LLM_MODEL_DEEP", "deepseek-v4-pro" if "deepseek" in base_url.lower()
                            else _get("LLM_MODEL", "deepseek-flash")),
        llm_auto_deep=_bool("LLM_AUTO_DEEP", True),
        llm_fast_thinking=fast_thinking,
        llm_deep_effort=deep_effort,
        llm_temperature=_float("LLM_TEMPERATURE", 1.0),
        # размышление тратит те же токены, что и ответ: с ним 8192 кончаются раньше ответа
        llm_fast_max_tokens=_int("LLM_FAST_MAX_TOKENS", 8192 if fast_thinking == "off" else 32768),
        llm_deep_max_tokens=_int("LLM_DEEP_MAX_TOKENS", 65536),
        llm_fast_timeout=fast_timeout,
        llm_deep_timeout=deep_timeout,
        llm_turn_budget=_float("LLM_TURN_BUDGET", max(150.0, fast_timeout + 30.0)),
        llm_deep_turn_budget=_float("LLM_DEEP_TURN_BUDGET", max(900.0, 1.5 * deep_timeout)),
        llm_max_steps=max(1, _int("LLM_MAX_STEPS", 8)),
        timezone=resolve_timezone(raw_tz) or raw_tz,
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
        tts_default=_get("TTS_DEFAULT", "off").lower(),
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
        db_path=_rooted(_get("DB_PATH", str(data_dir / "oracle.db"))),
        log_level=_get("LOG_LEVEL", "INFO").upper(),
        warnings=tuple(warnings),
    )
