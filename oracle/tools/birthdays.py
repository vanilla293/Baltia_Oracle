"""Дни рождения: хранение, ближайшие даты, живые поздравления и ежедневная проверка.

Поздравление пишет модель ОТ ИМЕНИ владельца — он перешлёт его сам (или кнопкой через userbot).
Отметки `last_prenotice_year` / `last_greeted_year` не дают напомнить или поздравить дважды за год
(год — того ДР, о котором речь), и ставятся только после отправки: повторный прогон безопасен.
Каждый написанный вариант поздравления — в kv `bday_greeting:<id>:<n>` (номер — счётчик `bday_gen:<id>`):
кнопка `bday:send:<id>:<n>` отправляет ровно тот вариант, под которым нажата, даже если потом написаны
другие. Последний вариант — ещё и в `bday_greeting:<id>` (от него новый вариант отталкивается, чтобы
не повторяться), а в `bday_greeting_for:<id>` — к какому ДР (дата) он написан: повтор после сбоя его
не переписывает.
В сам день, кроме поздравления, ставится «Поздравить…» — напоминание (ref_type='birthday') с долбёжкой
раз в час до вечера, пока владелец не отметит «Готово» или не отправит поздравление кнопкой.
Не поздравили в сам день (бот лежал, Telegram не принял) — догоняем назавтра с пометкой «вчера».
"""
from __future__ import annotations

import calendar
import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import timeutil
from ..db import normalize_text, stem
from .base import Buttons, OutItem, ToolContext, tool

log = logging.getLogger("oracle.tools.birthdays")

MAX_REMIND_DAYS = 60
GRACE_DAYS = 1                  # не поздравили в сам день — догоняем столько дней спустя
NAG_EVERY_MIN = 60              # «Поздравить…» долбит раз в час…
NAG_FROM = time(9, 0)           # …не раньше 9 утра (проверка в полночь не будит)…
NAG_UNTIL = time(22, 0)         # …и не позже 22:00 — последний нажим и «сдаюсь» тоже
NAG_MAX = 12

# ── разбор даты ──────────────────────────────────────────────────────────────
_MONTH_WORDS: tuple[tuple[str, int], ...] = (
    ("янв", 1), ("фев", 2), ("мар", 3), ("апр", 4), ("май", 5), ("мая", 5), ("мае", 5),
    ("июн", 6), ("июл", 7), ("авг", 8), ("сен", 9), ("окт", 10), ("ноя", 11), ("дек", 12),
    ("jan", 1), ("feb", 2), ("mar", 3), ("apr", 4), ("may", 5), ("jun", 6), ("jul", 7),
    ("aug", 8), ("sep", 9), ("oct", 10), ("nov", 11), ("dec", 12),
)

_RE_ISO = re.compile(r"(\d{4})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})(?:[t\s].*)?")
_RE_DMY = re.compile(r"(\d{1,2})(?:\s*[./\-]\s*|\s+)(\d{1,2})(?:(?:\s*[./\-]\s*|\s+)(\d{4}|\d{2}))?\.?")
_RE_D_WORD = re.compile(r"(\d{1,2})(?:\s*-?\s*(?:го|е|ое))?\s*([a-zа-я]+)\.?(?:\s*,?\s*(\d{4}|\d{2}))?")
_RE_WORD_D = re.compile(r"([a-zа-я]+)\.?\s*(\d{1,2})(?:\s*-?\s*(?:го|е|ое))?(?:\s*,?\s*(\d{4}|\d{2}))?")
_RE_YEAR_SUFFIX = re.compile(r"(?<=\d)\s*(?:гг|года|год|г)\.?$")


def _month_from_word(word: str) -> int | None:
    w = normalize_text(word).strip(".")
    if len(w) < 3:
        return None
    for pre, m in _MONTH_WORDS:
        if w.startswith(pre):
            return m
    return None


def _today() -> date:
    return timeutil.now_utc().date()


def _year(raw: str | None, today: date) -> int | None:
    if not raw:
        return None
    y = int(raw)
    if len(raw) == 2:   # «14.03.90» → 1990, «01.02.05» → 2005
        y = 2000 + y if 2000 + y <= today.year else 1900 + y
    if not 1900 <= y <= today.year:
        raise ValueError(f"год рождения {y} не похож на правду — нужен от 1900 до {today.year}")
    return y


