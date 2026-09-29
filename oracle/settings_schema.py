"""Схема настроек для дашборда: описание каждого поля .env, чтение текущих значений (секреты —
только маской), проверка ввода и перевод формы в список изменений для `oracle.runtime.update_env`.

Здесь НЕ читаются и НЕ пишутся файлы — только данные. Панель показывает форму по `SETTINGS`,
берёт текущие значения через `masked_values(cfg)`, проверяет каждое поле `validate(key, raw)` и,
если всё в порядке, отдаёт `to_env_updates(form, current)` тому, кто перезапишет .env.

Каждая настройка — `Setting` с секцией (Telegram, Модель, Голос, Расписание, Новости и погода,
Файлы, Панель, Прочее). Многопользовательские поля (ALLOW_REQUESTS, список OWNER_ID) в v3 убраны:
бот служит одному человеку.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import DEFAULT_FEEDS, resolve_timezone

# ── секции формы (порядок вывода на панели) ─────────────────────────────────────
SECTION_ORDER: tuple[str, ...] = (
    "Telegram", "Модель", "Голос", "Расписание", "Новости и погода",
    "Файлы", "Панель", "Прочее",
)

# как понимаем «включено / выключено» в булевых полях
_TRUE = {"1", "true", "yes", "on", "да", "y", "вкл", "включено", "enabled", "enable"}
_FALSE = {"0", "false", "no", "off", "нет", "n", "выкл", "выключено", "disabled", "disable", ""}
# для полей-времени: что значит «выключить задачу»
_TIME_OFF = {"", "off", "no", "0", "нет", "-", "выкл"}


@dataclass(frozen=True)
class Setting:
    """Описание одного поля .env.

    key       — имя переменной окружения (оно же попадёт в .env);
    title     — короткая подпись для человека (ru);
    help      — пояснение под полем (ru);
    type      — str | int | float | bool | secret | choice | time;
    choices   — варианты для type="choice" (первый — по сути умолчание);
    default   — значение по умолчанию строкой (для подсказки и разбора типа);
    secret    — прятать ли значение маской (ключи, токен);
    section   — к какой секции формы относится;
    attr      — атрибут в `Settings`, откуда брать текущее значение (по умолчанию key.lower());
    fallback  — что вернуть, если атрибута ещё нет (новые поля до интеграции);
    blank_zero — показывать пустым, если значение 0/None (id, координаты, «пусто = умолчание»).
    """

    key: str
    title: str
    help: str
    type: str = "str"
    choices: tuple[str, ...] = ()
    default: str = ""
    secret: bool = False
    section: str = "Прочее"
    attr: str = ""
    fallback: Any = None
    blank_zero: bool = False

    @property
    def cfg_attr(self) -> str:
        return self.attr or self.key.lower()

    def public(self) -> dict[str, Any]:
        """JSON-описание поля для страницы (без внутренних attr/fallback)."""
        return {
            "key": self.key,
            "title": self.title,
            "help": self.help,
            "type": self.type,
            "choices": list(self.choices),
            "default": self.default,
            "secret": bool(self.secret),
            "section": self.section,
        }


# ── полный список настроек (порядок = порядок в форме внутри секции) ─────────────
SETTINGS: tuple[Setting, ...] = (
    # ── Telegram ──────────────────────────────────────────────────────────────
    Setting("BOT_TOKEN", "Токен бота", "Токен от @BotFather (вид 1234567890:AAE…). Без него бот не запустится.",
            type="secret", secret=True, section="Telegram"),
    Setting("OWNER_ID", "Твой Telegram id", "Числовой id владельца. Не знаешь — напиши боту /start, он пришлёт. "
            "Бот слушается только тебя.", type="int", section="Telegram", blank_zero=True),
    Setting("TELEGRAM_API_URL", "Свой Bot API сервер", "Адрес своего telegram-bot-api. Обычно не нужно — "
            "пусто = api.telegram.org.", type="str", section="Telegram"),
    Setting("BOT_NAME", "Имя бота", "Как бот себя называет.", type="str", default="Оракул", section="Telegram"),
    Setting("OWNER_NAME", "Твоё имя", "Чтобы обращался по имени.", type="str", section="Telegram"),
    Setting("OWNER_GENDER", "Твой род", "В каком роде бот говорит о тебе («рад» / «рада»).",
            type="choice", choices=("m", "f"), default="m", section="Telegram"),

    # ── Модель ────────────────────────────────────────────────────────────────
    Setting("DEEPSEEK_API_KEY", "Ключ DeepSeek", "platform.deepseek.com → API keys (sk-…). Нужен "
            "положительный баланс. Можно несколько через запятую — при 402/401 бот перейдёт на следующий.",
            type="secret", secret=True, section="Модель", attr="llm_api_key"),
    Setting("LLM_API_KEY", "Ключ другого провайдера", "Если LLM_BASE_URL — не DeepSeek (OpenRouter и т.п.). "
            "Для Ollama / LM Studio впиши любое слово.", type="secret", secret=True, section="Модель",
            attr="llm_api_key"),
    Setting("LLM_BASE_URL", "Адрес API", "DeepSeek: https://api.deepseek.com; OpenRouter: "
            "https://openrouter.ai/api/v1; Ollama: http://localhost:11434/v1.",
            type="str", default="https://api.deepseek.com", section="Модель"),
    Setting("LLM_MODEL", "Быстрая модель", "Обычный разговор и инструменты.",
            type="str", default="deepseek-flash", section="Модель"),
    Setting("LLM_MODEL_DEEP", "Глубокая модель", "Сложные вопросы, /deep, разбор идей, дайджест, рефлексия. "
            "Пусто = deepseek-v4-pro у DeepSeek.", type="str", section="Модель"),
    Setting("LLM_AUTO_DEEP", "Сама включает глубокий режим", "Быстрая модель сама передаёт сложное глубокой.",
            type="bool", default="1", section="Модель"),
    Setting("LLM_FAST_THINKING", "Размышление в быстром режиме", "off — ответ за секунды; low/high/max — "
            "думает дольше, но тратит токены.", type="choice", choices=("off", "low", "high", "max"),
            default="off", section="Модель"),
    Setting("LLM_DEEP_EFFORT", "Глубина размышления", "В глубоком режиме. auto — пусть решает модель.",
            type="choice", choices=("low", "high", "max", "auto"), default="high", section="Модель"),
    Setting("LLM_TEMPERATURE", "Температура", "0–2: выше — живее и разнообразнее, ниже — суше.",
            type="float", default="1.0", section="Модель"),
    Setting("LLM_FAST_MAX_TOKENS", "Потолок ответа (быстрый)", "Максимум токенов в ответе быстрого режима.",
            type="int", default="8192", section="Модель"),
    Setting("LLM_DEEP_MAX_TOKENS", "Потолок ответа (глубокий)", "Максимум токенов в ответе глубокого режима.",
            type="int", default="65536", section="Модель"),
    Setting("LLM_FAST_TIMEOUT", "Таймаут ответа (быстрый), сек", "Сколько ждать один ответ модели.",
            type="float", default="120", section="Модель"),
    Setting("LLM_DEEP_TIMEOUT", "Таймаут ответа (глубокий), сек", "Сколько ждать один ответ глубокой модели.",
            type="float", default="600", section="Модель"),
    Setting("LLM_TURN_BUDGET", "Бюджет хода (быстрый), сек", "Сколько всего секунд на ответ (все шаги). "
            "Пусто = max(150, таймаут+30).", type="float", section="Модель"),
    Setting("LLM_DEEP_TURN_BUDGET", "Бюджет хода (глубокий), сек", "Пусто = max(900, 1,5×таймаут).",
            type="float", section="Модель"),
    Setting("LLM_MAX_STEPS", "Шагов на сообщение", "Сколько раз «подумал → вызвал инструмент» за один ответ.",
            type="int", default="8", section="Модель"),

    # ── Голос ─────────────────────────────────────────────────────────────────
    Setting("STT_PROVIDER", "Распознавание речи", "auto — сам выберет: Groq → OpenAI → локальный → выкл.",
            type="choice", choices=("auto", "groq", "openai", "local", "off"), default="auto", section="Голос"),
    Setting("STT_LANGUAGE", "Язык речи", "Код языка распознавания.", type="str", default="ru", section="Голос"),
    Setting("GROQ_API_KEY", "Ключ Groq", "Бесплатно: console.groq.com → API Keys (gsk_…). Самый простой путь "
            "к голосу.", type="secret", secret=True, section="Голос"),
    Setting("GROQ_STT_MODEL", "Модель Groq", "Модель распознавания у Groq.",
            type="str", default="whisper-large-v3-turbo", section="Голос"),
    Setting("OPENAI_API_KEY", "Ключ OpenAI", "Если распознаёшь через OpenAI (платно).",
            type="secret", secret=True, section="Голос"),
    Setting("OPENAI_STT_MODEL", "Модель OpenAI", "Модель распознавания у OpenAI.",
            type="str", default="whisper-1", section="Голос"),
    Setting("WHISPER_MODEL", "Локальная модель whisper", "Больше = точнее, но медленнее.",
            type="choice", choices=("tiny", "base", "small", "medium", "large-v3"), default="small",
            section="Голос"),
    Setting("WHISPER_DEVICE", "Устройство whisper", "cpu или cuda (видеокарта NVIDIA).",
            type="choice", choices=("cpu", "cuda"), default="cpu", section="Голос"),
    Setting("WHISPER_COMPUTE_TYPE", "Точность whisper", "int8 — для процессора, float16 — для видеокарты.",
            type="choice", choices=("int8", "float16", "float32"), default="int8", section="Голос"),
    Setting("SHOW_TRANSCRIPT", "Показывать расшифровку", "Показывать текст голосового перед ответом.",
            type="bool", default="1", section="Голос"),
    Setting("TTS_VOICE", "Голос ответа", "ru-RU-DmitryNeural (муж.) или ru-RU-SvetlanaNeural (жен.). "
            "Озвучка бесплатная (Microsoft Edge).", type="str", default="ru-RU-DmitryNeural", section="Голос"),
    Setting("TTS_DEFAULT", "Когда отвечать голосом", "off — только текст; mirror — на голос голосом; "
            "always — всегда дублировать.", type="choice", choices=("off", "mirror", "always"),
            default="off", section="Голос"),
    Setting("TTS_MAX_CHARS", "Символов озвучивать", "Длиннее — озвучит начало, остальное в тексте.",
            type="int", default="1500", section="Голос"),

    # ── Расписание ──────────────────────────────────────────────────────────────
    Setting("TIMEZONE", "Часовой пояс", "IANA-имя (Europe/Moscow) или сокращение (MSK, UTC+3). От него "
            "зависит всё расписание и будильники.", type="str", default="Europe/Moscow", section="Расписание"),
    Setting("MORNING_BRIEF_TIME", "Утренняя сводка", "Местное время ЧЧ:ММ, off — выключить.",
            type="time", default="08:00", section="Расписание"),
    Setting("NEWS_DIGEST_TIME", "Дайджест новостей", "Местное время ЧЧ:ММ, off — выключить.",
            type="time", default="09:00", section="Расписание"),
    Setting("REFLECTION_TIME", "Ночная рефлексия", "Дневник и пересмотр позиций. ЧЧ:ММ, off — выключить.",
            type="time", default="03:30", section="Расписание"),
    Setting("BIRTHDAY_TIME", "Проверка дней рождения", "Когда предупреждать и поздравлять. ЧЧ:ММ, off.",
            type="time", default="09:00", section="Расписание"),
    Setting("NAG_INTERVAL_MIN", "Долбёжка, раз в N минут", "Как часто напоминать, пока не нажмёшь «Готово».",
            type="int", default="3", section="Расписание"),
    Setting("NAG_MAX", "Сколько раз долбить", "Прежде чем сдаться.", type="int", default="20",
            section="Расписание"),
    Setting("WAKE_CHALLENGE", "Будильник с задачкой", "Снимается только решённой задачкой.",
            type="bool", default="1", section="Расписание"),

    # ── Новости и погода ─────────────────────────────────────────────────────────
    Setting("NEWS_FEEDS", "RSS-ленты", "Через запятую. Пусто = встроенный набор из разных лагерей.",
            type="str", section="Новости и погода"),
    Setting("WEB_SEARCH", "Поиск в интернете", "DuckDuckGo, без ключа.", type="bool", default="1",
            section="Новости и погода"),
    Setting("WEATHER_CITY", "Город для погоды", "Open-Meteo, без ключа. Например: Калининград.",
            type="str", section="Новости и погода"),
    Setting("WEATHER_LAT", "Широта", "Точные координаты вместо города. Пример: 54.71.",
            type="float", section="Новости и погода", blank_zero=True),
    Setting("WEATHER_LON", "Долгота", "Пример: 20.51.", type="float", section="Новости и погода",
            blank_zero=True),

    # ── Файлы ──────────────────────────────────────────────────────────────────
    Setting("FILES_ROOTS", "Папки для чтения", "Куда боту можно заглядывать (через запятую). "
            "Пусто = твоя домашняя папка.", type="str", section="Файлы", attr="files_roots",
            fallback=(Path.home(),)),
    Setting("FILES_WORKSPACE", "Рабочая папка", "Куда бот пишет заметки и зеркалит идеи.",
            type="str", section="Файлы", attr="files_workspace", fallback=Path.home() / "Oracle"),

    # ── Панель ─────────────────────────────────────────────────────────────────
    Setting("DASHBOARD_HOST", "Адрес панели", "Только локально. Менять без нужды не стоит.",
            type="str", default="127.0.0.1", section="Панель", attr="dashboard_host", fallback="127.0.0.1"),
    Setting("DASHBOARD_PORT", "Порт панели", "Порт локальной панели.", type="int", default="8765",
            section="Панель", attr="dashboard_port", fallback=8765),
    Setting("DASHBOARD_OPEN", "Открывать браузер", "Открывать панель в браузере при старте.",
            type="bool", default="1", section="Панель", attr="dashboard_open", fallback=True),

    # ── Прочее ─────────────────────────────────────────────────────────────────
    Setting("HISTORY_MESSAGES", "Реплик в контексте", "Сколько последних сообщений бот держит перед глазами "
            "(не меньше 4).", type="int", default="30", section="Прочее"),
    Setting("SUMMARY_CHUNK", "Сворачивать по N реплик", "Сколько старых реплик за раз уходит в конспект "
            "(не меньше 4).", type="int", default="20", section="Прочее"),
    Setting("USERBOT_ENABLED", "Userbot (чтение чатов)", "Читать свои чаты и предлагать ответы. Потом один раз "
            "войти: python -m oracle.userbot_login.", type="bool", default="0", section="Прочее"),
    Setting("TG_API_ID", "TG_API_ID", "С my.telegram.org → API development tools.", type="int",
            section="Прочее", blank_zero=True),
    Setting("TG_API_HASH", "TG_API_HASH", "С my.telegram.org. Секрет.", type="secret", secret=True,
            section="Прочее"),
    Setting("USERBOT_SESSION", "Файл сессии userbot", "Путь без .session. Пусто = data/userbot. "
            "Это полный доступ к аккаунту — не отдавай никому.", type="str", section="Прочее"),
    Setting("USERBOT_NOTIFY", "Уведомлять о личных сообщениях", "С кнопкой «Предложить ответ».",
            type="bool", default="0", section="Прочее"),
    Setting("USAGE_PEAK_PRICING", "Пиковый тариф DeepSeek", "Считать расходы дороже в пиковом окне (будни, "
            "ночь и утро UTC).", type="bool", default="1", section="Прочее", attr="usage_peak_pricing",
            fallback=True),
    Setting("SINGLE_INSTANCE", "Одна копия из этой папки", "Замок не даёт запустить второй экземпляр отсюда.",
            type="bool", default="1", section="Прочее", attr="single_instance", fallback=True),
    Setting("DATA_DIR", "Папка данных", "Пусто = data рядом с кодом.", type="str", section="Прочее"),
    Setting("DB_PATH", "Файл базы", "Пусто = oracle.db в папке данных.", type="str", section="Прочее"),
    Setting("LOG_LEVEL", "Подробность лога", "DEBUG | INFO | WARNING | ERROR.", type="choice",
            choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO", section="Прочее"),
)

_BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}


def get_setting(key: str) -> Setting | None:
    """Описание настройки по имени переменной, или None."""
    return _BY_KEY.get(key)


def sections() -> list[dict[str, Any]]:
    """Настройки, сгруппированные по секциям в порядке вывода: [{name, settings: [public…]}]."""
    out: list[dict[str, Any]] = []
    for name in SECTION_ORDER:
        items = [s.public() for s in SETTINGS if s.section == name]
        if items:
            out.append({"name": name, "settings": items})
    # секции, не попавшие в SECTION_ORDER (на случай новой) — в конец
    seen = set(SECTION_ORDER)
    for s in SETTINGS:
        if s.section not in seen:
            out.append({"name": s.section, "settings": [x.public() for x in SETTINGS
                                                        if x.section == s.section]})
            seen.add(s.section)
    return out


def schema_public() -> list[dict[str, Any]]:
    """Плоский список описаний всех настроек (для страницы)."""
    return [s.public() for s in SETTINGS]


# ── маска секрета ───────────────────────────────────────────────────────────────
MASK_MARK = "…"     # признак, что значение показано маской (в настоящих ключах этого символа нет)


def mask_secret(value: str) -> str:
    """Секрет → короткая маска «sk-…c4d5» (видно начало и хвост, середина скрыта).
    Пусто → пусто. Короткое (≤ 8) → «…» + пара последних символов."""
    v = str(value or "")
    if not v:
        return ""
    if len(v) <= 8:
        return MASK_MARK + v[-2:] if len(v) > 2 else MASK_MARK
    return f"{v[:3]}{MASK_MARK}{v[-4:]}"


def is_mask(value: str) -> bool:
    """Похоже ли значение на нашу маску (значит, секрет не меняли)."""
    return MASK_MARK in str(value or "")


# ── текущие значения ─────────────────────────────────────────────────────────────
def _current_str(s: Setting, value: Any) -> str:
    """Значение из cfg → строка того же вида, что вернёт validate (чтобы сравнивать «изменилось»)."""
    if s.secret:
        return mask_secret(str(value or ""))
    if s.type == "bool":
        return "1" if _as_bool(value) else "0"
    if value is None:
        return ""
    if isinstance(value, (tuple, list)):
        # NEWS_FEEDS: встроенный набор показываем пустым (пусто = умолчание)
        if s.key == "NEWS_FEEDS" and tuple(value) == tuple(DEFAULT_FEEDS):
            return ""
        return ", ".join(str(x) for x in value)
    if s.type in ("int",):
        try:
            iv = int(value)
        except (TypeError, ValueError):
            return str(value)
        return "" if (s.blank_zero and iv == 0) else str(iv)
    if s.type in ("float",):
        try:
            fv = float(value)
        except (TypeError, ValueError):
            return str(value)
        if s.blank_zero and fv == 0:
            return ""
        return _fmt_float(fv)
    text = str(value)
    return text


def masked_values(cfg: Any) -> dict[str, str]:
    """Текущие значения всех настроек строками; секреты — только маской. Новые поля, которых ещё
    нет в cfg, берутся через `getattr(cfg, attr, fallback)`."""
    out: dict[str, str] = {}
    for s in SETTINGS:
        value = getattr(cfg, s.cfg_attr, s.fallback)
        out[s.key] = _current_str(s, value)
    return out


# ── проверка ввода ───────────────────────────────────────────────────────────────
def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


def _fmt_float(f: float) -> str:
    """Красивая строка числа: целое без .0, иначе как есть."""
    if f == int(f):
        return str(int(f))
    return repr(f)


def _validate_time(raw: str) -> tuple[bool, str]:
    s = raw.strip().lower()
    if s in _TIME_OFF:
        return True, ""            # задача выключена
    try:
        h_s, m_s = s.split(":")
        h, m = int(h_s), int(m_s)
    except ValueError:
        return False, "нужно время в виде ЧЧ:ММ (или off, чтобы выключить)"
    if not (0 <= h < 24 and 0 <= m < 60):
        return False, "часы 0–23, минуты 0–59"
    return True, f"{h:02d}:{m:02d}"


def validate(key: str, raw: Any) -> tuple[bool, str]:
    """Проверить одно поле формы. → (True, нормализованное_значение) или (False, текст_ошибки_ru).

    Нормализованное значение приводится к тому же виду, что и `masked_values`, чтобы по нему
    можно было понять, изменилось ли поле. Пустое значение обычно допустимо (= умолчание).
    """
    s = _BY_KEY.get(key)
    if s is None:
        return False, f"неизвестная настройка «{key}»"
    text = "" if raw is None else str(raw).strip()

    # секрет: маску принимаем как «не меняли», любую строку — как новое значение
    if s.secret:
        return True, text

    if s.type == "bool":
        low = text.lower()
        if low in _TRUE:
            return True, "1"
        if low in _FALSE:
            return True, "0"
        return False, "нужно да/нет (1 или 0)"

    if s.type == "time":
        return _validate_time(text)

    if s.type == "choice":
        if not text:
            return True, ""        # пусто = умолчание
        low = text.lower()
        for c in s.choices:
            if c.lower() == low:
                return True, c
        return False, "допустимо: " + ", ".join(s.choices)

    if s.type == "int":
        if not text:
            return True, ""
        try:
            v = int(text)
        except ValueError:
            return False, "нужно целое число"
        return _check_int_range(s, v)

    if s.type == "float":
        if not text:
            return True, ""
        try:
            v = float(text.replace(",", "."))
        except ValueError:
            return False, "нужно число"
        return _check_float_range(s, v)

    # str
    return _validate_str(s, text)


def _check_int_range(s: Setting, v: int) -> tuple[bool, str]:
    if s.key == "OWNER_ID" and v <= 0:
        return False, "id — положительное число"
    if s.key == "TG_API_ID" and v < 0:
        return False, "нужно положительное число"
    if s.key == "DASHBOARD_PORT" and not (1 <= v <= 65535):
        return False, "порт 1–65535"
    if s.key == "LLM_MAX_STEPS" and v < 1:
        return False, "не меньше 1"
    if s.key in ("HISTORY_MESSAGES", "SUMMARY_CHUNK") and v < 4:
        return False, "не меньше 4"
    if s.key in ("NAG_INTERVAL_MIN", "NAG_MAX") and v < 1:
        return False, "не меньше 1"
    if s.key in ("LLM_FAST_MAX_TOKENS", "LLM_DEEP_MAX_TOKENS", "TTS_MAX_CHARS") and v < 1:
        return False, "не меньше 1"
    return True, str(v)


def _check_float_range(s: Setting, v: float) -> tuple[bool, str]:
    if s.key == "LLM_TEMPERATURE" and not (0 <= v <= 2):
        return False, "температура 0–2"
    if s.key == "WEATHER_LAT" and not (-90 <= v <= 90):
        return False, "широта −90…90"
    if s.key == "WEATHER_LON" and not (-180 <= v <= 180):
        return False, "долгота −180…180"
    if s.key in ("LLM_FAST_TIMEOUT", "LLM_DEEP_TIMEOUT", "LLM_TURN_BUDGET",
                 "LLM_DEEP_TURN_BUDGET") and v < 0:
        return False, "не меньше 0"
    return True, _fmt_float(v)


def _validate_str(s: Setting, text: str) -> tuple[bool, str]:
    if s.key == "TIMEZONE":
        if not text:
            return False, "часовой пояс не может быть пустым"
        tz = resolve_timezone(text)
        if tz is None:
            return False, "не распознал пояс — нужен вид Europe/Moscow (или MSK, UTC+3)"
        return True, tz
    if s.key == "LLM_BASE_URL" and text and not text.lower().startswith(("http://", "https://")):
        return False, "адрес должен начинаться с http:// или https://"
    return True, text


# ── форма → изменения для .env ───────────────────────────────────────────────────
def to_env_updates(form: dict[str, Any], current: dict[str, str] | None = None) -> dict[str, str | None]:
    """Из отправленной формы собрать только изменившиеся, реально введённые значения.

    Возвращает `{ENV_NAME: значение | None}` для `oracle.runtime.update_env`: None — стереть строку
    (поле очистили). Пропускаются: неизвестные ключи; невалидные (их ловит `validate` отдельно);
    секреты, оставшиеся маской; значения, совпавшие с текущими (`current`, обычно `masked_values(cfg)`).
    Без `current` считаем изменившимся всё, кроме секретов-масок.
    """
    updates: dict[str, str | None] = {}
    current = current or {}
    for key, raw in form.items():
        s = _BY_KEY.get(key)
        if s is None:
            continue
        # секрет, оставшийся маской (или пустой при заданном текущем) — не трогаем
        if s.secret and is_mask("" if raw is None else str(raw)):
            continue
        ok, norm = validate(key, raw)
        if not ok:
            continue
        if s.secret and not norm:
            # пустой секрет: очистить только если он и был задан
            if current.get(key):
                updates[key] = None
            continue
        if key in current and norm == current[key]:
            continue                        # не изменилось
        updates[key] = norm if norm != "" else None
    return updates
