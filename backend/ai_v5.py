# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — тонкие помощники над шлюзом DeepSeek (backend/ai.py).

Две модели: FLASH (быстро/дёшево, механика) и PRO (думает, совет).
Каждый вызов — НОВЫЙ чат: system + user, без истории. Ключ — из пула
аккаунтов DeepSeek (панель), иначе любой ключ инструмента.
"""
from __future__ import annotations

import asyncio
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from . import ai, config

MSK = timezone(timedelta(hours=3))
_WD = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _flash() -> str:
    return config.DEEPSEEK_MODEL_FAST


def _pro() -> str:
    return config.DEEPSEEK_MODEL


FLASH = _flash()
PRO = _pro()


def key() -> str:
    """Ключ DeepSeek: пул аккаунтов → любой ключ инструмента → ошибка."""
    k = config.pick_pool_key()
    if k:
        return k
    for v in (config.INSTRUMENT_KEYS or {}).values():
        if v:
            return v
    raise RuntimeError("нет ключа DeepSeek — введи его в панели (🔑 Ключи)")


def has_key() -> bool:
    try:
        key()
        return True
    except RuntimeError:
        return False


def _skew_s() -> float:
    """Сдвиг локальных часов к серверу биржи (market_clock.sync); без модуля/синка — 0.
    Единая база времени: «Сейчас» в промптах и метки fmt_ts живут в одном времени."""
    try:
        from . import market_clock as _mc
        return float((_mc.skew() or {}).get("skew_s") or 0.0)
    except Exception:            # noqa: BLE001
        return 0.0


def now_msk() -> datetime:
    return datetime.fromtimestamp(time.time() + _skew_s(), MSK)


def now_msk_str(dt: datetime | None = None) -> str:
    d = (dt or now_msk()).astimezone(MSK)
    return f"{d:%d.%m.%Y %H:%M} МСК, {_WD[d.weekday()]}"


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(float(ts) + _skew_s(), MSK).strftime("%d.%m %H:%M")


SEM_FLASH = asyncio.Semaphore(max(1, int(getattr(config, "PYTHIA_FLASH_PAR", 6))))
# 24.09.2026: узлы у денег не стоят в общей очереди с разметкой новостей — иначе таймер wait_for тикал
# в очереди и «решал таймер, а не FLASH» (воспроизведено проверяющим). Своя очередь на 3.
SEM_MONEY = asyncio.Semaphore(3)
MONEY_ROUTES = frozenset({"mission_guard", "mission_take", "event_triage", "mission_entry", "mission_profit",
                          "mission_exec", "mission_review", "summary"})


def _sem_for(route: str) -> asyncio.Semaphore:
    return SEM_MONEY if route in MONEY_ROUTES else SEM_FLASH


_REPAIR_SYS = ("Тебе дан текст, который должен быть одним JSON-объектом, но он сломан "
               "(обрезан, лишние кавычки, текст вокруг). Верни ТОЛЬКО исправленный JSON-объект: "
               "синтаксис починить, содержимое не менять, обрезанные элементы закрыть.")


async def _json_with_repair(system: str, user: str, *, model: str, think: bool,
                            route: str, max_tokens: int | None, attempt_timeout: float | None = None) -> Any:
    """JSON-стадия (24.09.2026).
    • деньги (MONEY_ROUTES): на любую беду — пустой ответ (модель ушла в размышления) или кривой JSON — ровно ОДИН
      повтор той же моделью с тем же размышлением; снова беда → исключение, для вызывающего это «молчание» → его
      безопасный ответ по правилу. Без размышления не переспрашиваем и чужим FLASH не «чиним»: решение о позиции
      принимает думающая модель или никто (воля владельца). Ответ повтора тоже проверяется на маркер —
      черновик из размышления решением не становится (находка проверяющего);
    • механика: повтор без размышления, затем дешёвая починка FLASH (весь текст, без среза) с записью в панель проблем."""
    lg = logging.getLogger("pythia.ai_v5")

    async def _ask(rt: str) -> str:
        # v5.4.2: у каждой попытки свой срок (attempt_timeout) — повтор после пустого/кривого ответа не умирает внутри
        # чужого таймера вызывающего; таймаут попытки — исключение сразу, без повтора (ещё один такой же долгий
        # вызов вызывающий всё равно не дождётся).
        coro = ai.ask(system, user, model=model, json_mode=True, thinking=think,
                      max_tokens=max_tokens, route=rt, api_key=key())
        if attempt_timeout:
            try:
                return await asyncio.wait_for(coro, float(attempt_timeout))
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError(f"[{rt}] модель не ответила за {int(attempt_timeout)} с") from None
        return await coro

    raw = await _ask(route)
    if route in MONEY_ROUTES:
        for attempt in (1, 2):
            if raw.startswith(ai.THINK_MARK):
                why = "модель ушла в размышления без ответа"
            else:
                try:
                    return ai._extract_json(raw)
                except Exception as e:                   # noqa: BLE001
                    why = f"JSON не разобрался ({str(e)[:60]})"
            if attempt == 2:
                ai.note_error(f"{why} и после повтора — решение по правилу", route)
                raise RuntimeError(f"[{route}] {why} и после повтора — решение по правилу")
            lg.warning("[%s] %s — один повтор с размышлением", route, why)
            raw = await _ask(route + "_retry")
    if think and raw.startswith(ai.THINK_MARK):
        lg.warning("[%s] модель ушла в размышления без ответа — повтор без thinking", route)
        raw = await ai.ask(system, user, model=model, json_mode=True, thinking=False,
                           max_tokens=max_tokens, route=route + "_nothink", api_key=key())
    try:
        return ai._extract_json(raw)
    except Exception as e:       # noqa: BLE001
        lg.warning("[%s] JSON не разобрался (%s): %r…", route, str(e)[:60], raw[:160])
    fixed = await ai.ask(_REPAIR_SYS, raw[:400_000], model=_flash(), json_mode=True,
                         thinking=False, max_tokens=max_tokens, route=route + "_repair",
                         api_key=key())
    ai.note_error("JSON чинился вторым вызовом FLASH — содержимое могло измениться", route + "_repair")
    return ai._extract_json(fixed)


# think=True — размышление НЕ глушим; reasoning_effort не шлём (режима «auto» у DeepSeek нет: пусто = умолчание API
# = high, как в первой ПИФИИ 4.5.4); max_tokens шлём явно = максимум модели 393 216 (без него сервер режет 64K
# вместе с размышлением). think=False только там, где ИИ не судит о рынке: починка JSON и health. Воля владельца 24.09.2026.
async def flash_json(system: str, user: str, *, think: bool = True,
                     route: str = "flash", max_tokens: int | None = None,
                     attempt_timeout: float | None = None) -> Any:
    async with _sem_for(route):
        return await _json_with_repair(system, user, model=_flash(), think=think,
                                       route=route, max_tokens=max_tokens, attempt_timeout=attempt_timeout)


async def flash_text(system: str, user: str, *, think: bool = True,
                     route: str = "flash", max_tokens: int | None = None) -> str:
    async with _sem_for(route):
        return await ai.ask(system, user, model=_flash(), thinking=think,
                            max_tokens=max_tokens, route=route, api_key=key())


async def pro_json(system: str, user: str, *, route: str = "pro",
                   max_tokens: int | None = None, attempt_timeout: float | None = None) -> Any:
    return await _json_with_repair(system, user, model=_pro(), think=True,
                                   route=route, max_tokens=max_tokens, attempt_timeout=attempt_timeout)


def money_model() -> str:
    """v5.4.1: модель узлов у денег (трос, тейк, триаж, проверка входа, мысль о прибыли) — PYTHIA_MONEY_MODEL:
    pro (умолчание, воля владельца: «пусть думает долго, но решает тот, кто должен») или flash (как в 5.3)."""
    return "flash" if str(getattr(config, "PYTHIA_MONEY_MODEL", "pro") or "pro").strip().lower() == "flash" else "pro"


async def money_json(system: str, user: str, *, route: str, max_tokens: int | None = None,
                     attempt_timeout: float | None = None) -> Any:
    """Узел у денег (route ∈ MONEY_ROUTES): PRO или FLASH по money_model(), всегда с размышлением, ровно один
    повтор той же моделью, без починки чужой моделью и без повтора без размышления (см. _json_with_repair).
    v5.4.2: attempt_timeout — срок каждой попытки (таймаут → исключение без повтора; повтор после пустого или
    кривого ответа получает свой срок, а не остаток чужого)."""
    extra = {"attempt_timeout": attempt_timeout} if attempt_timeout else {}   # подменам в тестах ключ не навязываем
    if money_model() == "pro":
        return await pro_json(system, user, route=route, max_tokens=max_tokens, **extra)
    return await flash_json(system, user, think=True, route=route, max_tokens=max_tokens, **extra)


# ── v5.4.2: разбор слова решения из JSON-ответа ИИ — один на все узлы у денег ──────────────────────────────
# До 5.4.2 каждый узел искал подстроки («КУП», «ВОЙ», «ЖД»…) и всё непонятое молча превращал в ЖДЁМ / ЖДАТЬ /
# ДЕРЖАТЬ: «BUY», «ВОЙТИ», «CLOSE», «ЗАФИКСИРОВАТЬ» у перепроверки и мысли о прибыли становились ожиданием, а
# «НЕ ВХОДИТЬ» у двери — входом. Здесь: целые слова (Ё=Е, регистр, латиница, знаки и markdown не мешают), ключ
# decision|choice|do|action, отрицание в начале и два разных решения → None. None — «решения нет»: вызывающий
# пишет «ответ не разобран» и переспрашивает, а не выбирает за ИИ.
# v5.4.4 (отчёт проверяющего 30.09, раздел D: живые слова DeepSeek, которые давали None или не тот токен): эхо схемы
# («КУПИТЬ | ПРОДАТЬ | …», «КУПИТЬ/ПРОДАТЬ») → None; семейство «засада / войти / вход взведён» у перепроверки вне
# рынка — сторона по словам ответа («на покупку», «в лонг», LIMIT_BUY), без стороны — по уровням ответа
# (side_by_levels, решает MissionPilot._parse_choice); ОТМЕНИТЬ у перепроверки при взведённом входе (снять вход);
# «подтянуть трос», «стоп в безубыток», «не закрывать» — держать; частичное закрытие («частично», «половину»,
# «сократить») → None: его нет, переспрос с пометкой; условный вход у двери («ВОЙТИ на откате», BUY_LIMIT, «позже»)
# → ЖДАТЬ с уровнем, а не вход по текущей цене; «ВЫЙТИ, ПЕРЕЗАЙТИ на …» → ВЫЙТИ_И_ПЕРЕЗАЙТИ; «звать совет»,
# «не сливать» у троса → ЖДАТЬ, CUT → СЛИТЬ.
DECISION_KEYS = ("decision", "choice", "do", "action", "решение", "выбор")
_NEGATIONS = frozenset({"НЕ", "НЕТ", "NOT", "NO", "DONT", "DO_NOT", "НЕЛЬЗЯ"})
# отрицание сразу ПОСЛЕ слова решения: «ВОЙТИ нельзя», «КУПИТЬ сейчас не стоит»
_POST_NEG = ("НЕЛЬЗЯ", "НЕ_НАДО", "НЕ_СТОИТ", "НЕ_НУЖНО", "НЕ_БУДЕМ", "NOT")

SYN_BUY = ("КУПИТЬ", "КУПИТЬ_СЕЙЧАС", "КУПИТЬ_НА_ОТКАТЕ", "КУПИТЬ_НА_ПРОБОЕ", "КУПИТЬ_НА_ПРОРЫВЕ", "ПОКУПКА", "ПОКУПАТЬ",
           "ПОКУПАЕМ", "ПОКУПАЮ", "ЛОНГ", "В_ЛОНГ", "BUY", "BUY_NOW", "LONG", "GO_LONG")
SYN_SELL = ("ПРОДАТЬ", "ПРОДАТЬ_СЕЙЧАС", "ПРОДАТЬ_НА_ОТКАТЕ", "ПРОДАТЬ_НА_ПРОБОЕ", "ПРОДАТЬ_НА_ПРОРЫВЕ", "ПРОДАЖА",
            "ПРОДАВАТЬ", "ПРОДАЕМ", "ПРОДАЮ", "ШОРТ", "В_ШОРТ", "SELL", "SELL_NOW", "SHORT", "GO_SHORT")
# «ДА/YES/GO» — не ответ на вопрос из трёх исходов (ВОЙТИ / ЖДАТЬ / ОТМЕНИТЬ): их здесь нет (находка ревью 5.4.2)
SYN_ENTER = ("ВОЙТИ", "ВОЙТИ_СЕЙЧАС", "ВХОД", "ВХОДИМ", "ВХОДИТЬ", "ВХОЖУ", "ЗАЙТИ", "ENTER", "ENTRY")
SYN_WAIT = ("ЖДАТЬ", "ЖДЕМ", "ЖДУ", "ПОДОЖДАТЬ", "ОЖИДАТЬ", "ОЖИДАНИЕ", "ОЖИДАЕМ", "ПОДОЖДЕМ", "WAIT")
# «вне рынка» зависит от позиции: без позиции — не входить (ожидание), в позиции — выйти; «NONE/PASS» — не ответ
SYN_OUT = ("ВНЕ_РЫНКА", "STAY_OUT", "NO_TRADE", "OUT_OF_MARKET")
# v5.4.4: «входа нет» у перепроверки вне рынка — ожидание (при взведённом входе — снять его); PASS — только здесь, в
# общий словарь ожидания не входит (у двери и троса «pass» — не ответ)
SYN_NO_ENTRY = ("ПАС", "ПАСУЕМ", "ПРОПУСТИТЬ", "ПРОПУСКАЕМ", "НЕТ_ВХОДА", "БЕЗ_ВХОДА", "НЕ_ВХОДИТЬ", "НЕ_ВХОДИМ",
                "NO_ENTRY", "PASS")
SYN_HOLD = ("ДЕРЖАТЬ", "ДЕРЖИМ", "ДЕРЖУ", "ПОДЕРЖАТЬ", "ОСТАВИТЬ", "HOLD", "KEEP")
# v5.4.4: «подтянуть трос / стоп в безубыток / запереть прибыль» — держать с новыми уровнями (числа — в invalidation)
SYN_TRAIL = ("ПОДТЯНУТЬ", "ПОДТЯГИВАЕМ", "ПОДТЯНУТЬ_ТРОС", "ПОДТЯНУТЬ_СТОП", "ПЕРЕСТАВИТЬ_СТОП", "ПЕРЕСТАВИТЬ_ТРОС",
             "ПЕРЕНЕСТИ_СТОП", "СДВИНУТЬ_СТОП", "СТОП_В_БЕЗУБЫТОК", "СТОП_В_БУ", "ТРОС_В_БЕЗУБЫТОК", "В_БЕЗУБЫТОК",
             "БЕЗУБЫТОК", "ЗАПЕРЕТЬ_ПРИБЫЛЬ", "TRAIL", "TRAIL_STOP", "MOVE_STOP", "BREAKEVEN")
SYN_CLOSE = ("ЗАКРЫТЬ", "ЗАКРЫВАЕМ", "ЗАКРЫВАТЬ", "ВЫЙТИ", "ВЫХОД", "ВЫХОДИМ", "ВЫХОДИТЬ", "СЛИТЬ", "СЛИВАЕМ", "СЛИВАТЬ",
             "ЗАФИКСИРОВАТЬ",
             "ФИКСИРОВАТЬ", "ФИКСИРУЕМ", "ЗАБРАТЬ", "ЗАБРАТЬ_ПРИБЫЛЬ", "ФИКСИРОВАТЬ_ПРИБЫЛЬ", "ЗАФИКСИРОВАТЬ_ПРИБЫЛЬ",
             "CLOSE", "EXIT", "FLAT", "TAKE_PROFIT", "CLOSE_ALL")
SYN_CANCEL = ("ОТМЕНИТЬ", "ОТМЕНА", "ОТМЕНЯЕМ", "ОТКАЗ", "ОТКАЗАТЬСЯ", "CANCEL", "SKIP", "ABORT")
# v5.4.4: снять взведённый вход (перепроверка при засаде, дверь)
SYN_UNARM = ("СНЯТЬ", "СНИМАЕМ", "СНЯТЬ_ЗАСАДУ", "ОТМЕНИТЬ_ЗАСАДУ", "СНЯТЬ_ВХОД", "ОТМЕНИТЬ_ВХОД", "СНЯТЬ_ПЛАН",
             "ОТМЕНИТЬ_ПЛАН", "СНЯТЬ_ЗАЯВКУ", "ОТМЕНИТЬ_ЗАЯВКУ")
SYN_ADD = ("ДОБРАТЬ", "ДОБОР", "ДОКУПИТЬ", "УСИЛИТЬ", "НАРАСТИТЬ", "ADD", "TOPUP", "TOP_UP", "SCALE_IN")
SYN_FLIP = ("ПЕРЕВЕРНУТЬ", "ПЕРЕВОРОТ", "РАЗВЕРНУТЬ", "РАЗВОРОТ_ПОЗИЦИИ", "FLIP", "REVERSE")
SYN_COUNCIL = ("НОВЫЙ_АНАЛИЗ", "СОВЕТ", "НОВЫЙ_СОВЕТ", "ПЕРЕАНАЛИЗ", "ЗВАТЬ_СОВЕТ", "ПОЗВАТЬ_СОВЕТ", "СОЗВАТЬ_СОВЕТ",
               "НУЖЕН_СОВЕТ", "ПЕРЕДАТЬ_СОВЕТУ", "COUNCIL", "REANALYZE", "NEW_ANALYSIS")
SYN_REENTER = ("ВЫЙТИ_И_ПЕРЕЗАЙТИ", "ВЫЙТИ_ПЕРЕЗАЙТИ", "ПЕРЕЗАЙТИ", "ПЕРЕЗАХОД", "ВЫЙТИ_И_ВОЙТИ_СНОВА",
               "ВЫЙТИ_И_ЗАЙТИ_СНОВА", "REENTER", "REENTRY", "RE_ENTER", "RE_ENTRY", "EXIT_AND_REENTER")
# v5.4.4: вход без стороны у перепроверки вне рынка («засада», «войти», «вход взведён», LIMIT, AMBUSH) — сторона по
# словам ответа, иначе по его уровням (side_by_levels у вызывающего); «ДЕРЖАТЬ ЗАСАДУ» — не вход, а ожидание (ДЕРЖАТЬ)
SYN_AMBUSH = ("ЗАСАДА", "ЗАСАДУ", "ЗАСАДЫ", "ВЗВЕСТИ", "ВЗВЕСТИ_ЗАСАДУ", "ПОСТАВИТЬ_ЗАСАДУ", "ПЕРЕСТАВИТЬ_ЗАСАДУ",
              "ВЫСТАВИТЬ_ЗАСАДУ", "ВХОД_ВЗВЕДЕН", "ВЗВЕСТИ_ВХОД", "ЛИМИТ", "ЛИМИТКА", "ЛИМИТКУ", "LIMIT", "AMBUSH",
              "SET_AMBUSH", "ОТКРЫТЬ", "ОТКРЫТЬ_ПОЗИЦИЮ", "ОТКРЫВАЕМ")
ENTRY_ANY = "ВХОД_БЕЗ_СТОРОНЫ"      # токен «вход, сторона не названа» — только у review_table(..., sideless=True)
REVIEW_CANCEL = "ОТМЕНИТЬ_ВХОД"     # v5.4.4: канон кода «снять взведённый вход» (модель видит «ОТМЕНИТЬ»)
# стороны в словах ответа («засада на покупку», «вход в лонг», LIMIT_BUY) — целые слова, не основы («продавцы» — не шорт)
_LONG_WORDS = frozenset({"КУПИТЬ", "КУПИМ", "КУПЛЮ", "ДОКУПИТЬ", "ПОКУПКА", "ПОКУПКУ", "ПОКУПКИ", "ПОКУПКЕ", "ПОКУПАТЬ",
                         "ПОКУПАЕМ", "ПОКУПАЮ", "ЛОНГ", "ЛОНГА", "ЛОНГЕ", "ЛОНГУ", "BUY", "LONG"})
_SHORT_WORDS = frozenset({"ПРОДАТЬ", "ПРОДАМ", "ПРОДАДИМ", "ПРОДАЖА", "ПРОДАЖУ", "ПРОДАЖИ", "ПРОДАЖЕ", "ПРОДАВАТЬ",
                          "ПРОДАЕМ", "ПРОДАЮ", "ШОРТ", "ШОРТА", "ШОРТЕ", "ШОРТУ", "SELL", "SHORT"})
# условный вход у двери — ожидание уровня, а не вход по текущей цене (основы слов и целые слова)
_COND_STEMS = ("ОТКАТ", "ПРОБО", "ПРОРЫВ", "ЛИМИТ", "LIMIT", "ПОЗЖЕ", "ПОТОМ", "ЗАСАД", "ВЗВЕД", "ОТЛОЖ", "РЕТЕСТ",
               "PULLBACK", "BREAKOUT", "RETEST", "LATER")
_COND_WORDS = frozenset({"ЕСЛИ", "КОГДА", "ПОСЛЕ", "ПРИ", "IF", "WHEN", "AFTER"})
# частичного закрытия / выхода у пилота нет: «ЗАКРЫТЬ ПОЛОВИНУ», «ЧАСТИЧНО ВЫЙТИ», «СОКРАТИТЬ» — не решение, переспрос
_PARTIAL = frozenset({"ЧАСТИЧНО", "ЧАСТЬ", "ЧАСТЬЮ", "ЧАСТИ", "ПОЛОВИНУ", "ПОЛОВИНА", "ПОЛОВИНОЙ", "ТРЕТЬ", "ЧЕТВЕРТЬ",
                      "СОКРАТИТЬ", "СОКРАЩАЕМ", "СОКРАТИМ", "УРЕЗАТЬ", "УМЕНЬШИТЬ", "PARTIAL", "PARTIALLY", "HALF",
                      "REDUCE", "TRIM"})
_FILLER = frozenset({"И", "А", "ПОТОМ", "ЗАТЕМ", "СРАЗУ", "THEN", "AND"})
_ECHO_SEP = r"[|/]"                 # эхо схемы: «КУПИТЬ | ПРОДАТЬ | …», «КУПИТЬ/ПРОДАТЬ»


class DecisionTable(dict):
    """v5.4.4: словарь слова решения узла {токен: синонимы} (обычный dict: токены, итерация, `in`) + правила разбора:
    side — (токен «вход без стороны», токен лонга, токен шорта, вернуть ли токен без стороны): вход без стороны →
      сторона по словам ответа (_LONG_WORDS / _SHORT_WORDS); нет — токен без стороны (разрешает вызывающий по уровням
      ответа) или None;
    cond — (токен входа, токен ожидания): условный вход («ВОЙТИ на откате», BUY_LIMIT, «ВОЙТИ позже», «ВОЙТИ, но на
      откате») → ожидание;
    combine — {(токен первого предложения, токен второго): итог} («ВЫЙТИ, ПЕРЕЗАЙТИ на 296.6» → ВЫЙТИ_И_ПЕРЕЗАЙТИ)."""
    side: tuple | None = None
    cond: tuple | None = None
    combine: dict | None = None


def _norm_word(x: Any) -> str:
    """«Ждём!» → «ЖДЕМ», «buy-now» → «BUY_NOW», «**ВОЙТИ**» → «ВОЙТИ»: верхний регистр, Ё=Е, всё, что не буква и не
    цифра, — разделитель слов (подчёркивание тоже)."""
    import re
    s = str(x or "").upper().replace("Ё", "Е")
    return " ".join(re.sub(r"[^0-9A-ZА-Я]+", " ", s).split())


def decision_raw(obj: Any, keys: tuple[str, ...] = DECISION_KEYS) -> str:
    """Сырое слово решения из ответа: строка как есть или первый непустой ключ из keys (без учёта регистра).
    v5.4.4: список из одного слова (["КУПИТЬ"]) — это слово; список из нескольких — решения нет («»)."""
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, dict):
        low = {str(k).strip().lower(): v for k, v in obj.items()}
        for k in keys:
            v = low.get(k)
            if isinstance(v, list) and len(v) == 1 and isinstance(v[0], (str, int, float)) and str(v[0]).strip():
                return str(v[0]).strip()
            if v not in (None, "") and not isinstance(v, (dict, list)):
                return str(v).strip()
    return ""


_CLAUSE_SEP = r"\s*(?:[—–;:()]|(?<!\d),(?!\d)|\s-\s)\s*"   # запятая между цифрами (99,5) — не граница
_NEG_WORDS = frozenset({"НЕ", "НЕТ", "НЕЛЬЗЯ", "NOT", "NO", "DONT"})
_CONTRAST = frozenset({"НО", "ОДНАКО", "BUT"})


def _prefix_hit(words: list[str], idx: dict[str, set[str]]) -> tuple[str | None, int]:
    """Самое длинное слово словаря в начале (все / 3 / 2 / 1 слово): (токен | None, сколько слов съедено)."""
    for n in (len(words), 3, 2, 1):
        if 0 < n <= len(words):
            hit = idx.get("_".join(words[:n]))
            if hit:
                return (next(iter(hit)) if len(hit) == 1 else None), n
    return None, 0


def _index(table: dict[str, tuple[str, ...]]) -> dict[str, set[str]]:
    idx: dict[str, set[str]] = {}
    for tok, syns in table.items():
        for w in (tok, *syns):
            idx.setdefault("_".join(_norm_word(w).split()), set()).add(tok)
    return idx


def text_side(raw: Any) -> str | None:
    """v5.4.4: сторона, названная словами ответа: «long» (купить / на покупку / в лонг / BUY), «short» (продать / на
    продажу / шорт / SELL); обе или ни одной — None."""
    words = set(_norm_word(raw).split())
    lg, sh = bool(words & _LONG_WORDS), bool(words & _SHORT_WORDS)
    return "long" if lg and not sh else "short" if sh and not lg else None


def side_by_levels(obj: Any, price: Any = None) -> str | None:
    """v5.4.4: сторона входа по уровням ответа (вход назван без стороны: «ЗАСАДА», «ВОЙТИ»): стоп ниже уровня входа
    (entry, нет — цена снимка price) и тейк выше — лонг; зеркально — шорт; уровни спорят или их нет — None."""
    if not isinstance(obj, dict):
        return None

    def num(v) -> float | None:
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        return x if x > 0 else None

    ref = num(obj.get("entry")) or num(price)
    if not ref:
        return None
    votes = set()
    inv, take = num(obj.get("invalidation")), num(obj.get("take"))
    if inv is not None and inv != ref:
        votes.add("long" if inv < ref else "short")
    if take is not None and take != ref:
        votes.add("long" if take > ref else "short")
    return next(iter(votes)) if len(votes) == 1 else None


def _conditional(words: list[str]) -> bool:
    return any(w in _COND_WORDS or w.startswith(_COND_STEMS) for w in words)


def _side_of(tok: str | None, text: str, table: dict) -> str | None:
    """Вход без стороны (DecisionTable.side) → сторона по словам ответа; нет — токен без стороны или None."""
    sd = getattr(table, "side", None)
    if not sd or tok is None or tok != sd[0]:
        return tok
    side = text_side(text)
    if side == "long":
        return sd[1]
    if side == "short":
        return sd[2]
    return tok if sd[3] else None


def _decide(text: str, table: dict, idx: dict[str, set[str]]) -> str | None:
    """Разбор одного ответа (без проверки эха схемы — она в decision_of)."""
    import re
    words = _norm_word(text).split()
    if not words:
        return None
    cond = getattr(table, "cond", None)
    whole = idx.get("_".join(words))       # точное слово словаря целиком («NO_TRADE») — раньше проверки отрицания
    if whole:
        tok = next(iter(whole)) if len(whole) == 1 else None
        if cond and tok == cond[0] and _conditional(words):
            return cond[1]                 # «КУПИТЬ_НА_ОТКАТЕ» у двери — ждать уровня
        return _side_of(tok, text, table)
    if "ИЛИ" in words or "OR" in words:
        return None                        # «ВОЙТИ или ЖДАТЬ» — решения нет, переспросить
    clauses = [c for c in re.split(_CLAUSE_SEP, text.strip()) if _norm_word(c)]
    if not clauses:
        return None
    first = _norm_word(clauses[0]).split()
    if any(w in _PARTIAL for w in first):
        return None                        # «ЗАКРЫТЬ ПОЛОВИНУ», «ЧАСТИЧНО ВЫЙТИ» — частичного выхода нет, переспросить
    hit, n = _prefix_hit(first, idx)
    if first[0] in _NEGATIONS and not (hit and n >= 2):
        # «НЕ СЛИВАТЬ — держать»: отвергнутое — в первом предложении, выбранное — дальше, и они должны различаться;
        # фраза с отрицанием из словаря («НЕ_ВХОДИТЬ», «НЕ_ЗАКРЫВАТЬ», «НЕ_СЕЙЧАС») — сама решение
        neg, _n = _prefix_hit(first[1:], idx) if len(first) > 1 else (None, 0)
        if neg is None or len(clauses) < 2:
            return None
        alt = _decide(", ".join(clauses[1:]), table, idx)
        return alt if alt is not None and alt != neg else None
    if hit is None:
        return None
    tail = first[n:n + 2]
    if tail and (tail[0] in _NEG_WORDS or "_".join(tail) in _POST_NEG):
        return None                        # «ВОЙТИ нельзя», «ВОЙТИ не сейчас» — не решение войти
    nxt = _norm_word(clauses[1]).split() if len(clauses) > 1 else []
    if nxt and nxt[0] in _CONTRAST and any(w in _NEG_WORDS for w in nxt):
        return None                        # «ВОЙТИ, но не сейчас»
    if cond and hit == cond[0] and _conditional(first[n:] + (nxt if nxt and nxt[0] in _CONTRAST else [])):
        return cond[1]                     # «ВОЙТИ на откате 296.6», «ВОЙТИ позже», «ВОЙТИ, но на откате» — ждать уровня
    comb = getattr(table, "combine", None)
    for more in ((first[n:], nxt) if comb else ()):
        k = 0
        while k < len(more) and more[k] in _FILLER:
            k += 1
        h2 = _prefix_hit(more[k:], idx)[0] if k < len(more) else None
        if (hit, h2) in comb:
            return comb[(hit, h2)]         # «ВЫЙТИ, ПЕРЕЗАЙТИ на 296.6», «ВЫЙТИ и потом ПЕРЕЗАЙТИ ниже»
    return _side_of(hit, text, table)


def echo_of(raw: Any, table: dict[str, tuple[str, ...]]) -> bool:
    """v5.4.4: ответ — эхо схемы: «|» или «/» разделяют РАЗНЫЕ решения («КУПИТЬ | ПРОДАТЬ | НОВЫЙ_АНАЛИЗ | ЖДЁМ»,
    «ВОЙТИ|ОТМЕНИТЬ|ЖДАТЬ», «СЛИТЬ|ЖДАТЬ»). Вход без стороны рядом со стороной («КУПИТЬ/ЗАСАДА») — не спор."""
    import re
    text = str(raw or "")
    if not re.search(_ECHO_SEP, text):
        return False
    idx = _index(table)
    if idx.get("_".join(_norm_word(text).split())):
        return False                       # «ВЫЙТИ/ПЕРЕЗАЙТИ» — слово словаря целиком
    parts = [p for p in re.split(_ECHO_SEP, text) if _norm_word(p)]
    if len(parts) < 2:
        return False
    toks = {_decide(p, table, idx) for p in parts} - {None}
    sd = getattr(table, "side", None)
    if sd and sd[0] in toks and toks - {sd[0]} <= {sd[1], sd[2]}:
        toks.discard(sd[0])                # «КУПИТЬ/ЗАСАДА» — засада на покупку, а «ЗАСАДА/ЖДЁМ» — спор
    return len(toks) > 1


_AMBUSH_STEMS = ("ЗАСАД", "ВЗВЕД", "ВЗВЕСТ", "ЛИМИТ", "LIMIT", "AMBUSH")


def ambush_of(raw: Any) -> bool:
    """v5.4.4: в первом предложении ответа — засада / лимитка / «вход взведён»: такой вход без уровня (entry) не вход
    («ЗАСАДА» без числа — вызывающий переспрашивает, а не входит по текущей цене)."""
    import re
    clauses = [c for c in re.split(_CLAUSE_SEP, str(raw or "").strip()) if _norm_word(c)]
    return bool(clauses) and any(w.startswith(_AMBUSH_STEMS) for w in _norm_word(clauses[0]).split())


def partial_of(raw: Any) -> bool:
    """v5.4.4: в первом предложении ответа — частичное закрытие/выход («ЗАКРЫТЬ ПОЛОВИНУ», «ЧАСТИЧНО ВЫЙТИ»,
    «СОКРАТИТЬ»): такого решения у пилота нет (вызывающий переспрашивает с пометкой «ЗАКРЫТЬ всё или ДЕРЖАТЬ»)."""
    import re
    clauses = [c for c in re.split(_CLAUSE_SEP, str(raw or "").strip()) if _norm_word(c)]
    return bool(clauses) and any(w in _PARTIAL for w in _norm_word(clauses[0]).split())


def decision_of(raw: Any, table: dict[str, tuple[str, ...]]) -> str | None:
    """Каноническое решение узла или None. table: {токен узла: синонимы} (токен сам себе синоним); DecisionTable —
    ещё и правила (сторона входа по словам, условный вход, сочетание двух предложений).
    Решение — в первом предложении ответа (граница — запятая, тире, «;», «:», скобка):
      «КУПИТЬ_СЕЙЧАС — анализ подтверждён» → КУПИТЬ_СЕЙЧАС; «ДЕРЖАТЬ, не надо сливать» → ДЕРЖАТЬ.
    Отрицание сразу после слова решения в том же предложении — переспрос: «ВОЙТИ нельзя», «ВОЙТИ не сейчас, …».
    «X, но не …» — переспрос. «НЕ X — Y» — решение Y, только если Y другое, чем отвергнутый X («НЕ ПОКУПАТЬ, ЖДЁМ» →
    ждём; «НЕ ВХОДИТЬ — вход выше 101» у двери → переспрос); фраза с отрицанием из словаря узла («НЕ ЗАКРЫВАТЬ» в
    позиции, «НЕ СЛИВАТЬ» у троса, «НЕ СЕЙЧАС» у двери) — решение. «… или …», два решения сразу и эхо схемы через «|» / «/»
    («КУПИТЬ | ПРОДАТЬ | НОВЫЙ_АНАЛИЗ | ЖДЁМ») — переспрос. Частичное закрытие («ЗАКРЫТЬ ПОЛОВИНУ») — переспрос."""
    text = str(raw or "")
    if not _norm_word(text):
        return None
    if echo_of(text, table):
        return None
    return _decide(text, table, _index(table))


def _side_syn(side: str | None, long_syn: tuple[str, ...], short_syn: tuple[str, ...]) -> tuple[str, ...]:
    return long_syn if side == "long" else short_syn if side == "short" else ()


def review_table(in_pos: bool, side: str | None = None, armed: bool = False,
                 sideless: bool = False) -> dict[str, tuple[str, ...]]:
    """Перепроверка дежурного PRO. В позиции «SELL» у лонга — закрыть, «BUY» у лонга — добрать (зеркально для шорта);
    «подтянуть трос», «стоп в безубыток», «не закрывать» — держать (канон ЖДЁМ, числа — в invalidation/take).
    Вне рынка (v5.4.4): «засада / войти / вход взведён / LIMIT / AMBUSH» — вход, сторона по словам ответа («на
    покупку», «в лонг», LIMIT_BUY); без стороны — ENTRY_ANY при sideless=True (MissionPilot._parse_choice решает по
    уровням ответа — side_by_levels), иначе None: скрытого крена в лонг нет (находка проверяющего 5.4.2). «Пас / нет
    входа / не входить / ожидание» — ЖДЁМ. armed (взведён вход без позиции — засада, вход у двери, заявка в полёте):
    ОТМЕНИТЬ_ВХОД — снять его («ОТМЕНИТЬ», «СНЯТЬ ЗАСАДУ», «ВНЕ РЫНКА», «пас / нет входа»); ЖДЁМ / «ДЕРЖАТЬ ЗАСАДУ» —
    взведённый вход остаётся."""
    if in_pos:
        return DecisionTable({"ЗАКРЫТЬ": SYN_CLOSE + SYN_OUT + _side_syn(side, SYN_SELL, SYN_BUY),
                              "ЖДЁМ": SYN_WAIT + SYN_HOLD + SYN_TRAIL + ("НЕ_ЗАКРЫВАТЬ",),
                              "ДОБРАТЬ": SYN_ADD + _side_syn(side, SYN_BUY, SYN_SELL),
                              "ПЕРЕВЕРНУТЬ": SYN_FLIP,
                              "НОВЫЙ_АНАЛИЗ": SYN_COUNCIL})
    t = DecisionTable({"КУПИТЬ_СЕЙЧАС": SYN_BUY + ("ДОКУПИТЬ",), "ПРОДАТЬ_СЕЙЧАС": SYN_SELL,
                       ENTRY_ANY: SYN_ENTER + SYN_AMBUSH})
    if armed:
        t[REVIEW_CANCEL] = SYN_CANCEL + SYN_UNARM + SYN_OUT + SYN_NO_ENTRY
        t["ЖДЁМ"] = SYN_WAIT + SYN_HOLD
    else:
        t["ЖДЁМ"] = SYN_WAIT + SYN_OUT + SYN_HOLD + SYN_NO_ENTRY
    t["НОВЫЙ_АНАЛИЗ"] = SYN_COUNCIL
    t.side = (ENTRY_ANY, "КУПИТЬ_СЕЙЧАС", "ПРОДАТЬ_СЕЙЧАС", bool(sideless))
    return t


def door_table(side: str | None) -> dict[str, tuple[str, ...]]:
    """Проверка входа у двери: BUY/LONG — «войти» только для плана лонга, SELL/SHORT — только для шорта. v5.4.4:
    «ДЕРЖАТЬ (засаду)», «отложить», «повременить», «засада», «не сейчас», «пока нет», «ожидание» — ЖДАТЬ; условный вход
    («ВОЙТИ на откате 296.6», «КУПИТЬ_НА_ПРОБОЕ», BUY_LIMIT, «ВОЙТИ позже», «ВОЙТИ, но на откате», «вход взведён») — ЖДАТЬ
    (уровень — в entry ответа), а не вход по текущей цене; «ДА» — не ответ (как было)."""
    t = DecisionTable({"ВОЙТИ": SYN_ENTER + _side_syn(side, SYN_BUY, SYN_SELL),
                       "ЖДАТЬ": SYN_WAIT + SYN_OUT + SYN_HOLD + (
                           "ДЕРЖАТЬ_ЗАСАДУ", "ОСТАВИТЬ_ЗАСАДУ", "ОТЛОЖИТЬ", "ОТКЛАДЫВАЕМ", "ПОВРЕМЕНИТЬ", "ЗАСАДА",
                           "ЗАСАДУ", "НЕ_СЕЙЧАС", "ПОКА_НЕТ", "ПОЗЖЕ", "НЕ_СПЕШИТЬ", "ВХОД_ВЗВЕДЕН", "LATER", "NOT_YET"),
                       "ОТМЕНИТЬ": SYN_CANCEL + SYN_UNARM})
    t.cond = ("ВОЙТИ", "ЖДАТЬ")
    return t


def profit_table(side: str | None) -> dict[str, tuple[str, ...]]:
    """Мысль о прибыли: «SELL» у лонга (и «BUY» у шорта) — выйти. v5.4.4: «подтянуть трос / стоп», «запереть прибыль» —
    ДЕРЖАТЬ (замок — в lock_price); «ВЫЙТИ, ПЕРЕЗАЙТИ на …» — ВЫЙТИ_И_ПЕРЕЗАЙТИ; «звать совет» — СОВЕТ; «частично
    выйти» — переспрос (частичного выхода нет)."""
    t = DecisionTable({"ВЫЙТИ": SYN_CLOSE + SYN_OUT + _side_syn(side, SYN_SELL, SYN_BUY),
                       "ВЫЙТИ_И_ПЕРЕЗАЙТИ": SYN_REENTER, "СОВЕТ": SYN_COUNCIL,
                       "ДЕРЖАТЬ": SYN_HOLD + SYN_WAIT + SYN_TRAIL})
    t.combine = {("ВЫЙТИ", "ВЫЙТИ_И_ПЕРЕЗАЙТИ"): "ВЫЙТИ_И_ПЕРЕЗАЙТИ"}
    return t


def guard_table() -> dict[str, tuple[str, ...]]:
    """Мягкий стоп у троса: СЛИТЬ = выйти, ЖДАТЬ = держать и передать совету. v5.4.4: «СОВЕТ / звать совет», «НЕ
    СЛИВАТЬ» — ЖДАТЬ (ожидание у троса и есть передача Совету); CUT / STOP_OUT — СЛИТЬ."""
    return DecisionTable({"СЛИТЬ": SYN_CLOSE + SYN_OUT + ("CUT", "CUT_LOSS", "CUT_LOSSES", "STOP_OUT", "STOPOUT"),
                          "ЖДАТЬ": SYN_WAIT + SYN_HOLD + SYN_COUNCIL + ("НЕ_СЛИВАТЬ",)})


def take_table() -> dict[str, tuple[str, ...]]:
    """Мягкий тейк: ЗАФИКСИРОВАТЬ = выйти, ПОДЕРЖАТЬ = держать (и передать Совету). v5.4.4: «звать совет» — ПОДЕРЖАТЬ
    (молчание у тейка — фиксация по правилу, а «совет» — не молчание); «частично зафиксировать» — переспрос."""
    return DecisionTable({"ЗАФИКСИРОВАТЬ": SYN_CLOSE + SYN_OUT, "ПОДЕРЖАТЬ": SYN_HOLD + SYN_WAIT + SYN_COUNCIL})


def exec_table(in_pos: bool) -> dict[str, tuple[str, ...]]:
    """Приказ шифровальщика: FLAT и «вне рынка» при позиции — закрыть, без позиции — WAIT. v5.4.2 (ревью): HOLD —
    только при позиции: держать как есть с новыми уровнями, без добора («держать» больше не кодируется как BUY той же
    стороны, после которого код добирал до максимума)."""
    if in_pos:
        return {"BUY": SYN_BUY, "SELL": SYN_SELL, "WAIT": SYN_WAIT + ("HOLD_FLAT",), "HOLD": SYN_HOLD,
                "CLOSE": SYN_CLOSE + SYN_OUT}
    close = tuple(x for x in SYN_CLOSE if x != "FLAT")
    return {"BUY": SYN_BUY, "SELL": SYN_SELL, "WAIT": SYN_WAIT + SYN_OUT + ("FLAT", "HOLD_FLAT"), "CLOSE": close}


async def pro_text(system: str, user: str, *, route: str = "pro",
                   max_tokens: int | None = None) -> str:
    return await ai.ask(system, user, model=_pro(), thinking=True,
                        max_tokens=max_tokens, route=route, api_key=key())


async def pro_stream(system: str, user: str, *,
                     on_think: Callable[[str], Awaitable[None]] | None = None,
                     on_text: Callable[[str], Awaitable[None]] | None = None,
                     route: str = "pro") -> str:
    async def _t(delta: str, _full):
        if on_think:
            await on_think(delta)

    async def _x(delta: str, _full):
        if on_text:
            await on_text(delta)

    return await ai.stream(system, user, on_think=_t if on_think else None,
                           on_text=_x if on_text else None, model=_pro(),
                           thinking=True, route=route, api_key=key())


def last_error() -> dict | None:
    """v5.3 фаза 3 (W1), панель проблем: последняя ошибка DeepSeek после всех повторов —
    {"ts","route","code","text"} | None; поверх — "stale": True, если позже был успешный ответ
    (ошибка старая, ИИ снова отвечает)."""
    try:
        err = ai.last_error()
    except Exception:            # noqa: BLE001
        return None
    if not err:
        return None
    try:
        err["stale"] = bool(ai.last_ok_ts() and ai.last_ok_ts() > float(err.get("ts") or 0))
    except Exception:            # noqa: BLE001
        err["stale"] = False
    return err


def note_error(text: str, route: str = "") -> None:
    """Ошибка ИИ вне ai.ask (таймаут ожидания у вызывающего, ответ не JSON) — в ту же память."""
    try:
        ai.note_error(str(text), route)
    except Exception:            # noqa: BLE001
        pass


def usage() -> dict:
    """Расход DeepSeek с запуска: итог (prompt/completion/total/calls) + по маршрутам и по моделям
    (v5.3 W2, панель фазы 3): {"by_route": {route: {calls, prompt, completion, model}},
    "by_model": {"FLASH"|"PRO"|имя: {calls, prompt, completion}}}."""
    out = dict(ai.tokens())
    try:
        by_route = ai.tokens_by_route()
    except Exception:            # noqa: BLE001
        by_route = {}
    by_model: dict[str, dict] = {}
    for rec in by_route.values():
        mdl = rec.get("model") or ""
        name = "FLASH" if mdl == _flash() else "PRO" if mdl == _pro() else (mdl or "?")
        b = by_model.setdefault(name, {"calls": 0, "prompt": 0, "completion": 0})
        b["calls"] += int(rec.get("calls") or 0)
        b["prompt"] += int(rec.get("prompt") or 0)
        b["completion"] += int(rec.get("completion") or 0)
    out["by_route"] = by_route
    out["by_model"] = by_model
    return out


if __name__ == "__main__":
    async def _t():
        calls = []

        async def fake_ask(system, user, **kw):
            calls.append(kw.get("route"))
            if kw.get("route") == "x":
                return '{"a": [1, 2'          # обрезано
            if kw.get("route") == "y":
                return "[⚠ модель ушла в размышления — ниже финальная часть]\nбла"
            return '{"a": [1, 2]}'

        real = ai.ask
        ai.ask = fake_ask
        try:
            key.__globals__["config"].set_deepseek_keys(["sk-test"])
            out = await flash_json("s", "u", route="x")
            assert out == {"a": [1, 2]} and calls == ["x", "x_repair"], (out, calls)
            calls.clear()
            out = await flash_json("s", "u", think=True, route="y")
            assert out == {"a": [1, 2]} and calls == ["y", "y_nothink"], (out, calls)
        finally:
            ai.ask = real
            key.__globals__["config"].wipe_keys() if False else None

    import asyncio as _a
    import tempfile as _tf
    from pathlib import Path as _P
    _cfg = key.__globals__["config"]
    _cfg_keys = _cfg.deepseek_keys()
    _real_cfg_path = _cfg.USER_CFG_PATH
    _cfg.USER_CFG_PATH = _P(_tf.mkdtemp(prefix="pythia_ai_v5_")) / "config_user.json"   # self-тест не трогает data/
    try:
        _a.run(_t())
        _cfg.set_deepseek_keys(_cfg_keys)   # вернуть как было (в памяти; файл — временный)
        assert _cfg.USER_CFG_PATH.exists() and not str(_cfg.USER_CFG_PATH).startswith(str(_cfg.DATA_DIR)), _cfg.USER_CFG_PATH
    finally:
        _cfg.USER_CFG_PATH = _real_cfg_path
    assert _cfg.deepseek_keys() == _cfg_keys, "ключи владельца в памяти — как были"
    # память ошибок (v5.3 фаза 3): note_error → last_error со stale по последнему успеху
    ai._last_err, ai._last_ok_ts = None, 0.0
    assert last_error() is None
    note_error("PRO промолчал (таймаут 300 с)", "mission_review")
    _le = last_error()
    assert _le and _le["route"] == "mission_review" and "таймаут" in _le["text"] and _le["stale"] is False, _le
    ai._add_usage(None, "x", _flash())            # ответ пришёл позже ошибки → ошибка старая
    assert last_error()["stale"] is True
    ai._last_err, ai._last_ok_ts = None, 0.0
    s = now_msk_str()
    assert "МСК" in s and any(w in s for w in _WD), s
    assert fmt_ts(None) == "—"
    # учёт по маршрутам и моделям (v5.3 W2): ai._add_usage(route, model) → usage()["by_route"/"by_model"]
    class _U:
        prompt_tokens, completion_tokens, total_tokens = 100, 20, 120
    _saved_tok, _saved_routes = dict(ai._tok), ai.tokens_by_route()
    ai.reset_tokens()
    ai._add_usage(_U(), "mission_take", _flash())
    ai._add_usage(_U(), "mission_take", _flash())
    ai._add_usage(_U(), "mission_review", _pro())
    u = usage()
    assert u["calls"] == 3 and u["prompt"] == 300 and u["by_route"]["mission_take"]["calls"] == 2, u
    assert u["by_route"]["mission_review"]["model"] == _pro() and u["by_model"]["FLASH"]["prompt"] == 200
    assert u["by_model"]["PRO"] == {"calls": 1, "prompt": 100, "completion": 20}, u["by_model"]
    ai.reset_tokens()
    ai._tok.update(_saved_tok)
    for _r, _v in _saved_routes.items():
        ai._tok_routes[_r] = dict(_v)
    # v5.4.2: срок попытки — таймаут сразу исключением, без повтора; пустой ответ → повтор со своим сроком
    async def _t_att():
        seen = []

        async def slow_ask(system, user, **kw):
            seen.append(kw.get("route"))
            if kw.get("route") == "mission_entry":
                await asyncio.sleep(0.2)
                return "[⚠ модель ушла в размышления — ниже финальная часть]\nчерновик"
            return '{"decision": "ВОЙТИ"}'

        real_ask = ai.ask
        ai.ask = slow_ask
        try:
            got = await _json_with_repair("s", "u", model="m", think=True, route="mission_entry", max_tokens=None,
                                          attempt_timeout=0.5)
            assert got == {"decision": "ВОЙТИ"} and seen == ["mission_entry", "mission_entry_retry"], seen
            seen.clear()
            try:
                await _json_with_repair("s", "u", model="m", think=True, route="mission_entry", max_tokens=None,
                                        attempt_timeout=0.05)
                raise AssertionError("таймаут попытки должен быть исключением")
            except asyncio.TimeoutError as e:
                assert "не ответила за" in str(e) and seen == ["mission_entry"], (e, seen)
        finally:
            ai.ask = real_ask
    _real_key = key
    key = lambda: "sk-test"                 # noqa: E731 — ключ из пула не нужен: ai.ask подменён
    try:
        asyncio.run(_t_att())
    finally:
        key = _real_key
    # v5.4.2: разбор слова решения — целые слова, синонимы, отрицание и «или» → None (решения нет, переспросить)
    _door = {"ВОЙТИ": SYN_ENTER + SYN_BUY, "ЖДАТЬ": SYN_WAIT, "ОТМЕНИТЬ": SYN_CANCEL}
    for _raw, _want in (("ВОЙТИ", "ВОЙТИ"), ("**войти**", "ВОЙТИ"), ("BUY", "ВОЙТИ"), ("ENTRY", "ВОЙТИ"),
                        ("Ждём!", "ЖДАТЬ"), ("ЖДАТЬ, хотя можно и войти", "ЖДАТЬ"), ("ЖДАТЬ ВХОДА НА ОТКАТЕ", "ЖДАТЬ"),
                        ("ОТКАЗ", "ОТМЕНИТЬ"), ("НЕ ВХОДИТЬ", None), ("ВОЙТИ или ЖДАТЬ", None), ("", None), ("?!", None)):
        assert decision_of(_raw, _door) == _want, (_raw, decision_of(_raw, _door), _want)
    _rev = {"КУПИТЬ_СЕЙЧАС": SYN_BUY, "ПРОДАТЬ_СЕЙЧАС": SYN_SELL, "ЖДЁМ": SYN_WAIT + SYN_HOLD, "НОВЫЙ_АНАЛИЗ": SYN_COUNCIL}
    assert decision_of("КУПИТЬ_СЕЙЧАС — анализ подтверждён", _rev) == "КУПИТЬ_СЕЙЧАС", "не совет вместо входа"
    assert decision_of("ЖДЁМ (не покупать до 101)", _rev) == "ЖДЁМ" and decision_of("не покупать", _rev) is None
    assert decision_of("ЖДЕМ", _rev) == "ЖДЁМ" and decision_of("новый анализ", _rev) == "НОВЫЙ_АНАЛИЗ"
    assert decision_raw({"Decision": "buy"}) == "buy" and decision_raw({"choice": "", "do": "SELL"}) == "SELL"
    # словари узлов: стороны и позиция
    assert decision_of("BUY", review_table(False)) == "КУПИТЬ_СЕЙЧАС" and decision_of("ВОЙТИ", review_table(False)) is None
    assert decision_of("CLOSE", review_table(True, "long")) == "ЗАКРЫТЬ" and decision_of("SELL", review_table(True, "long")) == "ЗАКРЫТЬ"
    assert decision_of("BUY", review_table(True, "long")) == "ДОБРАТЬ" and decision_of("BUY", review_table(True, "short")) == "ЗАКРЫТЬ"
    assert decision_of("ЗАФИКСИРОВАТЬ", review_table(True, "long")) == "ЗАКРЫТЬ" and decision_of("HOLD", review_table(True, "long")) == "ЖДЁМ"
    assert decision_of("BUY", door_table("long")) == "ВОЙТИ" and decision_of("BUY", door_table("short")) is None
    assert decision_of("ВОЙТИ", door_table("short")) == "ВОЙТИ" and decision_of("SKIP", door_table("long")) == "ОТМЕНИТЬ"
    assert decision_of("ВЫЙТИ И ПЕРЕЗАЙТИ", profit_table("long")) == "ВЫЙТИ_И_ПЕРЕЗАЙТИ"
    assert decision_of("ЗАФИКСИРОВАТЬ ПРИБЫЛЬ", profit_table("long")) == "ВЫЙТИ" and decision_of("SELL", profit_table("long")) == "ВЫЙТИ"
    assert decision_of("ДЕРЖАТЬ, НЕ ВЫХОДИТЬ", profit_table("long")) == "ДЕРЖАТЬ" and decision_of("СОВЕТ", profit_table("long")) == "СОВЕТ"
    assert decision_of("CLOSE", guard_table()) == "СЛИТЬ" and decision_of("HOLD", guard_table()) == "ЖДАТЬ"
    assert decision_of("TAKE_PROFIT", take_table()) == "ЗАФИКСИРОВАТЬ" and decision_of("ДЕРЖАТЬ", take_table()) == "ПОДЕРЖАТЬ"
    assert decision_of("FLAT", exec_table(True)) == "CLOSE" and decision_of("FLAT", exec_table(False)) == "WAIT"
    assert decision_of("КУПИТЬ", exec_table(False)) == "BUY" and decision_of("ЖДЁМ", exec_table(False)) == "WAIT"
    assert decision_of("NO_TRADE", exec_table(False)) == "WAIT" and decision_of("no trade", exec_table(False)) == "WAIT", \
        "слово словаря целиком — не отрицание"
    assert decision_of("NO BUY", exec_table(False)) is None and decision_of("НЕ ВХОДИТЬ", door_table("long")) is None
    assert decision_raw({}) == "" and decision_raw("ВОЙТИ ") == "ВОЙТИ" and decision_raw({"choice": {"x": 1}}) == ""
    # v5.4.4: эхо схемы, семейство ЗАСАДА (сторона словами / по уровням), ожидание и пас, ОТМЕНИТЬ при взведённом входе,
    # «подтянуть трос» и «не закрывать» — держать, частичка — переспрос, условный вход у двери — ЖДАТЬ, «ДА» — не ответ
    _r0, _rs, _ra = review_table(False), review_table(False, sideless=True), review_table(False, armed=True, sideless=True)
    assert decision_of("КУПИТЬ | ПРОДАТЬ | НОВЫЙ_АНАЛИЗ | ЖДЁМ", _r0) is None and echo_of("ВОЙТИ/ОТМЕНИТЬ", door_table("long"))
    assert decision_of("ЗАСАДА", _r0) is None and decision_of("ЗАСАДА", _rs) == ENTRY_ANY
    assert decision_of("засада на покупку", _rs) == "КУПИТЬ_СЕЙЧАС" and decision_of("LIMIT SELL", _rs) == "ПРОДАТЬ_СЕЙЧАС"
    assert side_by_levels({"entry": 99, "invalidation": 98}, 100) == "long" and side_by_levels({"invalidation": 102}, 100) == "short"
    assert side_by_levels({"entry": 99, "invalidation": 98, "take": 97}, 100) is None, "уровни спорят — стороны нет"
    assert ambush_of("ЗАСАДА у 99") and not ambush_of("КУПИТЬ — без засады") and partial_of("ЗАКРЫТЬ ПОЛОВИНУ")
    assert decision_of("ОЖИДАНИЕ", _r0) == "ЖДЁМ" and decision_of("ПАС", _r0) == "ЖДЁМ" and decision_of("НЕТ ВХОДА", _r0) == "ЖДЁМ"
    assert decision_of("ОТМЕНИТЬ", _r0) is None and decision_of("ОТМЕНИТЬ", _ra) == REVIEW_CANCEL
    assert decision_of("СНЯТЬ ЗАСАДУ", _ra) == REVIEW_CANCEL and decision_of("ДЕРЖАТЬ", _ra) == "ЖДЁМ"
    assert decision_of("ПОДТЯНУТЬ ТРОС", review_table(True, "long")) == "ЖДЁМ"
    assert decision_of("НЕ ЗАКРЫВАТЬ", review_table(True, "long")) == "ЖДЁМ"
    assert decision_of("ЗАКРЫТЬ ПОЛОВИНУ", review_table(True, "long")) is None
    assert decision_of("ОТЛОЖИТЬ", door_table("long")) == "ЖДАТЬ" and decision_of("НЕ СЕЙЧАС", door_table("long")) == "ЖДАТЬ"
    assert decision_of("ВОЙТИ на откате 99.5", door_table("long")) == "ЖДАТЬ" and decision_of("ДА", door_table("long")) is None
    assert decision_of("ВЫЙТИ, ПЕРЕЗАЙТИ на 99.5", profit_table("long")) == "ВЫЙТИ_И_ПЕРЕЗАЙТИ"
    assert decision_of("ЗВАТЬ СОВЕТ", profit_table("long")) == "СОВЕТ" and decision_of("ЧАСТИЧНО ВЫЙТИ", profit_table("long")) is None
    assert decision_of("ЗАПЕРЕТЬ ПРИБЫЛЬ", profit_table("long")) == "ДЕРЖАТЬ"
    assert decision_of("НЕ СЛИВАТЬ", guard_table()) == "ЖДАТЬ" and decision_of("CUT", guard_table()) == "СЛИТЬ"
    assert decision_of("СОВЕТ", guard_table()) == "ЖДАТЬ" and decision_of("СОВЕТ", take_table()) == "ПОДЕРЖАТЬ"
    assert decision_raw({"choice": ["КУПИТЬ"]}) == "КУПИТЬ" and decision_raw({"choice": ["КУПИТЬ", "ЖДЁМ"]}) == ""
    print("ai_v5 self-test OK:", s)