def parse_birth_date(s: Any, today: date | None = None) -> tuple[int, int, int | None]:
    """Дата рождения от модели → (месяц, день, год | None).

    Понимает «DD.MM», «D.M», «DD.MM.YYYY», «DD.MM.YY», «YYYY-MM-DD», «14 марта», «14 марта 1990 г.»,
    «14 март», «1-го мая», «March 14, 1990». 29.02 без года — можно; с годом — только високосным.
    """
    today = today or _today()
    raw = normalize_text(str(s if s is not None else "")).strip().strip(",;")
    if not raw:
        raise ValueError("пустая дата рождения — нужна «DD.MM», «DD.MM.YYYY» или «14 марта»")
    raw = _RE_YEAR_SUFFIX.sub("", raw).strip().rstrip(".").strip()
    raw = re.sub(r"\s+", " ", raw)

    day = month = None
    yraw: str | None = None
    if m := _RE_ISO.fullmatch(raw):
        yraw, month, day = m.group(1), int(m.group(2)), int(m.group(3))
    elif m := _RE_DMY.fullmatch(raw):
        day, month, yraw = int(m.group(1)), int(m.group(2)), m.group(3)
    elif (m := _RE_D_WORD.fullmatch(raw)) and _month_from_word(m.group(2)):
        day, month, yraw = int(m.group(1)), _month_from_word(m.group(2)), m.group(3)
    elif (m := _RE_WORD_D.fullmatch(raw)) and _month_from_word(m.group(1)):
        day, month, yraw = int(m.group(2)), _month_from_word(m.group(1)), m.group(3)
    else:
        raise ValueError(f"не понял дату рождения «{s}» — нужна «DD.MM», «DD.MM.YYYY», "
                         f"«YYYY-MM-DD» или «14 марта»")

    year = _year(yraw, today)
    if not 1 <= month <= 12:
        raise ValueError(f"месяца {month} не бывает — дата «{s}»")
    try:
        date(year or 2000, month, day)          # 2000 — високосный: 29.02 без года пропускаем
    except ValueError:
        if month == 2 and day == 29 and year:
            raise ValueError(f"29 февраля {year} не было — год не високосный") from None
        raise ValueError(f"такой даты нет: {day} {timeutil.month_gen(month)}") from None
    if year and date(year, month, day) > today:
        raise ValueError(f"дата рождения {day}.{month:02d}.{year} ещё не наступила")
    return month, day, year


def human_date(month: int, day: int, year: int | None = None) -> str:
    """'14 марта' или '14 марта 1990'."""
    s = f"{day} {timeutil.month_gen(month)}"
    return f"{s} {year}" if year else s


# ── расчёты ──────────────────────────────────────────────────────────────────
def _in_year(y: int, month: int, day: int) -> date:
    if month == 2 and day == 29 and not calendar.isleap(y):
        return date(y, 2, 28)
    return date(y, month, day)


def next_birthday(month: int, day: int, today: date) -> date:
    """Ближайший день рождения не раньше today (сегодня — это сегодня). 29.02 в невисокосный → 28.02."""
    d = _in_year(today.year, month, day)
    return d if d >= today else _in_year(today.year + 1, month, day)


def current_birthday(month: int, day: int, today: date) -> date:
    """ДР, о котором сейчас речь: прошедший не больше GRACE_DAYS дней назад (его ещё можно поздравить)
    или ближайший будущий."""
    return next_birthday(month, day, today - timedelta(days=GRACE_DAYS))


def _turns(year: int | None, next_date: date) -> int | None:
    if not year:
        return None
    n = next_date.year - int(year)
    return n if n > 0 else None


async def upcoming(db, tz: ZoneInfo, days: int = 30) -> list[dict]:
    """Дни рождения в ближайшие `days` дней (0 — только сегодня), ближайшие первыми.

    К строке таблицы добавляются "next_date" (ISO-дата), "days_left" (0 — сегодня), "turns" (или None).
    """
    today = timeutil.now_local(tz).date()
    out = []
    for r in await db.fetchall("SELECT * FROM birthdays"):
        try:
            nd = next_birthday(int(r["month"]), int(r["day"]), today)
        except (TypeError, ValueError):
            log.warning("кривая дата у дня рождения #%s: %s.%s", r.get("id"), r.get("day"), r.get("month"))
            continue
        left = (nd - today).days
        if left > days:
            continue
        out.append({**r, "next_date": nd.isoformat(), "days_left": left, "turns": _turns(r["year"], nd)})
    out.sort(key=lambda x: (x["days_left"], normalize_text(x["name"])))
    return out


async def get_birthday(db, bid: Any) -> dict | None:
    try:
        bid = int(bid)
    except (TypeError, ValueError):
        return None
    return await db.fetchone("SELECT * FROM birthdays WHERE id=?", (bid,))


def _with_next(b: dict, today: date) -> dict:
    """Строка + next_date / days_left / turns на сегодня."""
    nd = next_birthday(int(b["month"]), int(b["day"]), today)
    return {**b, "next_date": nd.isoformat(), "days_left": (nd - today).days,
            "turns": _turns(b.get("year"), nd)}


# ── поздравление ─────────────────────────────────────────────────────────────
BANNED_CLICHES = (
    "счастья, здоровья, успехов", "пусть сбудутся все мечты", "желаю всего самого наилучшего",
    "море позитива", "оставайся таким же", "всех благ", "исполнения желаний", "с днём варенья",
)

GREETING_SYSTEM = """\
Ты пишешь поздравление с днём рождения ОТ ИМЕНИ {owner} — он сам перешлёт его имениннику. \
Пиши от первого лица, как живой человек пишет в мессенджере, а не как открытка.

Правила:
- 3–6 предложений, один-два коротких абзаца. Обращайся по имени так, как оно записано \
(или как принято по отношению: маме — «мам»).
- Если о человеке что-то известно (заметки, кем приходится, факты из памяти) — возьми ОДНУ конкретную деталь \
и вплети естественно. Не перечисляй всё подряд и не выдумывай того, чего нет в данных.
- Тон по отношению: друг — тепло, можно пошутить и по-доброму подколоть; мама, папа, родные — тепло и просто, \
без пафоса; коллега, начальник, знакомый — сдержанно и по-человечески, без фамильярности; \
отношение неизвестно — нейтрально-тепло.
- Запрещены штампы: {banned}. И вообще никаких перечислений пожеланий через запятую — \
одно-два конкретных пожелания вместо списка.
- Возраст упоминай, только если это естественно (круглая дата, близкий человек); коллегам — не упоминай.
- Эмодзи — не больше одного, можно ни одного. Без подписи, без кавычек, без заголовка, без вариантов \
и пояснений — только сам текст поздравления.
- Пишет {gender}."""

_GENDER = {False: "мужчина: согласуй род («рад», «помню», «хотел»)",
           True: "женщина: согласуй род («рада», «помню», «хотела»)"}
BELATED_STYLE = "ДР был вчера, поздравление запоздалое — признай это одной лёгкой фразой, без оправданий"


def owner_female(ctx: ToolContext) -> bool:
    """Владелец — женщина? (cfg.owner_gender: m/f, «ж»; по умолчанию — мужчина)."""
    g = str(getattr(ctx.cfg, "owner_gender", "") or "").strip().lower()
    return g[:1] in ("f", "ж", "w")

_EMOJI_CHAR = "\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿"
_EMOJI = re.compile(
    f"[{_EMOJI_CHAR}](?:[️\U0001F3FB-\U0001F3FF])?(?:‍[{_EMOJI_CHAR}]️?)*")
_QUOTES = {'"': '"', "«": "»", "“": "”", "„": "“", "'": "'"}


def _limit_emoji(text: str, keep: int = 1) -> str:
    seen = 0

    def repl(m: re.Match) -> str:
        nonlocal seen
        seen += 1
        return m.group(0) if seen <= keep else ""

    out = _EMOJI.sub(repl, text)
    if seen > keep:
        out = re.sub(r"[ \t]{2,}", " ", out)
        out = re.sub(r"[ \t]+([,.!?…])", r"\1", out)
        out = re.sub(r"[ \t]+\n", "\n", out)
    return out.strip()


def clean_greeting(text: str) -> str:
    """Снять с ответа модели обёртку: «Вот поздравление:», кавычки, лишние эмодзи."""
    t = (text or "").strip()
    lines = t.splitlines()
    if len(lines) > 1 and lines[0].rstrip().endswith(":") and len(lines[0]) <= 60:
        t = "\n".join(lines[1:]).strip()
    while len(t) >= 2 and t[0] in _QUOTES and t[-1] == _QUOTES[t[0]]:
        t = t[1:-1].strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    return _limit_emoji(t, 1)


async def _person_facts(db, name: str, limit: int = 5) -> list[str]:
    """Факты из памяти, где упоминается человек (грубо, по основе слова имени)."""
    keys = [stem(w) for w in re.findall(r"[^\W\d_]+", normalize_text(name)) if len(w) >= 3]
    keys = [k for k in keys if len(k) >= 3]
    if not keys:
        return []
    try:
        rows = await db.fetchall("SELECT content FROM facts ORDER BY id DESC LIMIT 500")
    except Exception:  # таблицы может не быть в урезанной базе — поздравление важнее
        return []
    out = []
    for r in rows:
        words = re.findall(r"[^\W_]+", normalize_text(r["content"]))
        if any(w.startswith(k) for w in words for k in keys):
            out.append(r["content"].strip())
            if len(out) >= limit:
                break
    return out


def _greeting_user_prompt(b: dict, turns: int | None, facts: list[str], style: str, avoid: str) -> str:
    lines = [f"Кого поздравляем: {b['name']}",
             f"Кем приходится: {(b.get('relation') or '').strip() or 'не указано'}"]
    lines.append(f"Исполняется: {turns}" if turns else "Возраст: неизвестен")
    if (b.get("notes") or "").strip():
        lines.append(f"Что о нём известно: {b['notes'].strip()}")
    if facts:
        lines.append("Ещё из памяти:\n" + "\n".join(f"- {f}" for f in facts))
    if style.strip():
        lines.append(f"Пожелание к варианту: {style.strip()}")
    if avoid.strip():
        lines.append(f"Прошлый вариант — не повторяй его ни словами, ни ходом мысли:\n{avoid.strip()}")
    lines.append("Напиши поздравление.")
    return "\n".join(lines)


async def generate_greeting(ctx: ToolContext, bday: dict, style: str = "", *, avoid: str = "") -> str:
    """Живое поздравление от имени владельца: 3–6 предложений, одна деталь о человеке, без штампов.

    Ошибки модели (LLMError) пробрасываются; пустой ответ → ValueError.
    """
    owner = (ctx.cfg.owner_name or "").strip()
    owner = f"владельца ({owner})" if owner else "владельца"      # «ОТ ИМЕНИ владельца (Андрей)»
    system = GREETING_SYSTEM.format(owner=owner, banned=", ".join(f"«{c}»" for c in BANNED_CLICHES),
                                    gender=_GENDER[owner_female(ctx)])
    turns = bday.get("turns") if "turns" in bday else _with_next(bday, ctx.now_local().date())["turns"]
    facts = await _person_facts(ctx.db, bday.get("name") or "")
    user = _greeting_user_prompt(bday, turns, facts, style or "", avoid or "")
    raw = await ctx.llm.ask(system, user, deep=False, temperature=1.1)
    text = clean_greeting(raw)
    if not text:
        raise ValueError("модель вернула пустое поздравление")
    return text


_FAMILY = ("мам", "пап", "мать", "отец", "бабуш", "дедуш", "сестр", "брат", "доч", "сын", "жена", "муж",
           "тет", "дяд", "родн")
_FORMAL = ("коллег", "начальн", "шеф", "руковод", "партнер", "клиент", "знаком", "сосед")


def fallback_greeting(b: dict, *, female: bool = False) -> str:
    """Заготовка на случай, когда модель недоступна: по-человечески и без штампов."""
    name = (b.get("name") or "").strip() or "Слушай"
    rel = normalize_text(b.get("relation") or "")
    if any(w in rel for w in _FAMILY):
        return (f"{name}, с днём рождения! Спасибо, что ты у меня есть, — говорю это реже, чем стоило бы. "
                "Люблю тебя и крепко обнимаю.")
    if any(w in rel for w in _FORMAL):
        return (f"{name}, с днём рождения! Работать и общаться с тобой — правда удовольствие, "
                "и это не дежурная фраза. Хорошего года: интересных задач и спокойных выходных.")
    return (f"{name}, с днём рождения! {'Рада' if female else 'Рад'}, что ты есть в моей жизни, — без дежурных слов. "
            "Пусть этот год подкинет побольше поводов для хороших историй. Обнимаю!")


def userbot_ready(ctx: ToolContext) -> bool:
    ub = ctx.services.userbot
    return bool(getattr(ctx.cfg, "userbot_enabled", False) and ub is not None and getattr(ub, "ready", False))


def greeting_key(bid: int) -> str:
    return f"bday_greeting:{int(bid)}"


def greeting_for_key(bid: int) -> str:
    """kv: к какому ДР (ISO-дата) написан текст из greeting_key — его переиспользует ежедневная проверка."""
    return f"bday_greeting_for:{int(bid)}"


def variant_key(bid: int, n: int) -> str:
    """kv: текст варианта n поздравления для ДР bid (его отправляет кнопка bday:send:<bid>:<n>)."""
    return f"bday_greeting:{int(bid)}:{int(n)}"


def gen_key(bid: int) -> str:
    """kv: номер последнего написанного варианта поздравления."""
    return f"bday_gen:{int(bid)}"


VARIANTS_KEEP = 20          # сколько последних вариантов держать для кнопок «Отправить»


async def store_greeting(db, bid: int, text: str) -> int:
    """Новый вариант поздравления → kv (номерной и «последний») → его номер."""
    bid = int(bid)
    try:
        n = int(await db.kv_get(gen_key(bid), 0) or 0) + 1
    except (TypeError, ValueError):
        n = 1
    await db.kv_set(gen_key(bid), n)
    await db.kv_set(variant_key(bid, n), text)
    await db.kv_set(greeting_key(bid), text)
    if n > VARIANTS_KEEP:
        await db.kv_delete(variant_key(bid, n - VARIANTS_KEEP))
    return n


async def latest_variant(db, bid: int, text: str) -> int | None:
    """Номер последнего варианта, если его текст — text (иначе None: кнопка без номера)."""
    try:
        n = int(await db.kv_get(gen_key(bid), 0) or 0)
    except (TypeError, ValueError):
        return None
    return n if n and await db.kv_get(variant_key(bid, n)) == text else None


def send_data(bday: dict) -> str:
    """callback_data кнопки «Отправить»: с номером варианта, если он известен."""
    n = bday.get("greeting_variant")
    return f"bday:send:{int(bday['id'])}" + (f":{int(n)}" if n else "")


def greeting_buttons(ctx: ToolContext, bday: dict) -> Buttons:
    """Кнопки под поздравлением: «другой вариант» и, если userbot готов и есть username, «отправить»
    (ровно этот вариант — номер в bday["greeting_variant"])."""
    rows: Buttons = [[("🔁 Другой вариант", f"bday:regen:{bday['id']}")]]
    tg = (bday.get("tg_username") or "").strip()
    if tg and userbot_ready(ctx):
        rows.append([(f"📨 Отправить @{tg}", send_data(bday))])
    return rows


async def regenerate_greeting(ctx: ToolContext, bid: Any, style: str = "") -> tuple[dict, str]:
    """Новый вариант поздравления (не похожий на прошлый) → kv → (запись с greeting_variant, текст).
    Для кнопки bday:regen и инструмента birthday_greeting."""
    b = await get_birthday(ctx.db, bid)
    if b is None:
        raise ValueError(f"нет дня рождения #{bid}")
    prev = await ctx.db.kv_get(greeting_key(b["id"]), "")
    text = await generate_greeting(ctx, b, style, avoid=prev if isinstance(prev, str) else "")
    n = await store_greeting(ctx.db, b["id"], text)
    today = ctx.now_local().date()
    bd = current_birthday(int(b["month"]), int(b["day"]), today)
    if (bd - today).days <= 1:      # написан к ближайшему ДР — в сам день пришлю этот же, а не новый
        await ctx.db.kv_set(greeting_for_key(b["id"]), bd.isoformat())
    return {**b, "greeting_variant": n}, text


# ── ежедневная проверка ──────────────────────────────────────────────────────
def _who(b: dict) -> str:
    rel = (b.get("relation") or "").strip()
    return f"{b['name']} ({rel})" if rel else b["name"]


def _days(n: int) -> str:
    n = abs(int(n))
    word = "день" if n % 10 == 1 and n % 100 != 11 else \
        "дня" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "дней"
    return f"{n} {word}"


def _prenotice_text(b: dict) -> str:
    left = int(b["days_left"])
    when = "Завтра" if left == 1 else f"Через {_days(left)}"
    head = f"🎁 {when} день рождения: {_who(b)}"
    if b.get("turns"):
        head += f" — исполнится {b['turns']}"
    lines = [head + "."]
    if left > 1:
        nd = date.fromisoformat(b["next_date"])
        lines.append(f"Это {timeutil.weekday_name(nd)}, {human_date(nd.month, nd.day)}.")
    if (b.get("notes") or "").strip():
        lines.append(f"Из заметок: {b['notes'].strip()}")
    lines.append("Подарок или поздравление лучше решить заранее — могу помочь.")
    return "\n".join(lines)


def _today_text(b: dict, greeting: str, fallback: bool, left: int = 0) -> str:
    if left < 0:        # догоняем: в сам день не вышло
        ago = {-1: "Вчера", -2: "Позавчера"}.get(left, f"{_days(left)} назад")
        head = f"🎂 {ago} был день рождения: {_who(b)}"
        if b.get("turns"):
            head += f" — исполнилось {b['turns']}"
        head += ".\nВовремя напомнить не вышло — поздравь сейчас, лучше поздно"
    else:
        head = f"🎂 Сегодня день рождения: {_who(b)}"
        if b.get("turns"):
            head += f" — исполняется {b['turns']}"
    text = f"{head}.\n\nВот поздравление, можно переслать:\n\n{greeting}"
    if fallback:
        text += "\n\n(Модель сейчас не ответила — это заготовка. Нажми «Другой вариант», когда оживёт.)"
    return text


async def birthday_jobs(ctx: ToolContext, *, failures: list[int] | None = None) -> int:
    """Ежедневно (cfg.birthday_time): предупреждение за remind_days_before, в сам день — поздравление
    с кнопками и «Поздравить…» с долбёжкой; не вышло в сам день — назавтра, с пометкой «вчера».
    Отметки last_*_year ставятся только после успешной отправки, так что повторный прогон безопасен.
    → сколько сообщений ушло; id тех, что не ушли, — в failures (планировщик повторит позже)."""
    notifier = ctx.services.notifier
    if notifier is None:
        return 0
    today = ctx.now_local().date()
    sent = 0
    for b in await _due(ctx.db, today):
        try:
            sent += await _process_one(ctx, b, today)
        except Exception:   # один кривой ДР не должен ломать остальные
            log.exception("день рождения #%s: не смог обработать", b.get("id"))
            if failures is not None:
                failures.append(int(b["id"]))
    return sent


async def _due(db, today: date) -> list[dict]:
    """ДР, по которым сегодня может быть дело: в окне предупреждения или прошедшие ≤ GRACE_DAYS назад.
    days_left < 0 — ДР уже был (догоняем поздравление)."""
    out = []
    for r in await db.fetchall("SELECT * FROM birthdays"):
        try:
            bd = current_birthday(int(r["month"]), int(r["day"]), today)
        except (TypeError, ValueError):
            log.warning("кривая дата у дня рождения #%s: %s.%s", r.get("id"), r.get("day"), r.get("month"))
            continue
        left = (bd - today).days
        if left > min(max(int(r.get("remind_days_before") or 0), 0), MAX_REMIND_DAYS):
            continue
        out.append({**r, "next_date": bd.isoformat(), "days_left": left, "turns": _turns(r["year"], bd)})
    out.sort(key=lambda x: (x["days_left"], normalize_text(x["name"])))
    return out


async def _process_one(ctx: ToolContext, b: dict, today: date) -> int:
    db, notifier = ctx.db, ctx.services.notifier
    left = int(b["days_left"])
    rdb = int(b.get("remind_days_before") or 0)
    bd = date.fromisoformat(b["next_date"])
    # догоняем: если бот лежал в сам день предупреждения — предупредим позже, но один раз за год
    if 0 < left <= rdb and b.get("last_prenotice_year") != bd.year:
        await notifier.send(_prenotice_text(b))
        await db.execute("UPDATE birthdays SET last_prenotice_year=? WHERE id=?", (bd.year, b["id"]))
        return 1
    if left <= 0 and b.get("last_greeted_year") != bd.year:
        greeting, fallback, n = await _greeting_for(ctx, b, bd, belated=left < 0)
        await notifier.send(_today_text(b, greeting, fallback, left),
                            buttons=greeting_buttons(ctx, {**b, "greeting_variant": n}))
        await db.execute("UPDATE birthdays SET last_greeted_year=? WHERE id=?", (bd.year, b["id"]))
        await start_nag(ctx, b)
        what = "Сегодня ДР" if left == 0 else "Вчера был ДР"
        await db.add_message("event", f"{what} у {b['name']}; поздравление-черновик отправлено владельцу",
                             "system")
        return 1
    return 0


async def _greeting_for(ctx: ToolContext, b: dict, bd: date, *, belated: bool) -> tuple[str, bool, int | None]:
    """Текст поздравления к ДР bd → (текст, заготовка ли, номер варианта). Уже написанный к этому ДР
    (прошлая попытка отправки, или владелец сам просил накануне) — берём его, а не пишем новый."""
    db, bid = ctx.db, int(b["id"])
    prev = await db.kv_get(greeting_key(bid))
    if isinstance(prev, str) and prev.strip() and await db.kv_get(greeting_for_key(bid)) == bd.isoformat():
        return prev, False, await latest_variant(db, bid, prev)
    try:
        greeting = await generate_greeting(ctx, b, BELATED_STYLE if belated else "")
        fallback = False
    except Exception as e:
        log.warning("поздравление для #%s не сгенерировано: %s", bid, e)
        greeting, fallback = fallback_greeting(b, female=owner_female(ctx)), True
    n = await store_greeting(db, bid, greeting)
    if not fallback:
        await db.kv_set(greeting_for_key(bid), bd.isoformat())
    return greeting, fallback, n


async def start_nag(ctx: ToolContext, b: dict) -> int | None:
    """«Поздравить с днём рождения: …» — напоминание с долбёжкой раз в час, с первым срабатыванием через
    час (но не раньше NAG_FROM), пока не отметит «Готово» (или не отправит поздравление кнопкой); всё,
    включая «сдаюсь» после последнего нажима, — не позже NAG_UNTIL. Уже стоит — не дублируем.
    Не вышло — не беда (поздравление уже ушло): пишем в лог. → id напоминания или None."""
    try:
        if await ctx.db.scalar("SELECT 1 FROM reminders WHERE ref_type='birthday' AND ref_id=? "
                               "AND status='active'", (int(b["id"]),)):
            return None
        from . import reminders as rem
        now = ctx.now_local().replace(second=0, microsecond=0)
        step = timedelta(minutes=NAG_EVERY_MIN)
        until = datetime.combine(now.date(), NAG_UNTIL, tzinfo=ctx.tz)
        first = max(now + step, datetime.combine(now.date(), NAG_FROM, tzinfo=ctx.tz))
        # срабатывание в first, нажимы — first + k·step (k = 1…n), «сдаюсь» — first + (n+1)·step ≤ until
        n = min(NAG_MAX, int((until - first) / step) - 1)
        late = n < 1
        if late:                    # поздно вечером — один раз, скоро, и без долбёжки на ночь глядя
            first, n = now + timedelta(minutes=15), 1
        row = await rem.create_reminder(ctx, text=f"Поздравить с днём рождения: {_who(b)}", when=first,
                                        kind="reminder", nag=not late, nag_interval_min=NAG_EVERY_MIN, nag_max=n,
                                        ref_type="birthday", ref_id=int(b["id"]))
        return int(row["id"])
    except Exception:
        log.exception("день рождения #%s: не поставил «поздравить»", b.get("id"))
        return None


async def _cancel_nag(db, bid: int) -> None:
    from . import reminders as rem
    await rem.cancel_by_ref(db, "birthday", int(bid))


# ── инструменты ──────────────────────────────────────────────────────────────
def _clean_username(u: Any) -> str:
    s = str(u or "").strip()
    s = re.sub(r"^(?:https?://)?(?:t\.me|telegram\.me)/", "", s, flags=re.I).lstrip("@").strip()
    if s and not re.fullmatch(r"[A-Za-z0-9_]{4,32}", s):
        raise ValueError(f"«{u}» не похоже на username в Telegram — нужен вида @name (латиница, цифры, _)")
    return s


def _remind_days(v: Any, default: int = 1) -> int:
    if v is None or v == "":
        return default
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        raise ValueError("remind_days_before — число дней (0 — только в сам день)") from None
    return max(0, min(n, MAX_REMIND_DAYS))


def _clean_name(name: Any) -> str:
    s = re.sub(r"\s+", " ", str(name or "")).strip().strip("«»\"'")
    if not s:
        raise ValueError("нужно имя человека")
    return s[:100]


def _public(b: dict, today: date) -> dict:
    x = _with_next(b, today)
    nd = date.fromisoformat(x["next_date"])
    out = {"id": x["id"], "name": x["name"], "date": human_date(x["month"], x["day"], x.get("year")),
           "days_left": x["days_left"], "turns": x["turns"], "weekday": timeutil.weekday_name(nd),
           "relation": x.get("relation") or ""}
    if (x.get("notes") or "").strip():
        out["notes"] = x["notes"]
    if (x.get("tg_username") or "").strip():
        out["tg_username"] = "@" + x["tg_username"]
    return out


async def _mark_known(ctx: ToolContext, bid: int, today: date, *, date_told: bool = True) -> None:
    """Владелец только что сам говорил об этом ДР — предупреждать «завтра ДР» уже незачем. А если
    сказал дату, и ДР сегодня или только что прошёл, — отдельного сообщения с поздравлением не будет
    (текст модель даст сразу через birthday_greeting), но в сам день «Поздравить…» всё равно долбит."""
    db = ctx.db
    b = await get_birthday(db, bid)
    if b is None:
        return
    bd = current_birthday(int(b["month"]), int(b["day"]), today)
    left = (bd - today).days
    if 0 < left <= int(b["remind_days_before"] or 0):
        await db.execute("UPDATE birthdays SET last_prenotice_year=? WHERE id=?", (bd.year, bid))
    elif left <= 0 and date_told and b.get("last_greeted_year") != bd.year:
        await db.execute("UPDATE birthdays SET last_greeted_year=? WHERE id=?", (bd.year, bid))
        if left == 0:
            await start_nag(ctx, b)


def _add_note(bid: int, left: int) -> str:
    """Что модели сделать после сохранения: поздравление — только если ДР сегодня/завтра или был вчера
    (left — дни до ДР, о котором сейчас речь: current_birthday, как и в _mark_known)."""
    if left < 0:        # отдельного «вчера был ДР» не будет (_mark_known) — поздравление нужно сейчас
        return (f"ДР был вчера — вызови birthday_greeting(id={bid}, style=\"запоздалое: ДР был вчера\") "
                "и отдай текст поздравления как есть: лучше поздно.")
    if left <= 1:
        tail = " До вечера буду долбить его «поздравить», пока не отметит." if left == 0 else ""
        return (f"ДР {'сегодня' if left == 0 else 'завтра'} — вызови birthday_greeting(id={bid}) "
                f"и отдай текст поздравления как есть.{tail}")
    return ("Сохранено. Коротко подтверди дату и когда напомню; само поздравление пришлю в день ДР — "
            "сейчас его не пиши, если он не просил.")


@tool("add_birthday",
      "Сохранить день рождения. Дата: «DD.MM», «DD.MM.YYYY», «YYYY-MM-DD» или «14 марта [1990]»; "
      "год — если известен (тогда посчитаю, сколько исполнится). Тот же человек с той же датой — запись "
      "обновится, а не задвоится. Накануне (remind_days_before) предупрежу, в сам день пришлю поздравление "
      "с кнопками «Другой вариант»/«Отправить» и буду долбить «поздравить», пока не отметит. Сейчас "
      "поздравление не пиши, если он не просит и ДР не сегодня/завтра/вчера — тогда вызови birthday_greeting(id).",
      {"name": {"type": "string", "description": "как владелец зовёт человека: «Маша», «мама», «Иван Петров»"},
       "date": {"type": "string", "description": "дата рождения: «14.03», «14.03.1990», «1990-03-14», «14 марта»"},
       "relation": {"type": "string", "description": "кем приходится: друг, мама, коллега, сестра…"},
       "notes": {"type": "string", "description": "что о нём известно — пригодится для поздравления: "
                                                   "увлечения, общие истории, характер"},
       "tg_username": {"type": "string", "description": "username в Telegram (@name), если известен"},
       "remind_days_before": {"type": "integer", "description": "за сколько дней предупредить "
                                                                "(по умолчанию 1; 0 — только в сам день)"}},
      required=["name", "date"])
async def t_add_birthday(ctx: ToolContext, *, name: str, date: str, relation: str | None = None,
                         notes: str | None = None, tg_username: str | None = None,
                         remind_days_before: int | None = None) -> dict:
    today = ctx.now_local().date()
    nm = _clean_name(name)
    month, day, year = parse_birth_date(date, today)
    rel = (relation or "").strip()
    nts = (notes or "").strip()
    tg = _clean_username(tg_username)
    db = ctx.db
    same = [r for r in await db.fetchall("SELECT * FROM birthdays WHERE month=? AND day=?", (month, day))
            if normalize_text(r["name"]) == normalize_text(nm)]
    if same:
        old = same[0]
        bid = int(old["id"])
        await db.execute(
            "UPDATE birthdays SET name=?, year=?, relation=?, notes=?, tg_username=?, remind_days_before=? "
            "WHERE id=?",
            (nm, year or old["year"], rel or old["relation"], nts or old["notes"], tg or old["tg_username"],
             _remind_days(remind_days_before, int(old["remind_days_before"])), bid))
        updated = True
    else:
        bid = await db.execute(
            "INSERT INTO birthdays(name, month, day, year, relation, notes, tg_username, remind_days_before, "
            "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (nm, month, day, year, rel, nts, tg, _remind_days(remind_days_before),
             timeutil.iso(timeutil.now_utc())))
        updated = False
    await _mark_known(ctx, bid, today)
    b = await get_birthday(db, bid)
    pub = _public(b, today)
    left = (current_birthday(month, day, today) - today).days      # вчерашний ДР — ещё «текущий»
    out = {"ok": True, **pub, "updated": updated, "note": _add_note(bid, left)}
    others = [r for r in await db.fetchall("SELECT id, name, month, day FROM birthdays WHERE id!=?", (bid,))
              if normalize_text(r["name"]) == normalize_text(nm)]
    if others:
        o = others[0]
        out["warning"] = (f"есть ещё «{o['name']}» (#{o['id']}, {human_date(o['month'], o['day'])}) — "
                          "если это тот же человек с ошибкой в дате, удали лишнюю запись")
    return out


@tool("list_birthdays",
      "Дни рождения, ближайшие первыми: id, имя, дата, days_left (0 — сегодня), turns (сколько исполнится), "
      "кем приходится. days_ahead — горизонт в днях (по умолчанию весь год).",
      {"days_ahead": {"type": "integer", "description": "сколько дней вперёд смотреть (по умолчанию 365)"}})
async def t_list_birthdays(ctx: ToolContext, *, days_ahead: int | None = None) -> dict:
    try:
        days = 365 if days_ahead in (None, "") else int(float(days_ahead))
    except (TypeError, ValueError):
        raise ValueError("days_ahead — число дней") from None
    days = 366 if days >= 365 else max(0, days)   # 366 — чтобы через 29 февраля никто не выпал
    today = ctx.now_local().date()
    items = [_public(b, today) for b in await upcoming(ctx.db, ctx.tz, days)]
    out: dict = {"ok": True, "count": len(items), "items": items}
    if not items:
        total = await ctx.db.scalar("SELECT COUNT(*) FROM birthdays")
        out["note"] = ("дней рождения пока не записано" if not total
                       else f"в ближайшие {days} дн. дней рождения нет (всего записано: {total})")
    return out


@tool("update_birthday",
      "Изменить запись о дне рождения по id (id — из list_birthdays). Передавай только то, что меняется; "
      "пустая строка в relation / notes / tg_username очищает поле. Дата — в тех же форматах, что в add_birthday.",
      {"id": {"type": "integer"},
       "name": {"type": "string"},
       "date": {"type": "string", "description": "«DD.MM», «DD.MM.YYYY», «YYYY-MM-DD» или «14 марта [1990]»"},
       "relation": {"type": "string"},
       "notes": {"type": "string"},
       "tg_username": {"type": "string"},
       "remind_days_before": {"type": "integer"}},
      required=["id"])
async def t_update_birthday(ctx: ToolContext, *, id: Any, name: str | None = None, date: str | None = None,
                            relation: str | None = None, notes: str | None = None,
                            tg_username: str | None = None, remind_days_before: int | None = None) -> dict:
    b = await get_birthday(ctx.db, id)
    if b is None:
        raise ValueError(f"нет дня рождения #{id} — посмотри list_birthdays")
    today = ctx.now_local().date()
    sets: dict[str, Any] = {}
    if name is not None and str(name).strip():
        sets["name"] = _clean_name(name)
    if date is not None and str(date).strip():
        month, day, year = parse_birth_date(date, today)
        sets.update(month=month, day=day, year=year or b["year"])
        if (month, day) != (b["month"], b["day"]):
            sets.update(last_greeted_year=None, last_prenotice_year=None)
    if relation is not None:
        sets["relation"] = str(relation).strip()
    if notes is not None:
        sets["notes"] = str(notes).strip()
    if tg_username is not None:
        sets["tg_username"] = _clean_username(tg_username)
    if remind_days_before is not None and remind_days_before != "":
        sets["remind_days_before"] = _remind_days(remind_days_before)
    if not sets:
        raise ValueError("нечего менять — передай хотя бы одно поле")
    cols = ", ".join(f"{k}=?" for k in sets)
    await ctx.db.execute(f"UPDATE birthdays SET {cols} WHERE id=?", (*sets.values(), b["id"]))
    if "last_greeted_year" in sets:        # дата сменилась — «поздравить» про старую дату не нужно
        await _cancel_nag(ctx.db, b["id"])
    if "month" in sets or "remind_days_before" in sets:
        await _mark_known(ctx, b["id"], today, date_told="last_greeted_year" in sets)
    return {"ok": True, **_public(await get_birthday(ctx.db, b["id"]), today)}


@tool("delete_birthday", "Удалить день рождения по id (id — из list_birthdays).",
      {"id": {"type": "integer"}}, required=["id"])
async def t_delete_birthday(ctx: ToolContext, *, id: Any) -> dict:
    b = await get_birthday(ctx.db, id)
    if b is None:
        raise ValueError(f"нет дня рождения #{id} — посмотри list_birthdays")
    await ctx.db.execute("DELETE FROM birthdays WHERE id=?", (b["id"],))
    await _cancel_nag(ctx.db, b["id"])
    bid = int(b["id"])          # тексты поздравлений и отметки «отправлено» — больше ни к чему
    await ctx.db.execute("DELETE FROM kv WHERE key IN (?, ?, ?) OR key LIKE ? OR key LIKE ?",
                         (greeting_key(bid), greeting_for_key(bid), gen_key(bid),
                          greeting_key(bid) + ":%", f"bday_sent:{bid}:%"))
    return {"ok": True, "deleted": b["id"], "name": b["name"]}


@tool("birthday_greeting",
      "Новый вариант поздравления для человека по id (не похожий на прошлый). style — пожелание к тону: "
      "«короче», «смешнее», «официальнее», «в стихах», «упомяни поездку в Питер»… "
      "Верни владельцу текст поздравления как есть — кроме случая, когда в ответе note: тогда текст уже показан "
      "ему отдельным сообщением с кнопкой «Отправить».",
      {"id": {"type": "integer"},
       "style": {"type": "string", "description": "каким сделать поздравление (необязательно)"}},
      required=["id"])
async def t_birthday_greeting(ctx: ToolContext, *, id: Any, style: str | None = None) -> dict:
    from ..llm import LLMError
    try:
        b, text = await regenerate_greeting(ctx, id, style or "")
    except LLMError as e:
        raise ValueError(f"модель поздравлений не ответила ({e}) — напиши поздравление сам") from None
    tg = (b.get("tg_username") or "").strip()
    out = {"ok": True, "id": b["id"], "name": b["name"], "greeting": text}
    if tg and userbot_ready(ctx):   # сам текст — в том же сообщении, что и кнопка: видно, что уйдёт
        ctx.outbox.append(OutItem(kind="text", text=f"{text}\n\n— отправить это поздравление @{tg}?",
                                  buttons=[[("📨 Отправить", send_data(b))]]))
        out["note"] = ("текст уже показан ему отдельным сообщением с кнопкой «Отправить» — "
                       "не повторяй его целиком, скажи коротко")
    return out
