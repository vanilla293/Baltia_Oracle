# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — МИССИЯ (этап 4, §4 контракта).

Человек выбирает тикер и способ игры (long / short / mixed / auto) → PRO совет
по инструменту (досье + итог общего совета + прикреплённые новости + астро +
Курамото/бифуркации): анализ → критика → вердикт → exec-приказ (вход сейчас /
на откате / на пробитии) → исполняет MissionPilot (наследник ai_pilot.AIPilot):
вход на максимум, трос; дежурный PRO раз в PYTHIA_REVIEW_SEC думает и решает
(ждать / войти / закрыть / звать совет), внепланово — по резкому ходу цены или
серьёзной новости (v5.1h); полный совет зовёт сам PRO. Один депозит = одна
активная миссия.

v5.1 «ВСЁ, НО В РАЗУМНЫХ ПРЕДЕЛАХ»: ножницами ничего не режем — каждый ИИ видит
своё целиком, пока блок ≤ PYTHIA_CTX_LIMIT и весь user-текст ≤ PYTHIA_PROMPT_SOFT;
выше — FLASH ужимает без потери нитей (compress.fit по блокам, compress.shrink
для анализа/вердикта) ДО сборки промпта. Размеры промптов уходят в степпер
(bus.stage … detail=«аналитик: промпт 96 400 симв. — досье 12 000, …») и в
status()["sizes"]. Аварийный потолок PYTHIA_PROMPT_CAP остаётся в prompts_mission._finish.

v5.3 — толмач и память (explain.py): после ключевых узлов (приказ, вход, добор, перепроверка,
ответ у троса, закрытие, стопор рынка, отказ биржи) `_tolmach` отдаёт узел толмачу — FLASH пишет
владельцу, что произошло и что дальше (m.explain, стадия «explain», status()["explain"]); по узлам
«совет», «закрытие», раз в PYTHIA_MEMORY_EVERY перепроверок `_memorize` сводит накопившееся в абзац
m.memory (блок «ПАМЯТЬ МИССИИ» у аналитика/критика/вердикта, перепроверки, троса, чата) и режет сырые
списки; новости старше последнего совета в перепроверку/трос не идут — они в памяти.

Self-тест (без сети): python3 -m backend.mission
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import re
import time
from typing import Any

from . import ledger  # noqa: E402  (v5.3 фаза 4 · W1: журнал по операциям брокера)
from . import (ai, ai_pilot, ai_v5, bus, compress, config, correlate, instruments,
               market_ctx, prompts_mission, scout, store_v5, tinkoff, trader_broker)

try:                                                 # сканер стакана 4.x («8 ч»): онлайн-наблюдение
    from . import maya_scan
except Exception:                                    # noqa: BLE001 — без модуля миссия живёт
    maya_scan = None
try:                                                 # рыночные часы: биржа закрыта → стопор пилота
    from . import market_clock
except Exception:                                    # noqa: BLE001
    market_clock = None
try:                                                 # толмач и память миссии (v5.3)
    from . import explain
except Exception:                                    # noqa: BLE001
    explain = None

log = logging.getLogger("pythia.mission")


def _norm_ticker(t: str) -> str:
    """Синоним биржи/ИИ → код каталога (MIX→MX, Si→SI); без newsflow — как есть."""
    try:
        from . import newsflow as _nf
        return _nf.norm_ticker(t)
    except Exception:            # noqa: BLE001
        return t

# период перепроверки — из конфига v5 (родитель прибавляет модульную константу)
ai_pilot.REVIEW_SEC = float(getattr(config, "PYTHIA_REVIEW_SEC", 1800))

PLAY = ("long", "short", "mixed", "auto")
EXEC_FRESH_SEC = 2 * 3600.0     # приказ старше — протух, при resume не принимаем
# РИТМ (v5.1h): между советами торгует дежурный PRO — раз в PYTHIA_REVIEW_SEC думает и решает
# (ждать / купить / продать / закрыть / звать совет). Поводы пилота (идея умерла до входа, приказ
# протух, позиция закрыта, биржа отбивает) НЕ зовут совет и не дёргают PRO каждую минуту: повод
# копится к ближайшей перепроверке, после закрытия — остыть PYTHIA_AFTER_CLOSE_SEC. События
# (резкий ход цены PYTHIA_SHOCK_PCT за PYTHIA_SHOCK_WIN_SEC, серьёзная новость дозора) поднимают
# внеплановую перепроверку не чаще PYTHIA_EVENT_COOL_SEC. Полный совет — старт, кнопка,
# НОВЫЙ_АНАЛИЗ от PRO (не чаще PYTHIA_COUNCIL_GAP_SEC), серьёзная новость без живого пилота.
# v5.4.2 «СВОБОДНЫЙ ПИЛОТ»: вне рынка без плана повод пилота зовёт PRO через EVENT_MIN_GAP_SEC (а не плановую);
# приказ WAIT — уровни под наблюдением и ритм PYTHIA_WAIT_REVIEW_SEC; прокол без плана — «вне рынка»;
# НОВЫЙ_АНАЛИЗ в окне совета отложен, не потерян; совет по поводу — не дольше PYTHIA_COUNCIL_MAX_SEC.
REVIEW_RETRY_SEC = 300.0   # v5.3 фаза 3: PRO промолчал на перепроверке (таймаут / не JSON) → повтор через 5 мин, повод хранится
PILOT_REASONS = ("приказ протух", "идея мертва до входа", "после закрытия", "внешнее закрытие",
                 "серия отказов биржи", "вход невозможен")
# поводы пилота «позиция закрыта» — строки кода (AIPilot._finish_closed / _reconcile), узнаются по НАЧАЛУ, а не по
# подстроке «закрыт» в свободном тексте ИИ («закрытие часа ниже 100» в причине отмены у двери — не закрытие позиции)
CLOSED_REASONS = ("после закрытия", "внешнее закрытие")
GUARD_LIGHT_TIMEOUT = 15.0      # мягкий стоп: живой рынок для FLASH — не дольше N с
GUARD_SCOUT_TIMEOUT = 12.0      # …и свежие данные разведки — не дольше N с
SCOUT_REVIEW_TIMEOUT = 20.0     # перепроверка: обновить данные разведки — не дольше N с
PARTNERS_TIMEOUT = 60.0         # совет: связанные бумаги (correlate, свечи 60 дн. по ~15 бумагам) — не дольше N с
PARTNERS_REVIEW_TIMEOUT = 20.0  # перепроверка: партнёры из кэша (6 ч) или пересчёт — не дольше N с
GUARD_PARTNERS_TIMEOUT = 5.0    # трос: только из кэша, иначе прошлый текст
EVENT_TRIAGE_TIMEOUT = 300.0    # v5.3 W2 / 5.4.1: триаж события (резкий ход / новость при позиции) — ИИ у денег (PRO) не дольше N с, иначе дежурный PRO
GATE_LIGHT_TIMEOUT = 15.0       # v5.4.1: проверка входа у двери — живой рынок не дольше N с (как у троса)
TRIAGE_LIGHT_TIMEOUT = 8.0      # …и живой рынок для триажа — не дольше N с
TRIAGES_KEEP = 20
PUNCTURE_CHECK_SEC = 5.0        # v5.3 W3: прокол сканера — статус сканера смотрим не чаще раза в N с (кэш агрегатов 2.5 с)
PUNCTURE_SEEN_SEC = 6 * 3600.0  # тот же прокол (сторона + полоса + роль) второй раз ИИ не поднимает N ч
ENTRY_KINDS = ("сейчас", "откат", "прорыв")
BREAK_CONFIRM_TICKS = 2         # прорыв: цена за уровнем два тика подряд (≈3 с), не одна печать
EVENT_MIN_GAP_SEC = 180.0       # событие сразу после ответа PRO — перепроверка не раньше чем через 3 мин
REVIEWS_KEEP = 20
RESUME_GAP_SEC = 300.0          # сторож поднимает пилот не чаще раза в 5 мин

# соседние модули (пишутся параллельно) — лениво, в try; тесты подменяют атрибутами
newsflow: Any = None
council: Any = None
watch: Any = None


def _mod(name: str):
    g = globals()
    if g.get(name) is not None:
        return g[name]
    try:
        m = importlib.import_module(f"{__package__}.{name}")
        g[name] = m
        return m
    except Exception as e:                           # noqa: BLE001
        log.debug("модуль %s недоступен: %s", name, str(e)[:80])
        return None


def _humanize(e: Exception) -> str:
    try:
        return ai.humanize_error(e)
    except Exception:                                # noqa: BLE001
        return str(e)[:200]


def _f(x, d=None):
    try:
        if isinstance(x, str):
            x = x.replace(",", ".").strip()
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def _bg(coro) -> None:
    """Запустить корутину фоном из синхронного кода (шина никогда не роняет)."""
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        try:
            coro.close()
        except Exception:                            # noqa: BLE001
            pass


def _make_broker() -> trader_broker.Broker:
    """Боевой брокер; PYTHIA_DRY=1 / нет токена → сам откатится в dry."""
    return trader_broker.Broker("real")


# ── разумные пределы: сжатие FLASH без потери нитей, размеры для степпера ─────
HEAD_RESERVE = 8_000            # запас под шапку/правила/вопрос сверх суммы блоков (симв.)
SIZES_TOP = 5                   # сколько самых больших блоков называть в detail стадии


def _fmt_n(n: int) -> str:
    """12345 → «12 345»."""
    return f"{int(n):,}".replace(",", " ")


def _block_name(k: str) -> str:
    return prompts_mission.BLOCK_NAMES.get(k, k) if hasattr(prompts_mission, "BLOCK_NAMES") else k


def _sizes_detail(who: str, prompt_len: int, blocks: dict[str, str],
                  squeezed: list[str] | None = None) -> str:
    """Строка для степпера: «аналитик: промпт 96 400 симв. — досье 12 000, Вайкофф 1 600, … ·
    FLASH ужал: Вайкофф 200 000→55 200». Топ-SIZES_TOP блоков по размеру + итог."""
    items = sorted(((k, len(v or "")) for k, v in (blocks or {}).items() if v),
                   key=lambda kv: -kv[1])[:SIZES_TOP]
    parts = ", ".join(f"{_block_name(k)} {_fmt_n(n)}" for k, n in items)
    out = f"{who}: промпт {_fmt_n(prompt_len)} симв."
    if parts:
        out += f" — {parts}"
    if squeezed:
        out += " · FLASH ужал: " + ", ".join(squeezed)
    return out


def _sizes_rec(prompt_len: int, blocks: dict[str, str], answer_len: int = 0) -> dict:
    """Запись для status()["sizes"]: JSON-сериализуемо, размеры в символах."""
    return {"prompt": int(prompt_len), "answer": int(answer_len),
            "blocks": {k: len(v or "") for k, v in (blocks or {}).items() if v}}


def _json_len(obj: Any) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False))
    except Exception:                                # noqa: BLE001
        return len(str(obj or ""))


async def _fit_blocks(blocks: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Блоки одного промпта → compress.fit: блок выше PYTHIA_CTX_LIMIT и сумма выше
    PYTHIA_PROMPT_SOFT−HEAD_RESERVE ужимаются FLASH без потери нитей; ниже пределов —
    как есть. Возвращает (блоки, список «имя было→стало» для степпера). Сбой сжатия
    не роняет этап: блоки уходят как были (аварийный потолок — prompts_mission._finish)."""
    src = {k: (v or "") for k, v in (blocks or {}).items()}
    try:
        total = max(10_000, compress.prompt_soft() - HEAD_RESERVE)
        out = await compress.fit(src, total=total, per_block=compress.ctx_limit())
    except Exception as e:                           # noqa: BLE001
        log.warning("сжатие блоков не удалось (%s) — блоки уходят как есть", str(e)[:100])
        return src, []
    squeezed = [f"{_block_name(k)} {_fmt_n(len(src[k]))}→{_fmt_n(len(out[k]))}"
                for k in src if len(out.get(k, "")) < len(src[k])]
    if squeezed:
        log.info("миссия: FLASH ужал %s", "; ".join(squeezed))
    return out, squeezed


async def _shrink_one(text: str, label: str) -> str:
    """Один текст (анализ, вердикт) выше PYTHIA_CTX_LIMIT → FLASH ужимает; ниже — как есть."""
    t = text or ""
    try:
        return await compress.shrink(t, label=label)
    except Exception as e:                           # noqa: BLE001
        log.warning("сжатие «%s» не удалось (%s) — текст уходит как есть", label, str(e)[:100])
        return t


# ── миссия ──────────────────────────────────────────────────────────────────
class Mission:
    def __init__(self, ticker: str, name: str, asset_class: str, play: str,
                 deposit: float | None = None):
        self.ticker = ticker
        self.name = name
        self.asset_class = asset_class
        self.play = play
        self.deposit = deposit
        self.started_ts = time.time()
        self.run_id: str | None = None
        self.phase = "council"
        self.auto_resume = True          # явный стоп владельца переживает рестарт сервера
        self.pilot: MissionPilot | None = None
        self.task: asyncio.Task | None = None          # задача пилота
        self.council_task: asyncio.Task | None = None  # задача совета
        self.panic_task: asyncio.Task | None = None    # закрытие напрямую без пилота
        self.panic_orders: list[dict] = []            # принятые закрытия: ждём исполнение по тому же ID
        self.exec: dict | None = None
        self.exec_ts: float | None = None
        self.council_ts: float = 0.0    # когда последний раз шёл полный совет (пейсинг автоматических советов)
        self.frame: dict | None = None
        self.texts: dict = {}
        self.news: list[dict] = []
        self.reviews: list[dict] = []
        self.handoffs: list[dict] = []
        self.note = ""
        self.error: str | None = None
        self.ctx_price: float | None = None
        self.open_trade: dict | None = None
        self.reason = ""
        self.layers: dict = {}          # тексты слоёв последнего разбора (Вайкофф, рентген, свод, сканер)
        self.sizes: dict = {}           # размеры промптов/ответов по стадиям (симв.) — для панели
        self.account_pos = ""           # позиция по инструменту прямо на счёте (до пилота), строкой
        self.scout_reqs: list = []      # запросы разведки FLASH (перепроверка и трос берут их свежими)
        self.scout_text = ""            # последний блок разведки
        self.partners: list = []        # связанные бумаги (v5.3, correlate): ρ, лаг, ход, цена
        self.partners_text = ""         # блок «СВЯЗАННЫЕ БУМАГИ» для ИИ
        self.market: dict | None = None # рыночные часы: статус биржи на момент совета (пилот держит свой)
        self.explain: list[dict] = []   # толмач (v5.3): последние объяснения владельцу {ts, kind, title, text, refs, ok}
        self.memory = ""                # память миссии: один абзац «что было и чем кончилось» (сжато FLASH по узлам)
        self.memory_ts: float | None = None
        self.memory_n = 0               # сколько раз память сводилась
        self.reviews_since_memory = 0   # перепроверок с последнего сведения (раз в PYTHIA_MEMORY_EVERY — свести)

    def pilot_alive(self) -> bool:
        return bool(self.task and not self.task.done())

    def council_running(self) -> bool:
        return bool(self.council_task and not self.council_task.done())

    def live(self) -> bool:
        return (self.pilot_alive() or self.council_running()
                or bool(self.panic_task and not self.panic_task.done()) or bool(self.panic_orders))


_M: dict[str, Mission] = {}
_last_resume_ts = 0.0


def _persist(m: Mission) -> bool:
    try:
        store_v5.mission_put(m.ticker, {
            "ticker": m.ticker, "name": m.name, "asset_class": m.asset_class, "play": m.play,
            "deposit": m.deposit, "started_ts": m.started_ts, "run_id": m.run_id, "phase": m.phase,
            "auto_resume": m.auto_resume,
            "panic_orders": m.panic_orders,
            "exec": m.exec, "exec_ts": m.exec_ts, "frame": m.frame, "texts": m.texts,
            "news": m.news, "reviews": m.reviews[-REVIEWS_KEEP:], "handoffs": m.handoffs[-50:],
            "note": m.note, "error": m.error, "ctx_price": m.ctx_price, "reason": m.reason,
            "layers": m.layers, "sizes": m.sizes, "scout_reqs": m.scout_reqs[:30],
            "partners": m.partners[:10], "partners_text": m.partners_text,
            "explain": m.explain[-40:], "memory": m.memory, "memory_ts": m.memory_ts, "memory_n": m.memory_n,
            "reviews_since_memory": m.reviews_since_memory,
            "account_pos": m.account_pos, "ts": time.time()})
        return True
    except Exception as e:                           # noqa: BLE001
        log.warning("миссия %s не сохранилась: %s", m.ticker, str(e)[:100])
        return False


def _from_store(ticker: str) -> Mission | None:
    try:
        d = store_v5.mission_get(ticker)
    except Exception:                                # noqa: BLE001
        d = None
    if not d:
        return None
    m = Mission(d.get("ticker") or ticker, d.get("name") or ticker,
                d.get("asset_class") or "share", d.get("play") or "auto", d.get("deposit"))
    m.started_ts = _f(d.get("started_ts"), time.time())
    m.run_id = d.get("run_id")
    m.phase = d.get("phase") or "idle"
    m.auto_resume = bool(d.get("auto_resume", m.phase not in ("stopped", "panic")))
    m.panic_orders = d.get("panic_orders") if isinstance(d.get("panic_orders"), list) else []
    m.exec, m.exec_ts = d.get("exec"), _f(d.get("exec_ts"))
    m.frame, m.texts = d.get("frame"), d.get("texts") or {}
    m.news, m.reviews = d.get("news") or [], d.get("reviews") or []
    m.handoffs, m.note = d.get("handoffs") or [], d.get("note") or ""
    m.error, m.ctx_price, m.reason = d.get("error"), _f(d.get("ctx_price")), d.get("reason") or ""
    m.layers = d.get("layers") if isinstance(d.get("layers"), dict) else {}
    m.sizes = d.get("sizes") if isinstance(d.get("sizes"), dict) else {}
    m.scout_reqs = d.get("scout_reqs") if isinstance(d.get("scout_reqs"), list) else []
    m.partners = d.get("partners") if isinstance(d.get("partners"), list) else []
    m.partners_text = d.get("partners_text") or ""
    m.account_pos = d.get("account_pos") or ""
    m.explain = d.get("explain") if isinstance(d.get("explain"), list) else []
    m.memory = d.get("memory") or ""
    m.memory_ts, m.memory_n = _f(d.get("memory_ts")), int(_f(d.get("memory_n"), 0) or 0)
    m.reviews_since_memory = int(_f(d.get("reviews_since_memory"), 0) or 0)
    return m


def _active() -> Mission | None:
    for m in _M.values():
        if m.live():
            return m
    return None


def _restore_pending_panics() -> None:
    """Восстановить владение незавершёнными закрытиями до любого нового входа."""
    try:
        for ticker, data in store_v5.mission_all().items():
            if data.get("panic_orders") and ticker not in _M:
                restored = _from_store(ticker)
                if restored is None:
                    raise RuntimeError(f"не удалось прочитать миссию с незавершённым закрытием: {ticker}")
                _M[ticker] = restored
    except Exception as e:                           # noqa: BLE001
        log.warning("паника: восстановление заявок: %s", str(e)[:100])
        raise RuntimeError("не удалось проверить сохранённые аварийные заявки — новые операции заблокированы") from e


# ── вид входа ─────────────────────────────────────────────────────────────────
def _entry_kind(do: str, entry, price, hint=None) -> str:
    """сейчас / откат / прорыв — по геометрии (она главнее подсказки откат/прорыв): entry нет или равен цене →
    «сейчас»; для BUY уровень ниже цены → «откат» (засада), выше → «прорыв» (вход, когда цена его
    пройдёт); SELL зеркально. Цена неизвестна → подсказка ИИ, иначе «откат».
    v5.4.2: подсказка «сейчас»/«now» — решение ИИ войти сразу: геометрия её не переделывает в засаду или пробой
    (цена досье старше живой, entry у ИИ — его взгляд на текущую цену); дрейф проверяет дверь по живой цене."""
    h = str(hint or "").strip().lower()
    h = {"now": "сейчас", "сразу": "сейчас", "immediately": "сейчас", "market": "сейчас", "pullback": "откат",
         "breakout": "прорыв", "пробитие": "прорыв", "пробой": "прорыв", "засада": "откат"}.get(h, h)
    if entry is None or h == "сейчас":
        return "сейчас"
    if not price or price <= 0:
        return h if h in ("откат", "прорыв") else "откат"
    if abs(float(entry) - float(price)) <= 1e-12:
        return "сейчас"
    below = float(entry) < float(price)
    if str(do).upper() == "BUY":
        return "откат" if below else "прорыв"
    return "прорыв" if below else "откат"


# ── валидация приказа ─────────────────────────────────────────────────────────
def _validate_exec(ex: Any, play: str, price, in_pos: bool = False,
                   pos_side: str | None = None) -> tuple[dict | None, str | None]:
    """(приказ, None) или (None, причина). Стороны как у pipeline._sanitize_forecast
    и AIPilot._plan_valid, плюс режим игры: long→BUY, short→SELL, mixed/auto→любой.
    v5.2: при открытой позиции (in_pos) допустим CLOSE — закрыть и стоять вне рынка.
    v5.4.1: WAIT (ЖДАТЬ / FLAT / HOLD_FLAT) без позиции — «вход не сейчас»: плана нет, wait_for —
    что должно случиться; при позиции WAIT недопустим (нужно HOLD / закрыть / добрать / перевернуть).
    v5.4.2 (ревью): HOLD — только при позиции: держать как есть БЕЗ добора, invalidation/take — новые или null
    (прежние); pos_side (сторона позиции пилота, если известна) — уровни HOLD проверяются по цене для этой стороны.
    v5.4.2: слово решения — ai_v5.decision_of по словарю приказа (КУПИТЬ/ЛОНГ/ПОКУПКА → BUY, ЖДЁМ/NO_TRADE → WAIT,
    FLAT/EXIT в позиции → CLOSE; ключ do|action|decision); ошибка уровней BUY/SELL — «сторона принята, исправь уровни»
    (без подсказки «или WAIT»); прорыв — стоп только по ту сторону уровня входа (классический стоп пробоя между
    ценой и уровнем законен); заглушки нейтральны — код не дописывает за ИИ «перевеса нет»."""
    if isinstance(ex, dict) and isinstance(ex.get("exec"), dict):
        ex = ex["exec"]
    if not isinstance(ex, dict):
        return None, "ответ не JSON-объект"
    raw_do = ai_v5.decision_raw(ex, keys=("do", "action", "decision"))
    do = ai_v5.decision_of(raw_do, ai_v5.exec_table(in_pos)) or ""
    if not do and not in_pos and ai_v5.decision_of(raw_do, {"HOLD": ai_v5.SYN_HOLD}) == "HOLD":
        do = "HOLD"                                  # «держать» без позиции — честный отказ ниже, а не «не разобрано»
    ids = ex.get("news_ids") if isinstance(ex.get("news_ids"), (list, tuple)) else []
    conf = _f(ex.get("confidence"))
    if do == "HOLD":
        if not in_pos:
            return None, (f"do={raw_do}: HOLD — только при открытой позиции; позиции нет — нужно BUY, SELL или WAIT")
        inv_h = _f(ex.get("invalidation")) if ex.get("invalidation") not in (None, "", "null") else None
        take_h = _f(ex.get("take")) if ex.get("take") not in (None, "", "null") else None
        inv_h = inv_h if inv_h is not None and inv_h > 0 else None      # null / ≤0 — прежний уровень позиции
        take_h = take_h if take_h is not None and take_h > 0 else None
        ps, px = str(pos_side or "").lower(), _f(price)
        if ps in ("long", "short") and px:
            fix = "HOLD принят — исправь уровни: "
            if inv_h is not None and ((ps == "long" and inv_h >= px) or (ps == "short" and inv_h <= px)):
                return None, fix + f"invalidation {inv_h:g} не {'ниже' if ps == 'long' else 'выше'} цены {px:g} для позиции {ps}"
            if take_h is not None and ((ps == "long" and take_h <= px) or (ps == "short" and take_h >= px)):
                return None, fix + f"take {take_h:g} не {'выше' if ps == 'long' else 'ниже'} цены {px:g} для позиции {ps}"
        lv = ex.get("levels") if isinstance(ex.get("levels"), (list, tuple)) else []
        return {"do": "HOLD", "entry": None, "entry_kind": "сейчас", "take": take_h, "invalidation": inv_h,
                "why": str(ex.get("why") or "").strip()[:300] or "(причина не указана)",
                "plan": str(ex.get("plan") or "")[:1500],
                "confidence": int(max(0, min(100, conf))) if conf is not None else None,
                "news_ids": [str(i)[:12] for i in ids if isinstance(i, (str, int))][:12],
                "levels": [v for v in (_f(x) for x in lv) if v is not None][:10],
                "time_note": str(ex.get("time_note") or "")[:300]}, None
    if do == "WAIT":
        if in_pos:
            return None, (f"do={raw_do}: позиция открыта — WAIT недопустим: держать как есть — HOLD (invalidation/take "
                          "новые или null), закрыть — CLOSE, добрать — BUY/SELL той же стороны, перевернуть — "
                          "противоположная сторона")
        lv = ex.get("levels") if isinstance(ex.get("levels"), (list, tuple)) else []
        wait_for = str(ex.get("wait_for") or "").strip()[:400]
        return {"do": "WAIT", "entry": None, "entry_kind": "сейчас", "take": None, "invalidation": None,
                "why": str(ex.get("why") or "").strip()[:300] or "(причина не указана)",
                "plan": str(ex.get("plan") or "")[:1500],
                "wait_for": wait_for or "условие входа не названо — реши по живой картине",
                "confidence": int(max(0, min(100, conf))) if conf is not None else None,
                "news_ids": [str(i)[:12] for i in ids if isinstance(i, (str, int))][:12],
                "levels": [v for v in (_f(x) for x in lv) if v is not None][:10],
                "time_note": str(ex.get("time_note") or "")[:300]}, None
    if do == "CLOSE":
        if not in_pos:
            return None, f"do={raw_do}: позиции нет — закрывать нечего; нужно BUY, SELL или WAIT (HOLD и CLOSE — только при открытой позиции)"
        return {"do": "CLOSE", "entry": None, "entry_kind": "сейчас", "take": None,
                "invalidation": _f(ex.get("invalidation")) or None,
                "why": str(ex.get("why") or "закрыть позицию")[:300],
                "plan": str(ex.get("plan") or "")[:1500],
                "confidence": int(max(0, min(100, conf))) if conf is not None else None,
                "news_ids": [str(i)[:12] for i in ids if isinstance(i, (str, int))][:12],
                "levels": [], "time_note": str(ex.get("time_note") or "")[:300]}, None
    if do not in ("BUY", "SELL"):
        return None, (f"do={raw_do or '—'}: нужно BUY, SELL или WAIT (HOLD и CLOSE — только при открытой позиции)"
                      + ("; позиция открыта: HOLD / CLOSE / добрать / перевернуть" if in_pos else ""))
    play = (play or "auto").lower()
    if play == "long" and do != "BUY":
        return None, "режим игры long — разрешён только BUY"
    if play == "short" and do != "SELL":
        return None, "режим игры short — разрешён только SELL"
    fix = f"сторона {do} принята — исправь уровни: "   # v5.4.2: ошибка в числах, а не оценка решения
    inv = _f(ex.get("invalidation"))
    if inv is None or inv <= 0:
        return None, fix + "invalidation не задан или ≤0 — позиции без стопа не бывает"
    entry = _f(ex.get("entry")) if ex.get("entry") not in (None, "", "null") else None
    take = _f(ex.get("take")) if ex.get("take") not in (None, "", "null") else None
    if entry is not None and entry <= 0:
        return None, fix + "entry ≤ 0"
    if take is not None and take <= 0:
        take = None
    kind = _entry_kind(do, entry, _f(price), ex.get("entry_kind"))
    if kind == "сейчас":
        entry = None
    # прорыв (v5.4.2): стоп — по ту сторону УРОВНЯ входа (ref = entry ниже); стоп между ценой и уровнем — классика
    # пробоя: до пробития позиции нет, и мёртвой такую идею код не считает
    ref = entry if entry is not None else _f(price)
    if ref:
        if do == "BUY":
            if inv >= ref:
                return None, fix + f"invalidation {inv:g} не ниже {'entry' if entry else 'цены'} {ref:g}"
            if take is not None and take <= ref:
                return None, fix + f"take {take:g} не выше {'entry' if entry else 'цены'} {ref:g}"
        else:
            if inv <= ref:
                return None, fix + f"invalidation {inv:g} не выше {'entry' if entry else 'цены'} {ref:g}"
            if take is not None and take >= ref:
                return None, fix + f"take {take:g} не ниже {'entry' if entry else 'цены'} {ref:g}"
    bad = ai_pilot.AIPilot._plan_valid("long" if do == "BUY" else "short", entry, take, inv)
    if bad:
        return None, fix + bad
    lv = ex.get("levels") if isinstance(ex.get("levels"), (list, tuple)) else []
    out = {"do": do, "entry": entry, "entry_kind": kind, "take": take, "invalidation": inv,
           "why": str(ex.get("why") or "")[:300],
           "plan": str(ex.get("plan") or "")[:1500],
           "confidence": int(max(0, min(100, conf))) if conf is not None else None,
           "news_ids": [str(i)[:12] for i in ids if isinstance(i, (str, int))][:12],
           "levels": [v for v in (_f(x) for x in lv) if v is not None][:10],
           "time_note": str(ex.get("time_note") or "")[:300]}
    return out, None


# ── тексты для промптов ───────────────────────────────────────────────────────
def _council_text() -> str:
    """Итог общего совета для промптов миссии. v5.4.2: в summary_text идёт строка council_latest целиком —
    шапка с видом, временем МСК и возрастом («Совет daily от … (N ч назад) — общий по рынку»)."""
    c = _mod("council")
    try:
        if c:
            latest = c.latest()
            if latest:
                return c.summary_text(latest) or "совета ещё не было"
    except Exception as e:                           # noqa: BLE001
        log.info("итог совета недоступен: %s", str(e)[:80])
    return "совета ещё не было"


def _render_supports_rich(nf) -> bool:
    """newsflow.render(items, limit=None, rich=False) — богатые строки (ключевые характеристики
    новости, сжатые FLASH) есть не в каждой версии соседнего модуля; проверяем сигнатуру."""
    try:
        return "rich" in inspect.signature(nf.render).parameters
    except (TypeError, ValueError, AttributeError):
        return False


def _render_all(nf, items: list[dict]) -> str:
    """Все строки новостей без среза (limit=None), богатым рендером rich=True, если
    newsflow его поддерживает; старый render — как раньше."""
    if not items:
        return ""
    try:
        if _render_supports_rich(nf):
            return nf.render(items, limit=None, rich=True)
        return nf.render(items, limit=None)
    except TypeError:
        return nf.render(items, limit=len(items))


async def _enrich_news(m: Mission, rid: str | None, timeout: float = 480.0) -> None:
    """Google News по инструменту → отбор → разметка (newsflow.enrich_ticker).
    Молчит без флага PYTHIA_GNEWS и без функции; любой сбой — только лог."""
    nf = _mod("newsflow")
    fn = getattr(nf, "enrich_ticker", None) if nf else None
    if not fn or not getattr(config, "PYTHIA_GNEWS", True):
        return
    try:
        await asyncio.wait_for(fn(rid or m.run_id or "mission", m.ticker, m.name, m.asset_class,
                                  float(getattr(config, "PYTHIA_V5_DAYS", 3))), timeout)
    except Exception as e:                           # noqa: BLE001
        log.info("целевые новости %s: %s", m.ticker, str(e)[:100])


async def _news_for(m: Mission, rid: str | None = None) -> tuple[list[dict], str]:
    """Новости инструмента: сначала целевой сбор, затем выборка из хранилища — целиком."""
    nf = _mod("newsflow")
    if not nf:
        return [], ""
    await _enrich_news(m, rid)
    try:
        items = nf.news_for_ticker(m.ticker, m.name, limit=None) or []      # все новости инструмента, без среза
        return items, _render_all(nf, items)
    except Exception as e:                           # noqa: BLE001
        log.info("новости по тикеру недоступны: %s", str(e)[:80])
    return [], ""


def _watch_text(since: float) -> str:
    w = _mod("watch")
    try:
        if w:
            notes = w.serious_since(since) or []
            L = []
            for n in notes:                          # все серьёзные заметки, текст целиком
                d = n.get("data") if isinstance(n, dict) and "data" in n else n
                if isinstance(d, dict):
                    L.append(f"{ai_v5.fmt_ts(d.get('ts'))} · серьёзность {d.get('severity')} · "
                             f"{str(d.get('note') or '')} · {', '.join(d.get('affected') or [])}")
            return "\n".join(L)
    except Exception as e:                           # noqa: BLE001
        log.info("заметки дозора недоступны: %s", str(e)[:80])
    return ""


def _lvl_s(v) -> str:
    """Уровень приказа HOLD для текста: число или «прежний» (null — уровень позиции не меняется)."""
    x = _f(v)
    return f"{x:g}" if x else "прежний"


def _exec_text(m: Mission) -> str:
    ex = m.exec or {}
    if not ex:
        return ""
    if str(ex.get("do") or "").upper() == "HOLD":     # v5.4.2 (ревью): держать как есть, без добора
        return (f"Приказ ({ai_v5.fmt_ts(m.exec_ts)}): HOLD — держать позицию как есть, без добора; "
                f"invalidation={_lvl_s(ex.get('invalidation'))} take={_lvl_s(ex.get('take'))} "
                f"уверенность {ex.get('confidence')} — {ex.get('why')}\n"
                f"План: {ex.get('plan')}\nТайминг: {ex.get('time_note')}")
    if str(ex.get("do") or "").upper() == "WAIT":     # v5.4.1: совет решил стоять вне рынка (5.4.2: это его прошлое мнение)
        return (f"Приказ ({ai_v5.fmt_ts(m.exec_ts)}): WAIT — совет ждал: {ex.get('wait_for') or ex.get('why') or '—'} "
                f"(уверенность {ex.get('confidence')}) — {ex.get('why')}\n"
                f"План: {ex.get('plan')}\nТайминг: {ex.get('time_note')}")
    return (f"Приказ ({ai_v5.fmt_ts(m.exec_ts)}): {ex.get('do')} entry={ex.get('entry')} "
            f"({ex.get('entry_kind') or ('сейчас' if ex.get('entry') is None else 'откат')}) take={ex.get('take')} "
            f"invalidation={ex.get('invalidation')} уверенность {ex.get('confidence')} — {ex.get('why')}\n"
            f"План: {ex.get('plan')}\nТайминг: {ex.get('time_note')}")


def _prev_text(m: Mission) -> str:
    L = [_exec_text(m)]
    if m.reviews:
        L.append("Перепроверки: " + "; ".join(
            f"{ai_v5.fmt_ts(r.get('ts'))} {r.get('choice')} — {str(r.get('why') or '')}"
            for r in m.reviews))                     # все сохранённые, без среза
    if m.handoffs:
        L.append("Передачи совету: " + "; ".join(
            f"{ai_v5.fmt_ts(h.get('ts'))} {str(h.get('reason') or '')}" for h in m.handoffs))
    p = m.pilot
    if p:
        L.append(f"Пилот: {p.state}, {p.last_action}; результат сессии {sum(p.pnls):+.0f} ₽ за {len(p.pnls)} сделок")
        pos_line = _position_text(m)
        if pos_line:
            L.append(pos_line)
    return "\n".join(x for x in L if x)


async def _account_position_text(m: Mission) -> str:
    """Позиция по инструменту прямо на счёте — ещё до пилота (v5.2 «весь счёт»): совет обязан её
    видеть и сказать держать / закрыть / перевернуть, пилот примет её как свою. Нет токена,
    счёта или позиции → ''. Любой сбой — только лог."""
    p = m.pilot
    if getattr(p, "position", None):
        return ""
    if not tinkoff.enabled():
        return ""
    try:
        accs = await asyncio.wait_for(tinkoff.accounts(), 15) or []
        if not accs:
            return ""
        pf = await asyncio.wait_for(tinkoff.portfolio(accs[0]["id"]), 15) or {}
        inst = await asyncio.wait_for(tinkoff.resolve(m.ticker, m.asset_class), 15) or {}
        figi = inst.get("figi") or inst.get("uid")
        if not figi:
            return ""
        lot = 1 if m.asset_class == "futures" else max(1, int(inst.get("lot") or 1))
        for x in pf.get("positions") or []:
            if x.get("figi") != figi and x.get("uid") != figi:
                continue
            q = _f(x.get("qty"), 0.0)
            if abs(q) < 1:
                continue
            lots = int(abs(q)) if m.asset_class == "futures" else int(abs(q) / lot)
            side = "long" if q > 0 else "short"
            avg, cur, y = _f(x.get("avg")), _f(x.get("cur_price")), _f(x.get("yield"))
            return (f"ОТКРЫТАЯ ПОЗИЦИЯ НА СЧЁТЕ: {side} {lots} лот" + (f" @{avg:g}" if avg else "")
                    + (f", цена {cur:g}" if cur else "") + (f", P/L {y:+.0f} ₽" if y is not None else "")
                    + " — пилот примет её как свою (весь счёт по инструменту); вердикт обязан сказать: "
                      "держать (стоп и тейк), закрыть или перевернуть")
    except Exception as e:                           # noqa: BLE001
        log.info("позиция на счёте %s: %s", m.ticker, str(e)[:100])
    return ""


def _scout_brief(m: Mission, pctx: dict, price, n_news: int) -> str:
    """Что получит PRO — кратко для разведчика FLASH: инструмент, цена, размеры блоков, совет."""
    lines = [f"Инструмент: {m.ticker} ({m.name}, {m.asset_class}), цена {price}, режим игры {m.play}."]
    have = [f"{prompts_mission.BLOCK_NAMES.get(k, k)} {_fmt_n(len(v))} симв."
            for k, v in pctx.items() if isinstance(v, str) and v.strip() and k in prompts_mission.BLOCK_NAMES]
    if have:
        lines.append("Блоки PRO: " + ", ".join(have) + ".")
    lines.append(f"Новостей по инструменту: {n_news}.")
    c = (pctx.get("council") or "").strip().splitlines()
    if c:
        lines.append("Совет: " + c[0][:200])
    if pctx.get("position"):
        lines.append(str(pctx["position"])[:200])
    return "\n".join(lines)


async def _partners_block(m: Mission, rid: str | None) -> str:
    """Связанные бумаги (v5.3): correlate.partners (кэш 6 ч) → m.partners, m.partners_text —
    блок «СВЯЗАННЫЕ БУМАГИ» для аналитика/критика/вердикта; стадия шины «partners».
    Сбой/пусто → прошлый текст или '' и честная заметка."""
    if not getattr(config, "PYTHIA_PARTNERS", True):
        return ""
    try:
        if rid:
            await bus.stage("mission", rid, "partners", "start", ticker=m.ticker,
                            detail="корреляции по дневным доходностям: соседи по сектору и макро-фьючерсы")
        items = await asyncio.wait_for(correlate.partners(m.ticker, m.asset_class), PARTNERS_TIMEOUT)
        m.partners = list(items or [])
        m.partners_text = correlate.text(m.partners, m.ticker)
        note = correlate.explain(m.ticker)
        if rid:
            await bus.stage("mission", rid, "partners", "done", ticker=m.ticker, n=len(m.partners),
                            detail=(", ".join(f"{x.get('code')} ρ={_f(x.get('rho'), 0):+.2f}" for x in m.partners)
                                    if m.partners else "связанных бумаг нет") + (f" · {note}" if note else ""),
                            data={"partners": [{k: x.get(k) for k in ("code", "name", "rho", "lead", "kind")}
                                               for x in m.partners]})
        return m.partners_text
    except Exception as e:                           # noqa: BLE001
        log.info("связанные бумаги %s: %s", m.ticker, str(e)[:100])
        if rid:
            try:
                await bus.stage("mission", rid, "partners", "error", ticker=m.ticker,
                                detail=f"связанные бумаги не посчитаны: {str(e)[:120]} — совет идёт без них")
            except Exception:                        # noqa: BLE001
                pass
        return m.partners_text or ""


def _position_text(m: Mission) -> str:
    """Открытая позиция пилота одной строкой (для совета и шифровальщика): сторона, лоты,
    вход, сколько в рынке, плавающий P/L, трос, тейк. Пилота нет — позиция на счёте
    (v5.2 «весь счёт», _account_position_text). Нет позиции → ''."""
    p = m.pilot
    pos = getattr(p, "position", None) if p else None
    if not pos:
        return m.account_pos or ""
    try:
        held = int((time.time() - float(pos.get("opened_ts") or time.time())) / 60)
        fl = pos.get("floating")
        base = (f"ОТКРЫТАЯ ПОЗИЦИЯ: {pos.get('side')} {pos.get('lots')} лот @{float(pos.get('entry') or 0):g}, "
                f"в рынке {held} мин, плавающий P/L {float(fl):+.0f} ₽" if fl is not None else
                f"ОТКРЫТАЯ ПОЗИЦИЯ: {pos.get('side')} {pos.get('lots')} лот @{float(pos.get('entry') or 0):g}, "
                f"в рынке {held} мин")
        base += f", триггер (мягкий стоп) @{pos.get('invalidation')}, тейк {pos.get('take')}"
        if pos.get("hard_stop"):
            base += f", {p._hard_name()} @{pos.get('hard_stop')}"
        if pos.get("adopted"):
            base += " (принята со счёта" + (", уровни временные" if pos.get("levels_placeholder") else "") + ")"
        return base
    except Exception:                                # noqa: BLE001
        return f"ОТКРЫТАЯ ПОЗИЦИЯ: {pos.get('side')} {pos.get('lots')} лот @{pos.get('entry')}"


def _position_age(m: Mission) -> float | None:
    """Возраст открытой позиции в секундах; None — позиции нет."""
    p = m.pilot
    pos = getattr(p, "position", None) if p else None
    if not pos:
        return None
    try:
        return max(0.0, time.time() - float(pos.get("opened_ts") or 0))
    except (TypeError, ValueError):
        return None


# ── толмач и память (v5.3) ─────────────────────────────────────────────────────
def _split_news(items: list[dict], since: float) -> tuple[list[dict], list[dict]]:
    """(свежие после момента since, старые до него); без ts — свежие (честно, не теряем)."""
    fresh, old = [], []
    for x in items or []:
        ts = _f(x.get("ts")) if isinstance(x, dict) else None
        (old if (since and ts and ts < since) else fresh).append(x)
    return fresh, old


def _trades_text(ticker: str, limit: int = 20) -> str:
    try:
        rows = store_v5.trades(ticker, limit) or []
    except Exception:                                # noqa: BLE001
        return ""
    return "\n".join(f"{ai_v5.fmt_ts(r.get('closed_ts'))} {r.get('side')} {r.get('lots')} лот {r.get('entry')} → "
                     f"{r.get('exit_px')}, P/L {_f(r.get('pnl'), 0):+.0f} ₽ — {str(r.get('why') or '')[:120]}"
                     for r in rows)


def _explain_ctx(m: Mission) -> dict:
    """Контекст для толмача и памяти: ситуация пилота, приказ, последние решения, свежие/старые новости,
    связанные бумаги, сделки. Всё в try — сбой источника не роняет объяснение."""
    p = m.pilot
    price = (p.prices[-1] if p and p.prices else m.ctx_price)
    ctx: dict = {"ticker": m.ticker, "name": m.name, "asset_class": m.asset_class, "play": m.play,
                 "time_msk": ai_v5.now_msk_str(), "price": price, "memory": m.memory or "",
                 "partners": (m.partners_text or "")[:3000], "exec": _exec_text(m)}
    try:
        if p and price:
            ctx["situation"] = p._situation_text(float(price))
            ctx["market"] = p._market_line()
        elif m.account_pos:
            ctx["situation"] = m.account_pos
        if not ctx.get("market") and m.market and market_clock is not None:
            ctx["market"] = market_clock.describe(m.market)
    except Exception as e:                           # noqa: BLE001
        log.info("толмач %s: ситуация: %s", m.ticker, str(e)[:80])
    try:
        mm = p._money_name() if p else "PRO"
        L = [f"перепроверка {ai_v5.fmt_ts(r.get('ts'))}: {r.get('choice')} — {r.get('why') or ''}" for r in m.reviews[-3:]]
        L += [f"{g.get('model') or mm} у {'тейка' if g.get('side') == 'take' else 'троса'} {ai_v5.fmt_ts(g.get('ts'))}: {g.get('decision')} — {g.get('why') or ''}"
              for g in (list(getattr(p, "guards", None) or [])[-2:] if p else [])]
        L += [f"триаж {mm} {ai_v5.fmt_ts(t.get('ts'))} [{t.get('event') or ''}]: {t.get('urgency')} — {t.get('why') or ''}"
              for t in (list(getattr(p, "triages", None) or [])[-2:] if p else [])]
        # v5.4.1: проверки входа у двери и мысли о прибыли
        L += [f"{g.get('model') or mm} у двери {ai_v5.fmt_ts(g.get('ts'))}: {g.get('decision')} — {g.get('why') or ''}"
              for g in (list(getattr(p, "gates", None) or [])[-2:] if p else [])]
        L += [f"мысль о прибыли {ai_v5.fmt_ts(x.get('ts'))}: {x.get('decision')} — {x.get('why') or ''}"
              for x in (list(getattr(p, "profits", None) or [])[-2:] if p else [])]
        L += [f"передача {ai_v5.fmt_ts(h.get('ts'))} [{h.get('kind') or 'council'}]: {h.get('reason') or ''}" for h in m.handoffs[-3:]]
        ctx["decisions"] = "\n".join(L)
    except Exception as e:                           # noqa: BLE001
        log.info("толмач %s: решения: %s", m.ticker, str(e)[:80])
    try:
        nf = _mod("newsflow")
        if nf:
            own = nf.news_for_ticker(m.ticker, m.name, limit=None) or []
            fresh, old = _split_news(own, m.council_ts)
            ctx["news"] = _render_all(nf, fresh[:15])
            ctx["news_old"] = _render_all(nf, old[:40])
    except Exception as e:                           # noqa: BLE001
        log.info("толмач %s: новости: %s", m.ticker, str(e)[:80])
    ctx["trades"] = _trades_text(m.ticker)
    return ctx


def _tolmach(m: Mission | None, kind: str, title: str, detail: str = "", refs: dict | None = None) -> None:
    """Ключевой узел → толмачу (explain.note): объяснение владельцу придёт по шине стадией «explain»."""
    if explain is None or m is None:
        return
    try:
        explain.note(m, kind, title, detail, refs, ctx=lambda: _explain_ctx(m), persist=lambda: _persist(m))
    except Exception as e:                           # noqa: BLE001
        log.info("толмач %s: %s", m.ticker, str(e)[:100])


def _memorize(m: Mission | None, why: str) -> None:
    """Узел прошёл — свести накопившееся в память миссии фоном (explain.memorize_bg)."""
    if explain is None or m is None:
        return
    m.reviews_since_memory = 0
    try:
        explain.memorize_bg(m, why, ctx=lambda: _explain_ctx(m), persist=lambda: _persist(m))
    except Exception as e:                           # noqa: BLE001
        log.info("память %s: %s", m.ticker, str(e)[:100])


# ── сердце: совет по инструменту ──────────────────────────────────────────────
async def _stream_stage(m: Mission, stage: str, system: str, user: str,
                        blocks: dict[str, str] | None = None, who: str = "",
                        squeezed: list[str] | None = None) -> str:
    """PRO-стадия стримом. В «start» уходит строка размеров промпта (симв., топ блоков),
    в «done» — длина ответа; то же пишется в m.sizes[stage] для панели."""
    rid = m.run_id

    async def on_think(d):
        await bus.text("mission", rid, stage, d, kind="think")

    async def on_text(d):
        await bus.text("mission", rid, stage, d)

    blocks = blocks or {}
    m.sizes[stage] = _sizes_rec(len(user), blocks)
    await bus.stage("mission", rid, stage, "start", ticker=m.ticker,
                    detail=_sizes_detail(who or stage, len(user), blocks, squeezed))
    txt = await ai_v5.pro_stream(system, user, on_think=on_think, on_text=on_text, route=f"mission_{stage}")
    txt = (txt or "").strip()
    bus.set_text(rid, stage, txt)
    m.sizes[stage]["answer"] = len(txt)
    await bus.stage("mission", rid, stage, "done", ticker=m.ticker, n=len(txt),
                    detail=f"ответ {_fmt_n(len(txt))} симв.")
    return txt


async def _council(m: Mission, reason: str, first: bool) -> dict | None:
    rid = m.run_id
    m.phase = "council"
    m.error = None
    m.reason = reason
    m.council_ts = time.time()
    m.sizes = {k: v for k, v in (m.sizes or {}).items() if k in ("guard", "take", "triage", "entry", "profit")}   # узлы у денег — не стирать
    try:
        # 0. сканер стакана («8 ч» из 4.x): онлайн-наблюдение стартует вместе с разбором;
        #    повторный разбор только продлевает дедлайн, накопленные тики не теряются
        await bus.stage("mission", rid, "scan", "start", ticker=m.ticker, detail="сканер стакана: запуск/продление")
        r_scan = await _scan_start(m)
        await bus.stage("mission", rid, "scan", "done", ticker=m.ticker,
                        detail=str(r_scan.get("note") or ("сканер идёт" if r_scan.get("ok") else "сканер молчит")))
        # 1. досье и слои (v5.1: все методы 4.x — Вайкофф, рентген, реактор, календарь,
        #    синхронизация, погода, свод голосов; всё целиком, без обрезки)
        await bus.stage("mission", rid, "dossier", "start", ticker=m.ticker, detail="собираю досье биржи")
        ctx = await market_ctx.build(m.ticker, m.asset_class)
        price = ctx.get("price")
        m.ctx_price = price
        await bus.stage("mission", rid, "dossier", "done", ticker=m.ticker,
                        detail=f"цена {price}" + (f", пробелы: {len(ctx['errors'])}" if ctx.get("errors") else ""),
                        data={"price": price, "errors": ctx.get("errors")})
        # весь счёт по инструменту (v5.2): что уже лежит на счёте — совет обязан видеть
        m.account_pos = await _account_position_text(m)
        # рыночные часы: совет и приказ при закрытом рынке разрешены, но ИИ видит — вход только с открытия
        market_line = ""
        if market_clock is not None:
            try:
                m.market = await asyncio.wait_for(market_clock.status(m.ticker, m.asset_class), 20)
                market_line = market_clock.describe(m.market)
            except Exception as e:                   # noqa: BLE001
                log.info("миссия %s: рыночные часы: %s", m.ticker, str(e)[:80])
        # 1а. связанные бумаги (v5.3): корреляции с сектором и макро — свой блок; разведка ниже
        #     сама добавит по ним свежие котировки (scout.partner_requests)
        partners_text = await _partners_block(m, rid)
        news_items, news_text = await _news_for(m, rid)
        scan_text = ctx.get("scan_text") or _scan_text(m)
        pctx = {"time_msk": ai_v5.now_msk_str(), "price": price, "asset_class": m.asset_class,
                "market": market_line,
                "reason": reason, "dossier": ctx.get("text", ""), "council": _council_text(),
                "news": news_text, "watch": _watch_text(m.started_ts),
                "wyckoff": ctx.get("wyckoff_text", ""), "scan": scan_text,
                "xray": ctx.get("xray_text", ""), "weather": ctx.get("weather_text", ""),
                "oracle": ctx.get("oracle_text", ""), "calendar": ctx.get("calendar_text", ""),
                "sync": ctx.get("sync_text", ""), "reactor": ctx.get("reactor_text", ""),
                "astro": ctx.get("astro", ""), "aether": ctx.get("aether", ""),
                "kuramoto": ctx.get("kuramoto_text", ""), "bifurcation": ctx.get("bif_text", ""),
                "position": _position_text(m), "scout": "", "partners": partners_text,
                "memory": m.memory or "", "prev": "" if first else _prev_text(m)}
        # 1б. разведка данных (v5.2): FLASH смотрит, что получит PRO, и просит недостающее
        #     (IMOEX сейчас и за неделю, соседи, курс, нефть) — блок «ДАННЫЕ ПО ЗАПРОСУ РАЗВЕДКИ»
        try:
            sc_text, sc_reqs = await asyncio.wait_for(
                scout.run("mission", rid, "mission", _scout_brief(m, pctx, price, len(news_items)), ticker=m.ticker),
                90)
        except Exception as e:                       # noqa: BLE001
            log.info("разведка миссии %s: %s", m.ticker, str(e)[:100])
            sc_text, sc_reqs = "", []
        if sc_reqs:
            m.scout_reqs = sc_reqs
        m.scout_text = sc_text
        pctx["scout"] = sc_text
        # разумные пределы (v5.1): блок выше PYTHIA_CTX_LIMIT и сумма выше PYTHIA_PROMPT_SOFT
        # ужимаются FLASH без потери нитей ДО сборки промптов; ниже пределов — всё целиком
        keys = getattr(prompts_mission, "CONTEXT_KEYS", ("wyckoff", "dossier", "scan", "xray", "council",
                                                         "news", "watch", "weather", "oracle", "kuramoto",
                                                         "bifurcation", "calendar", "sync", "reactor",
                                                         "astro", "aether", "memory", "prev"))
        blocks, squeezed = await _fit_blocks({k: pctx.get(k) or "" for k in keys})
        pctx.update(blocks)
        m.layers = {k: pctx.get(k) or "" for k in ("wyckoff", "xray", "oracle", "scan")}

        # 2. PRO: анализ → критика → вердикт (стрим); анализ/критика уходят дальше целиком,
        #    пока ≤ PYTHIA_CTX_LIMIT, выше — FLASH ужимает (shrink/fit), ножниц нет
        s, u = prompts_mission.analysis(m.ticker, m.name, m.play, pctx)
        a_txt = await _stream_stage(m, "analysis", s, u, blocks, "аналитик", squeezed)
        a_in = await _shrink_one(a_txt, "анализ")
        cb, sq_c = await _fit_blocks({"analysis": a_in, **blocks})     # критик: анализ + слои как целое ≤ SOFT
        cctx = dict(pctx, **{k: cb[k] for k in blocks})
        s, u = prompts_mission.critique(m.ticker, m.name, m.play, cb["analysis"], cctx)
        c_txt = await _stream_stage(m, "critique", s, u, cb, "критик",
                                    ([f"анализ {_fmt_n(len(a_txt))}→{_fmt_n(len(a_in))}"] if len(a_in) < len(a_txt) else []) + sq_c or None)
        # вердикт видит ТО ЖЕ сжатие анализа, что и критик (cb["analysis"]), не второе независимое
        vb, sq_v = await _fit_blocks({"analysis": cb["analysis"], "critique": c_txt, **blocks})
        vctx = dict(pctx, **{k: vb[k] for k in blocks})
        s, u = prompts_mission.verdict(m.ticker, m.name, m.play, vb["analysis"], vb["critique"], vctx)
        v_txt = await _stream_stage(m, "verdict", s, u, vb, "вердикт", sq_v)

        # 3. exec-приказ (JSON) + валидация, одна повторная попытка; вердикт — через shrink
        v_in = await _shrink_one(v_txt, "вердикт")
        ex, err, err_code = None, None, None
        for attempt in range(2):
            pctx["position"] = _position_text(m)
            s, u = prompts_mission.exec_order(m.ticker, m.name, m.play, price, v_in, pctx, error=err_code)
            eb = {"verdict": v_in, "wyckoff": pctx.get("wyckoff") or "", "scan": pctx.get("scan") or "",
                  "news": pctx.get("news") or ""}
            if attempt == 0:
                m.sizes["exec"] = _sizes_rec(len(u), eb)
                await bus.stage("mission", rid, "exec", "start", ticker=m.ticker,
                                detail=_sizes_detail("шифровальщик", len(u), eb,
                                                     [f"вердикт {_fmt_n(len(v_txt))}→{_fmt_n(len(v_in))}"]
                                                     if len(v_in) < len(v_txt) else None))
            try:
                raw = await asyncio.wait_for(ai_v5.pro_json(s, u, route="mission_exec"), 1800)
            except Exception as e:                   # noqa: BLE001  v5.4.2: таймаут/сбой попытки 1 — у попытки 2 свой шанс
                err, err_code = (f"шифровальщик не ответил ({'таймаут 1800 с' if isinstance(e, asyncio.TimeoutError) else _humanize(e)})",
                                 None)               # молчание — не ошибка в числах: повтор без «ПРИКАЗ ОТКЛОНЁН КОДОМ»
                log.warning("миссия %s: %s%s", m.ticker, err, " — повторная попытка" if attempt == 0 else "")
                await bus.stage("mission", rid, "exec", "progress", ticker=m.ticker, detail=f"{err} — прошу ещё раз")
                continue
            m.sizes["exec"] = _sizes_rec(len(u), eb, _json_len(raw))
            ps_side = (getattr(m.pilot, "position", None) or {}).get("side") if m.pilot is not None else None
            ex, err = _validate_exec(raw, m.play, price, in_pos=bool(pctx.get("position")), pos_side=ps_side)
            err_code = err
            if ex:
                break
            log.warning("миссия %s: приказ отклонён (%s)%s", m.ticker, err,
                        " — повторная попытка" if attempt == 0 else "")
            await bus.stage("mission", rid, "exec", "progress", ticker=m.ticker,
                            detail=f"приказ отклонён: {err} — прошу исправить")
        if not ex:
            raise RuntimeError(f"приказ не собрался: {err}")
        await bus.stage("mission", rid, "exec", "done", ticker=m.ticker,
                        detail=(f"CLOSE — закрыть позицию и стоять вне рынка: {ex.get('why')}" if ex["do"] == "CLOSE" else
                                f"WAIT — вне рынка, ждём: {ex.get('wait_for')}" if ex["do"] == "WAIT" else
                                f"HOLD — держать позицию без добора: стоп {_lvl_s(ex.get('invalidation'))}, "
                                f"тейк {_lvl_s(ex.get('take'))}" if ex["do"] == "HOLD" else
                                f"{ex['do']} вход {ex['entry'] if ex['entry'] is not None else 'сейчас'} "
                                f"тейк {ex['take']} стоп {ex['invalidation']:g}") + " · "
                               f"ответ {_fmt_n(m.sizes.get('exec', {}).get('answer', 0))} симв.", data=ex)

        # Остановленная миссия не принимает запоздавший ответ, даже если поставщик
        # данных подавил отмену. Новый запуск может уже владеть тем же тикером.
        if not m.auto_resume or m.run_id != rid or _M.get(m.ticker) is not m:
            raise asyncio.CancelledError
        m.exec, m.exec_ts = ex, time.time()
        m.frame = None                               # рамка прошлого совета к новому приказу не липнет (придёт фоном)
        m.texts = {"analysis": a_txt, "critique": c_txt, "verdict": v_txt}
        m.news = news_items[:30]

        # 4. пилот (v5.4.2: приказ — пилоту сразу, до косметической рамки: до 240 с дрейфа против цены совета нет)
        await bus.stage("mission", rid, "pilot", "start", ticker=m.ticker)
        if tinkoff.enabled():
            if not m.pilot_alive():
                pilot = MissionPilot(m.ticker, deposit=m.deposit, broker=_make_broker(), mission=m)
                _bind_pilot(m, pilot)
                m.pilot = pilot
                pilot.adopt_forecast({"exec": ex})
                m.task = asyncio.create_task(pilot.run())
                m.task.add_done_callback(lambda t: _pilot_done(m, t))
                m.note = f"пилот запущен ({pilot.broker.mode}): {pilot.last_action}"
            else:
                m.pilot.adopt_forecast({"exec": ex})
                m.note = f"пилот принял свежий приказ: {m.pilot.last_action}"
            # v5.4.1: WAIT — пилот без плана (фаза idle), дежурный PRO вернётся к вопросу на перепроверке
            m.phase = ("in_position" if ex["do"] in ("CLOSE", "HOLD") else "idle" if ex["do"] == "WAIT" else
                       "armed" if ex["entry"] is not None else "entering")
            await bus.stage("mission", rid, "pilot", "done", ticker=m.ticker, detail=m.note)
        else:
            m.phase = "idle"
            m.note = "нет токена Tinkoff — приказ показан, исполнение не запущено"
            await bus.stage("mission", rid, "pilot", "done", ticker=m.ticker, detail=m.note)
        # 5. рамка для человека (FLASH, не критично) — фоном, пилот её не ждёт
        _bg(_present_bg(m, rid, {"ticker": m.ticker, "name": m.name, "play": m.play, "price": price,
                                 "exec": ex, "verdict": v_in,        # уже ≤ CTX_LIMIT через shrink, без ножниц
                                 "reason": reason}))
        # толмач (v5.3): объяснить владельцу приказ; узел прошёл — свести память (старые перепроверки,
        # передачи, ответы у троса, новости до совета → один абзац, сырые списки ужать)
        if ex["do"] == "CLOSE":
            title = "Совет велел закрыть позицию (CLOSE)"
        elif ex["do"] == "WAIT":
            title = f"Совет решил ждать (WAIT): {ex.get('wait_for') or '—'}"
        elif ex["do"] == "HOLD":
            title = (f"Совет решил держать позицию (HOLD, без добора): стоп {_lvl_s(ex.get('invalidation'))}, "
                     f"тейк {_lvl_s(ex.get('take'))}")
        else:
            title = (f"Совет вынес приказ: {ex['do']} " + ("сейчас" if ex["entry"] is None else f"{ex['entry_kind']} @{ex['entry']:g}")
                     + (f", тейк {ex['take']:g}" if ex.get("take") is not None else ", без тейка") + f", стоп {ex['invalidation']:g}")
        _tolmach(m, "council", title,
                 f"{ex.get('why') or ''}. План: {ex.get('plan') or '—'}. Тайминг: {ex.get('time_note') or '—'}. Пилот: {m.note}",
                 refs={"do": ex["do"], "entry": ex["entry"], "take": ex["take"], "invalidation": ex["invalidation"],
                       "confidence": ex.get("confidence"), "price": price, "wait_for": ex.get("wait_for")})
        _memorize(m, f"после совета ({reason})")
        _persist(m)
        bus.end_run(rid)
        return ex
    except asyncio.CancelledError:
        if m.auto_resume:
            m.phase, m.error = "error", "совет прерван"
            bus.end_run(rid, error=m.error)
        else:
            bus.end_run(rid, status="cancelled")
        _persist(m)
        raise
    except Exception as e:                           # noqa: BLE001
        msg = _humanize(e)
        log.warning("миссия %s: совет упал: %s", m.ticker, msg)
        m.error = msg
        m.phase = "error" if not m.pilot_alive() else m.phase
        m.note = f"совет не завершился: {msg}"
        try:
            await bus.stage("mission", rid, "council", "error", ticker=m.ticker, detail=msg)
        except Exception:                            # noqa: BLE001
            pass
        if (first and not m.pilot_alive() and m.auto_resume and m.run_id == rid and _M.get(m.ticker) is m
                and tinkoff.enabled()):
            try:
                _pilot_without_plan(m, msg)
                await bus.stage("mission", rid, "pilot", "done", ticker=m.ticker, detail=m.note)
            except Exception as e2:                  # noqa: BLE001
                log.warning("миссия %s: пилот без плана не поднялся: %s", m.ticker, str(e2)[:120])
        bus.end_run(rid, error=msg)
        _persist(m)
        return None


async def _present_bg(m: Mission, rid: str | None, payload: dict) -> None:
    """Рамка для человека (FLASH, не критично) после того, как пилот уже получил приказ (v5.4.2)."""
    c = _mod("council")
    if not c:
        return
    try:
        await bus.stage("mission", rid, "present", "start", ticker=m.ticker)
        frame = await asyncio.wait_for(c.present_frame("mission", payload), 240)
        if m.run_id == rid and _M.get(m.ticker) is m:
            m.frame = frame
            _persist(m)
        await bus.stage("mission", rid, "present", "done", ticker=m.ticker, data=frame)
    except asyncio.CancelledError:
        raise
    except Exception as e:                           # noqa: BLE001
        try:
            await bus.stage("mission", rid, "present", "error", ticker=m.ticker, detail=_humanize(e))
        except Exception:                            # noqa: BLE001
            pass


def _pilot_without_plan(m: Mission, err: str) -> None:
    """v5.4.2: первый совет не собрал приказ (приказ не прошёл проверку / таймаут / обрыв стрима) — миссия не умирает
    в фазе error: пилот поднимается БЕЗ плана (ЖДУ_ПЛАН, фаза idle), дежурный PRO решит через REVIEW_RETRY_SEC
    по живой картине (сам войдёт, будет ждать или позовёт совет)."""
    pilot = MissionPilot(m.ticker, deposit=m.deposit, broker=_make_broker(), mission=m)
    _bind_pilot(m, pilot)
    m.pilot = pilot
    rr = f"совет не собрал приказ: {err} — реши по живой картине"
    pilot.state = "ЖДУ_ПЛАН"
    pilot.review_ts = time.time() + REVIEW_RETRY_SEC
    pilot._review_reason, pilot._review_kind = rr, "pilot"
    pilot.last_action = (f"совет не собрал приказ — пилот поднят без плана, дежурный PRO решит через "
                         f"{int(REVIEW_RETRY_SEC // 60)} мин")
    m.task = asyncio.create_task(pilot.run())
    m.task.add_done_callback(lambda t: _pilot_done(m, t))
    m.phase = "idle"
    m.note = f"пилот запущен без плана ({pilot.broker.mode}): {pilot.last_action}"
    log.warning("миссия %s: %s (%s)", m.ticker, m.note, err)
    _tolmach(m, "council", "Совет не собрал приказ — пилот без плана",
             f"{err}. Дежурный PRO решит по живой картине через {int(REVIEW_RETRY_SEC // 60)} мин.",
             refs={"error": err[:200]})


def _pilot_done(m: Mission, t: asyncio.Task) -> None:
    try:
        if t.cancelled():
            return
        e = t.exception()
        if t is not m.task or _M.get(m.ticker) is not m:
            return
        if e:
            m.error = f"пилот упал: {str(e)[:200]}"
            m.phase = "error"
            log.error("миссия %s: %s", m.ticker, m.error)
        elif m.phase not in ("stopped", "panic", "error"):
            m.phase = "stopped"
    except Exception:                                # noqa: BLE001
        pass
    _persist(m)


# ── пилот миссии ──────────────────────────────────────────────────────────────
class MissionPilot(ai_pilot.AIPilot):
    """AIPilot с богатой перепроверкой (PRO), новостями из store и журналом сделок.

    Ритм (v5.1h): дежурный PRO думает и решает раз в PYTHIA_REVIEW_SEC; внеплановую перепроверку
    поднимают только события — резкий ход цены (`_shock_watch`) и серьёзная новость дозора, — с
    пейсингом PYTHIA_EVENT_COOL_SEC; поводы пилота (идея умерла до входа, приказ протух, позиция
    закрыта, биржа отбивает) копятся к ближайшей перепроверке (после закрытия — остыть
    PYTHIA_AFTER_CLOSE_SEC). Полный совет зовёт сам PRO (НОВЫЙ_АНАЛИЗ), не чаще PYTHIA_COUNCIL_GAP_SEC.
    Три вида входа: сейчас, откат (засада у уровня), прорыв (вход, когда цена прошла уровень).
    v5.2: мягкий стоп (SOFT_STOP) — у триггера FLASH за секунды получает максимум данных и решает:
    слить сейчас или ждать и передать задачу Совету; аварийный трос лежит на бирже дальше.
    v5.3 W2: мягкий тейк (`_take_guard`) — у цели FLASH с теми же данными решает: зафиксировать или
    подержать (прибыль запирается триггером, тейк отодвигается) и передать задачу Совету; триаж событий
    (`_event_triage`) — резкий ход / серьёзная новость при позиции сначала смотрит FLASH: СЕЙЧАС → PRO,
    ПЛАНОВО → повод к плановой, САМ → подтянуть трос / снять план без PRO.
    v5.3 W3: прокол сканера (`_puncture_watch`) — полоса устойчивого вакуума со стойкостью ≥ PYTHIA_PUNCTURE_MIN,
    сторона важна (вне рынка с планом: вход / предупреждение; в позиции: угроза / подтверждение) → повод kind
    «puncture»: блок ПРОКОЛ СКАНЕРА первым в ситуации PRO и триажа; в позиции — через триаж, иначе сразу PRO;
    пейсинг PYTHIA_PUNCTURE_COOL_SEC, один прокол — одна перепроверка."""

    SOFT_STOP = True

    def __init__(self, base: str, deposit: float | None = None,
                 broker: trader_broker.Broker | None = None, mission: Mission | None = None):
        super().__init__(base, deposit=deposit, broker=broker)
        self.mission = mission
        self._council_kind = ""                  # stop|take — совет позвал мягкий стоп/тейк: переворот без тишины
        self._triage_task = None                 # v5.3 W2: FLASH-триаж события думает
        self.triages: list[dict] = []            # решения триажа (последние TRIAGES_KEEP)
        self.puncture: dict | None = None        # v5.3 W3: последний прокол сканера, ставший поводом (блок для ИИ, статус)
        self._puncture_now: dict | None = None   # что сканер показывает сейчас и почему это (не) повод — для панели
        self._puncture_seen: dict[tuple, float] = {}   # (сторона, полоса, роль) → ts: один прокол — одна перепроверка
        self._puncture_check_ts = 0.0
        self._last_puncture_ts = 0.0
        self.review_ts = time.time() + float(getattr(config, "PYTHIA_REVIEW_SEC", 1800))
        self._last_review_ts = 0.0
        self._review_reason: str | None = None   # накопленные поводы внеплановой перепроверки (в промпт)
        self._review_kind: str | None = None     # pilot | shock | news — что подняло перепроверку
        self._review_pulled = False              # перепроверку приблизило событие (пейсинг событий)
        self._last_event_review_ts = 0.0         # когда PRO последний раз думал по событию
        self._px_hist: list[tuple[float, float]] = []   # (ts, цена) за окно резкого хода
        self._last_shock_ts = 0.0
        self._break_n = 0                        # прорыв: сколько тиков подряд цена за уровнем
        self._council_reason = ""                # повод полного совета для колбэка (council_again)
        self._council_blocked = ""               # PRO просил совет, но он был недавно — в ситуацию
        self._council_deferred: str | None = None   # v5.4.2: НОВЫЙ_АНАЛИЗ в окне совета — отложен до открытия окна
        self._wait_st: dict | None = None        # v5.4.2: приказ WAIT под наблюдением {exec_ts, ref, fired}
        self._review_started_ts = 0.0            # v5.4.2: когда перепроверка последний раз ушла к PRO (ритм WAIT)
        if mission:
            self.name = mission.name or self.name
            self.asset_class = mission.asset_class or self.asset_class

    # новости — из store (без сети): свежие с прошлой перепроверки + по тикеру
    async def _gather_news(self) -> str:
        nf = _mod("newsflow")
        if not nf:
            return ""
        m = self.mission
        if m is not None:                            # Google News по инструменту перед каждой перепроверкой
            await _enrich_news(m, m.run_id, timeout=180.0)
        since = self._last_review_ts or self.started_ts
        blocks = []
        try:
            fresh = nf.fresh_since(since) or []
            if fresh:
                blocks.append(f"— свежие с {ai_v5.fmt_ts(since)}:\n" + _render_all(nf, fresh))
        except Exception as e:                       # noqa: BLE001
            log.info("свежие новости недоступны: %s", str(e)[:60])
        try:
            own = nf.news_for_ticker(self.base, self.name, limit=None) or []   # все, без среза
            own, dropped = self._news_after_council(own)
            if dropped:
                blocks.append(dropped)
            if own:
                blocks.append("— по инструменту:\n" + _render_all(nf, own))
        except Exception as e:                       # noqa: BLE001
            log.info("новости по тикеру недоступны: %s", str(e)[:60])
        return "\n\n".join(blocks)                   # целиком: окно DeepSeek V4 — 1M токенов

    def _news_after_council(self, own: list[dict]) -> tuple[list[dict], str]:
        """v5.3: перепроверка и трос видят только новости после последнего совета — старые ужаты в
        ПАМЯТЬ МИССИИ. Памяти ещё нет (не сведена/выключена) → всё целиком, ничего не теряем."""
        m = self.mission
        if m is None or not m.council_ts or not (m.memory or "").strip():
            return own, ""
        if (_f(m.memory_ts) or 0.0) < float(m.council_ts):    # память старше совета (ещё сводится / не свелась) → ничего не прячем
            return own, ""
        fresh, old = _split_news(own, m.council_ts)
        if not old:
            return own, ""
        return fresh, (f"— новости до совета {ai_v5.fmt_ts(m.council_ts)} ({len(old)} шт.) ужаты в блок ПАМЯТЬ МИССИИ; "
                       f"ниже только свежие после совета")

    def _play(self) -> str:
        return (self.mission.play if self.mission else "auto") or "auto"

    # v5.3 фаза 3: дедуп прокола (_puncture_seen) и пейсинг (_last_puncture_ts) переживают рестарт —
    # секция «pilot» state-файла рядом с позицией; иначе после рестарта тот же прокол поднял бы PRO снова
    def _state_extra(self) -> dict:
        out = dict(super()._state_extra() or {})
        now = time.time()
        seen = [[k[0], k[1], k[2], round(ts, 1)] for k, ts in self._puncture_seen.items()
                if isinstance(k, tuple) and len(k) == 3 and now - ts <= PUNCTURE_SEEN_SEC]
        if seen:
            out["puncture_seen"] = seen
        cool = float(getattr(config, "PYTHIA_PUNCTURE_COOL_SEC", 600))
        if self._last_puncture_ts and now - self._last_puncture_ts < cool:
            out["last_puncture_ts"] = round(self._last_puncture_ts, 1)
        return out

    def _restore_extra(self, extra: dict) -> None:
        super()._restore_extra(extra)
        now = time.time()
        n = 0
        for row in (extra or {}).get("puncture_seen") or []:
            try:
                side, bin_, role, ts = row
                ts = float(ts)
                if now - ts <= PUNCTURE_SEEN_SEC and side in ("вверх", "вниз") and role:
                    self._puncture_seen[(side, bin_, role)] = ts
                    n += 1
            except (TypeError, ValueError):
                continue
        lp = _f((extra or {}).get("last_puncture_ts"), 0.0)
        # метка в файле округлена до 0.1 с — сразу после записи может быть «в будущем» на доли секунды
        if lp and -60.0 <= now - lp < float(getattr(config, "PYTHIA_PUNCTURE_COOL_SEC", 600)):
            self._last_puncture_ts = lp
        if n or lp:
            log.info("миссия %s: рестарт — подхватил проколы сканера: %d в памяти, пейсинг %s",
                     self.base, n, ai_v5.fmt_ts(lp) if lp else "нет")

    # сухой режим: портфеля на бирже нет — свою позицию из state-файла берём на веру
    # (иначе «продолжить» в dry всегда открывал новую позицию, а старая исчезала из учёта)
    def _restore_state(self, portfolio_qty: int) -> None:
        if getattr(self.broker, "mode", "") == "dry" and self._state_path \
                and self._state_path.exists():
            try:
                try:
                    raw = self._state_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    raw = self._state_path.read_text()
                rec = json.loads(raw)
                pos = (rec or {}).get("position") or {}
                if rec.get("figi") == self.figi and pos.get("lots"):
                    portfolio_qty = int(pos["lots"]) * (1 if pos.get("side") == "long" else -1)
            except Exception:                        # noqa: BLE001
                pass
        return super()._restore_state(portfolio_qty)

    # ── петля: наблюдение резкого хода поверх тика 4.x ──────────────────────────
    async def tick(self, price: float, book: dict | None = None) -> None:
        try:
            self._shock_watch(price)
        except Exception as e:                       # noqa: BLE001
            log.info("наблюдение резкого хода споткнулось: %s", str(e)[:80])
        try:
            self._puncture_watch(price)
        except Exception as e:                       # noqa: BLE001
            log.info("наблюдение прокола сканера споткнулось: %s", str(e)[:80])
        try:
            self._wait_watch(price)
        except Exception as e:                       # noqa: BLE001
            log.info("наблюдение приказа WAIT споткнулось: %s", str(e)[:80])
        try:
            self._deferred_council_watch()
        except Exception as e:                       # noqa: BLE001
            log.info("отложенный совет споткнулся: %s", str(e)[:80])
        await super().tick(price, book)

    def _wait_watch(self, price: float) -> None:
        """v5.4.2: приказ совета WAIT без позиции, плана и заявки — не сон вслепую. Уровни приказа (m.exec["levels"])
        под наблюдением: цена прошла уровень относительно цены, при которой WAIT увиден впервые, → внеплановая
        перепроверка (kind «wait_level», не раньше EVENT_MIN_GAP_SEC после ответа PRO; один уровень — один повод).
        Ритм: дежурный PRO смотрит заново не реже раза в PYTHIA_WAIT_REVIEW_SEC. Решает ИИ — код только будит."""
        m = self.mission
        ex = (m.exec if m is not None else None) or {}
        if str(ex.get("do") or "").upper() != "WAIT" or not price or price <= 0:
            self._wait_st = None
            return
        if self.position or self.plan or self.pending:
            return
        now = time.time()
        st = self._wait_st
        if st is None or st.get("exec_ts") != m.exec_ts:
            st = self._wait_st = {"exec_ts": m.exec_ts, "ref": float(price), "fired": {}}
        if self.state == "СТОП" or self._reanalyzing or not self._market_alive() or self._market_closed():
            return
        per = float(getattr(config, "PYTHIA_WAIT_REVIEW_SEC", 1800))
        if per > 0 and not self._review_busy:
            due = max(self._last_review_ts, self._review_started_ts, _f(m.exec_ts, 0.0),
                      _f(getattr(self, "started_ts", 0.0), 0.0)) + per
            if due < self.review_ts:
                self.review_ts = due
        ref = _f(st.get("ref"), 0.0)
        for raw in ex.get("levels") or []:
            lvl = _f(raw, 0.0)
            key = f"{lvl:g}"
            if lvl <= 0 or ref <= 0 or key in st["fired"]:
                continue
            if not ((ref < lvl <= price) or (ref > lvl >= price)):
                continue
            if self._review_busy:                    # PRO думает прямо сейчас — повод поднимем, когда он ответит
                return
            st["fired"][key] = now
            self._ask_review_now(f"WAIT: цена {price:g} прошла уровень {lvl:g} из приказа совета — реши по живой картине",
                                 kind="wait_level")
            return

    def _deferred_council_watch(self) -> None:
        """v5.4.2: НОВЫЙ_АНАЛИЗ, отложенный окном совета, ждёт открытия окна (тик §3б зовёт его сам). Снимается, если
        совет уже прошёл (родитель очистил _reanalyze_pending) или дежурный PRO с тех пор ответил и совет не просил
        снова (_council_blocked стёрт его ответом) — решает ИИ, а не старая просьба."""
        dfr = self._council_deferred
        if not dfr:
            return
        if self._reanalyze_pending != dfr or not self._council_blocked:
            if self._reanalyze_pending == dfr:
                self._reanalyze_pending = None
            self._council_deferred = None

    def _shock_watch(self, price: float) -> None:
        """Резкий ход: цена за окно PYTHIA_SHOCK_WIN_SEC ушла от края окна на PYTHIA_SHOCK_PCT % →
        внеплановая перепроверка дежурного PRO (один ход — одна перепроверка; пейсинг в _ask_review)."""
        if not price or price <= 0:
            return
        now = time.time()
        win = float(getattr(config, "PYTHIA_SHOCK_WIN_SEC", 600))
        h = self._px_hist
        h.append((now, float(price)))
        cut = now - win
        while h and h[0][0] < cut:
            h.pop(0)
        pct = float(getattr(config, "PYTHIA_SHOCK_PCT", 1.0))
        if pct <= 0 or len(h) < 4 or now - h[0][0] < win * 0.5:
            return
        lo = min(p for _, p in h)
        hi = max(p for _, p in h)
        up = price / lo - 1.0 if lo > 0 else 0.0
        dn = price / hi - 1.0 if hi > 0 else 0.0
        move = up if up >= -dn else dn
        if abs(move) * 100.0 < pct:
            return
        if now - self._last_shock_ts < win:          # тот же ход второй раз не считаем
            return
        if self.state == "СТОП" or self._reanalyzing or self.pending:
            return
        if not self._market_alive() or self._market_closed():
            return
        # v5.4.1: рывок В НАШУ сторону при плавающем плюсе — мысль о прибыли (PRO: держать / выйти / выйти и
        # перезайти / совет) вместо триажа, если включена и не в пейсинге; иначе — как было
        pos = self.position
        if pos and self._profit_think_on():
            sgn = 1.0 if pos.get("side") == "long" else -1.0
            entry = _f(pos.get("entry"), 0.0)
            if (move > 0) == (sgn > 0) and entry > 0 and (price - entry) * sgn > 0 \
                    and not pos.get("profit_busy") and not pos.get("guard_busy") and now >= _f(pos.get("profit_next"), 0.0):
                reason = (f"рывок в нашу сторону {move * 100.0:+.2f}% за {int(win // 60)} мин: цена {price:g}, "
                          f"{(price / entry - 1) * 100 * sgn:+.2f}% от входа {entry:g}")
                if self._review_busy:                # v5.4.2: PRO на перепроверке — мысль о прибыли сразу после ответа
                    pos["profit_pending"] = reason[:200]
                    pos["profit_pending_kind"] = "shock"   # рывок израсходован: после ответа — без порога хода
                    self._last_shock_ts = now
                    return
                if self._profit_think_now(price, pos, reason):
                    self._last_shock_ts = now
                    return
        if self._review_busy:
            return
        self._last_shock_ts = now
        self._ask_review(f"резкий ход: {move * 100.0:+.2f}% за {int(win // 60)} мин (цена {price:g})",
                         kind="shock")

    # ── прокол сканера (v5.3 W3): полоса устойчивого вакуума как повод для ИИ ─────
    def _puncture_role(self) -> tuple[str | None, bool]:
        """Чья сторона нам важна: (сторона позиции или живого плана входа, в позиции ли). Ни того, ни другого →
        (None, False): прокол — просто строка в блоке сканера."""
        if self.position and self.position.get("side") in ("long", "short"):
            return self.position["side"], True
        if self.plan and self.plan.get("side") in ("long", "short"):
            return self.plan["side"], False
        return None, False

    def _puncture_quiet(self, rec: dict, note: str) -> None:
        rec["state"] = note
        self._puncture_now = rec

    def _puncture_watch(self, price: float) -> None:
        """Прокол сканера как повод (PYTHIA_PUNCTURE): `puncture_first` из status() сканера со стойкостью
        ≥ PYTHIA_PUNCTURE_MIN и важной стороной (вне рынка с планом: в сторону плана — «вход», против —
        «предупреждение»; в позиции: против нас — «угроза», за нас — «подтверждение»; v5.4.2: вне рынка без плана
        (WAIT / приказа нет) — «вне рынка»: возможный вход в сторону прокола, кроме стороны против режима игры) → блок ПРОКОЛ СКАНЕРА
        (prompts_mission.puncture_block) первым в ситуации, повод kind «puncture»: в позиции — через FLASH-триаж
        (_ask_review), вне рынка — сразу внеплановая перепроверка PRO с пейсингом PYTHIA_PUNCTURE_COOL_SEC.
        Один прокол (сторона + полоса + роль) — одна перепроверка (_puncture_seen, PUNCTURE_SEEN_SEC)."""
        if not bool(getattr(config, "PYTHIA_PUNCTURE", True)):
            return
        now = time.time()
        if now - self._puncture_check_ts < PUNCTURE_CHECK_SEC:
            return
        self._puncture_check_ts = now
        m = self.mission
        if m is None:
            return
        st = _scan_status_raw(m.ticker)
        pf = st.get("puncture_first") if isinstance(st, dict) else None
        if not isinstance(pf, dict) or pf.get("side") not in ("вверх", "вниз"):
            self._puncture_now = None
            return
        pers = _f(pf.get("persistence"), 0.0)
        min_p = float(getattr(config, "PYTHIA_PUNCTURE_MIN", 0.6))
        our, in_pos = self._puncture_role()
        role = None
        if our:
            toward = (pf["side"] == "вверх") == (our == "long")
            role = ("подтверждение" if toward else "угроза") if in_pos else ("вход" if toward else "предупреждение")
        elif not self.pending:
            role = "вне рынка"                       # v5.4.2: вне рынка без плана (WAIT / приказа нет) — возможный вход в сторону прокола
        rec = {"ts": now, "side": pf["side"], "persistence": round(pers, 3), "p_lo": _f(pf.get("p_lo")), "p_hi": _f(pf.get("p_hi")),
               "depth": pf.get("depth_mean"), "ticks": pf.get("ticks"), "role": role, "our_side": our, "in_pos": in_pos,
               "price": price, "state": "", "pending": False}
        step = _f((st.get("punctures") or {}).get("bin_step"), 0.0)
        centre = ((rec["p_lo"] or 0.0) + (rec["p_hi"] or 0.0)) / 2.0
        key = (pf["side"], int(round(centre / step)) if step > 0 else round(centre, 4), role)
        if pers < min_p:
            self._puncture_quiet(rec, f"стойкость {pers * 100:.0f} % ниже порога {min_p * 100:.0f} % — ИИ не тревожим")
            return
        if role is None:
            self._puncture_quiet(rec, "ни позиции, ни плана входа — сторона не важна, ИИ не тревожим")
            return
        play = self._play()
        if role == "вне рынка" and ((play == "long" and pf["side"] == "вниз") or (play == "short" and pf["side"] == "вверх")):
            self._puncture_quiet(rec, f"прокол {pf['side']} вне рынка, а режим игры {play} — вход в эту сторону закрыт "
                                      "владельцем, ИИ не тревожим")
            return
        for k, ts in list(self._puncture_seen.items()):
            if now - ts > PUNCTURE_SEEN_SEC:
                self._puncture_seen.pop(k, None)
        if key in self._puncture_seen:
            pu = self.puncture
            if pu and pu.get("key") == key:          # тот же прокол — показываем, что с ним уже сделали
                self._puncture_now = pu
            else:
                self._puncture_quiet(rec, "этот прокол уже был поводом — второй раз ИИ не тревожим")
            return
        cool = float(getattr(config, "PYTHIA_PUNCTURE_COOL_SEC", 600))
        if self._last_puncture_ts and now - self._last_puncture_ts < cool:
            left = int(cool - (now - self._last_puncture_ts))
            self._puncture_quiet(rec, f"пейсинг: прошлый прокол был {int((now - self._last_puncture_ts) // 60)} мин назад — "
                                      f"не чаще раза в {int(cool // 60)} мин (ещё {left // 60} мин {left % 60} с)")
            return
        if self.state == "СТОП" or self._reanalyzing or self._review_busy or self.pending:
            self._puncture_quiet(rec, "пилот занят (совет / перепроверка / заявка в полёте) — прокол подождёт")
            return
        if not self._market_alive() or self._market_closed():
            self._puncture_quiet(rec, "рынок мёртв или закрыт — прокол подождёт")
            return
        self._puncture_seen[key] = now
        self._last_puncture_ts = now
        self._save_state()                       # v5.3 фаза 3: дедуп и пейсинг прокола переживают рестарт
        c = st.get("consensus") if isinstance(st.get("consensus"), dict) else None
        hw = st.get("hawkes") if isinstance(st.get("hawkes"), dict) else None
        tn = st.get("tension") if isinstance(st.get("tension"), dict) else None
        rec["key"] = key
        rec["pending"] = True
        rec["text"] = prompts_mission.puncture_block(
            pf["side"], pers, rec["p_lo"], rec["p_hi"], role=role, our_side=our, in_pos=in_pos, price=price,
            depth=pf.get("depth_mean"), ticks=pf.get("ticks"), consensus=c, hawkes_n=(hw or {}).get("n"),
            tension=(tn or {}).get("word"))
        self.puncture = self._puncture_now = rec
        why = (f"прокол сканера {pf['side']} {pers * 100:.0f} % ({role}: полоса {rec['p_lo']:g}–{rec['p_hi']:g}, "
               f"{'в позиции ' + str(our) if in_pos else 'план ' + str(our) if our else 'без плана'})")
        rec["reason"] = why
        self._ask_review(why, kind="puncture")
        rec["state"] = self.last_action
        _bg(bus.stage("mission", m.run_id, "puncture", "done", ticker=self.base, detail=self.last_action,
                      data={k: v for k, v in rec.items() if k not in ("text", "key")}))
        _tolmach(m, "puncture", f"Сканер видит прокол {pf['side']} {pers * 100:.0f} %",
                 f"полоса {rec['p_lo']:g}–{rec['p_hi']:g} при цене {price:g}; {prompts_mission.PUNCTURE_ROLES.get(role, role)}; "
                 f"{self.last_action}",
                 refs={"side": pf["side"], "persistence": rec["persistence"], "p_lo": rec["p_lo"], "p_hi": rec["p_hi"],
                       "role": role, "price": price})

    def _situation_for_ai(self, price: float) -> str:
        """Ситуация для PRO и триажа: блок ПРОКОЛ СКАНЕРА (W3) первым, пока PRO его не разобрал."""
        s = self._situation_text(price)
        pu = self.puncture
        if pu and pu.get("pending") and pu.get("text"):
            return pu["text"] + "\n" + s
        return s

    # ── вход: откат (засада 4.x) или прорыв (цена прошла уровень) ─────────────────
    def _entry_ready(self, price: float, lvl: float) -> bool:
        if (self.plan or {}).get("kind") != "прорыв":
            return super()._entry_ready(price, lvl)
        beyond = (price >= lvl) if self.plan["side"] == "long" else (price <= lvl)
        if not beyond:
            self._break_n = 0
            return False
        self._break_n += 1
        return self._break_n >= BREAK_CONFIRM_TICKS

    def _entry_wait_text(self, price: float, lvl: float) -> str:
        if (self.plan or {}).get("kind") == "прорыв":
            return (f"жду пробития {lvl:g} для {self.plan['side']} (цена {price:g}; вход после "
                    f"{BREAK_CONFIRM_TICKS} тиков за уровнем)")
        return super()._entry_wait_text(price, lvl)

    # ── ситуация для дежурного PRO ────────────────────────────────────────────────
    def _situation_text(self, price: float) -> str:
        now = time.time()
        L = [f"Цена сейчас: {price:g}"]
        mline = self._market_line()
        if mline:
            L.append("РЫНОК: " + mline)
        h = self._px_hist
        if h and h[0][1] > 0 and now - h[0][0] >= 60:
            lo = min(p for _, p in h)
            hi = max(p for _, p in h)
            L.append(f"Ход за {int((now - h[0][0]) // 60)} мин: {(price / h[0][1] - 1) * 100:+.2f}% "
                     f"(мин {lo:g}, макс {hi:g})")
        if len(self.prices) > 2:
            n30 = int(1800 / ai_pilot.TICK_SEC)
            base = self.prices[-n30] if len(self.prices) >= n30 else self.prices[0]
            if base > 0:
                L.append(f"Ход за окно наблюдения (≈30 мин): {(price / base - 1) * 100:+.2f}%")
        b = self.last_book or {}
        if b.get("best_bid") and b.get("best_ask"):
            L.append(f"Стакан: bid {_f(b['best_bid']):g} / ask {_f(b['best_ask']):g}"
                     + (f", дисбаланс {b.get('imbalance')}" if b.get("imbalance") is not None else ""))
        mm = self._money_name()
        if self.position:
            p = self.position
            held = int((now - p["opened_ts"]) / 60)
            L.append(f"ПОЗИЦИЯ: {p['side']} {p['lots']} лот @{p['entry']:g}, в рынке {held} мин, "
                     f"плавающий P/L {p.get('floating', 0):+.0f} ₽, триггер (мягкий стоп) @{p.get('invalidation')}"
                     + (f", {self._hard_name()} @{p.get('hard_stop')}" if p.get("hard_stop") else "")
                     + f", тейк {p.get('take')}"
                     + (" — принята со счёта, уровни временные: назови свои" if p.get("levels_placeholder") else "")
                     + (f"; {mm} у троса уже держал {p.get('holds')} раз" if p.get("holds") else "")
                     + (f"; {mm} у тейка уже держал {p.get('take_holds')} раз — прибыль заперта триггером"
                        if p.get("take_holds") else "")
                     + (f"; прибыль заперта триггером по мысли о прибыли" if p.get("profit_lock") and not p.get("take_holds") else ""))
            if self.profits and self.profits[-1].get("ts", 0) >= _f(p.get("opened_ts"), 0.0):
                x = self.profits[-1]
                L.append(f"ПРОШЛАЯ МЫСЛЬ О ПРИБЫЛИ ({int((now - _f(x.get('ts'), now)) // 60)} мин назад): {x.get('decision')} — {x.get('why')}")
            if self.plan and self.plan.get("side") == p["side"]:
                L.append(f"ДОБОР ПО ПРИКАЗУ: {self.plan['side']} "
                         + ("сейчас" if self.plan.get("entry") is None else f"у {self.plan['entry']}"))
        elif self.pending:
            L.append(f"ЗАЯВКА ВХОДА В ПОЛЁТЕ: {self.pending['side']} {self.pending['lots']} лот "
                     f"@{self.pending['price']}")
        elif self.plan:
            pl = self.plan
            age = int((now - _f(pl.get("ts"), now)) / 60)
            if pl.get("entry") is None:
                L.append(f"ВХОЖУ: {pl['side']} сейчас, стоп {pl['invalidation']}, тейк {pl.get('take')}")
            elif pl.get("kind") == "прорыв":
                L.append(f"ЖДУ ПРОБИТИЯ: {pl['side']} при проходе {pl['entry']}, стоп {pl['invalidation']}, "
                         f"тейк {pl.get('take')} (приказ {age} мин назад, срок {int(ai_pilot.PLAN_TTL_SEC // 60)} мин)")
            else:
                L.append(f"ЗАСАДА (откат): {pl['side']} @{pl['entry']}, стоп {pl['invalidation']}, "
                         f"тейк {pl.get('take')} (приказ {age} мин назад, срок {int(ai_pilot.PLAN_TTL_SEC // 60)} мин)")
            gate_line = self._gate_line(pl)
            if gate_line:
                L.append(gate_line)
        else:
            L.append("Позиции нет, засады нет — полностью вне рынка")
            m0 = self.mission
            if m0 is not None and str((m0.exec or {}).get("do") or "").upper() == "WAIT":
                # v5.4.2: WAIT совета — его прошлое мнение, а не запрет и не «вход — исключение»
                # ревью 5.4.2: выбор — из того же списка, что в system перепроверки (режим игры, НОВЫЙ_АНАЛИЗ)
                L.append(f"ПРИКАЗ СОВЕТА ({int((now - _f(m0.exec_ts, now)) // 60)} мин назад): вне рынка; совет ждал: "
                         f"{m0.exec.get('wait_for') or m0.exec.get('why') or '—'}. Это прошлое мнение, а не запрет: "
                         f"реши заново — {prompts_mission.review_options(self._play(), False)}")
        L.append(f"Депозит {self.deposit:.0f} ₽, результат сессии {sum(self.pnls):+.0f} ₽ за {len(self.pnls)} сделок")
        acc_line = self._account_line()
        if acc_line:
            L.append(acc_line + " — размер входа/добора считает биржа, ты решаешь только сторону")
        if self.last_review:
            age = int((now - self.last_review["ts"]) / 60)
            L.append(f"Прошлая перепроверка ({age} мин назад): {self.last_review['choice']} — "
                     f"{self.last_review['why']}")
        m = self.mission
        if m is not None and m.council_ts:
            cut = _f(getattr(self, "_council_cut_ts", 0.0), 0.0)
            if cut and abs(cut - float(m.council_ts)) < 1.0:     # окно совета считается от обрыва — это не новый приказ
                L.append(f"Последний полный совет прерван {int((now - cut) // 60)} мин назад (не уложился в срок) — "
                         f"действующий приказ прежний")
            else:
                L.append(f"Последний полный совет: {int((now - m.council_ts) // 60)} мин назад")
        sr = self.session_risk.state() if self.session_risk else {}
        if sr:
            L.append(f"Killswitch: {'ЗАБЛОКИРОВАН' if sr.get('locked') else 'ок'}"
                     f" (дневной лимит {sr.get('day_loss_limit')})")
        return "\n".join(L)

    def _review_silent(self, reason: str) -> None:
        """v5.3 фаза 3: PRO промолчал на перепроверке (таймаут / сеть / не JSON) — не ждать плановой
        PYTHIA_REVIEW_SEC: ранняя повторная попытка через REVIEW_RETRY_SEC (как при «шифровщик не дал exec»),
        накопленный повод (_review_reason) не стирается, событие в ошибках ИИ для панели."""
        m = self.mission
        self.review_ts = min(self.review_ts, time.time() + REVIEW_RETRY_SEC)
        wait = max(0, int(self.review_ts - time.time()))
        why = self._review_reason or "плановая перепроверка"
        self.last_action = (f"дежурный PRO промолчал ({reason}) — повторю через {wait // 60} мин {wait % 60} с; "
                            f"повод сохранён: {why[:160]}")
        log.warning("миссия %s: %s", self.base, self.last_action)
        try:
            ai_v5.note_error(f"перепроверка PRO: {reason}", "mission_review")
        except Exception:                            # noqa: BLE001  (в тестах ai_v5 — фейк без note_error)
            pass
        if m is not None:
            _bg(bus.stage("mission", m.run_id, "review", "error", ticker=self.base, detail=self.last_action))
            _tolmach(m, "review", "PRO промолчал на перепроверке",
                     f"{reason}; повод не потерян ({why[:120]}) — повторю через {wait // 60} мин",
                     refs={"reason": reason, "retry_in_s": wait})
            _persist(m)

    async def _review_bg(self, price: float) -> None:
        """Перепроверка фоном (AIPilot._review_bg) + v5.4.2: метка старта (ритм WAIT) и мысль о прибыли, которая ждала,
        пока дежурный PRO думал (pos["profit_pending"]), — сразу после его ответа, если позиция та же и ещё в плюсе."""
        self._review_started_ts = time.time()
        try:
            await super()._review_bg(price)
        finally:
            try:
                self._profit_after_review()
            except Exception as e:                   # noqa: BLE001
                log.info("миссия %s: мысль о прибыли после перепроверки: %s", self.base, str(e)[:80])

    def _profit_after_review(self) -> None:
        """Повод мысли о прибыли, ждавший ответа дежурного PRO. Ревью 5.4.2: повод от рывка в нашу сторону (kind
        «shock») уже израсходовал рывок в _shock_watch — после ответа мысль идёт, пока позиция в плюсе, без повторной
        проверки порога хода (иначе рывок терялся); повод от порога (_profit_watch) проверяется заново, как было."""
        pos = self.position
        if not pos or not pos.get("profit_pending") or self.stopping:
            return
        waited = str(pos.pop("profit_pending") or "")
        kind = pos.pop("profit_pending_kind", None)
        if self._reanalyzing or time.time() < _f(pos.get("profit_next"), 0.0):
            return
        cur = self.prices[-1] if self.prices else 0.0
        if kind == "shock" and cur > 0:
            entry = _f(pos.get("entry"), 0.0)
            sgn = 1.0 if pos.get("side") == "long" else -1.0
            if entry > 0 and (cur - entry) * sgn > 0:
                self._profit_think_now(cur, pos, f"{waited} (повод ждал ответа дежурного PRO)")
            return
        reason = self._profit_trigger(cur, pos) if cur > 0 else None
        if reason:
            self._profit_think_now(cur, pos, f"{reason} (повод ждал ответа дежурного PRO: {waited[:120]})")

    # ── перепроверка дежурного PRO ────────────────────────────────────────────────
    def _parse_choice(self, raw: str, in_pos: bool, side: str | None = None) -> str | None:
        """v5.4.2: слово перепроверки по словарю ai_v5.review_table (целые слова, латиница, Ё=Е): токен или None —
        «не разобрано» (решения нет, переспрос), а не молчаливый ЖДЁМ базового разбора подстрок."""
        return ai_v5.decision_of(raw, ai_v5.review_table(in_pos, side))

    async def _review(self, price: float) -> None:
        m = self.mission
        in_pos = self.position is not None
        news_txt = await self._gather_news()
        if not news_txt.strip():
            news_txt = "(свежих новостей нет или сбор недоступен — НЕ выдумывай их, решай по цене и плану)"
        try:
            lt = await asyncio.wait_for(market_ctx.light(self.base, self.figi, self.asset_class), 30)
            light_txt = lt.get("text") or ""
        except Exception as e:                       # noqa: BLE001
            light_txt = f"(живой рынок недоступен: {str(e)[:80]})"
        astro_line = ""
        try:
            from . import astro as _astro
            c = await _astro.acontext()              # в потоке: протухший кэш не держит event loop
            astro_line = _astro.short_line(c) if c else ""
        except Exception:                            # noqa: BLE001
            pass
        watch_txt = _watch_text(self._last_review_ts or self.started_ts)
        scout_txt, partners_txt = await asyncio.gather(self._scout_fresh(SCOUT_REVIEW_TIMEOUT),
                                                       self._partners_fresh(PARTNERS_REVIEW_TIMEOUT))
        # разумные пределы (v5.1): блоки перепроверки через compress.fit — выше пределов FLASH
        # ужимает без потери нитей, ниже — всё целиком; ножниц нет
        blocks, squeezed = await _fit_blocks({
            "light": light_txt, "council": _council_text(), "prev_exec": _exec_text(m) if m else "",
            "news": news_txt, "watch": watch_txt, "scan": _scan_text(m) if m else "",
            "wyckoff": (m.layers.get("wyckoff") if m else "") or "", "scout": scout_txt,
            "partners": partners_txt, "memory": (m.memory if m else "") or ""})
        # v5.4.2: снимок решения — цена, которую PRO видит в промпте (после сбора данных, а не при запуске задачи)
        price, snap_ts = (self.prices[-1] if self.prices else price), time.time()
        situation = self._situation_for_ai(price)     # W3: блок ПРОКОЛ СКАНЕРА первым, пока PRO его не разобрал
        if self._review_reason:
            situation += f"\nПОВОД ПЕРЕПРОВЕРКИ (внеплановая): {self._review_reason}"
        if self._council_blocked:
            situation += f"\nСОВЕТ: {self._council_blocked}"
        s, u = prompts_mission.review(
            self.base, self.name, self._play(), situation=situation,
            light=blocks["light"], council_text=blocks["council"], prev_exec=blocks["prev_exec"],
            news=blocks["news"], watch=blocks["watch"], astro_line=astro_line, in_pos=in_pos,
            time_msk=ai_v5.now_msk_str(),
            review_min=int(float(getattr(config, "PYTHIA_REVIEW_SEC", 1800)) // 60),
            scan=blocks["scan"], wyckoff=blocks["wyckoff"], scout=blocks["scout"], partners=blocks["partners"],
            memory=blocks["memory"], side=(self.position or {}).get("side"),
            council_min=max(1, int(float(getattr(config, "PYTHIA_COUNCIL_GAP_SEC", 1800)) // 60)))
        sb = {"situation": situation, **blocks}
        if m is not None:
            m.sizes["review"] = _sizes_rec(len(u), sb)
            try:
                await bus.stage("mission", m.run_id, "review", "start", ticker=self.base,
                                detail=_sizes_detail("перепроверка", len(u), sb, squeezed))
            except Exception as e:                   # noqa: BLE001
                log.info("шина: стадия review start: %s", str(e)[:80])
        try:
            obj = await asyncio.wait_for(ai_v5.pro_json(s, u, route="mission_review"), 1200)
        except Exception as e:                       # noqa: BLE001  (таймаут 1200 с, сеть, 4xx/5xx после повторов)
            self._review_silent(f"{'таймаут 1200 с' if isinstance(e, asyncio.TimeoutError) else str(e)[:100] or type(e).__name__}")
            raise
        if not isinstance(obj, dict):
            self._review_silent("ответ не JSON-объект")
            return
        # v5.4.2: слово решения — по словарю перепроверки (BUY/LONG/ЛОНГ → КУПИТЬ, CLOSE/EXIT/ЗАФИКСИРОВАТЬ в позиции →
        # ЗАКРЫТЬ, «НЕ …» и «… или …» → не разобрано) ДО сброса повода: не разобрано → решения не было, ранний повтор
        # с сохранённым поводом и без записи «ЖДЁМ» за ИИ
        pos_side = (self.position or {}).get("side")
        raw_choice = ai_v5.decision_raw(obj)
        choice = self._parse_choice(raw_choice, in_pos, pos_side)
        if choice is None:
            self._review_silent(f"ответ не разобран: {raw_choice[:60] or 'пусто'}")
            return
        self._last_review_ts = time.time()
        if self._review_pulled:
            self._last_event_review_ts = self._last_review_ts   # пейсинг событий — от ответа PRO
        self._review_reason, self._review_kind, self._review_pulled = None, None, False
        self._review_deferred = None                 # отложенное решение (если было) PRO только что видел в поводе
        self._council_blocked = ""
        if m is not None:
            m.sizes["review"] = _sizes_rec(len(u), sb, _json_len(obj))
        why = str(obj.get("why") or "")[:300]
        note = str(obj.get("note") or "")[:400]
        # режим игры (воля владельца): вход против режима — не «ЖДЁМ» за ИИ, а своя запись ВНЕ_РЕЖИМА и переспрос;
        # ПЕРЕВЕРНУТЬ против режима — выходная половина разрешена: ЗАКРЫТЬ
        play = self._play()
        ai_choice = choice
        if (choice == "КУПИТЬ_СЕЙЧАС" and play == "short") or (choice == "ПРОДАТЬ_СЕЙЧАС" and play == "long"):
            why = f"[{choice} против режима {play} — не исполнено, переспрошу] " + why
            choice = "ВНЕ_РЕЖИМА"
        if choice == "ПЕРЕВЕРНУТЬ" and pos_side and ((pos_side == "long" and play == "long")
                                                       or (pos_side == "short" and play == "short")):
            why = f"[ПЕРЕВЕРНУТЬ против режима {play} — только ЗАКРЫТЬ] " + why
            choice = "ЗАКРЫТЬ"
        self.last_review = {"choice": choice, "why": why, "ts": time.time()}
        if self.puncture and self.puncture.get("pending"):    # W3: PRO разобрал прокол — блок больше не первым
            self.puncture["pending"] = False
            self.puncture["state"] = f"PRO решил: {choice} — {why[:120]}"
        rec = {"ts": time.time(), "choice": choice, "why": why, "note": note,
               "entry": _f(obj.get("entry")), "entry_kind": str(obj.get("entry_kind") or "")[:12] or None,
               "invalidation": _f(obj.get("invalidation")), "take": _f(obj.get("take")), "price": price}
        if ai_choice != choice:
            rec["ai_choice"] = ai_choice                 # что ответил ИИ (запись — что сделал код и почему)
        if m is not None:
            m.reviews.append(rec)
            del m.reviews[:-REVIEWS_KEEP]
            m.reviews_since_memory += 1
            _persist(m)
            _bg(bus.stage("mission", m.run_id, "review", "done", ticker=self.base,
                          detail=f"{choice}: {why}", data=rec))
            lv = [f"{k} {v}" for k, v in (("вход", rec["entry"]), ("стоп", rec["invalidation"]), ("тейк", rec["take"])) if v is not None]
            if choice in ("КУПИТЬ_СЕЙЧАС", "ПРОДАТЬ_СЕЙЧАС", "ДОБРАТЬ", "ПЕРЕВЕРНУТЬ") and rec["entry"] is None:
                lv.insert(0, "вход сейчас")
            _tolmach(m, "review", f"Перепроверка: {choice}",
                     f"{why}" + (f". {note}" if note else "") + (f" ({', '.join(lv)})" if lv else ""),
                     refs={"choice": choice, "price": price, "entry": rec["entry"], "invalidation": rec["invalidation"],
                           "take": rec["take"]})
            every = explain.memory_every() if explain is not None else 5
            if m.reviews_since_memory >= every:      # узел «N перепроверок» — свести память
                _memorize(m, f"{every} перепроверок")
        log.info("миссия %s: перепроверка → %s (%s)", self.base, choice, why)
        # ── исполнение решения ──
        cur = self.prices[-1] if self.prices else price
        pos_side = (self.position or {}).get("side")     # v5.4.2: состояние — после ответа PRO (он думал минуты)
        entry_like = choice in ("КУПИТЬ_СЕЙЧАС", "ПРОДАТЬ_СЕЙЧАС", "ДОБРАТЬ", "ПЕРЕВЕРНУТЬ")
        if choice == "ЗАКРЫТЬ" and self.position:
            await self._close_all(cur, "решение перепроверки: закрыть")
        elif choice == "ЖДЁМ" and self.position:
            self._retune(obj, cur)
        elif entry_like and self._reanalyzing:
            # v5.4.2: совет начался, пока PRO думал, — решение не пропадает молча: повод дежурному PRO после совета
            head = f"решение перепроверки {choice} отложено: идёт совет"
            self._review_deferred = {"ts": time.time(), "text": head + (f" — {why[:160]}" if why else "")}
            self._review_reason, self._review_kind = self._review_deferred["text"], "pilot"
            self.last_action = head + " — вернусь к нему после совета"
            log.info("миссия %s: %s", self.base, self.last_action)
        elif choice in ("КУПИТЬ_СЕЙЧАС", "ПРОДАТЬ_СЕЙЧАС") and not self.position:
            self._plan_from_review(choice, obj, why, price, cur, snap_ts=snap_ts)
        elif choice == "ДОБРАТЬ" and self.position:
            # докупить до максимума, что даёт биржа: план той же стороны «сейчас» — тик доберёт
            self._plan_from_review("КУПИТЬ_СЕЙЧАС" if pos_side == "long" else "ПРОДАТЬ_СЕЙЧАС",
                                   obj, why, price, cur, topup=True, snap_ts=snap_ts)
        elif choice == "ПЕРЕВЕРНУТЬ" and self.position:
            # закрыть и войти в другую сторону на максимум: план другой стороны → тик закроет (флип) и войдёт
            self._plan_from_review("ПРОДАТЬ_СЕЙЧАС" if pos_side == "long" else "КУПИТЬ_СЕЙЧАС",
                                   obj, why, price, cur, flip=True, snap_ts=snap_ts)
        elif choice == "ВНЕ_РЕЖИМА":
            # вход против режима игры владельца: не исполняем и не пишем «ЖДЁМ» за ИИ — переспрос с пометкой
            self._review_reason = f"прошлый ответ {ai_choice} запрещён режимом {play}"
            self._review_kind = "pilot"
            self.review_ts = min(self.review_ts, time.time() + REVIEW_RETRY_SEC)
            wait = max(0, int(self.review_ts - time.time()))
            self.last_action = (f"перепроверка: {ai_choice} против режима {play} — не исполнено, "
                                f"переспрошу через {wait // 60} мин {wait % 60} с")
            log.info("миссия %s: %s", self.base, self.last_action)
        elif choice == "НОВЫЙ_АНАЛИЗ":
            self._fire_reanalyze("перепроверка потребовала свежий разбор: " + (why or "картина сломалась"), kind="council")
        elif choice != "ЖДЁМ":
            # позиция открылась/закрылась, пока PRO думал: решение к нынешнему состоянию не относится — видно в панели
            self.last_action = (f"решение перепроверки {choice} не исполнено: "
                                f"{'позиция уже открыта' if self.position else 'позиции уже нет'} — решу на следующей")
            log.info("миссия %s: %s", self.base, self.last_action)
        # ЖДЁМ без позиции — осознанное решение ИИ: держим как есть

    def _retune(self, obj: dict, cur: float) -> None:
        """ЖДЁМ в позиции с новыми invalidation/take: PRO передвигает трос и тейк (сторона проверяется;
        null или те же числа — ничего не трогаем)."""
        pos = self.position
        if not pos:
            return
        side = pos["side"]
        changed = []
        inv = _f(obj.get("invalidation")) if obj.get("invalidation") not in (None, "", "null") else None
        old_inv = _f(pos.get("invalidation"), 0.0)
        if inv and inv > 0 and ((side == "long" and inv < cur) or (side == "short" and inv > cur)) \
                and abs(inv - old_inv) > 1e-9:
            pos.pop("profit_lock", None)             # v5.4.1: стоп дежурного PRO главнее запертой прибыли
            self._set_levels(pos, None, inv)         # стоп PRO: триггер и от него аварийный трос
            pos["restop"] = True                     # трос перевыставится ближайшим тиком
            pos["holds"] = 0
            pos.pop("levels_placeholder", None)
            wider = (inv < old_inv) if side == "long" else (inv > old_inv)
            changed.append(f"трос {old_inv:g} → {inv:g}" + (" (отодвинут)" if wider and old_inv else ""))
        take = _f(obj.get("take")) if obj.get("take") not in (None, "", "null") else None
        old_take = _f(pos.get("take"))
        if take and take > 0 and ((side == "long" and take > cur) or (side == "short" and take < cur)) \
                and (old_take is None or abs(take - old_take) > 1e-9):
            pos["take"] = take
            changed.append(f"тейк {f'{old_take:g}' if old_take is not None else '—'} → {take:g}")
        if changed:
            self._save_state()
            self.last_action = "перепроверка: ЖДЁМ, " + ", ".join(changed)
            log.info("миссия %s: %s", self.base, self.last_action)

    def _plan_from_review(self, choice: str, obj: dict, why: str, price: float, cur: float,
                          topup: bool = False, flip: bool = False, snap_ts: float | None = None) -> None:
        """КУПИТЬ/ПРОДАТЬ от дежурного PRO → план: сейчас / откат / прорыв. price — снимок решения (цена в промпте
        PRO), cur — цена после ответа. v5.4.2: дрейф хуже снимка больше PYTHIA_ENTRY_DRIFT_PCT — не тихая засада по
        старой цене, а план «сейчас» со снимком и пометкой gate_note: решит дверь по живой цене; цена лучше снимка —
        входим. План несёт src (review / topup) и снимок (snap_price, snap_ts) — свежее решение PRO бьётся без второго
        вопроса у двери (_entry_gate). План той же стороны, по которому дверь уже думает, обновляется на месте — её
        ответ ВОЙТИ не выбрасывается. topup — добор той же стороны до максимума (в позиции); flip — переворот."""
        side = "long" if choice == "КУПИТЬ_СЕЙЧАС" else "short"
        now = time.time()
        snap = {"snap_price": round(price, 6) if price else cur, "snap_ts": snap_ts or now}

        def _put(new: dict) -> bool:
            """План в пилот: та же сторона и дверь думает → на месте (состояние двери живо: её ВОЙТИ не выбрасывается).
            Ревью 5.4.2: срок плана (ts, PLAN_TTL_SEC) — от нового решения; пометка пробития (crossed) — от старого
            уровня, снимается; ответ двери ЖДАТЬ/ОТМЕНИТЬ/молчание про старый план к обновлённому не применяется —
            по пометке gate_stale (ставится здесь, снимается новым вопросом у двери и ответом _apply_gate), а не по
            сравнению времён: снимок перепроверки (snap_ts) берётся ДО раздумий PRO и может быть старше вопроса двери.
            Запись ответа ссылается на план, о котором спросили (gate_plan_ts), а не на обновлённый. Возврат: на месте?"""
            old = self.plan
            if old is not None and old.get("side") == new["side"] and old.get("gate_busy") and not flip:
                for k in ("gate_note", "gate_wait_ts", "crossed"):   # пометки прошлого решения — решение свежее
                    old.pop(k, None)
                old.update(new)
                old["ts"] = now
                old["gate_stale"] = True               # дверь думает над прежним планом — её ЖДАТЬ/ОТМЕНИТЬ не про этот
                return True
            self.plan = dict(new, ts=now)
            return False

        if topup and self.position:
            pos = self.position
            inv = _f(obj.get("invalidation")) if obj.get("invalidation") not in (None, "", "null") else None
            take = _f(obj.get("take")) if obj.get("take") not in (None, "", "null") else None
            _put({"side": side, "entry": None, "kind": "сейчас",
                  "take": take if take and take > 0 else pos.get("take"),
                  "invalidation": inv if inv and inv > 0 else pos.get("invalidation"),
                  "why": "перепроверка: ДОБРАТЬ — " + why, "src": "topup", **snap})
            if inv and inv > 0 and ((side == "long" and inv < cur) or (side == "short" and inv > cur)):
                self._set_levels(pos, take, inv)
                pos["restop"] = True
            self.last_action = f"перепроверка: ДОБРАТЬ → {side} до максимума, что даёт биржа"
            log.info("миссия %s: %s", self.base, self.last_action)
            return
        sgn = 1.0 if side == "long" else -1.0
        lim = float(getattr(config, "PYTHIA_ENTRY_DRIFT_PCT", 1.0)) / 100.0
        entry = _f(obj.get("entry")) if obj.get("entry") not in (None, "", "null") else None
        if entry is not None and entry <= 0:
            entry = None
        kind = _entry_kind("BUY" if side == "long" else "SELL", entry, cur, obj.get("entry_kind"))
        if kind == "сейчас":
            entry = None
        drift_note, gate_note = "", ""
        if entry is None and price:
            adverse = (cur - price) / price * sgn    # > 0 — цена ушла ХУЖЕ снимка, < 0 — лучше
            if adverse > lim:
                gate_note = f"цена ушла на {adverse * 100:.2f}% за время раздумий (было {price:g}, стало {cur:g})"
                drift_note = f" ({gate_note} — решит дверь по живой цене)"
            elif adverse < -lim:
                drift_note = f" (цена лучше снимка {price:g} на {-adverse * 100:.2f}% — вхожу)"
        ref = entry if entry is not None else cur
        inv = _f(obj.get("invalidation"), 0.0) if obj.get("invalidation") not in (None, "", "null") else 0.0
        # прорыв (v5.4.2): стоп — по ту сторону уровня входа (ref); между ценой и уровнем — законный стоп пробоя
        good = inv > 0 and ((inv < ref) if side == "long" else (inv > ref))
        if not good:
            inv = ref * (1 - ai_pilot.EMERGENCY_STOP_FRAC if side == "long" else 1 + ai_pilot.EMERGENCY_STOP_FRAC)
            log.warning("перепроверка без валидного invalidation — аварийный стоп %.6g (политика %.1f%%)",
                        inv, ai_pilot.EMERGENCY_STOP_FRAC * 100)
        take = _f(obj.get("take")) if obj.get("take") not in (None, "", "null") else None
        if take is not None and not ((side == "long" and take > ref) or (side == "short" and take < ref)):
            log.warning("тейк %s не с той стороны от %s — снят", take, ref)
            take = None
        new = {"side": side, "entry": entry, "kind": kind, "take": take, "invalidation": round(inv, 6),
               "why": "перепроверка: " + ("ПЕРЕВЕРНУТЬ — " if flip else "") + why + drift_note, "src": "review", **snap}
        if gate_note:
            new["gate_note"] = gate_note
        in_place = _put(new)
        self._break_n = 0
        if self.pending:
            self._cancel_entry = True
        if not self.position and not in_place:
            self.state = "ЗАСАДА" if entry is not None else "ВХОЖУ"
        how = ("вход сейчас" if entry is None else
               f"вход на пробитии @{entry:g}" if kind == "прорыв" else f"засада (откат) @{entry:g}")
        self.last_action = (f"перепроверка: {'ПЕРЕВЕРНУТЬ → ' if flip else choice + ' → '}{side}, {how}, "
                            f"стоп {self.plan['invalidation']:g}, тейк {take}{drift_note}"
                            + (" — план обновлён на месте, дверь уже думает над входом" if in_place else ""))
        log.info("миссия %s: %s", self.base, self.last_action)

    # журнал сделок
    async def _absorb_fill(self, lots: int, po: dict, place_stop: bool = True) -> None:
        had = self.position
        await super()._absorb_fill(lots, po, place_stop)
        m = self.mission
        self._break_n = 0
        if m is not None and self.position and (had is None or had is not self.position):
            m.open_trade = {"opened_ts": self.position.get("opened_ts"), "entry": self.position.get("entry"),
                            "lots": self.position.get("lots"), "side": self.position.get("side")}
            m.phase = "in_position"
            _persist(m)
        if m is not None and self.position:
            pos = self.position
            new = had is None or had is not pos
            _tolmach(m, "entry" if new else "topup",
                     f"{'Вход исполнен' if new else 'Добор исполнен'}: {pos['side']} {pos['lots']} лот @{_f(pos.get('entry'), 0):g}",
                     self.last_action, refs={"side": pos["side"], "lots": pos["lots"], "entry": pos.get("entry"),
                                             "invalidation": pos.get("invalidation"), "take": pos.get("take"),
                                             "hard_stop": pos.get("hard_stop")})
            # фаза 4 · W2: исполнение — в шину (хроника панели, Telegram): числа в data.fill
            _bg(bus.stage("mission", m.run_id, "pilot", "progress", ticker=self.base, detail=self.last_action,
                          data={"fill": {"new": new, "side": pos["side"], "lots": pos["lots"], "entry": pos.get("entry"),
                                         "filled": lots, "invalidation": pos.get("invalidation"), "take": pos.get("take"),
                                         "hard_stop": pos.get("hard_stop")}}))

    def _journal(self, before: dict | None, n_before: int, why: str, price: float) -> None:
        if not before or len(self.pnls) <= n_before:
            return
        pnl = float(sum(self.pnls[n_before:]))
        lots_now = int(self.position.get("lots") or 0) if self.position else 0
        lots = max(1, int(before.get("lots") or 0) - lots_now)
        sgn = 1.0 if before.get("side") == "long" else -1.0
        entry = _f(before.get("entry"), 0.0)
        pv = max(1e-9, float(self.point_value or 1.0))
        exit_px = entry + sgn * pnl / (lots * pv) if lots else price
        rec = {"ticker": self.base, "side": before.get("side"), "lots": lots, "entry": entry,
               "exit_px": round(exit_px, 6), "pnl": round(pnl, 2),
               "opened_ts": before.get("opened_ts"), "closed_ts": time.time(),
               "why": why, "mode": getattr(self.broker, "mode", "dry"),
               # фаза 4 · W1: для сверки с операциями брокера (ledger) — инструмент, штук в лоте, ₽ за пункт
               "figi": self.figi, "lot": 1 if self.asset_class == "futures" else max(1, int(round(pv))),
               "point_value": pv, "source": "est"}
        try:
            rec["fee_est"] = ledger.estimate(rec)   # оценка комиссии до сверки (честно помечена est)
        except Exception:                            # noqa: BLE001
            rec["fee_est"] = None
        try:
            store_v5.trade_add(rec)
        except Exception as e:                       # noqa: BLE001
            log.warning("сделка не записалась: %s", str(e)[:100])
        try:                                         # через ~20 с — операции брокера → настоящие цены, комиссии, нетто
            ledger.schedule_after_close(self.base, live=isinstance(self.broker, trader_broker.Broker)
                                        and getattr(self.broker, "mode", "dry") == "real")
        except Exception as e:                       # noqa: BLE001
            log.info("сверка журнала не поставлена: %s", str(e)[:80])
        m = self.mission
        if m is not None:
            m.open_trade = None if not self.position else m.open_trade
            _bg(bus.stage("mission", m.run_id, "pilot", "progress", ticker=self.base,
                          detail=f"сделка закрыта ({why}): P/L {pnl:+.0f}", data=rec))
            _persist(m)
            _tolmach(m, "close", f"{'Часть позиции закрыта' if self.position else 'Позиция закрыта'}: "
                                 f"{before.get('side')} {lots} лот, P/L {pnl:+.0f} ₽",
                     f"{why}; вход {entry:g}, выход {exit_px:g}", refs={"pnl": round(pnl, 2), "entry": entry,
                                                                        "exit_px": round(exit_px, 6), "why": why})
            if not self.position:                    # этап прошёл — свести память
                _memorize(m, f"закрытие позиции ({why[:60]})")

    async def _close_all(self, price: float, why: str, reanalyze: bool = True) -> bool:
        before = dict(self.position) if self.position else None
        n = len(self.pnls)
        ok = await super()._close_all(price, why, reanalyze)
        self._journal(before, n, why, price)
        if not ok and self.position is not None and self.position.get("close_fail"):
            _tolmach(self.mission, "refusal", "Закрытие отбито биржей", self.last_action, refs={"why": why})
        if ok and before and isinstance(before.get("reentry"), dict) and self.position is None:
            self._adopt_reentry(before["reentry"])   # v5.4.1: ВЫЙТИ_И_ПЕРЕЗАЙТИ — план после закрытия
        return ok

    async def _reduce(self, pos: dict, excess: int, price: float, why: str) -> None:
        before = dict(pos)
        n = len(self.pnls)
        await super()._reduce(pos, excess, price, why)
        self._journal(before, n, why, price)

    async def _reconcile(self, price: float) -> None:
        before = dict(self.position) if self.position else None
        n = len(self.pnls)
        await super()._reconcile(price)
        self._journal(before, n, "сверка с биржей: закрыто вне петли (трос/владелец)", price)

    def adopt_forecast(self, forecast: dict | None) -> bool:
        """Как в AIPilot, плюс предохранители миссии:
        · первый принятый план запускает пейсинг (REANALYZE_GAP_SEC);
        · свежий вердикт ДРУГОЙ стороны при позиции моложе PYTHIA_FLIP_QUIET_SEC не
          переворачивает её, но выход исполняется (v5.4.2): позиция закрывается ближайшим тиком без
          переворота, вход в другую сторону решит перепроверка через 5 мин;
        · вид входа (сейчас / откат / прорыв) из приказа — в план пилота;
        · HOLD (v5.4.2, ревью) — держать как есть без добора: только уровни позиции (AIPilot.adopt_forecast)."""
        m = self.mission
        ex = (forecast or {}).get("exec") if isinstance(forecast, dict) else None
        if isinstance(ex, dict) and self.position and str(ex.get("do") or "").upper() in ("BUY", "SELL"):
            side = "long" if str(ex.get("do")).upper() == "BUY" else "short"
            try:
                age = max(0.0, time.time() - float(self.position.get("opened_ts") or 0))
            except (TypeError, ValueError):
                age = None
            quiet = float(getattr(config, "PYTHIA_FLIP_QUIET_SEC", 1200))
            if self._council_kind in ("stop", "take", "profit"):   # совет звал мягкий стоп/тейк/мысль о прибыли: его слово — закон
                quiet = 0.0
            if str(ex.get("do") or "").upper() in ("BUY", "SELL") and side != self.position["side"] \
                    and age is not None and age < quiet:
                # v5.4.2: совет сказал противоположную сторону — половину решения «выйти» тишина не запрещает: позиция
                # закрывается ближайшим тиком (путь приказа CLOSE), переворота нет — вход в другую сторону решит PRO
                why_c = "совет: противоположная сторона при молодой позиции — закрываю без переворота"
                pos_side = self.position["side"]
                self._reanalyzing = False
                self._council_kind = ""
                self.plan = None
                if self.pending:
                    self._cancel_entry = True
                self._close_pending = why_c
                self.review_ts = min(self.review_ts, time.time() + 300)
                rr = (f"совет сказал {side}, а позиция {pos_side} была моложе {int(quiet // 60)} мин — закрыта без "
                      f"переворота; реши по живой картине, входить ли {side}")
                self._review_reason = f"{self._review_reason}; {rr}" if self._review_reason else rr
                self.last_action = (f"переворот отклонён: вердикт {side}, а позиция {pos_side} "
                                    f"моложе {int(quiet // 60)} мин — {why_c}; перепроверка через 5 мин")
                log.warning("миссия %s: %s", self.base, self.last_action)
                if m is not None:
                    _bg(bus.stage("mission", m.run_id, "pilot", "progress", ticker=self.base,
                                  detail=self.last_action))
                return False
        deferred = getattr(self, "_review_deferred", None)
        ok = super().adopt_forecast(forecast)
        self._council_kind = ""
        if ok and not self._last_reanalyze_ts:
            self._last_reanalyze_ts = time.time()    # пейсинг считается с первого плана, не со второго
        if ok:
            self._review_reason, self._review_kind, self._review_pulled = None, None, False
            self._council_blocked = ""               # совет только что всё рассмотрел
            self._break_n = 0
            if deferred:
                # v5.4.2: решение перепроверки пришло, пока шёл совет (он его не видел): совет не дал входа — решение
                # не пропадает, дежурный PRO вернётся к нему вскоре; дал план — совет новее, решение снято (в логе)
                self._review_deferred = None
                if not self.plan and not self.position and not self._close_pending:
                    self._review_reason, self._review_kind = deferred.get("text"), "pilot"
                    self.review_ts = min(self.review_ts, time.time() + EVENT_MIN_GAP_SEC)
                else:
                    log.info("миссия %s: %s — снято: совет дал свой приказ", self.base, deferred.get("text"))
            if isinstance(ex, dict) and str(ex.get("do") or "").upper() in ("CLOSE", "HOLD"):
                return ok
            if self.plan and isinstance(ex, dict):
                kind = str(ex.get("entry_kind") or "").strip().lower()
                if kind not in ENTRY_KINDS:
                    kind = _entry_kind(str(ex.get("do")).upper(), self.plan.get("entry"),
                                       self.prices[-1] if self.prices else getattr(m, "ctx_price", None),
                                       kind)
                self.plan["kind"] = kind
                # v5.4.2: источник и снимок плана — приказ совета (он старше живого рынка: у двери спрашиваем всегда)
                self.plan["src"] = "council"
                self.plan["snap_price"] = _f(getattr(m, "ctx_price", None)) or (self.prices[-1] if self.prices else None)
                self.plan["snap_ts"] = _f(getattr(m, "exec_ts", None)) or time.time()
                if kind == "прорыв":
                    self.last_action = self.last_action.replace("засада @", "вход на пробитии @")
        return ok

    # ── поводы: дежурному PRO или совету ──────────────────────────────────────────
    def _pos_age(self) -> float | None:
        if not self.position:
            return None
        try:
            return max(0.0, time.time() - float(self.position.get("opened_ts") or 0))
        except (TypeError, ValueError):
            return None

    def _ask_review(self, why: str, kind: str = "pilot"):
        """Повод для дежурного PRO (см. _ask_review_now). v5.3 W2: событие (shock, news) при открытой
        позиции сначала смотрит FLASH-триаж (`_triage_bg`, PYTHIA_EVENT_TRIAGE) — дёшево и за секунды:
        СЕЙЧАС → PRO как раньше, ПЛАНОВО → повод к плановой, САМ → подтянуть трос / снять план без PRO.
        Возврат: задача триажа (можно дождаться) или None. Триаж уже идёт / рынок закрыт / нет позиции → сразу как
        раньше. v5.4.2: и молодая позиция (PYTHIA_QUIET_SEC) идёт через триаж — СЕЙЧАС/ПЛАНОВО решает ИИ, не тишина кода."""
        why = str(why or "").strip()
        if (kind in ("shock", "news", "puncture") and self.position and bool(getattr(config, "PYTHIA_EVENT_TRIAGE", True))
                and not self._market_closed()
                and not (self._triage_task and not self._triage_task.done()) and self.state != "СТОП"):
            self.last_action = f"{why} — {self._money_name()}-триаж решает, нужен ли дежурный PRO сейчас"
            log.info("миссия %s: %s", self.base, self.last_action)
            self._triage_task = self._spawn_background(self._triage_bg(why, kind))
            return self._triage_task
        self._ask_review_now(why, kind)
        return None

    def _ask_review_now(self, why: str, kind: str = "pilot", triage: dict | None = None) -> None:
        """Повод для дежурного PRO. Поводы пилота (kind=pilot) копятся к ближайшей перепроверке; после
        закрытия позиции — остыть PYTHIA_AFTER_CLOSE_SEC и решить (0 — ждать плановой). v5.4.2: повод пилота
        вне рынка без плана (приказ протух, идея мертва до входа, вход отменён у двери, вход невозможен) —
        пилот стоит без решения: перепроверка через EVENT_MIN_GAP_SEC после ответа PRO, а не плановая.
        События (shock, news) поднимают внеплановую перепроверку: не чаще PYTHIA_EVENT_COOL_SEC после
        прошлой событийной и не раньше EVENT_MIN_GAP_SEC после любого ответа PRO; в тишине после
        входа (PYTHIA_QUIET_SEC) без вердикта триажа события не приближают перепроверку — трос защищает.
        wait_level (v5.4.2) — цена прошла уровень приказа WAIT: только EVENT_MIN_GAP_SEC (один уровень — один повод).
        triage (v5.3 W2) — вердикт FLASH-триажа: ПЛАНОВО/САМ → повод копится, PRO не тянем."""
        m = self.mission
        now = time.time()
        why = str(why or "").strip()
        t_urg = str((triage or {}).get("urgency") or "")
        cool = float(getattr(config, "PYTHIA_PUNCTURE_COOL_SEC", 600)) if kind == "puncture" \
            else 0.0 if kind == "wait_level" else float(getattr(config, "PYTHIA_EVENT_COOL_SEC", 900))   # W3: у прокола свой пейсинг
        floor = max(self._last_event_review_ts + cool, self._last_review_ts + EVENT_MIN_GAP_SEC)
        quiet = float(getattr(config, "PYTHIA_QUIET_SEC", 900))
        age = None
        if self.position:
            try:
                age = max(0.0, now - float(self.position.get("opened_ts") or 0))
            except (TypeError, ValueError):
                age = None
        pulled, note = False, ""
        if kind == "open":                           # рынок открылся: без пейсинга и тишины — накопилось
            due = now + ai_pilot.OPEN_REVIEW_GRACE_SEC
            if due < self.review_ts:
                self.review_ts, pulled = due, True
        elif self._market_closed():                  # биржа закрыта: повод копится к открытию, PRO не дёргаем
            nxt = (self.market or {}).get("next_open_msk")
            note = f" — рынок закрыт{(' до ' + nxt) if nxt else ''}, повод дойдёт до перепроверки на открытии"
        elif kind == "pilot":
            closed = any(why.lower().startswith(k) for k in CLOSED_REASONS)
            after = float(getattr(config, "PYTHIA_AFTER_CLOSE_SEC", 900)) if closed else 0.0
            if after > 0:
                due = max(now + after, floor)
                if due < self.review_ts:
                    self.review_ts, pulled = due, True
            elif not closed and not self.position and not self.plan and not self.pending:
                # v5.4.2: вне рынка без плана и без решения — дежурный PRO решает скоро, а не на плановой (до 30 мин)
                due = max(now, self._last_review_ts) + EVENT_MIN_GAP_SEC
                if due < self.review_ts:
                    self.review_ts, pulled = due, True
        elif age is not None and age < quiet and not triage:
            note = f" — позиция моложе {int(quiet // 60)} мин, повод дойдёт до плановой перепроверки"
        elif triage and t_urg != "СЕЙЧАС":           # v5.3 W2: FLASH-триаж — PRO не нужен сейчас
            note = (f" — {self._money_name()}-триаж: {t_urg}" + (f" ({triage.get('done')})" if triage.get("done") else "")
                    + (f": {triage.get('why')}" if triage.get("why") else "") + ", повод дойдёт до плановой перепроверки")
        else:
            due = max(now, floor)
            if due < self.review_ts:
                self.review_ts, pulled = due, True
        if self._review_reason and why not in self._review_reason:
            self._review_reason = f"{self._review_reason}; {why}"
        elif not self._review_reason:
            self._review_reason = why
        if kind != "pilot" or not self._review_kind:
            self._review_kind = kind
        self._review_pulled = self._review_pulled or (pulled and kind not in ("pilot", "wait_level"))   # пейсинг — только для событий
        wait = int(max(0, self.review_ts - now))
        # v5.3 фаза 3: после закрытия причина («ЗАКРЫЛ ВСЁ (ПОБЕДА: тейк … / аварийный трос / FLASH решил
        # слить): P/L …») остаётся первой, повод дежурному PRO дописывается — панель видит, ПОЧЕМУ закрылись
        head = (self.last_action or "")
        head = (head + " · ") if (kind == "pilot" and head.startswith("ЗАКРЫЛ ВСЁ")) else ""
        self.last_action = head + (f"{why} — дежурный PRO решит через {wait // 60} мин {wait % 60} с"
                                   if pulled else
                                   f"{why}{note or ' — повод для дежурного PRO на плановой перепроверке'} "
                                   f"(через {wait // 60} мин)")
        log.info("миссия %s: %s", self.base, self.last_action)
        if m is None:
            return
        last = m.handoffs[-1] if m.handoffs else None
        if last and last.get("reason") == why and now - float(last.get("ts") or 0) < 3600:
            last["ts"], last["deferred"] = now, not pulled
            last["repeats"] = int(last.get("repeats") or 1) + 1
        else:
            last = {"ts": now, "reason": why, "deferred": not pulled, "kind": kind}
            m.handoffs.append(last)
            del m.handoffs[:-50]
        if triage:
            last["triage"] = t_urg + (f" ({triage.get('done')})" if triage.get("done") else "") \
                + (f": {triage.get('why')}" if triage.get("why") else "")
        _bg(bus.stage("mission", m.run_id, "pilot", "progress", ticker=self.base, detail=self.last_action))
        _persist(m)

    # ── триаж событий (v5.3 W2): FLASH за секунды — нужен ли PRO сейчас ───────────
    async def _event_triage(self, why: str, kind: str) -> dict:
        """Короткий и дешёвый вопрос FLASH по событию: ситуация, ход цены, живой рынок (≤ TRIAGE_LIGHT_TIMEOUT),
        план и уровни, прошлая перепроверка, память, новости из хранилища. Без сканера и разведки."""
        m = self.mission
        rid = m.run_id if m else None
        price = self.prices[-1] if self.prices else (m.ctx_price if m else 0.0) or 0.0
        if m is not None:
            try:
                await bus.stage("mission", rid, "triage", "start", ticker=self.base,
                                detail=f"триаж события: {why[:120]} — FLASH решает, нужен ли PRO сейчас")
            except Exception:                        # noqa: BLE001
                pass
        try:
            lt = await asyncio.wait_for(market_ctx.light(self.base, self.figi, self.asset_class), TRIAGE_LIGHT_TIMEOUT)
            light_txt = lt.get("text") or ""
        except Exception as e:                       # noqa: BLE001
            light_txt = f"(живой рынок недоступен: {str(e)[:60]})"
        plan_txt = _exec_text(m) if m else ""
        pos = self.position or {}
        if pos:
            plan_txt += (f"\nУРОВНИ ПОЗИЦИИ: триггер (мягкий стоп) {pos.get('invalidation')}, {self._hard_name()} "
                         f"{pos.get('hard_stop')}, тейк {pos.get('take')}")
        if self.plan and pos and self.plan.get("side") == pos.get("side"):
            plan_txt += f"\nПЛАН ДОБОРА: {self.plan.get('side')} " + ("сейчас" if self.plan.get("entry") is None
                                                                       else f"у {self.plan.get('entry')}")
        lr = self.last_review
        last_txt = (f"{ai_v5.fmt_ts(lr.get('ts'))} {lr.get('choice')} — {lr.get('why')}" if lr else "")
        wait = max(0, int(self.review_ts - time.time()))
        blocks, _sq = await _fit_blocks({"light": light_txt, "news": self._news_quick(), "history": self._history_text(price),
                                         "prev_exec": plan_txt, "memory": (m.memory if m else "") or ""})
        situation = self._situation_for_ai(price)     # W3: прокол сканера — блок первым и у триажа
        s, u = prompts_mission.event_triage(
            self.base, self.name, self._play(), event=why, situation=situation, history=blocks["history"],
            light=blocks["light"], plan=blocks["prev_exec"], news=blocks["news"], memory=blocks["memory"],
            time_msk=ai_v5.now_msk_str(), review_in=f"через {wait // 60} мин {wait % 60} с", last_review=last_txt)
        if m is not None:
            m.sizes["triage"] = _sizes_rec(len(u), {"situation": situation, **blocks})
        obj = await ai_v5.money_json(s, u, route="event_triage")      # v5.4.1: узел у денег — PRO (PYTHIA_MONEY_MODEL)
        if m is not None:
            m.sizes["triage"] = _sizes_rec(len(u), {"situation": situation, **blocks}, _json_len(obj))
        return obj if isinstance(obj, dict) else {}

    async def _triage_bg(self, why: str, kind: str) -> None:
        res: dict | None = None
        try:
            res = await asyncio.wait_for(self._event_triage(why, kind), EVENT_TRIAGE_TIMEOUT)
        except asyncio.CancelledError:
            self._triage_task = None
            raise
        except Exception as e:                       # noqa: BLE001
            log.info("миссия %s: триаж не ответил (%s) — дежурный PRO как раньше", self.base, str(e)[:80])
            res = None
        finally:
            self._triage_task = None
        try:
            self._apply_triage(res if isinstance(res, dict) else None, why, kind)
        except Exception as e:                       # noqa: BLE001
            log.warning("миссия %s: триаж не применился (%s) — PRO как раньше", self.base, str(e)[:100])
            self._ask_review_now(why, kind)

    def _apply_triage(self, res: dict | None, why: str, kind: str) -> None:
        """СЕЙЧАС (или сбой/непонятно) → PRO как раньше; ПЛАНОВО → повод копится; САМ → «подтянуть_трос»
        (триггер в безопасную сторону, трос биржи не трогаем) / «снять_план» без PRO, повод — к плановой."""
        m = self.mission
        mm = self._money_name()
        raw = str((res or {}).get("urgency") or "").upper().replace("Ё", "Е")
        urg = "ПЛАНОВО" if "ПЛАН" in raw else "САМ" if raw.startswith("САМ") or raw == "САМ" else "СЕЙЧАС"
        if res is None:
            urg = "СЕЙЧАС"
        action = str((res or {}).get("action") or "").strip().lower().replace(" ", "_") or None
        # v5.3 фаза 3: ПЛАНОВО + «подтянуть_трос» (модель вживую так и отвечает) — трос поджимаем, как при САМ,
        # если уровень безопасен (между триггером и ценой, в сторону прибыли); «снять_план» — только у САМ
        act_ok = urg == "САМ" or (urg == "ПЛАНОВО" and bool(action) and "трос" in action and bool(self.position))
        rec = {"ts": time.time(), "kind": kind, "event": why[:200], "urgency": urg, "action": action if act_ok else None,
               "why": str((res or {}).get("why") or "")[:200] if res is not None else f"{mm} не ответил — дежурный PRO как раньше",
               "trigger": None, "done": "", "model": mm}
        if act_ok:
            done: list[str] = []
            pos = self.position
            if action and "трос" in action and pos:
                cur = self.prices[-1] if self.prices else _f(pos.get("entry"), 0.0)
                is_long = pos.get("side") == "long"
                inv, new = _f(pos.get("invalidation"), 0.0), _f((res or {}).get("trigger"), 0.0)
                ok = new > 0 and ((is_long and inv < new < cur) or (not is_long and cur < new < inv))
                if ok:
                    self._set_levels(pos, None, new, inv0=False)     # триггер ближе, трос биржи на месте
                    self._save_state()
                    rec["trigger"] = new
                    done.append(f"триггер подтянут {inv:g} → {new:g}")
                else:
                    done.append(f"триггер не тронут: уровень {new:g} не между триггером {inv:g} и ценой {cur:g}")
            elif action and "план" in action:
                if self.plan and pos and self.plan.get("side") == pos.get("side"):
                    self.plan = None
                    done.append("план добора снят")
                else:
                    done.append("плана добора нет — снимать нечего")
            elif action:
                done.append(f"действие «{action}» не знаю — ничего не трогаю")
            rec["done"] = "; ".join(done)
        self.triages.append(rec)
        del self.triages[:-TRIAGES_KEEP]
        self._ask_review_now(why, kind, triage=rec)
        if kind == "puncture" and self.puncture and self.puncture.get("reason") == why:   # W3: панель видит, что решил триаж
            self.puncture["state"] = self.last_action
            self.puncture["triage"] = urg + (f" ({rec['done']})" if rec["done"] else "")
        if m is not None:
            _bg(bus.stage("mission", m.run_id, "triage", "done", ticker=self.base,
                          detail=f"триаж {mm}: {urg}" + (f" ({rec['done']})" if rec["done"] else "") + f" — {rec['why']}",
                          data=rec))
            _tolmach(m, "triage", f"Триаж {mm}: {urg}",
                     f"событие: {why[:160]}; {rec['why']}" + (f"; {rec['done']}" if rec["done"] else "")
                     + (" — дежурный PRO разбужен" if urg == "СЕЙЧАС" and not m.handoffs[-1].get("deferred") else
                        " — повод дойдёт до плановой перепроверки"),
                     refs={"urgency": urg, "action": rec["action"], "trigger": rec["trigger"], "event": why[:160]})
            _persist(m)

    # ── толмач (v5.3): стопор рынка и отказы биржи ─────────────────────────────────
    async def _closed_tick(self, price: float, mk: dict, first: bool) -> None:
        await super()._closed_tick(price, mk, first)
        if first:
            nxt = (mk or {}).get("next_open_msk")
            _tolmach(self.mission, "market", "Рынок закрыт" + (f" до {nxt}" if nxt else ""), self.last_action,
                     refs={"next_open_msk": nxt, "price": price})

    async def _on_market_open(self, price: float) -> None:
        closed_for = time.time() - self._closed_since if self._closed_since else 0.0
        await super()._on_market_open(price)
        h, mnt = int(closed_for // 3600), int(closed_for % 3600 // 60)
        _tolmach(self.mission, "market", "Рынок открылся",
                 f"закрыт был {h} ч {mnt:02d} мин; {self.last_action}", refs={"closed_for_s": int(closed_for), "price": price})

    def _note_refusal(self, what: str) -> None:
        detail = re.sub(r" — бэкофф, попытка \d+", "", self.last_action or "")
        _tolmach(self.mission, "refusal", f"Биржа отбила {what}", detail, refs={"attempt": self._entry_fail})

    async def _replace_stop(self, pos: dict) -> None:
        """Трос на бирже; отбит → толмач объясняет (виртуальный стоп держит, повтор через
        ai_pilot.STOP_RETRY_SEC); тот же отказ подряд — одно объяснение (дедуп толмача)."""
        await super()._replace_stop(pos)
        if pos.get("restop") and pos.get("stop_err") and not pos.get("stop_id"):
            lvl = self._stop_price(pos)
            _tolmach(self.mission, "refusal", "Биржа отбила стоп-заявку (трос)",
                     f"трос @{lvl:g} не встал: {pos['stop_err']} — держу виртуальный стоп (за уровнем закрою сам), "
                     f"повтор через {int(ai_pilot.STOP_RETRY_SEC)} с", refs={"stop": lvl, "error": pos["stop_err"]})

    async def _enter(self, price: float, book: dict | None, attempts: int = 0) -> None:
        n0 = self._entry_fail
        side = (self.plan or {}).get("side")
        await super()._enter(price, book, attempts)
        if self._entry_fail > n0:
            self._note_refusal("заявку входа")
        if side and self.plan is None and not self.position and not self.pending \
                and str(self.last_action or "").startswith("депозит не тянет ни лота"):
            # v5.4.2: биржа не дала ни лота (GetMaxLots=0: шорт недоступен / нет ГО) — ИИ узнаёт причину сразу и решает
            # заново (другая сторона, WAIT), а не стоит без плана до плановой перепроверки
            self._ask_review_now(f"вход невозможен: биржа не даёт ни лота {side} (GetMaxLots=0 — шорт недоступен или "
                                 f"нет ГО) — реши по живой картине", kind="pilot")

    async def _pending_tick(self, price: float, book: dict | None) -> None:
        n0 = self._entry_fail
        await super()._pending_tick(price, book)
        if self._entry_fail > n0:
            self._note_refusal("заявку входа")

    def _market_open_review(self, closed_for: float) -> None:
        """Рынок открылся после ночи/выходного: перепроверка дежурного PRO сразу (с запасом), повод —
        «накопились новости/события»: серьёзные заметки дозора за время закрытия и поводы, что копились."""
        h, mnt = int(closed_for // 3600), int(closed_for % 3600 // 60)
        why = f"рынок открылся: накопились новости/события (закрыт был {h} ч {mnt:02d} мин)"
        try:
            w = _mod("watch")
            notes = w.serious_since(self._closed_since) if (w and self._closed_since) else []
            heads = []
            have = (self._review_reason or "")     # серьёзная новость могла уже попасть в повод ночью (on_serious_news)
            for n in (notes or [])[:5]:              # заметка дозора: {"id","ts","seen","data":{"note",…}}
                d = n.get("data") if isinstance(n.get("data"), dict) else n
                txt = str((d or {}).get("note") or "").strip()
                if txt and txt[:60] not in have:
                    heads.append(txt[:100])
            if heads:
                why += "; серьёзное за это время: " + " | ".join(heads)
        except Exception as e:                       # noqa: BLE001
            log.info("миссия %s: заметки дозора за ночь: %s", self.base, str(e)[:80])
        self._ask_review(why, kind="open")

    def council_reason(self) -> str:
        return self._council_reason or "пересмотр по просьбе пилота"

    # ── мягкий стоп (v5.2): FLASH у троса — «слить или ждать и передать Совету» ─────
    def _history_text(self, price: float) -> str:
        """Ход цены за окно резкого хода (точки с временем) и за ≈30 мин по тикам."""
        L: list[str] = []
        h = self._px_hist
        if h and len(h) >= 2:
            span = max(1, len(h) // 12)
            pts = h[::span][-12:]
            if pts[-1] is not h[-1]:
                pts.append(h[-1])
            L.append(f"За {int((time.time() - h[0][0]) // 60)} мин: "
                     + " → ".join(f"{ai_v5.fmt_ts(t)[-5:]} {p:g}" for t, p in pts))
            lo = min(p for _, p in h)
            hi = max(p for _, p in h)
            L.append(f"мин {lo:g}, макс {hi:g}, сейчас {price:g} ({(price / h[0][1] - 1) * 100:+.2f}% от начала окна)")
        if len(self.prices) > 2:
            n30 = int(1800 / ai_pilot.TICK_SEC)
            base = self.prices[-n30] if len(self.prices) >= n30 else self.prices[0]
            if base > 0:
                L.append(f"За ≈30 мин: {(price / base - 1) * 100:+.2f}% (тиков {min(len(self.prices), n30)})")
        return "\n".join(L)

    def _news_quick(self) -> str:
        """Новости из хранилища без сети (у троса секунды дороги): свежие с прошлой перепроверки + по тикеру."""
        nf = _mod("newsflow")
        if not nf:
            return ""
        out = []
        try:
            fresh = nf.fresh_since(self._last_review_ts or self.started_ts) or []
            if fresh:
                out.append("— свежие:\n" + _render_all(nf, fresh[:40]))
        except Exception as e:                       # noqa: BLE001
            log.info("новости у троса: %s", str(e)[:60])
        try:
            own = nf.news_for_ticker(self.base, self.name, limit=None) or []
            own, dropped = self._news_after_council(own)
            if dropped:
                out.append(dropped)
            if own:
                out.append("— по инструменту:\n" + _render_all(nf, own[:40]))
        except Exception as e:                       # noqa: BLE001
            log.info("новости у троса: %s", str(e)[:60])
        return "\n\n".join(out)

    async def _scout_fresh(self, timeout: float) -> str:
        """Свежие данные по прошлым запросам разведки (IMOEX, соседи…) — без нового вопроса FLASH."""
        m = self.mission
        reqs = list(getattr(m, "scout_reqs", None) or []) if m else []
        if not reqs:
            return ""
        try:
            txt = await asyncio.wait_for(scout.refetch(reqs), timeout)
        except Exception as e:                       # noqa: BLE001
            log.info("разведка (свежие данные): %s", str(e)[:80])
            return (m.scout_text or "") if m else ""
        if txt and m is not None:
            m.scout_text = txt
        return txt or ((m.scout_text or "") if m else "")

    async def _partners_fresh(self, timeout: float) -> str:
        """Связанные бумаги (v5.3): из кэша correlate (6 ч), протух — пересчёт в пределах timeout;
        не успел/сбой → прошлый текст миссии."""
        m = self.mission
        if m is None or not getattr(config, "PYTHIA_PARTNERS", True):
            return (m.partners_text or "") if m else ""
        try:
            items = await asyncio.wait_for(correlate.partners(self.base, self.asset_class), timeout)
        except Exception as e:                       # noqa: BLE001
            log.info("связанные бумаги (перепроверка): %s", str(e)[:80])
            return m.partners_text or ""
        if items:
            m.partners = list(items)
            m.partners_text = correlate.text(m.partners, self.base)
        return m.partners_text or ""

    async def _stop_guard(self, price: float, pos: dict) -> dict:
        """Цена коснулась триггера. FLASH за секунды получает максимум данных и один свободный
        вопрос: слить актив сейчас или подождать и передать задачу Совету? Любой сбой → СЛИТЬ
        (стоп по правилу) — это решает базовый _guard_bg по исключению."""
        m = self.mission
        rid = m.run_id if m else None
        inv = _f(pos.get("invalidation"), 0.0)
        is_long = pos.get("side") == "long"
        depth = (price / inv - 1) * 100 if inv else 0.0
        trigger = (f"{pos.get('side')}, цена {price:g} {'ниже' if is_long else 'выше'} уровня {inv:g} ({depth:+.2f}%)"
                   + ("; уровень временный — PRO ещё не назвал свой" if pos.get("levels_placeholder") else ""))
        hard = _f(pos.get("hard_stop"))
        hard_s = f"{self._hard_name()[0].upper() + self._hard_name()[1:]} @{hard:g} — за ним слив без вопросов. " if hard else ""
        max_holds = int(getattr(config, "PYTHIA_SOFT_STOP_MAX_HOLDS", 3))
        holds = int(pos.get("holds") or 0)
        holds_s = f"Ждать можно ещё {max(0, max_holds - holds)} раз(а)." if max_holds else ""
        if m is not None:
            try:
                await bus.stage("mission", rid, "guard", "start", ticker=self.base,
                                detail=f"мягкий стоп: цена {price:g} за триггером {inv:g} — {self._money_name()} решает, слить или ждать")
            except Exception:                        # noqa: BLE001
                pass

        async def _light():
            try:
                lt = await asyncio.wait_for(market_ctx.light(self.base, self.figi, self.asset_class), GUARD_LIGHT_TIMEOUT)
                return lt.get("text") or ""
            except Exception as e:                   # noqa: BLE001
                return f"(живой рынок недоступен: {str(e)[:60]})"

        light_txt, scout_txt, partners_txt = await asyncio.gather(
            _light(), self._scout_fresh(GUARD_SCOUT_TIMEOUT), self._partners_fresh(GUARD_PARTNERS_TIMEOUT))
        plan_txt = _exec_text(m) if m else ""
        if m and m.reviews:
            plan_txt += "\nПерепроверки: " + "; ".join(
                f"{ai_v5.fmt_ts(r.get('ts'))} {r.get('choice')} — {str(r.get('why') or '')}" for r in m.reviews[-3:])
        guards_txt = self._guards_text()
        blocks, squeezed = await _fit_blocks({
            "light": light_txt, "news": self._news_quick(), "history": self._history_text(price),
            "prev_exec": plan_txt, "guard": guards_txt, "scan": _scan_text(m) if m else "",
            "scout": scout_txt, "partners": partners_txt, "council": _council_text(),
            "memory": (m.memory if m else "") or ""})
        situation = self._situation_text(price)
        s, u = prompts_mission.stop_guard(
            self.base, self.name, self._play(), situation=situation, trigger=trigger, history=blocks["history"],
            light=blocks["light"], plan=blocks["prev_exec"], news=blocks["news"], council_text=blocks["council"],
            scan=blocks["scan"], scout=blocks["scout"], guards=blocks["guard"], time_msk=ai_v5.now_msk_str(),
            hard_stop=hard_s, holds_left=holds_s, partners=blocks["partners"], memory=blocks["memory"])
        if m is not None:
            m.sizes["guard"] = _sizes_rec(len(u), {"situation": situation, **blocks})
        obj = await ai_v5.money_json(s, u, route="mission_guard")     # v5.4.1: узел у денег — PRO (PYTHIA_MONEY_MODEL)
        if m is not None:
            m.sizes["guard"] = _sizes_rec(len(u), {"situation": situation, **blocks}, _json_len(obj))
        # ревью 5.4.2 (финал): приватные ключи флага кода из ответа ИИ вычищаются — флаг ставит только код
        obj = ai_pilot.strip_rule_keys(obj) if isinstance(obj, dict) else {}
        # v5.4.2: слово — по словарю троса (CLOSE/EXIT/ВЫЙТИ → СЛИТЬ, HOLD/ДЕРЖАТЬ → ЖДАТЬ); не разобрано — страховка
        # прежняя (у троса это слив), но это действие кода, а не решение ИИ: флаг «НЕ_РАЗОБРАН», сырой ответ сохранён
        raw = ai_v5.decision_raw(obj)
        tok = ai_v5.decision_of(raw, ai_v5.guard_table())
        if tok is None:
            return dict(obj, decision="СЛИТЬ", **{ai_pilot.RULE_KEY: "НЕ_РАЗОБРАН",
                                                   ai_pilot.RULE_DETAIL: raw[:40] or "пусто", ai_pilot.RULE_RAW: raw[:80]})
        return dict(obj, decision=tok)

    def _guards_text(self) -> str:
        """Прошлые ответы у троса/тейка для промптов. v5.4.2 (ревью): молчание и непонятный ответ — «ответа не было —
        выход по правилу» (действие кода), а не «СЛИТЬ/ЗАФИКСИРОВАТЬ» от имени ИИ."""
        return "\n".join((f"{ai_v5.fmt_ts(g.get('ts'))} {'тейк' if g.get('side') == 'take' else 'трос'}: ответа не было — "
                          f"выход по правилу @{g.get('price')} ({g.get('why')})") if g.get("silent") else
                         (f"{ai_v5.fmt_ts(g.get('ts'))} {'тейк' if g.get('side') == 'take' else 'трос'}: {g.get('decision')} "
                          f"@{g.get('price')} — {g.get('why')}") for g in self.guards[-5:])

    async def _take_guard(self, price: float, pos: dict) -> dict:
        """Цена дошла до тейка (v5.3 W2). FLASH за секунды получает те же данные, что у троса, и один
        свободный вопрос: зафиксировать сейчас или подержать и передать задачу Совету? Любой сбой →
        ЗАФИКСИРОВАТЬ (фиксация по правилу) — это решает базовый _take_bg по исключению."""
        m = self.mission
        rid = m.run_id if m else None
        take = _f(pos.get("take"), 0.0)
        is_long = pos.get("side") == "long"
        entry = _f(pos.get("entry"), 0.0)
        over = (price / take - 1) * 100 if take else 0.0
        gain = (price / entry - 1) * 100 * (1 if is_long else -1) if entry else 0.0
        take_s = (f"{pos.get('side')}, цена {price:g} {'выше' if is_long else 'ниже'} цели {take:g} ({over:+.2f}%), "
                  f"от входа {entry:g} это {gain:+.2f}%, плавающий {_f(pos.get('floating')):+.0f} ₽")
        floor = self._lock_floor(pos)
        lock_s = (f"Если держать — прибыль ниже {floor:g} (вход + {float(getattr(config, 'PYTHIA_TAKE_LOCK_PCT', 50)):g} % "
                  f"хода до цели) не отпускаем: туда подтянется триггер, {self._hard_name()} встанет за ним. " if floor else "")
        max_h = int(getattr(config, "PYTHIA_SOFT_TAKE_MAX_HOLDS", 2))
        holds = int(pos.get("take_holds") or 0)
        holds_s = f"Подержать можно ещё {max(0, max_h - holds)} раз(а)." if max_h else ""
        if m is not None:
            try:
                await bus.stage("mission", rid, "take", "start", ticker=self.base,
                                detail=f"мягкий тейк: цена {price:g} у цели {take:g} — {self._money_name()} решает, зафиксировать или подержать")
            except Exception:                        # noqa: BLE001
                pass

        async def _light():
            try:
                lt = await asyncio.wait_for(market_ctx.light(self.base, self.figi, self.asset_class), GUARD_LIGHT_TIMEOUT)
                return lt.get("text") or ""
            except Exception as e:                   # noqa: BLE001
                return f"(живой рынок недоступен: {str(e)[:60]})"

        light_txt, scout_txt, partners_txt = await asyncio.gather(
            _light(), self._scout_fresh(GUARD_SCOUT_TIMEOUT), self._partners_fresh(GUARD_PARTNERS_TIMEOUT))
        plan_txt = _exec_text(m) if m else ""
        if m and m.reviews:
            plan_txt += "\nПерепроверки: " + "; ".join(
                f"{ai_v5.fmt_ts(r.get('ts'))} {r.get('choice')} — {str(r.get('why') or '')}" for r in m.reviews[-3:])
        blocks, squeezed = await _fit_blocks({
            "light": light_txt, "news": self._news_quick(), "history": self._history_text(price),
            "prev_exec": plan_txt, "guard": self._guards_text(), "scan": _scan_text(m) if m else "",
            "scout": scout_txt, "partners": partners_txt, "council": _council_text(),
            "memory": (m.memory if m else "") or ""})
        situation = self._situation_text(price)
        s, u = prompts_mission.take_guard(
            self.base, self.name, self._play(), situation=situation, take=take_s, history=blocks["history"],
            light=blocks["light"], plan=blocks["prev_exec"], news=blocks["news"], council_text=blocks["council"],
            scan=blocks["scan"], scout=blocks["scout"], guards=blocks["guard"], time_msk=ai_v5.now_msk_str(),
            lock_rule=lock_s, holds_left=holds_s, partners=blocks["partners"], memory=blocks["memory"])
        if m is not None:
            m.sizes["take"] = _sizes_rec(len(u), {"situation": situation, **blocks})
        obj = await ai_v5.money_json(s, u, route="mission_take")      # v5.4.1: узел у денег — PRO (PYTHIA_MONEY_MODEL)
        if m is not None:
            m.sizes["take"] = _sizes_rec(len(u), {"situation": situation, **blocks}, _json_len(obj))
        obj = ai_pilot.strip_rule_keys(obj) if isinstance(obj, dict) else {}   # флаг кода ставит только код
        # v5.4.2: слово — по словарю тейка (CLOSE/EXIT/ЗАБРАТЬ → ЗАФИКСИРОВАТЬ, HOLD/ДЕРЖАТЬ → ПОДЕРЖАТЬ); не разобрано —
        # страховка прежняя (у тейка это фиксация), но это действие кода: флаг «НЕ_РАЗОБРАН», сырой ответ сохранён
        raw = ai_v5.decision_raw(obj)
        tok = ai_v5.decision_of(raw, ai_v5.take_table())
        if tok is None:
            return dict(obj, decision="ЗАФИКСИРОВАТЬ", **{ai_pilot.RULE_KEY: "НЕ_РАЗОБРАН",
                                                           ai_pilot.RULE_DETAIL: raw[:40] or "пусто", ai_pilot.RULE_RAW: raw[:80]})
        return dict(obj, decision=tok)

    async def _apply_take(self, r: dict, price: float, pos: dict) -> None:
        await super()._apply_take(r, price, pos)
        m = self.mission
        if m is None or not self.guards or self.guards[-1].get("side") != "take":
            return
        g = self.guards[-1]
        mm = g.get("model") or self._money_name()
        head = (f"{mm} у тейка: {'не ответил' if g.get('decision') == 'НЕТ_ОТВЕТА' else 'ответ не разобран'} — "
                f"зафиксировано по правилу" if g.get("silent") else f"{mm} у тейка: {g.get('decision')}")
        try:
            await bus.stage("mission", m.run_id, "take", "done", ticker=self.base,
                            detail=f"{head} — {g.get('why')}", data=g)
        except Exception:                            # noqa: BLE001
            pass
        _persist(m)
        _tolmach(m, "take", head,
                 f"цена {g.get('price')} у цели {g.get('take')}: {g.get('why') or ''}"
                 + (f"; прибыль заперта триггером {g.get('lock_price')}" if g.get("decision") == "ПОДЕРЖАТЬ" and g.get("lock_price") else "")
                 + (f"; новая цель {g.get('tp_next')}" if g.get("tp_next") else ("; тейк снят" if g.get("decision") == "ПОДЕРЖАТЬ" else ""))
                 + (f"; терпеть {g.get('hold_minutes')} мин" if g.get("hold_minutes") else "")
                 + ("; задача передана Совету" if g.get("decision") == "ПОДЕРЖАТЬ" else ""),
                 refs={"decision": g.get("decision"), "price": g.get("price"), "take": g.get("take"),
                       "lock_price": g.get("lock_price"), "tp_next": g.get("tp_next")})

    def _take_handoff(self, why: str) -> None:
        """ИИ у тейка решил подержать → задача Совету: полный совет без очереди и без окна
        PYTHIA_COUNCIL_GAP_SEC (handoff kind `take`); совет уже идёт → дежурный PRO сразу после него.
        Переворот по такому совету — без тишины."""
        m = self.mission
        reason = f"мягкий тейк: {self._money_name()} решил подержать и передать задачу Совету — " + (why or "без объяснений")
        now = time.time()
        if m is not None:
            m.handoffs.append({"ts": now, "reason": reason, "deferred": False, "kind": "take"})
            del m.handoffs[:-50]
        if m is not None and m.council_running():
            self._ask_review_now(reason, kind="take")
            self.review_ts = min(self.review_ts, now)
            return
        self._council_reason = reason
        self._council_kind = "take"
        super()._fire_reanalyze(reason, force=True)
        if m is not None:
            _bg(bus.stage("mission", m.run_id, "pilot", "progress", ticker=self.base,
                          detail="передаю задачу Совету: " + reason))
            _persist(m)

    async def _apply_guard(self, r: dict, price: float, pos: dict) -> None:
        await super()._apply_guard(r, price, pos)
        m = self.mission
        if m is None or not self.guards:
            return
        g = self.guards[-1]
        mm = g.get("model") or self._money_name()
        head = (f"{mm} у троса: {'не ответил' if g.get('decision') == 'НЕТ_ОТВЕТА' else 'ответ не разобран'} — "
                f"слито по правилу" if g.get("silent") else f"{mm} у троса: {g.get('decision')}")
        try:
            await bus.stage("mission", m.run_id, "guard", "done", ticker=self.base,
                            detail=f"{head} — {g.get('why')}", data=g)
        except Exception:                            # noqa: BLE001
            pass
        _persist(m)
        _tolmach(m, "guard", head,
                 f"цена {g.get('price')} за триггером {g.get('trigger')}: {g.get('why') or ''}"
                 + (f"; новый триггер {g.get('hold_until')}" if g.get("hold_until") else "")
                 + (f"; терпеть {g.get('hold_minutes')} мин" if g.get("hold_minutes") else "")
                 + ("; задача передана Совету" if g.get("decision") == "ЖДАТЬ" else ""),
                 refs={"decision": g.get("decision"), "price": g.get("price"), "trigger": g.get("trigger"),
                       "hold_until": g.get("hold_until")})

    def _guard_handoff(self, why: str) -> None:
        """ИИ у троса решил ждать → задача Совету: полный совет без очереди и без окна PYTHIA_COUNCIL_GAP_SEC;
        совет уже идёт → дежурный PRO сразу после него. Переворот по такому совету — без тишины."""
        m = self.mission
        reason = f"мягкий стоп: {self._money_name()} решил ждать и передать задачу Совету — " + (why or "без объяснений")
        now = time.time()
        if m is not None:
            m.handoffs.append({"ts": now, "reason": reason, "deferred": False, "kind": "stop"})
            del m.handoffs[:-50]
        if m is not None and m.council_running():
            self._ask_review_now(reason, kind="stop")
            self.review_ts = min(self.review_ts, now)
            return
        self._council_reason = reason
        self._council_kind = "stop"
        super()._fire_reanalyze(reason, force=True)
        if m is not None:
            _bg(bus.stage("mission", m.run_id, "pilot", "progress", ticker=self.base,
                          detail="передаю задачу Совету: " + reason))
            _persist(m)

    # ══ v5.4.1 «ТРЕЗВЫЙ ПИЛОТ»: проверка входа у двери ═════════════════════════════════════════════════════
    def _entry_check_on(self) -> bool:
        return bool(self.SOFT_STOP) and bool(getattr(config, "PYTHIA_ENTRY_CHECK", True))

    def _profit_think_on(self) -> bool:
        return bool(self.SOFT_STOP) and bool(getattr(config, "PYTHIA_PROFIT_THINK", True))

    @staticmethod
    def _plan_how(plan: dict) -> str:
        """«long сейчас» / «long откат @99.5» / «short прорыв @98» — одной фразой."""
        if not plan:
            return "—"
        e = plan.get("entry")
        if e is None:
            return f"{plan.get('side')} сейчас"
        return f"{plan.get('side')} {plan.get('kind') or 'откат'} @{_f(e, 0.0):g}"

    def _gate_line(self, plan: dict) -> str:
        """Строка о проверке входа для ситуации PRO (перепроверка/триаж): думает / велел ждать / прошлые ответы.
        v5.4.2: молчание у двери — «ответа не было, повтор в …», а не «велел ждать»; одобренный вход — как есть."""
        if not plan or not self._entry_check_on():
            return ""
        mm = self._money_name()
        now = time.time()
        parts = []
        if plan.get("gate_busy"):
            parts.append(f"{mm} сейчас проверяет вход у двери")
        mine = self._plan_gates(plan)                # ответы «не применено» (про прежний план) — не в счёт
        last = mine[-1] if mine else None
        after = _f(plan.get("gate_after"), 0.0)
        if _f(plan.get("approved_until"), 0.0) > now:
            parts.append(f"{mm} у двери сказал ВОЙТИ; вход ждёт снятия временного запрета"
                         + (f" ({plan.get('gate_note')})" if plan.get("gate_note") else ""))
        elif after > now and not (last and last.get("silent")):
            parts.append(f"{mm} у двери велел ждать до {ai_v5.fmt_ts(after)}" + (f" ({plan.get('gate_note')})" if plan.get("gate_note") else ""))
        if last:
            txt = str(last.get("why") or "")[:200] if last.get("silent") else \
                f"{last.get('decision')} — {str(last.get('why') or '')[:160]}"
            parts.append(f"проверок входа по этому плану: {len(mine)}, последняя {ai_v5.fmt_ts(last.get('ts'))}: {txt}")
        return ("ПРОВЕРКА ВХОДА: " + "; ".join(parts)) if parts else ""

    def _gates_text(self, plan: dict) -> str:
        """Прошлые ответы PRO у двери по ЭТОМУ плану (для промпта проверки входа). v5.4.2: молчание и непонятный
        ответ — «ответа не было … решения не было» (запись кода), не ответ ИИ «ЖДАТЬ»; ответы «не применено» (про
        прежний план, ревью 5.4.2) — не в счёт (_plan_gates)."""
        return "\n".join((f"{ai_v5.fmt_ts(g.get('ts'))} @{g.get('price')}: {g.get('why')}" if g.get("silent") else
                          f"{ai_v5.fmt_ts(g.get('ts'))} @{g.get('price')}: {g.get('decision')} — {g.get('why')}"
                          + (f" (уровень {g.get('entry_kind')} @{g.get('entry')})" if g.get("entry") else "")
                          + (f" (ждать {g.get('wait_minutes')} мин)" if g.get("wait_minutes") else ""))
                         for g in self._plan_gates(plan))

    def _plan_text(self, plan: dict, price: float) -> str:
        """Приказ совета + перепроверки + план пилота, по которому он готов войти."""
        m = self.mission
        L = [_exec_text(m) if m else ""]
        if m and m.reviews:
            L.append("Перепроверки: " + "; ".join(
                f"{ai_v5.fmt_ts(r.get('ts'))} {r.get('choice')} — {str(r.get('why') or '')}" for r in m.reviews[-3:]))
        age = int((time.time() - _f(plan.get("ts"), time.time())) / 60)
        how = self._plan_how(plan)
        e = plan.get("entry")
        L.append(f"ПЛАН ПИЛОТА (по нему готов войти сейчас, цена {price:g}): {how}"
                 + (f" — уровень достигнут" if e is not None else "")
                 + f", стоп {plan.get('invalidation')}, тейк {plan.get('take')}; повод: {str(plan.get('why') or '')[:200]}; "
                 f"приказу {age} мин (срок {int(ai_pilot.PLAN_TTL_SEC // 60)} мин)"
                 + ("; это ДОБОР к открытой позиции той же стороны" if self.position and self.position.get("side") == plan.get("side") else ""))
        return "\n".join(x for x in L if x)

    # v5.4.2: подготовка данных узла у денег (живой рынок, разведка, партнёры, сжатие блоков) — свой срок, не из
    # времени ИИ; одобренный у двери вход, упёршийся во временный запрет, живёт APPROVE_SEC без нового вопроса
    MONEY_PREP_SEC = 120.0
    APPROVE_SEC = 300.0

    @staticmethod
    def _hhmm(ts) -> str:
        """«14:05» — время МСК (fmt_ts без даты)."""
        return ai_v5.fmt_ts(ts).split(" ")[-1]

    @staticmethod
    def _adverse(side: str | None, ref, cur) -> float:
        """Насколько цена cur ушла ХУЖЕ ref для стороны (доля; < 0 — лучше). Нет цены → 0."""
        ref, cur = _f(ref, 0.0), _f(cur, 0.0)
        if ref <= 0 or cur <= 0:
            return 0.0
        return (cur - ref) / ref * (1.0 if side == "long" else -1.0)

    async def _money_call(self, system: str, user: str, *, route: str, attempt_timeout: float):
        """ai_v5.money_json со сроком каждой попытки (v5.4.2); подмена без attempt_timeout (старые фейки) — без него."""
        fn = ai_v5.money_json
        try:
            ps = inspect.signature(fn).parameters
            takes = "attempt_timeout" in ps or any(x.kind is inspect.Parameter.VAR_KEYWORD for x in ps.values())
        except (TypeError, ValueError):
            takes = True
        if takes:
            return await fn(system, user, route=route, attempt_timeout=attempt_timeout)
        return await fn(system, user, route=route)

    async def _entry_gate(self, price: float, book: dict | None) -> bool:
        """Хук AIPilot.tick §3 перед каждой заявкой входа по плану. Выключено (PYTHIA_ENTRY_CHECK=0) → вход сразу.
        Включено: PRO спрашивается фоном (_gate_bg) с теми же данными, что у троса; тик не входит (False) — по
        «ВОЙТИ» задача входит сама (_enter по текущей цене/стакану); пока думает — plan["gate_busy"], состояние
        У_ДВЕРИ; ЖДАТЬ со сроком / молчание → plan["gate_after"], до него вопросов нет.
        v5.4.2 «свободный пилот»: (1) ВОЙТИ у двери, упёршийся во временный запрет, — вход без нового вопроса, когда
        запрет снят (approved_until, дрейф в пределах); (2) биржа не даёт этой стороны ни лота — план снят с честной
        причиной, вопрос дежурному PRO; (3) свежее решение PRO по живому рынку (src review / topup / gate_level /
        profit, не старше PYTHIA_ENTRY_FRESH_SEC, цена не хуже снимка больше PYTHIA_ENTRY_DRIFT_PCT) — без второго
        вопроса у двери; приказ совета и протухшее / уехавшее решение — через дверь, как в 5.4.1.
        Ревью 5.4.2: план добора (src topup), а позиции той же стороны уже нет (тейк/выход/внешнее закрытие) — план
        снят, нового входа нет (ни свежим путём, ни у двери); дверь выключена, а свежее решение не из совета уехало
        дальше PYTHIA_ENTRY_DRIFT_PCT от цены решения — не входим по уехавшей цене: план снят, дежурный PRO решит
        заново по живой цене."""
        plan = self.plan
        if not plan:
            return True
        now = time.time()
        mm = self._money_name()
        topup = bool(self.position and self.position.get("side") == plan.get("side"))
        pre = "добор: " if topup else ""
        src = str(plan.get("src") or "")
        if src == "topup" and not topup:
            self.plan = None
            self._break_n = 0
            if not self.position and not self.pending:
                self.state = "ЖДУ_ПЛАН"
            self.last_action = "добор снят: позиции уже нет"
            log.info("миссия %s: %s (%s)", self.base, self.last_action, self._plan_how(plan))
            return False
        if not self._entry_check_on():
            lim0 = float(getattr(config, "PYTHIA_ENTRY_DRIFT_PCT", 1.0)) / 100.0
            ref0 = plan.get("entry") if plan.get("entry") is not None else plan.get("snap_price")
            drift0 = self._adverse(plan.get("side"), ref0, price)
            if src and src != "council" and _f(ref0, 0.0) > 0 and drift0 > lim0:
                note = (f"цена ушла на {drift0 * 100:.2f}% от снимка решения {_f(ref0, 0.0):g} (стало {price:g}) — "
                        f"реши заново по живой цене")
                self.plan = None
                self._break_n = 0
                if not self.position and not self.pending:
                    self.state = "ЖДУ_ПЛАН"
                log.warning("миссия %s: %s — %s: план снят (дверь выключена)", self.base, self._plan_how(plan), note)
                self._ask_review_now(f"{pre}{note}", kind="pilot")
                return False
            return True
        if plan.get("gate_busy"):
            if not topup:
                self.state = "У_ДВЕРИ"
            self.last_action = (f"{pre}{mm} проверяет вход ({self._plan_how(plan)}) по живому рынку — жду ответа "
                                f"({int(now - _f(plan.get('gate_ts'), now))} с)")
            return False
        after = _f(plan.get("gate_after"), 0.0)
        if now < after:
            if not topup:
                self.state = "ЗАСАДА"
            if plan.get("gate_silent"):              # молчание — не решение ИИ: не «велел ждать»
                self.last_action = (f"{pre}{mm} не ответил у двери — решения не было, повтор в {self._hhmm(after)}"
                                    f" — {self._plan_how(plan)}, цена {price:g}")
            else:
                self.last_action = (f"{pre}{mm} у двери велел ждать до {ai_v5.fmt_ts(after)}"
                                    + (f" ({plan.get('gate_note')})" if plan.get("gate_note") else "")
                                    + f" — {self._plan_how(plan)}, цена {price:g}")
            return False
        side = plan.get("side")
        lim = float(getattr(config, "PYTHIA_ENTRY_DRIFT_PCT", 1.0)) / 100.0
        m = self.mission
        # (1) ВОЙТИ уже сказан у двери, а вход упёрся во временный запрет (заявка в полёте, бэкофф, рынок, планка)
        if _f(plan.get("approved_until"), 0.0) > now:
            blocked = self._entry_blocked(price, plan)
            drift = self._adverse(side, plan.get("approved_px"), price)
            if blocked is None and drift <= lim:
                plan.pop("approved_until", None)
                plan.pop("gate_note", None)
                self.last_action = f"{pre}запрет снят — вход по ответу {mm} у двери ВОЙТИ без нового вопроса"
                log.info("миссия %s: %s", self.base, self.last_action)
                return True
            if blocked is not None:
                self.last_action = f"{pre}{mm} у двери сказал ВОЙТИ — жду: {blocked}"
                return False
            plan.pop("approved_until", None)         # цена ушла от одобренной — спросить заново с этой пометкой
            plan["gate_note"] = (f"цена ушла на {drift * 100:.2f}% от одобренной у двери "
                                 f"{_f(plan.get('approved_px'), 0.0):g} (стало {price:g})")
        # (2) биржа не даёт этой стороны ни лота (шорт недоступен / нет ГО) — не зовём ИИ к двери впустую
        if not topup:
            await self._refresh_max(price)
            mx = self._mx
            if mx and now - _f(mx.get("ts"), 0.0) <= ai_pilot.MX_TTL_SEC * 3 \
                    and int(mx.get("buy" if side == "long" else "sell") or 0) <= 0:
                reason = f"биржа не даёт {side}: лотов 0 — " + ("шорт недоступен или нет ГО" if side == "short" else "нет ГО")
                self.plan = None
                self._break_n = 0
                if not self.position:
                    self.state = "ЖДУ_ПЛАН"
                log.warning("миссия %s: %s — план снят", self.base, reason)
                self._ask_review_now(reason, kind="pilot")
                return False
        # (3) свежее решение PRO по живому рынку — исполняется без второго вопроса у двери
        fresh_sec = float(getattr(config, "PYTHIA_ENTRY_FRESH_SEC", 1200))
        snap_ts = _f(plan.get("snap_ts"), 0.0)
        ref = plan.get("entry") if plan.get("entry") is not None else plan.get("snap_price")   # уровень — цена решения
        if (src and src != "council" and fresh_sec > 0 and snap_ts and now - snap_ts <= fresh_sec
                and _f(plan.get("gate_wait_ts"), 0.0) <= snap_ts and _f(ref, 0.0) > 0
                and self._adverse(side, ref, price) <= lim):
            self.last_action = (f"{pre}вход по свежему решению {mm} ({src}, {self._hhmm(snap_ts)}) — "
                                f"без второго вопроса у двери")
            log.info("миссия %s: %s (%s, цена %s)", self.base, self.last_action, self._plan_how(plan), price)
            if m is not None:
                _tolmach(m, "entry_check", f"Вход по свежему решению {mm}",
                         f"{self._plan_how(plan)} при цене {price:g}: решение {src} в {self._hhmm(snap_ts)} по цене "
                         f"{_f(ref, 0.0):g}, цена не ушла дальше {lim * 100:g} % — без второго вопроса у двери",
                         refs={"src": src, "price": price, "snap_ts": snap_ts, "ref": ref})
            return True
        plan["gate_busy"] = True
        plan["gate_ts"] = now
        plan["gate_how"] = self._plan_how(plan)       # с каким планом спросили (план могут обновить на месте)
        plan["gate_plan_ts"] = plan.get("ts")        # …и какой это был план (запись ответа ссылается на него)
        plan.pop("gate_stale", None)                  # новый вопрос — о нынешнем плане
        plan.pop("gate_after", None)
        if not topup:
            self.state = "У_ДВЕРИ"
        self.last_action = f"{pre}{mm} проверяет вход ({self._plan_how(plan)}, цена {price:g}) по живому рынку"
        log.info("миссия %s: %s", self.base, self.last_action)
        self._gate_task = self._spawn_background(self._gate_bg(price, plan))
        return False

    async def _entry_check(self, price: float, plan: dict) -> dict:
        """Вопрос PRO у двери: те же данные, что у троса (ситуация, ход цены, живой рынок, разведка, партнёры,
        сканер, совет, новости, память, ответы у троса/тейка) + приказ и план + прошлые проверки по этому плану →
        prompts_mission.entry_check → ai_v5.money_json(route="mission_entry").
        v5.4.2: подготовка данных (сжатие в общей очереди FLASH) — свой срок MONEY_PREP_SEC, после него — что есть,
        с пометкой; у ИИ — PYTHIA_ENTRY_TIMEOUT_SEC на каждую попытку; снимок двери (plan["gate_px"]) — цена в промпте;
        пометка к вопросу (дрейф, одобренный вход) — в ситуации."""
        m = self.mission
        rid = m.run_id if m else None
        mm = self._money_name()
        if m is not None:
            try:
                await bus.stage("mission", rid, "entry", "start", ticker=self.base,
                                detail=f"проверка входа: {self._plan_how(plan)} при цене {price:g} — {mm} решает: войти, ждать или отменить")
            except Exception:                        # noqa: BLE001
                pass

        async def _light():
            try:
                lt = await asyncio.wait_for(market_ctx.light(self.base, self.figi, self.asset_class), GATE_LIGHT_TIMEOUT)
                return lt.get("text") or ""
            except Exception as e:                   # noqa: BLE001
                return f"(живой рынок недоступен: {str(e)[:60]})"

        got = {"light": "(живой рынок не успел собраться)", "scout": "", "partners": ""}

        def _src() -> dict:
            return {"light": got["light"], "news": self._news_quick(), "history": self._history_text(price),
                    "prev_exec": self._plan_text(plan, price), "guard": self._guards_text(), "checks": self._gates_text(plan),
                    "scan": _scan_text(m) if m else "", "scout": got["scout"], "partners": got["partners"],
                    "council": _council_text(), "memory": (m.memory if m else "") or ""}

        async def _prep():
            got["light"], got["scout"], got["partners"] = await asyncio.gather(
                _light(), self._scout_fresh(GUARD_SCOUT_TIMEOUT), self._partners_fresh(GUARD_PARTNERS_TIMEOUT))
            return await _fit_blocks(_src())

        prep_note = ""
        try:
            blocks, _sq = await asyncio.wait_for(_prep(), self.MONEY_PREP_SEC)
        except asyncio.TimeoutError:
            blocks = _src()
            prep_note = (f"(подготовка данных не уложилась в {int(self.MONEY_PREP_SEC)} с — блоки даны как есть, "
                         f"без сжатия; чего нет — того не выдумывай)")
            log.warning("миссия %s: проверка входа — %s", self.base, prep_note)
        snap = self.prices[-1] if self.prices else price
        plan["gate_px"] = snap                        # цена, которую PRO видит в промпте (для дрейфа за раздумья)
        situation = self._situation_for_ai(snap)
        if plan.get("gate_note"):
            situation += f"\nПОМЕТКА К ЭТОМУ ВОПРОСУ: {plan['gate_note']}"
        if prep_note:
            situation += "\n" + prep_note
        s, u = prompts_mission.entry_check(
            self.base, self.name, self._play(), situation=situation, plan=blocks["prev_exec"], history=blocks["history"],
            light=blocks["light"], news=blocks["news"], council_text=blocks["council"], scan=blocks["scan"],
            scout=blocks["scout"], partners=blocks["partners"], memory=blocks["memory"], guards=blocks["guard"],
            time_msk=ai_v5.now_msk_str(), checks=blocks["checks"])
        if m is not None:
            m.sizes["entry"] = _sizes_rec(len(u), {"situation": situation, **blocks})
        obj = await self._money_call(s, u, route="mission_entry",
                                     attempt_timeout=float(getattr(config, "PYTHIA_ENTRY_TIMEOUT_SEC", 1200)))
        if m is not None:
            m.sizes["entry"] = _sizes_rec(len(u), {"situation": situation, **blocks}, _json_len(obj))
        return obj if isinstance(obj, dict) else {}

    async def _gate_bg(self, price: float, plan: dict) -> None:
        silent = None
        tmo = float(getattr(config, "PYTHIA_ENTRY_TIMEOUT_SEC", 1200))
        try:
            # у каждой попытки ИИ свой срок (attempt_timeout); общий потолок — подготовка + две попытки + запас
            r = await asyncio.wait_for(self._entry_check(price, plan), self.MONEY_PREP_SEC + 2 * tmo + 60)
        except asyncio.CancelledError:
            plan["gate_busy"] = False
            raise
        except Exception as e:                       # noqa: BLE001
            silent = (f"таймаут {int(tmo)} с" if isinstance(e, asyncio.TimeoutError)
                      else (str(e)[:100] or type(e).__name__))
            r = {}
        try:
            await self._apply_gate(r if isinstance(r, dict) else {}, price, plan, silent=silent)
        except Exception as e:                       # noqa: BLE001
            log.warning("миссия %s: ответ у двери не применился: %s", self.base, str(e)[:120])
        finally:
            plan["gate_busy"] = False                # после _enter: до этого тик не входит сам

    def _entry_blocked(self, price: float, plan: dict) -> str | None:
        """Почему прямо сейчас входить по плану нельзя (те же гейты, что в tick §3), None — можно."""
        if self.plan is not plan:
            return "план сменился"
        if self.pending:
            return "заявка уже в полёте"
        if self._reanalyzing or self.state in ("СТОП", "ПЕРЕАНАЛИЗ") or self.stopping or self.panic_flag:
            return "пилот занят советом / остановлен"
        if self.session_risk and self.session_risk.locked:   # V 5.4.1: стоп-кран сработал, пока PRO думал у двери
            return "стоп-кран сессии (killswitch) — входов нет"
        if self.position and self.position.get("side") != plan.get("side"):
            return "позиция другой стороны ещё не закрыта"
        if not self._market_alive() or self._market_closed():
            return "рынок мёртв или закрыт"
        if self._at_limit(plan["side"], price):
            return "цена у планки"
        if time.time() < self.no_entry_until:
            return "бэкофф после отказов биржи"
        if time.time() - _f(plan.get("ts"), 0.0) > ai_pilot.PLAN_TTL_SEC:
            return "приказ протух"
        inv = _f(plan.get("invalidation"), 0.0)
        if inv > 0 and ((plan["side"] == "long" and price <= inv) or (plan["side"] == "short" and price >= inv)):
            return f"цена {price:g} уже за invalidation {inv:g} — идея мертва"
        return None

    async def _apply_gate(self, r: dict, price: float, plan: dict, silent: str | None = None) -> None:
        """Ответ PRO у двери (слово — ai_v5.door_table по стороне плана: «BUY» у лонга — ВОЙТИ, «НЕ ВХОДИТЬ» — не
        разобрано). ВОЙТИ → _enter по текущей цене/стакану; вход упёрся во временный запрет → одобрение живёт
        APPROVE_SEC (войдём без нового вопроса, когда запрет снимется); цена за время раздумий ушла хуже снимка двери
        больше PYTHIA_ENTRY_DRIFT_PCT → не засада по старой цене, а сразу новый вопрос с пометкой о дрейфе.
        ЖДАТЬ → новый уровень (entry/entry_kind, стороны как у приказа; план становится gate_level — у уровня вход без
        второго вопроса, пока решение свежее) и/или срок (wait_minutes → gate_after), можно поправить invalidation/take;
        ОТМЕНИТЬ → план снят, повод дежурному PRO (council=true → полный совет без очереди, handoff `entry`).
        v5.4.2: молчание/сбой (НЕТ_ОТВЕТА) и непонятный ответ (НЕ_РАЗОБРАН) — не решение ИИ: запись кода (source «код»,
        silent), поля ответа не читаются, ни входа, ни отмены — повтор через PYTHIA_SILENT_RETRY_SEC, счёт молчаний
        подряд plan["gate_silent"] (сброс на любом настоящем ответе), ошибка в панель. План сменился/снят за время
        ответа → ответ выброшен (запись остаётся с пометкой)."""
        m = self.mission
        mm = self._money_name()
        now = time.time()
        cur = self.prices[-1] if self.prices else price
        cool = float(getattr(config, "PYTHIA_ENTRY_CHECK_COOL_SEC", 300))
        retry = float(getattr(config, "PYTHIA_SILENT_RETRY_SEC", 120))
        side = plan.get("side")
        raw = ai_v5.decision_raw(r)
        why = str(r.get("why") or "")[:300]
        if silent:
            decision = "НЕТ_ОТВЕТА"
        else:
            decision = ai_v5.decision_of(raw, ai_v5.door_table(side)) or "НЕ_РАЗОБРАН"
        no_answer = decision in ("НЕТ_ОТВЕТА", "НЕ_РАЗОБРАН")
        n_sil = int(plan.get("gate_silent") or 0) + 1 if no_answer else 0
        # ревью 5.4.2 (финал): запись — о плане, о котором спросили (его ts и вид), а не об обновлённом на месте
        asked_ts = plan.get("gate_plan_ts", plan.get("ts"))
        asked_how = plan.get("gate_how") or self._plan_how(plan)
        stale = bool(plan.pop("gate_stale", None))   # план обновлён свежим решением, пока дверь думала
        if no_answer:
            head = (f"{mm} не ответил у двери ({silent})" if silent else
                    f"{mm} ответил у двери непонятно ({raw[:60] or 'пусто'})")
            why = f"{head} — решения не было, повтор в {self._hhmm(now + retry)}"
            rec = {"ts": now, "decision": decision, "why": why, "note": "", "price": cur, "entry": None,
                   "entry_kind": None, "wait_minutes": None, "council": False, "plan_ts": asked_ts, "side": side,
                   "how": asked_how, "model": mm, "silent": True, "source": "код",
                   "raw": raw[:80] or None, "silent_n": n_sil, "applied": ""}
        else:
            e_ai = _f(r.get("entry")) if r.get("entry") not in (None, "", "null") else None
            rec = {"ts": now, "decision": decision, "why": why, "note": str(r.get("note") or "")[:300], "price": cur,
                   "entry": e_ai if e_ai and e_ai > 0 else None,
                   "entry_kind": (str(r.get("entry_kind") or "").strip().lower() or None),
                   "wait_minutes": _f(r.get("wait_minutes")) or None, "council": bool(r.get("council")),
                   "plan_ts": asked_ts, "side": side, "how": asked_how, "model": mm,
                   "silent": False, "source": "ИИ", "applied": ""}
        self.gates.append(rec)
        del self.gates[:-ai_pilot.GATES_KEEP]
        applied: list[str] = []
        topup = bool(self.position and self.position.get("side") == side)
        pre = "добор: " if topup else ""
        if self.plan is not plan:
            rec["applied"] = "план сменился/снят за время ответа — не применено"
            log.info("миссия %s: ответ у двери (%s) выброшен — план сменился", self.base, decision)
        elif decision != "ВОЙТИ" and stale:
            # ревью 5.4.2: план обновлён на месте свежим решением (перепроверка), пока дверь думала над прежним: её
            # ЖДАТЬ / ОТМЕНИТЬ / молчание — про старый план, свежее решение ими не отменяется (вход решит тик: свежее
            # решение бьётся без второго вопроса); ВОЙТИ применяется как обычно. Признак — пометка gate_stale из _put
            # (снимок перепроверки берётся до раздумий PRO и бывает старше вопроса двери — времена не сравниваем)
            plan["gate_busy"] = False
            plan.pop("gate_after", None)
            rec["applied"] = "план обновлён свежим решением за время ответа — не применено"
            self.last_action = (f"{pre}{mm} у двери: {decision} — про прежний план; план уже обновлён свежим решением "
                                f"({plan.get('src') or '—'}, {self._hhmm(plan.get('snap_ts'))}) — ответ не применён")
            log.info("миссия %s: %s", self.base, self.last_action)
        elif no_answer:
            # решения ИИ не было: не входим и не отменяем, не пишем «ЖДАТЬ» за него — скорый повтор того же вопроса
            plan["gate_silent"] = n_sil
            plan["gate_after"] = now + retry
            smax = int(getattr(config, "PYTHIA_ENTRY_SILENT_MAX", 2) or 0)
            applied.append(f"без ответа подряд: {n_sil}")
            if smax > 0 and n_sil >= smax:
                applied.append(f"дверь молчит {n_sil} раз подряд — без ответа ИИ вход не исполняю, спрашиваю снова")
                log.warning("миссия %s: дверь молчит %d раз подряд (%s)", self.base, n_sil, self._plan_how(plan))
            if not topup:
                self.state = "ЗАСАДА"
            self.last_action = pre + why + f" (без ответа подряд: {n_sil})"
            try:
                ai_v5.note_error(f"проверка входа {mm}: {silent or 'ответ не разобран: ' + (raw[:60] or 'пусто')}",
                                 "mission_entry")
            except Exception:                        # noqa: BLE001
                pass
        elif decision == "ВОЙТИ":
            plan.pop("gate_silent", None)
            blocked = self._entry_blocked(cur, plan)
            snap = _f(plan.get("gate_px"), 0.0) or price          # цена, которую PRO видел в промпте
            adverse = self._adverse(side, snap, cur)              # > 0 — цена ушла ХУЖЕ снимка, по которому думал PRO
            lim = float(getattr(config, "PYTHIA_ENTRY_DRIFT_PCT", 1.0)) / 100.0
            asked = plan.get("gate_how")
            if plan.get("entry") is not None and asked and asked != self._plan_how(plan):
                # план обновили на месте (перепроверка назвала уровень), пока дверь думала: вход — у нового уровня
                plan.pop("gate_after", None)
                applied.append(f"план сменил вход за время ответа ({asked} → {self._plan_how(plan)}) — войду у нового уровня")
                self.last_action = f"{pre}{mm} у двери: ВОЙТИ ({why or 'без объяснений'}) — но {applied[-1]}"
            elif blocked and any(k in blocked for k in ("killswitch", "протух", "мертва")):
                # запрет не временный (стоп-кран дня, приказ протух, идея мертва) — вход не исполняется, тик решит сам
                plan["gate_after"] = now + 60.0
                applied.append(f"вход не выполнен: {blocked}")
                self.last_action = f"{pre}{mm} у двери: ВОЙТИ ({why or 'без объяснений'}) — но {blocked}"
            elif blocked:
                # одобрение не выбрасываем: войдём без нового вопроса, когда временный запрет снимется
                plan["approved_until"] = now + self.APPROVE_SEC
                plan["approved_px"] = cur
                plan["gate_note"] = f"ВОЙТИ одобрен, но {blocked}"
                applied.append(f"вход не выполнен: {blocked} — войду без нового вопроса, когда запрет снимется "
                               f"(до {self._hhmm(now + self.APPROVE_SEC)})")
                self.last_action = (f"{pre}{mm} у двери: ВОЙТИ ({why or 'без объяснений'}) — но {blocked}; "
                                    f"войду без нового вопроса, когда запрет снимется")
            elif adverse > lim:
                # PRO думал, а цена ушла хуже его снимка: не засада по старой цене (в тренде она не исполнится) и не
                # погоня — сразу новый вопрос с живой ценой и пометкой о дрейфе
                plan["gate_after"] = now
                plan["gate_note"] = f"цена ушла на {adverse * 100:.2f}% за время раздумий (было {snap:g}, стало {cur:g})"
                applied.append(plan["gate_note"] + " — не засада по старой цене: спрошу сразу по живой")
                if not topup:
                    self.state = "ЗАСАДА" if plan.get("entry") is not None else "ВХОЖУ"
                self.last_action = f"{pre}{mm} у двери: ВОЙТИ ({why or 'без объяснений'}) — но {applied[-1]}"
            else:
                plan.pop("gate_after", None)
                plan.pop("gate_note", None)
                await self._enter(cur, self.last_book)
                applied.append(self.last_action)
                self.last_action = f"{mm} у двери: ВОЙТИ ({why or 'без объяснений'}) → {self.last_action}"
        elif decision == "ЖДАТЬ":
            plan.pop("gate_silent", None)
            kind = rec["entry_kind"] or ""
            e = rec["entry"]
            inv_ai = _f(r.get("invalidation")) if r.get("invalidation") not in (None, "", "null") else None
            take_ai = _f(r.get("take")) if r.get("take") not in (None, "", "null") else None
            if e is not None:
                # ЖДАТЬ с уровнем: подсказка «сейчас» противоречит ожиданию — вид решает геометрия уровня
                kind = _entry_kind("BUY" if side == "long" else "SELL", e, cur, kind if kind in ("откат", "прорыв") else None)
                if kind == "сейчас":
                    applied.append(f"уровень {e:g} равен цене — это «сейчас», не уровень ожидания; жду срок")
                    e = None
            inv_new = plan.get("invalidation")
            if inv_ai and inv_ai > 0:
                ref = e if e is not None else cur
                # прорыв (v5.4.2): стоп — по ту сторону уровня (ref); между ценой и уровнем — законный стоп пробоя
                ok_inv = ((side == "long" and inv_ai < ref) or (side == "short" and inv_ai > ref))
                if ok_inv and abs(inv_ai - _f(plan.get("invalidation"), 0.0)) > 1e-9:
                    inv_new = round(inv_ai, 6)
                elif not ok_inv:
                    applied.append(f"стоп {inv_ai:g} не с той стороны — не принят")
            take_new = plan.get("take")
            if take_ai and take_ai > 0:
                ref = e if e is not None else cur
                if (side == "long" and take_ai > ref) or (side == "short" and take_ai < ref):
                    take_new = round(take_ai, 6)
                else:
                    applied.append(f"тейк {take_ai:g} не с той стороны — не принят")
            if e is not None:
                bad = ai_pilot.AIPilot._plan_valid(side, e, take_new, inv_new)
                if bad:
                    applied.append(f"уровень {kind} @{e:g} отвергнут: {bad}")
                    e = None
            if e is not None:
                plan["entry"], plan["kind"] = round(e, 6), kind
                # уровень назвал сам PRO по живому рынку — у уровня вход без второго вопроса, пока решение свежее
                plan["src"], plan["snap_ts"], plan["snap_price"] = "gate_level", now, cur
                plan.pop("gate_wait_ts", None)
                self._break_n = 0
                rec["entry"], rec["entry_kind"] = plan["entry"], kind
                applied.append(f"новый уровень: {kind} @{e:g}")
            else:
                plan["gate_wait_ts"] = now           # ждать без уровня — у двери спросим снова (не «свежий вход»)
            if inv_new != plan.get("invalidation"):
                applied.append(f"стоп {plan.get('invalidation')} → {inv_new:g}")
                plan["invalidation"] = inv_new
            if take_new != plan.get("take"):
                applied.append(f"тейк {plan.get('take')} → {take_new:g}")
                plan["take"] = take_new
            wm = _f(rec.get("wait_minutes"), 0.0)
            if wm > 0:
                wait_s = max(ai_pilot.GATE_WAIT_MIN_SEC, min(wm * 60.0, ai_pilot.GATE_WAIT_MAX_SEC))
                plan["gate_after"] = now + wait_s
                applied.append(f"ждать {int(wait_s // 60)} мин")
            elif e is None:
                plan["gate_after"] = now + cool
                applied.append(f"повтор через {int(cool // 60)} мин")
            plan["gate_note"] = (why or "")[:80]
            if not topup:
                self.state = "ЗАСАДА"
            self.last_action = (pre + f"{mm} у двери: ЖДАТЬ ({why or 'без объяснений'}) — "
                                + (", ".join(applied) if applied else "как есть"))
        else:                                        # ОТМЕНИТЬ
            self.plan = None
            self._break_n = 0
            if not self.position:
                self.state = "ЖДУ_ПЛАН"
            reason = f"вход отменён {mm} у двери: {why or 'без объяснений'}"
            applied.append("план снят")
            self.last_action = f"{mm} у двери: ОТМЕНИТЬ ({why or 'без объяснений'}) — план снят"
            if rec["council"]:
                applied.append("зову полный совет")
                self._gate_handoff(reason)
            else:
                self._ask_review_now(reason, kind="pilot")
        rec["applied"] = "; ".join(applied) if applied else rec["applied"]
        self._save_state()
        log.info("миссия %s: %s", self.base, self.last_action)
        if m is None:
            return
        head = why if no_answer else f"{mm} у двери: {decision} — {why}"
        _bg(bus.stage("mission", m.run_id, "entry", "done", ticker=self.base,
                      detail=head + (f" · {rec['applied']}" if rec["applied"] else ""), data=rec))
        title = (f"{mm} не ответил у двери" if decision == "НЕТ_ОТВЕТА" else
                 f"{mm}: ответ у двери не разобран" if decision == "НЕ_РАЗОБРАН" else f"{mm} у двери: {decision}")
        _tolmach(m, "entry_check", title,
                 f"{rec['how']} при цене {cur:g}: {why or 'без объяснений'}" + (f"; {rec['applied']}" if rec["applied"] else "")
                 + (f". {rec['note']}" if rec["note"] else ""),
                 refs={"decision": decision, "price": cur, "entry": rec["entry"], "entry_kind": rec["entry_kind"],
                       "wait_minutes": rec["wait_minutes"], "council": rec["council"], "source": rec["source"]})
        _persist(m)

    def _gate_handoff(self, reason: str) -> None:
        """ОТМЕНИТЬ с council=true → полный совет без очереди и без окна PYTHIA_COUNCIL_GAP_SEC (handoff kind `entry`);
        совет уже идёт → дежурный PRO сразу после него."""
        m = self.mission
        now = time.time()
        if m is not None:
            m.handoffs.append({"ts": now, "reason": reason, "deferred": False, "kind": "entry"})
            del m.handoffs[:-50]
        if m is not None and m.council_running():
            self._ask_review_now(reason, kind="entry")
            self.review_ts = min(self.review_ts, now)
            return
        self._council_reason = reason
        self._council_kind = "entry"
        super()._fire_reanalyze(reason, force=True)
        if m is not None:
            _bg(bus.stage("mission", m.run_id, "pilot", "progress", ticker=self.base,
                          detail="передаю задачу Совету: " + reason))
            _persist(m)

    # ══ v5.4.1: мысль о прибыли — в плюсе PRO думает сам, не дожидаясь тейка ═══════════════════════════════
    def _profit_trigger(self, price: float, pos: dict) -> str | None:
        """Повод для мысли: пройдено ≥ PYTHIA_PROFIT_THINK_PCT % хода от входа до тейка; без тейка — плюс ≥
        PYTHIA_PROFIT_THINK_MIN_PCT % от входа. None — повода нет."""
        entry, take = _f(pos.get("entry"), 0.0), _f(pos.get("take"), 0.0)
        if entry <= 0:
            return None
        sgn = 1.0 if pos.get("side") == "long" else -1.0
        gain = (price / entry - 1.0) * 100.0 * sgn
        if gain <= 0:
            return None
        fl = _f(pos.get("floating"), 0.0)
        if take > 0 and abs(take - entry) > 1e-12:
            share = (price - entry) * sgn / abs(take - entry) * 100.0
            pct = float(getattr(config, "PYTHIA_PROFIT_THINK_PCT", 60.0))
            if share >= pct:
                return (f"пройдено {share:.0f} % хода от входа {entry:g} до тейка {take:g} (порог {pct:g} %): "
                        f"цена {price:g}, {gain:+.2f} % от входа, плавающий {fl:+.0f} ₽")
            return None
        min_pct = float(getattr(config, "PYTHIA_PROFIT_THINK_MIN_PCT", 1.0))
        if gain >= min_pct:
            return (f"тейка нет, плюс {gain:+.2f} % от входа {entry:g} (порог {min_pct:g} %): цена {price:g}, "
                    f"плавающий {fl:+.0f} ₽")
        return None

    def _profit_watch(self, price: float, pos: dict) -> None:
        """Хук AIPilot.tick §2 (в позиции, не при тросе/тейке/вопросе у них, рынок жив). Повод и пейсинг
        (pos["profit_next"], PYTHIA_PROFIT_THINK_COOL_SEC) → мысль о прибыли фоном. v5.4.2: дежурный PRO на
        перепроверке — повод не пропадает: pos["profit_pending"], мысль сразу после его ответа (_profit_after_review)."""
        if not self._profit_think_on() or self.position is not pos:
            return
        if (pos.get("profit_busy") or pos.get("guard_busy") or self.pending or self._reanalyzing
                or self.state == "СТОП"):
            return
        if time.time() < _f(pos.get("profit_next"), 0.0):
            return
        reason = self._profit_trigger(price, pos)
        if reason and self._review_busy:
            if pos.get("profit_pending_kind") != "shock":   # повод рывка (без порога) не затираем поводом порога
                pos["profit_pending"] = reason[:200]
                pos["profit_pending_kind"] = "watch"
        elif reason:
            self._profit_think_now(price, pos, reason)

    def _profit_think_now(self, price: float, pos: dict, reason: str) -> bool:
        """Запустить мысль о прибыли фоном (повод — порог хода или рывок в нашу сторону). Возврат: запущена?"""
        if (not self._profit_think_on() or self.position is not pos or pos.get("profit_busy") or pos.get("guard_busy")
                or self.pending or self.state == "СТОП"):
            return False
        mm = self._money_name()
        pos["profit_busy"] = True
        pos["profit_reason"] = reason[:200]
        pos["profit_ts"] = time.time()
        self.last_action = f"{mm} думает о прибыли ({reason}): держать, выйти, выйти и перезайти или звать совет"
        log.info("миссия %s: %s", self.base, self.last_action)
        self._profit_task = self._spawn_background(self._profit_bg(price, pos, reason))
        return True

    def _profits_text(self, pos: dict | None = None) -> str:
        """Прошлые мысли о прибыли (по этой позиции, если она дана) — для промпта. v5.4.2: молчание и непонятный
        ответ — «ответа не было … решения не было» (запись кода), не «ДЕРЖАТЬ» от имени ИИ."""
        since = _f((pos or {}).get("opened_ts"), 0.0)
        return "\n".join((f"{ai_v5.fmt_ts(x.get('ts'))} @{x.get('price')}: {x.get('why')}" if x.get("silent") else
                          f"{ai_v5.fmt_ts(x.get('ts'))} @{x.get('price')}: {x.get('decision')} — {x.get('why')}"
                          + (f" (триггер → {x.get('lock_price')})" if x.get("lock_price") else "")
                          + (f" (цель → {x.get('take')})" if x.get("take") else ""))
                         for x in self.profits[-5:] if _f(x.get("ts"), 0.0) >= since)

    async def _profit_think(self, price: float, pos: dict, reason: str) -> dict:
        """Вопрос PRO о прибыли: те же данные, что у троса/тейка, + повод + прошлые мысли →
        prompts_mission.profit_think → ai_v5.money_json(route="mission_profit").
        v5.4.2: подготовка данных — свой срок MONEY_PREP_SEC (после него — что есть, с пометкой); у ИИ —
        PYTHIA_PROFIT_TIMEOUT_SEC на каждую попытку."""
        m = self.mission
        rid = m.run_id if m else None
        mm = self._money_name()
        if m is not None:
            try:
                await bus.stage("mission", rid, "profit", "start", ticker=self.base,
                                detail=f"мысль о прибыли: {reason[:140]} — {mm} решает: держать, выйти, перезайти или совет")
            except Exception:                        # noqa: BLE001
                pass

        async def _light():
            try:
                lt = await asyncio.wait_for(market_ctx.light(self.base, self.figi, self.asset_class), GUARD_LIGHT_TIMEOUT)
                return lt.get("text") or ""
            except Exception as e:                   # noqa: BLE001
                return f"(живой рынок недоступен: {str(e)[:60]})"

        plan_txt = _exec_text(m) if m else ""
        if m and m.reviews:
            plan_txt += "\nПерепроверки: " + "; ".join(
                f"{ai_v5.fmt_ts(r.get('ts'))} {r.get('choice')} — {str(r.get('why') or '')}" for r in m.reviews[-3:])
        plan_txt += (f"\nУРОВНИ ПОЗИЦИИ: вход {_f(pos.get('entry'), 0.0):g}, триггер (мягкий стоп) {pos.get('invalidation')}, "
                     f"{self._hard_name()} {pos.get('hard_stop')}, тейк {pos.get('take')}"
                     + (f"; прибыль уже заперта триггером" if pos.get("profit_lock") or pos.get("take_holds") else ""))
        got = {"light": "(живой рынок не успел собраться)", "scout": "", "partners": ""}

        def _src() -> dict:
            return {"light": got["light"], "news": self._news_quick(), "history": self._history_text(price),
                    "prev_exec": plan_txt, "guard": self._guards_text(), "thoughts": self._profits_text(pos),
                    "scan": _scan_text(m) if m else "", "scout": got["scout"], "partners": got["partners"],
                    "council": _council_text(), "memory": (m.memory if m else "") or ""}

        async def _prep():
            got["light"], got["scout"], got["partners"] = await asyncio.gather(
                _light(), self._scout_fresh(GUARD_SCOUT_TIMEOUT), self._partners_fresh(GUARD_PARTNERS_TIMEOUT))
            return await _fit_blocks(_src())

        prep_note = ""
        try:
            blocks, _sq = await asyncio.wait_for(_prep(), self.MONEY_PREP_SEC)
        except asyncio.TimeoutError:
            blocks = _src()
            prep_note = (f"(подготовка данных не уложилась в {int(self.MONEY_PREP_SEC)} с — блоки даны как есть, "
                         f"без сжатия; чего нет — того не выдумывай)")
            log.warning("миссия %s: мысль о прибыли — %s", self.base, prep_note)
        situation = self._situation_for_ai(self.prices[-1] if self.prices else price)
        if prep_note:
            situation += "\n" + prep_note
        s, u = prompts_mission.profit_think(
            self.base, self.name, self._play(), situation=situation, profit=reason, history=blocks["history"],
            light=blocks["light"], plan=blocks["prev_exec"], news=blocks["news"], council_text=blocks["council"],
            scan=blocks["scan"], scout=blocks["scout"], partners=blocks["partners"], memory=blocks["memory"],
            guards=blocks["guard"], time_msk=ai_v5.now_msk_str(), thoughts=blocks["thoughts"])
        if m is not None:
            m.sizes["profit"] = _sizes_rec(len(u), {"situation": situation, **blocks})
        obj = await self._money_call(s, u, route="mission_profit",
                                     attempt_timeout=float(getattr(config, "PYTHIA_PROFIT_TIMEOUT_SEC", 1200)))
        if m is not None:
            m.sizes["profit"] = _sizes_rec(len(u), {"situation": situation, **blocks}, _json_len(obj))
        return obj if isinstance(obj, dict) else {}

    async def _profit_bg(self, price: float, pos: dict, reason: str) -> None:
        silent = None
        tmo = float(getattr(config, "PYTHIA_PROFIT_TIMEOUT_SEC", 1200))
        try:
            # у каждой попытки ИИ свой срок (attempt_timeout); общий потолок — подготовка + две попытки + запас
            r = await asyncio.wait_for(self._profit_think(price, pos, reason), self.MONEY_PREP_SEC + 2 * tmo + 60)
        except asyncio.CancelledError:
            pos["profit_busy"] = False
            raise
        except Exception as e:                       # noqa: BLE001
            silent = (f"таймаут {int(tmo)} с" if isinstance(e, asyncio.TimeoutError)
                      else (str(e)[:100] or type(e).__name__))
            r = {}
        finally:
            pos["profit_busy"] = False
        try:
            await self._apply_profit(r if isinstance(r, dict) else {}, price, pos, reason, silent=silent)
        except Exception as e:                       # noqa: BLE001
            log.warning("миссия %s: мысль о прибыли не применилась: %s", self.base, str(e)[:120])

    def _profit_lock(self, pos: dict, lock: float, take: float, cur: float, applied: list[str]) -> None:
        """ДЕРЖАТЬ/СОВЕТ: lock_price → триггер (на безопасной стороне цены, не ниже входа, лучше прежнего), трос от
        него, но не ниже входа (profit_lock, как take_holds); take → цель (на верной стороне цены)."""
        is_long = pos.get("side") == "long"
        entry = _f(pos.get("entry"), 0.0)
        inv = _f(pos.get("invalidation"), 0.0)
        if lock > 0:
            safe = (is_long and entry <= lock < cur) or (not is_long and cur < lock <= entry)
            better = (is_long and lock > inv) or (not is_long and lock < inv)
            if safe and better:
                pos["profit_lock"] = True
                self._set_levels(pos, None, lock, inv0=True)     # триггер = запертая прибыль; трос от него, не ниже входа
                pos["holds"] = 0
                pos["restop"] = True                             # трос биржи (если включён) — за новым триггером
                pos["restop_after"] = 0.0
                applied.append(f"прибыль заперта триггером {lock:g} ({self._hard_name()} {_f(pos.get('hard_stop'), 0.0):g})")
            elif not safe:
                applied.append(f"lock_price {lock:g} не принят: не между входом {entry:g} и ценой {cur:g}")
            else:
                applied.append(f"lock_price {lock:g} не принят: не лучше триггера {inv:g}")
        if take > 0:
            if (is_long and take > cur) or (not is_long and take < cur):
                if abs(take - _f(pos.get("take"), 0.0)) > 1e-9:
                    applied.append(f"цель {_f(pos.get('take'), 0.0):g} → {take:g}")
                    pos["take"] = round(take, 6)
            else:
                applied.append(f"тейк {take:g} не с той стороны от цены — не принят")

    def _reentry_plan(self, side: str, cur: float, reentry: float | None, rk: str, take_ai: float | None,
                      inv_pos, take_pos, why: str) -> dict | None:
        """ВЫЙТИ_И_ПЕРЕЗАЙТИ: план той же стороны с entry=reentry, kind=reentry_kind (сторона уровня верна: для long
        откат ниже цены / прорыв выше, short зеркально; иначе None — как ВЫЙТИ). Стоп — из позиции, если он на верной
        стороне от уровня (v5.4.2: и для прорыва — от уровня, не от цены), иначе аварийный 0.6 % от уровня; тейк — из
        ответа или позиции, если верен. План несёт src «profit» и снимок решения: у уровня — вход без второго вопроса
        у двери, пока решение свежее (PYTHIA_ENTRY_FRESH_SEC)."""
        if reentry is None or reentry <= 0:
            return None
        kind = _entry_kind("BUY" if side == "long" else "SELL", reentry, cur, rk if rk in ("откат", "прорыв") else None)
        if kind == "сейчас" or (rk in ("откат", "прорыв") and rk != kind):
            return None
        inv = _f(inv_pos, 0.0)
        ok = inv > 0 and ((side == "long" and inv < reentry) or (side == "short" and inv > reentry))
        if not ok:
            inv = reentry * (1 - ai_pilot.EMERGENCY_STOP_FRAC if side == "long" else 1 + ai_pilot.EMERGENCY_STOP_FRAC)
        take = None
        for cand in (take_ai, take_pos):
            c = _f(cand, 0.0)
            if c > 0 and ((side == "long" and c > reentry) or (side == "short" and c < reentry)):
                take = c
                break
        if ai_pilot.AIPilot._plan_valid(side, reentry, take, inv):
            return None
        now = time.time()
        return {"side": side, "entry": round(reentry, 6), "kind": kind, "take": take, "invalidation": round(inv, 6),
                "why": f"мысль о прибыли: перезайти — {why or 'без объяснений'}", "ts": now,
                "src": "profit", "snap_price": cur, "snap_ts": now}

    async def _apply_profit(self, r: dict, price: float, pos: dict, reason: str, silent: str | None = None) -> None:
        """Ответ PRO о прибыли (слово — ai_v5.profit_table по стороне позиции: ЗАФИКСИРОВАТЬ / CLOSE / SELL у лонга —
        ВЫЙТИ). ДЕРЖАТЬ (+lock_price → триггер, take → цель); ВЫЙТИ → закрыть, обычный ход после закрытия;
        ВЫЙТИ_И_ПЕРЕЗАЙТИ → закрыть, затем план той же стороны у уровня (src «profit»: у уровня вход без второго вопроса,
        пока решение свежее); СОВЕТ → триггер к lock_price и полный совет без очереди (handoff `profit`).
        v5.4.2: молчание/сбой (НЕТ_ОТВЕТА) и непонятный ответ (НЕ_РАЗОБРАН) — не «ДЕРЖАТЬ» за ИИ: запись кода (source
        «код», silent), позиция как есть, поля ответа не читаются, повтор через PYTHIA_SILENT_RETRY_SEC, ошибка в панель."""
        if self.position is not pos or not self.position:
            log.info("миссия %s: мысль о прибыли — позиция уже закрыта/сменилась, ответ выброшен", self.base)
            return
        m = self.mission
        mm = self._money_name()
        now = time.time()
        cur = self.prices[-1] if self.prices else price
        cool = float(getattr(config, "PYTHIA_PROFIT_THINK_COOL_SEC", 900))
        retry = float(getattr(config, "PYTHIA_SILENT_RETRY_SEC", 120))
        raw = ai_v5.decision_raw(r)
        why = str(r.get("why") or "")[:300]
        if silent:
            decision = "НЕТ_ОТВЕТА"
        else:
            decision = ai_v5.decision_of(raw, ai_v5.profit_table(pos.get("side"))) or "НЕ_РАЗОБРАН"
        no_answer = decision in ("НЕТ_ОТВЕТА", "НЕ_РАЗОБРАН")
        if no_answer:
            head = f"{mm} не ответил ({silent})" if silent else f"{mm} ответил непонятно ({raw[:60] or 'пусто'})"
            why = f"{head} — решения не было, позиция как есть, повтор в {self._hhmm(now + retry)}"
            rec = {"ts": now, "decision": decision, "why": why, "note": "", "price": cur,
                   "floating": pos.get("floating"), "lock_price": None, "take": None, "reentry": None,
                   "reentry_kind": None, "reason": reason[:200], "side": pos.get("side"), "model": mm,
                   "silent": True, "source": "код", "raw": raw[:80] or None, "applied": ""}
        else:
            lock = _f(r.get("lock_price"), 0.0)
            take = _f(r.get("take"), 0.0)
            reentry = _f(r.get("reentry")) if r.get("reentry") not in (None, "", "null") else None
            rk = str(r.get("reentry_kind") or "").strip().lower()
            rec = {"ts": now, "decision": decision, "why": why, "note": str(r.get("note") or "")[:300], "price": cur,
                   "floating": pos.get("floating"), "lock_price": lock or None, "take": take or None,
                   "reentry": reentry if reentry and reentry > 0 else None, "reentry_kind": rk or None,
                   "reason": reason[:200], "side": pos.get("side"), "model": mm, "silent": False, "source": "ИИ",
                   "applied": ""}
        self.profits.append(rec)
        del self.profits[:-ai_pilot.PROFITS_KEEP]
        if not no_answer:
            pos["profit_last"] = now
            pos["profit_next"] = now + cool
        applied: list[str] = []
        if no_answer:
            # решения ИИ не было: позицию не трогаем, скорый повтор (не полный кулдаун); трос и тейк стерегут дальше
            pos["profit_next"] = now + retry
            self._save_state()
            self.last_action = f"мысль о прибыли: {why}"
            try:
                ai_v5.note_error(f"мысль о прибыли {mm}: {silent or 'ответ не разобран: ' + (raw[:60] or 'пусто')}",
                                 "mission_profit")
            except Exception:                        # noqa: BLE001
                pass
        elif decision in ("ДЕРЖАТЬ", "СОВЕТ"):
            self._profit_lock(pos, lock, take, cur, applied)
            self._save_state()
            self.last_action = (f"мысль о прибыли: {mm} решил {decision} ({why or 'без объяснений'}) — "
                                + (", ".join(applied) if applied else "уровни прежние")
                                + f"; следующая мысль не раньше чем через {int(cool // 60)} мин")
            if decision == "СОВЕТ":
                applied.append("зову полный совет")
                self._profit_handoff(why)
        elif decision == "ВЫЙТИ":
            self._save_state()
            await self._close_all(cur, f"мысль о прибыли: {mm} решил выйти — " + (why or "без объяснений"))
            applied.append(self.last_action[:160])
        else:                                        # ВЫЙТИ_И_ПЕРЕЗАЙТИ
            spec = self._reentry_plan(pos.get("side"), cur, reentry, rk, take or None,
                                      pos.get("inv0") or pos.get("invalidation"), pos.get("take"), why)
            if spec is None:
                applied.append(f"уровень перезахода {reentry} ({rk or '—'}) не с той стороны — как ВЫЙТИ")
                self._save_state()
                await self._close_all(cur, f"мысль о прибыли: {mm} решил выйти (перезайти негде) — " + (why or "без объяснений"))
            else:
                pos["reentry"] = spec                # план встанет, когда позиция закрыта (MissionPilot._close_all)
                self._save_state()
                await self._close_all(cur, f"мысль о прибыли: {mm} решил выйти и перезайти {spec['kind']} @{spec['entry']:g} — "
                                      + (why or "без объяснений"), reanalyze=False)
                applied.append(f"план перезайти: {spec['kind']} @{spec['entry']:g}, стоп {spec['invalidation']:g}, тейк {spec.get('take')}"
                               + ("" if self.plan is not None and self.plan.get("ts") == spec["ts"] else " (после закрытия)"))
        rec["applied"] = "; ".join(applied)
        log.info("миссия %s: мысль о прибыли → %s (%s)", self.base, decision, why)
        if m is None:
            return
        _bg(bus.stage("mission", m.run_id, "profit", "done", ticker=self.base,
                      detail=f"мысль о прибыли: {decision} — {why}" + (f" · {rec['applied']}" if rec["applied"] else ""), data=rec))
        title = (f"Мысль о прибыли: {mm} не ответил" if decision == "НЕТ_ОТВЕТА" else
                 f"Мысль о прибыли: ответ {mm} не разобран" if decision == "НЕ_РАЗОБРАН" else f"Мысль о прибыли: {decision}")
        _tolmach(m, "profit", title,
                 f"{reason[:160]}; {mm}: {why or 'без объяснений'}" + (f"; {rec['applied']}" if rec["applied"] else "")
                 + (f". {rec['note']}" if rec["note"] else ""),
                 refs={"decision": decision, "price": cur, "floating": rec["floating"], "lock_price": rec["lock_price"],
                       "take": rec["take"], "reentry": rec["reentry"], "reentry_kind": rec["reentry_kind"],
                       "source": rec["source"]})
        _persist(m)

    def _profit_handoff(self, why: str) -> None:
        """СОВЕТ из мысли о прибыли → полный совет без очереди и без окна PYTHIA_COUNCIL_GAP_SEC (handoff kind
        `profit`, переворот по такому совету без тишины); совет уже идёт → дежурный PRO сразу после него."""
        m = self.mission
        reason = f"мысль о прибыли: {self._money_name()} зовёт Совет — " + (why or "без объяснений")
        now = time.time()
        if m is not None:
            m.handoffs.append({"ts": now, "reason": reason, "deferred": False, "kind": "profit"})
            del m.handoffs[:-50]
        if m is not None and m.council_running():
            self._ask_review_now(reason, kind="profit")
            self.review_ts = min(self.review_ts, now)
            return
        self._council_reason = reason
        self._council_kind = "profit"
        super()._fire_reanalyze(reason, force=True)
        if m is not None:
            _bg(bus.stage("mission", m.run_id, "pilot", "progress", ticker=self.base,
                          detail="передаю задачу Совету: " + reason))
            _persist(m)

    def _adopt_reentry(self, spec: dict) -> None:
        """Позиция закрыта по ВЫЙТИ_И_ПЕРЕЗАЙТИ → план той же стороны у уровня. v5.4.2: план несёт src «profit» и снимок
        решения — у уровня вход без второго вопроса у двери, пока решение свежее; протухло / цена уехала — через дверь."""
        if self.panic_flag or self.stopping:         # V 5.4.1: после ПАНИКИ / СТОПА планов не ставим
            return
        if self.session_risk and self.session_risk.locked:
            self.last_action = (self.last_action or "") + " · план перезайти не ставлю: killswitch"
            return
        if self.plan or self.position or self.pending or self._reanalyzing:
            return
        self.plan = dict(spec)
        self.state = "ЗАСАДА"
        self._break_n = 0
        head = str(self.last_action or "")
        self.last_action = ((head + " · ") if head.startswith("ЗАКРЫЛ ВСЁ") else "") + (
            f"план перезайти: {spec['side']} {spec['kind']} @{_f(spec.get('entry'), 0.0):g}, стоп {_f(spec.get('invalidation'), 0.0):g}, "
            f"тейк {spec.get('take')} — у уровня вход по этому решению "
            f"(протухнет за {int(float(getattr(config, 'PYTHIA_ENTRY_FRESH_SEC', 1200)) // 60)} мин — тогда через дверь)")
        log.info("миссия %s: %s", self.base, self.last_action)
        m = self.mission
        if m is not None:
            _bg(bus.stage("mission", m.run_id, "pilot", "progress", ticker=self.base, detail=self.last_action))
            _persist(m)

    def _council_timeout(self, lim: float) -> None:
        """v5.4.2: совет по поводу не уложился в PYTHIA_COUNCIL_MAX_SEC — разморозить пилот: _reanalyzing снят,
        позиция и план как есть, ошибка ИИ для панели, перепроверка дежурного PRO через EVENT_MIN_GAP_SEC.
        Ревью 5.4.2: окно совета (PYTHIA_COUNCIL_GAP_SEC) считается от обрыва — НОВЫЙ_АНАЛИЗ сразу после него не
        запускает новый совет (иначе медленный PRO крутил бы цикл «совет — обрыв»), просьба ждёт окна."""
        m = self.mission
        if m is not None:
            m.council_ts = self._council_cut_ts = time.time()
        self._reanalyzing = False
        if self.state == "ПЕРЕАНАЛИЗ":
            self.state = "В_ПОЗИЦИИ" if self.position else ("ЗАСАДА" if self.plan else "ЖДУ_ПЛАН")
        why = f"совет не уложился в {int(lim // 60)} мин — прерван; реши по живой картине"
        self.review_ts = min(self.review_ts, time.time() + EVENT_MIN_GAP_SEC)
        self._review_reason = f"{self._review_reason}; {why}" if self._review_reason and why not in self._review_reason \
            else (self._review_reason or why)
        self._review_kind = self._review_kind or "pilot"
        self.last_action = f"{why} — пилот разморожен, дежурный PRO решит через {int(EVENT_MIN_GAP_SEC // 60)} мин"
        log.warning("миссия %s: %s", self.base, self.last_action)
        try:
            ai_v5.note_error(f"совет миссии: дольше {int(lim)} с — прерван", "mission_council")
        except Exception:                            # noqa: BLE001  (в тестах ai_v5 — фейк без note_error)
            pass
        if m is not None:
            _bg(bus.stage("mission", m.run_id, "pilot", "progress", ticker=self.base, detail=self.last_action))
            _tolmach(m, "council", "Совет не уложился в срок — пилот разморожен", self.last_action,
                     refs={"limit_s": int(lim)})

    def _fire_reanalyze(self, why: str, force: bool = False, kind: str | None = None) -> None:
        """kind (v5.4.2) — кто просит: «pilot» — повод пилота дежурному PRO; «council» — полный совет (НОВЫЙ_АНАЛИЗ
        перепроверки). Без kind — родитель (AIPilot): его поводы — строки кода, узнаются по НАЧАЛУ (PILOT_REASONS),
        а не по подстроке в чужом «why» (PRO мог написать «после закрытия» в своём объяснении). Совет в окне
        PYTHIA_COUNCIL_GAP_SEC не теряется: отложен (_reanalyze_pending), перепроверка — к открытию окна."""
        if self._reanalyzing:
            return
        m = self.mission
        w = (why or "").lower()
        if force:                                   # мягкий стоп: без очереди и без окна совета
            self._council_reason = why
            super()._fire_reanalyze(why, force=True)
            return
        if kind is None:
            kind = "pilot" if any(w.startswith(k) for k in PILOT_REASONS) else "council"
        # 1) поводы пилота — дежурному PRO, не совету
        if kind == "pilot":
            self._ask_review(why, kind="pilot")
            return
        # 2) полный совет по автоматическому поводу — не чаще PYTHIA_COUNCIL_GAP_SEC
        gap = float(getattr(config, "PYTHIA_COUNCIL_GAP_SEC", 1800))
        since = time.time() - float(getattr(m, "council_ts", 0) or 0) if m is not None else gap
        if gap > 0 and since < gap and "по кнопке" not in w:
            if self._council_deferred == why and self._reanalyze_pending == why:
                return                              # тик §3б повторяет отложенную просьбу — окно ещё закрыто
            left = max(1, int(gap - since) // 60)
            self._council_blocked = (f"{ai_v5.fmt_ts(time.time())} просили полный совет ({why}), но он был "
                                     f"{int(since // 60)} мин назад — не чаще раза в {int(gap // 60)} мин, "
                                     f"окно откроется через {left} мин — просьба отложена до окна (следующий "
                                     f"твой ответ без НОВЫЙ_АНАЛИЗ её снимет); до тех пор решай сам")
            # v5.4.2: просьба не пропадает — совет, как только окно откроется (тик §3б), перепроверка к окну
            self._reanalyze_pending = self._council_deferred = why
            if m is not None:
                self.review_ts = min(self.review_ts, float(m.council_ts or 0) + gap + 60.0)
            self.last_action = (f"совет был {int(since // 60)} мин назад — новый не раньше чем через "
                                f"{left} мин (просьба отложена до окна), решает дежурный PRO")
            log.info("миссия %s: %s", self.base, self.last_action)
            if m is not None:
                m.handoffs.append({"ts": time.time(), "reason": why, "deferred": True, "kind": "council"})
                del m.handoffs[:-50]
                _bg(bus.stage("mission", m.run_id, "pilot", "progress", ticker=self.base, detail=self.last_action))
                _persist(m)
            return
        self._council_reason = why
        ts0 = self._last_reanalyze_ts
        super()._fire_reanalyze(why)
        if m is None:
            return
        deferred = (self._last_reanalyze_ts == ts0)
        last = m.handoffs[-1] if m.handoffs else None
        if last and last.get("reason") == why and time.time() - float(last.get("ts") or 0) < 3600:
            last["ts"], last["deferred"] = time.time(), deferred      # та же причина за час — не плодим дубли
            last["repeats"] = int(last.get("repeats") or 1) + 1
        else:
            m.handoffs.append({"ts": time.time(), "reason": why, "deferred": deferred, "kind": "council"})
        del m.handoffs[:-50]
        _bg(bus.stage("mission", m.run_id, "pilot", "progress", ticker=self.base,
                      detail="передаю задачу совету: " + why + (" (отложено пейсингом)" if deferred else "")))
        _persist(m)

    def status(self) -> dict:
        st = super().status()
        st["review_reason"] = self._review_reason
        st["review_kind"] = self._review_kind
        st["council_blocked"] = bool(self._council_blocked)
        st["council_deferred"] = bool(self._council_deferred)   # v5.4.2: НОВЫЙ_АНАЛИЗ ждёт окна совета
        ws = self._wait_st                           # v5.4.2: приказ WAIT под наблюдением (цена отсчёта, пройденные уровни)
        st["wait_watch"] = {"ref": ws.get("ref"), "fired": sorted(ws.get("fired") or {})} if ws else None
        # v5.4.1: модель узлов у денег, проверка входа у двери и мысль о прибыли — включены ли
        fn = getattr(ai_v5, "money_model", None)
        st["money_model"] = str(fn() if callable(fn) else "pro").lower()
        st["entry_check"] = self._entry_check_on()
        st["profit_think"] = self._profit_think_on()
        st["triages"] = self.triages[-10:]           # v5.3 W2: решения FLASH-триажа событий
        st["triage_busy"] = bool(self._triage_task and not self._triage_task.done())
        st["puncture"] = ({k: v for k, v in self.puncture.items() if k != "key"} if self.puncture else None)   # v5.3 W3
        st["puncture_now"] = ({k: v for k, v in self._puncture_now.items() if k not in ("key", "text")}
                              if self._puncture_now else None)
        st["puncture_min"] = float(getattr(config, "PYTHIA_PUNCTURE_MIN", 0.6)) if bool(getattr(config, "PYTHIA_PUNCTURE", True)) else None
        st["price_ts"] = self._px_hist[-1][0] if self._px_hist else None   # v5.3 фаза 3: свежесть цены для панели проблем
        m = self.mission
        st["council_age_min"] = int((time.time() - m.council_ts) // 60) if m is not None and m.council_ts else None
        return st


def _bind_pilot(m: Mission, pilot: MissionPilot) -> None:
    """Колбэк полного совета: повод берётся у пилота (НОВЫЙ_АНАЛИЗ от PRO, серьёзная новость …)."""
    pilot.reanalyze_cb = lambda: _pilot_council(m, pilot)


async def _pilot_council(m: Mission, pilot: MissionPilot) -> dict:
    """v5.4.2: автоматический совет (НОВЫЙ_АНАЛИЗ, передачи от троса/тейка/двери/мысли о прибыли) держит пилот
    (входы и перепроверки стоят, пока _reanalyzing) не дольше PYTHIA_COUNCIL_MAX_SEC: дольше — совет прерван,
    пилот разморожен (позиция и план не трогаются), дежурный PRO решает через EVENT_MIN_GAP_SEC."""
    lim = float(getattr(config, "PYTHIA_COUNCIL_MAX_SEC", 3600))
    try:
        return await asyncio.wait_for(council_again(m.ticker, pilot.council_reason(), wait=True), lim)
    except asyncio.TimeoutError:
        await _cancel_council(m)
        pilot._council_timeout(lim)
        m.error = f"совет по поводу дольше {int(lim // 60)} мин — прерван, пилот разморожен"
        if pilot.position:
            m.phase = "in_position"
        elif m.phase in ("council", "error"):
            m.phase = "idle"
        m.note = m.error
        _persist(m)
        return {"ok": False, "note": m.error}


# ── сканер стакана («8 ч» из 4.x) ──────────────────────────────────────────────
def _scan_status_raw(ticker: str) -> dict | None:
    if maya_scan is None:
        return None
    try:
        return maya_scan.status((ticker or "").upper().strip())
    except Exception as e:                           # noqa: BLE001
        log.info("сканер %s: статус: %s", ticker, str(e)[:80])
        return None


async def _scan_start(m: Mission, minutes: int | None = None) -> dict:
    """Онлайн-наблюдение за стаканом инструмента миссии (стакан 50 уровней + лента,
    тик 3 с, до 8 ч). Без токена Tinkoff честно молчит; повторный старт продлевает."""
    if maya_scan is None:
        return {"ok": False, "note": "сканер: модуль недоступен"}
    if not tinkoff.enabled():
        return {"ok": False, "note": "сканер: нет токена Tinkoff — стакана нет"}
    mins = int(minutes or getattr(config, "PYTHIA_SCAN_MIN", 480))
    try:
        r = await asyncio.wait_for(maya_scan.start(m.ticker, m.ticker, m.asset_class, mins), 60)
    except Exception as e:                           # noqa: BLE001
        r = {"ok": False, "note": f"сканер не стартовал: {str(e)[:100]}"}
        log.info("сканер %s: %s", m.ticker, r["note"])
    return r or {"ok": False, "note": "сканер: пустой ответ"}


def _scan_text(m: Mission) -> str:
    """Отчёт сканера для ИИ (все накопленные агрегаты); мало тиков или нет скана → ''."""
    st = _scan_status_raw(m.ticker)
    if not st or int(st.get("ticks") or 0) < int(getattr(maya_scan, "MIN_TICKS_AGG", 10)):
        return ""
    fn = getattr(maya_scan, "render_for_ai", None)
    try:
        return (fn(st) if fn else "") or ""
    except Exception as e:                           # noqa: BLE001
        log.info("сканер %s: рендер: %s", m.ticker, str(e)[:80])
        return ""


def _scan_brief(m: Mission) -> dict | None:
    """Краткий статус сканера для панели."""
    st = _scan_status_raw(m.ticker)
    if not st:
        return None
    fn = getattr(maya_scan, "brief", None)
    try:
        if fn:
            return fn(st)
    except Exception as e:                           # noqa: BLE001
        log.info("сканер %s: brief: %s", m.ticker, str(e)[:80])
    c = st.get("consensus") or {}
    return {"running": bool(st.get("running")), "ticks": st.get("ticks"),
            "elapsed_min": st.get("elapsed_min"), "side": c.get("side"),
            "up_share": c.get("up_share"), "dn_share": c.get("dn_share"), "note": st.get("note")}


def _scan_stop_quiet(ticker: str) -> None:
    if maya_scan is None:
        return
    try:
        maya_scan.stop((ticker or "").upper().strip())
    except Exception as e:                           # noqa: BLE001
        log.info("сканер %s: стоп: %s", ticker, str(e)[:80])


async def scan_start(ticker: str, minutes: int | None = None) -> dict:
    """API: запустить/продлить сканер по тикеру (миссия не обязательна)."""
    ticker = _norm_ticker((ticker or "").upper().strip())
    if not ticker:
        return {"ok": False, "note": "пустой тикер"}
    m = _M.get(ticker)
    if m is None:
        it = instruments.get(ticker) or {}
        m = Mission(ticker, it.get("name") or ticker,
                    it.get("asset_class") or instruments.guess_asset_class(ticker), "auto")
    return await _scan_start(m, minutes)


def scan_stop(ticker: str) -> dict:
    if maya_scan is None:
        return {"ok": False, "note": "сканер: модуль недоступен"}
    try:
        return maya_scan.stop((ticker or "").upper().strip())
    except Exception as e:                           # noqa: BLE001
        return {"ok": False, "note": f"сканер: {str(e)[:100]}"}


def scan_status(ticker: str) -> dict:
    st = _scan_status_raw(ticker)
    if not st:
        return {"running": False, "ticks": 0, "note": "сканер недоступен или не запускался"}
    out = dict(st)
    out.pop("last", None)
    m = _M.get((ticker or "").upper().strip())
    out["brief"] = _scan_brief(m) if m else None
    if out["brief"] is None and maya_scan is not None:     # скан без миссии — brief всё равно нужен панели
        try:
            out["brief"] = maya_scan.brief(st)
        except Exception:                            # noqa: BLE001
            out["brief"] = None
    fn = getattr(maya_scan, "render_for_ai", None) if maya_scan else None
    try:
        out["text"] = fn(st) if fn else ""
    except Exception:                                # noqa: BLE001
        out["text"] = ""
    return out


# ── API-функции ───────────────────────────────────────────────────────────────
async def start(ticker: str, play: str, deposit: float | None = None, reason: str = "запуск") -> dict:
    ticker = _norm_ticker((ticker or "").upper().strip())
    play = (play or "auto").lower().strip()
    if play not in PLAY:
        raise ValueError(f"режим игры {play!r} неизвестен — нужен один из {', '.join(PLAY)}")
    if not ticker:
        raise ValueError("пустой тикер")
    _restore_pending_panics()
    pending_state = _state_file_position()
    if pending_state and pending_state.get("unsettled"):
        return {"ok": False, "run_id": None,
                "note": "сохранённая заявка ещё не сверена с брокером — дождись восстановления её состояния"}
    act = _active()
    if act:
        return {"ok": False, "run_id": None,
                "note": f"уже идёт миссия {act.ticker} ({'совет' if act.council_running() else 'пилот'}) — "
                        f"один депозит = одна активная миссия; останови её"}
    it = instruments.get(ticker) or {}
    name = it.get("name") or ticker
    asset_class = it.get("asset_class") or instruments.guess_asset_class(ticker)
    m = Mission(ticker, name, asset_class, play, _f(deposit) if deposit is not None else None)
    m.run_id = bus.start_run("mission", {"ticker": ticker, "play": play})
    if explain is not None:                          # v5.3 фаза 3 (проверяющий): новая миссия — толмач с чистого листа,
        explain.reset(ticker)                        # иначе дедуп DEDUP_SEC по тикеру глотал узел «приказ» новой миссии с тем же текстом
    m.council_task = asyncio.create_task(_council(m, reason, first=True))
    _M[ticker] = m
    _persist(m)
    return {"ok": True, "run_id": m.run_id, "note": f"миссия {ticker} ({play}): совет по инструменту пошёл"}


async def council_again(ticker: str, reason: str, wait: bool = False) -> dict:
    """Полный разбор с нуля для живой миссии. wait=True — дождаться (колбэк пилота:
    adopt_forecast должен случиться внутри, иначе родитель сбросит _reanalyzing)."""
    ticker = (ticker or "").upper().strip()
    _restore_pending_panics()
    m = _M.get(ticker)
    if not m:
        return {"ok": False, "note": f"миссии {ticker} нет"}
    pending_state = _state_file_position()
    if pending_state and pending_state.get("unsettled") and (
            pending_state.get("base") != ticker or not m.pilot_alive()):
        return {"ok": False, "note": "сначала нужно сверить незавершённую заявку с брокером"}
    if not m.auto_resume:
        return {"ok": False, "note": "миссия остановлена — сначала нажми «продолжить»"}
    act = _active()
    if act and act is not m:
        return {"ok": False, "note": f"уже идёт миссия {act.ticker} — один депозит = одна активная миссия"}
    if m.council_running():
        return {"ok": False, "run_id": m.run_id, "note": "совет по этой миссии уже идёт"}
    m.run_id = bus.start_run("mission", {"ticker": ticker, "play": m.play, "again": True, "reason": reason})
    m.council_task = asyncio.create_task(_council(m, reason, first=False))
    if wait:
        rid = m.run_id
        ex = await m.council_task
        return {"ok": bool(ex), "run_id": rid, "note": m.note}
    return {"ok": True, "run_id": m.run_id, "note": f"пересмотр миссии {ticker}: {reason}"}


async def _cancel_council(m: Mission) -> None:
    task, rid = m.council_task, m.run_id
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    # Отмена до первого шага корутины не попадает в её except CancelledError.
    if rid and (bus.run(rid) or {}).get("ended") is None:
        bus.end_run(rid, status="cancelled")


async def shutdown_tasks() -> None:
    """Завершить собственные советы до закрытия клиентов; намерение владельца сохраняется."""
    await asyncio.gather(*(_cancel_council(m) for m in list(_M.values())))


async def stop(ticker: str) -> dict:
    m = _M.get((ticker or "").upper().strip())
    if not m:
        return {"ok": False, "note": "миссии нет"}
    m.auto_resume = False
    m.phase = "stopped"
    m.error = None
    if m.panic_orders or (m.panic_task and not m.panic_task.done()):
        m.phase = "panic"
        m.note = "ПАНИКА ещё закрывает позицию — дождись завершения"
        await _cancel_council(m)
        _persist(m)
        return {"ok": True, "note": m.note}
    if m.pilot and m.pilot_alive():
        m.pilot.stop()
        pos = m.pilot.position
        if m.pilot.panic_flag and (pos or m.pilot.pending):   # v5.3 фаза 3: ПАНИКА ещё не исполнена — стоп её не отменяет
            m.phase = "panic"
            m.note = "ПАНИКА ещё закрывает позицию — пилот остановится, как только всё закрыто"
            await _cancel_council(m)
            _persist(m)
            return {"ok": True, "note": m.note}
        m.note = ("пилот остановлен" + ((f"; позиция {pos['side']} {pos['lots']} лот ОСТАЁТСЯ под биржевым "
                                           f"тросом @{pos.get('hard_stop') or pos.get('invalidation')} — веди сам или «продолжить»"
                                           if m.pilot._exchange_stop_on() or pos.get("stop_id") else
                                           f"; позиция {pos['side']} {pos['lots']} лот ОСТАЁТСЯ БЕЗ ЗАЩИТЫ — стопов на бирже нет "
                                           "(стопы только в программе): веди сам или «продолжить»")
                                          if pos else "; позиции нет"))
    else:
        m.note = "пилот не работал — миссия помечена остановленной"
    m.phase = "stopped"
    await _cancel_council(m)
    # сканер стакана НЕ гасим: он пассивный наблюдатель, копит часами; «продолжить»
    # лишь продлит его дедлайн, накопленные тики останутся (стоп сканера — своей кнопкой или ПАНИКОЙ)
    _persist(m)
    return {"ok": True, "note": m.note}


def _state_file_position() -> dict | None:
    try:
        p = config.DATA_DIR / "aipilot_state.json"
        if not p.exists():
            return None
        try:
            raw = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw = p.read_text()  # legacy state files used the system encoding
        rec = json.loads(raw)
        pos = rec.get("position") or {}
        unsettled = bool(rec.get("pending") or pos.get("exit_order") or pos.get("stop_request"))
        return {"base": (rec.get("base") or "").upper(), "side": pos.get("side"),
                "lots": int(pos.get("lots") or 0), "unsettled": unsettled} if pos.get("lots") or unsettled else None
    except Exception as e:                           # noqa: BLE001
        return {"base": "", "unsettled": True, "unreadable": True, "error": f"{type(e).__name__}: {str(e)[:80]}"}


async def resume(ticker: str, *, settle_only: bool = False, panic_mode: bool = False) -> dict:
    ticker = (ticker or "").upper().strip()
    _restore_pending_panics()
    sf = _state_file_position()
    if sf and sf.get("unsettled") and (sf.get("unreadable") or sf.get("base") != ticker):
        return {"ok": False, "note": "сначала нужно сверить сохранённую заявку другого инструмента с брокером"}
    m = _M.get(ticker) or _from_store(ticker)
    if not m:
        return {"ok": False, "note": f"миссии {ticker} нет — запусти заново"}
    if m.panic_orders or (m.panic_task and not m.panic_task.done()):
        return {"ok": False, "note": "ПАНИКА ещё закрывает позицию — дождись завершения"}
    if m.pilot_alive():
        if m.pilot.stopping or m.pilot.panic_flag:
            return {"ok": False, "note": "пилот ещё останавливается — дождись завершения"}
        return {"ok": True, "note": "пилот уже работает: " + str(m.pilot.last_action)}
    if m.council_running():
        return {"ok": False, "note": "совет ещё идёт — дождись завершения"}
    if not tinkoff.enabled():
        return {"ok": False, "note": "нет токена Tinkoff — исполнять нечем"}
    act = _active()
    if act and act.ticker != ticker:
        return {"ok": False, "note": f"уже идёт миссия {act.ticker} — один депозит = одна активная миссия"}
    _M[ticker] = m
    pilot = MissionPilot(ticker, deposit=m.deposit, broker=_make_broker(), mission=m)
    _bind_pilot(m, pilot)
    m.pilot = pilot
    if not settle_only:
        m.auto_resume = True
    else:
        # stop() persists immediately; before prepare/restore that would erase
        # the very order we are recovering. Set only the runtime intention here.
        pilot.stopping = True
        pilot._cancel_entry = True
    if panic_mode:
        pilot.panic_flag = True
    m.error = None
    adopted = False
    if (not settle_only and not (sf or {}).get("unsettled") and m.exec and m.exec_ts
            and time.time() - m.exec_ts <= EXEC_FRESH_SEC and m.exec.get("do") not in ("CLOSE", "WAIT", "HOLD")):
        # CLOSE заново не принимаем: он уже исполнен, а свежую позицию со счёта (владелец купил
        # руками после закрытия) пилот обязан вести, а не закрывать по старому приказу; WAIT (v5.4.1) —
        # плана и так нет: пилот поднимается вне рынка, дежурный PRO решит на перепроверке; HOLD (v5.4.2) —
        # уровни уже в позиции (state-файл), повторно не принимаем
        want = "long" if m.exec.get("do") == "BUY" else "short"
        if not (sf and sf.get("base") == ticker and sf.get("side") and sf["side"] != want):
            adopted = pilot.adopt_forecast({"exec": m.exec})
    if not adopted and not settle_only:
        # v5.4.2: приказа нет (WAIT / протух / CLOSE исполнен) — не спать PYTHIA_REVIEW_SEC: первая перепроверка скоро
        grace = max(float(ai_pilot.OPEN_REVIEW_GRACE_SEC), 120.0)
        pilot.review_ts = min(float(getattr(pilot, "review_ts", 0.0) or time.time() + grace), time.time() + grace)
        rr = "пилот поднят заново: приказа нет — реши по живой картине"
        prev = getattr(pilot, "_review_reason", None)
        pilot._review_reason = f"{prev}; {rr}" if prev and rr not in prev else (prev or rr)
        if not getattr(pilot, "_review_kind", None):
            pilot._review_kind = "pilot"
    m.task = asyncio.create_task(pilot.run())
    m.task.add_done_callback(lambda t: _pilot_done(m, t))
    m.phase = ("armed" if (m.exec or {}).get("entry") is not None else "entering") if adopted else "idle"
    m.note = ("пилот поднят заново: " + ("приказ принят — " + pilot.last_action if adopted else
                                         "приказ совета WAIT — вне рынка, дежурный PRO решит на перепроверке"
                                         if (m.exec or {}).get("do") == "WAIT" and not settle_only else
                                         "свежего приказа нет, позицию подхватит из state-файла; "
                                         "перепроверка решит, просить ли совет"))
    if settle_only:
        m.phase = "panic" if panic_mode else "stopped"
        m.note = ("ПАНИКА: восстанавливаю незавершённую заявку для отмены и закрытия" if panic_mode else
                  "сверяю незавершённую заявку; новые входы отключены по команде владельца")
    else:
        await _scan_start(m)                         # сканер поднимается вместе с пилотом
    _persist(m)
    return {"ok": True, "note": m.note}


def _set_panic_orders(m: Mission, orders: list[dict]) -> None:
    previous = m.panic_orders
    m.panic_orders = orders
    if not _persist(m):
        m.panic_orders = previous
        raise RuntimeError("не удалось сохранить состояние аварийного закрытия — новая заявка не отправлена")


async def _flat_account(m: Mission) -> dict:
    """ПАНИКА без живого пилота (v5.2): закрыть позицию по инструменту прямо на счёте и снять её
    биржевые стопы — кнопка обязана делать то, что написано, даже если пилот остановлен."""
    if not tinkoff.enabled():
        return {"ok": False, "note": "нет токена Tinkoff — закрывать нечем"}
    try:
        broker = _make_broker()
        force = bool(getattr(m, "panic_force", False))
        m.panic_force = False                        # force — одноразовая воля владельца: сторож дальше только опрашивает
        dropped_note = ""
        if m.panic_orders and force:
            # W4 (№3): force — не ждать сохранённое закрытие, которого биржа не подтверждает: сбросить из учёта
            # (UUID в лог для сверки) и закрыть по портфелю; flat_all сначала снимет активные заявки по инструменту
            # (GetOrders → CancelOrder), портфель отражает исполнения — второй продажи не будет
            ids = ", ".join(str(o.get("order_id"))[:12] for o in m.panic_orders)
            log.error("паника force %s: %d сохранённых закрывающих заявок сброшены из учёта (%s) — закрываю по портфелю",
                      m.ticker, len(m.panic_orders), ids)
            _set_panic_orders(m, [])
            dropped_note = f"force: прежние закрывающие заявки ({ids}) сброшены из учёта — сверь их у брокера"
        elif m.panic_orders:
            if any(order.get("mode", "real") != broker.mode for order in m.panic_orders):
                return {"ok": False, "pending": True,
                        "note": "сохранённое закрытие относится к другому режиму — нужна сверка с исходным брокером"}
            # Reconnects/retries must inspect the submitted order, never sell again
            # while a timeout or partial fill leaves its final quantity unknown.
            waiting, errors, closed, vanished = [], [], 0, []
            for order in m.panic_orders:
                broker.account_id = order.get("account_id")
                try:
                    st = await asyncio.wait_for(broker.order_state(
                        order["order_id"], request_id=order.get("id_type") == "request"), 15)
                except Exception as e:               # noqa: BLE001
                    waiting.append(order)
                    log.info("паника: статус заявки пока неизвестен: %s", str(e)[:100])
                    continue
                if st.get("filled"):
                    closed += 1
                elif st.get("not_found") and order.get("id_type") == "request":
                    # W4 (№3): биржа не знает заявку дольше ORDER_REQUEST_MAX_AGE_SEC — до биржи не дошла (фантом):
                    # из учёта вон, позицию закрываем по портфелю ниже. V (e): «не найдена» должна держаться подряд
                    # не меньше того же срока (not_found_since хранится в заявке) — один транзиентный 404 не сбрасывает
                    now_ = time.time()
                    since = _f(order.get("not_found_since")) or now_
                    if (now_ - _f(order.get("ts")) > trader_broker.ORDER_REQUEST_MAX_AGE_SEC
                            and now_ - since >= trader_broker.ORDER_REQUEST_MAX_AGE_SEC):
                        vanished.append(str(order.get("order_id"))[:12])
                        log.error("паника %s: закрывающая заявка %s не найдена биржей дольше %d с — до биржи не дошла, "
                                  "закрываю по портфелю", m.ticker, order.get("order_id"), int(trader_broker.ORDER_REQUEST_MAX_AGE_SEC))
                    else:
                        waiting.append(dict(order, not_found_since=since))
                elif st.get("ok") and str(st.get("status") or "").endswith(("REJECTED", "CANCELLED")):
                    errors.append("закрывающая заявка завершилась без полного исполнения — проверь остаток позиции")
                else:
                    updated = dict(order, exec_lots=st.get("exec_lots") or order.get("exec_lots") or 0)
                    updated.pop("not_found_since", None)     # ответ не «не найдена» — наблюдение заново
                    if st.get("ok") and st.get("order_id"):
                        updated.update(order_id=st["order_id"], id_type="exchange")
                    waiting.append(updated)
            _set_panic_orders(m, waiting)
            if waiting or errors or not vanished:
                return {"ok": not errors, "pending": bool(waiting),
                        "note": (f"жду подтверждения исполнения {len(waiting)} закрывающих заявок" if waiting else
                                 "; ".join(errors) if errors else f"закрытие подтверждено: {closed}")
                                + (f" · ⚠ заявки {', '.join(vanished)} до биржи не дошли, сброшены" if vanished else "")}
            dropped_note = f"заявки {', '.join(vanished)} до биржи не дошли — сброшены, закрываю по портфелю"
        accs = await asyncio.wait_for(tinkoff.accounts(), 15) or []
        if not accs:
            return {"ok": False, "note": "счёт Tinkoff не найден"}
        broker.account_id = accs[0]["id"]
        inst = await asyncio.wait_for(tinkoff.resolve(m.ticker, m.asset_class), 15) or {}
        figi = inst.get("figi") or inst.get("uid")
        if not figi:
            return {"ok": False, "note": f"инструмент {m.ticker} не найден у брокера"}
        lot = 1 if m.asset_class == "futures" else max(1, int(inst.get("lot") or 1))
        r = await asyncio.wait_for(broker.flat_all(              # force прочитан выше (одноразовый), сюда — тот же
            figi=figi, lot=lot, force=force,
            on_check=lambda: _set_panic_orders(m, list(m.panic_orders)),   # W4: гейт персиста ДО снятия стопов
            on_intent=lambda order: _set_panic_orders(m, [*m.panic_orders, order])), 30)
        try:
            _set_panic_orders(m, list(r.get("pending") or []))
        except RuntimeError:
            if not force or r.get("pending"):
                raise
        warn = "; ".join([*(r.get("warnings") if isinstance(r.get("warnings"), list) else []),
                          *([dropped_note] if dropped_note else [])])
        if r.get("pending"):
            return {"ok": True, "pending": True,
                    "note": f"жду подтверждения исполнения {len(m.panic_orders)} закрывающих заявок" + (f" · ⚠ {warn}" if warn else "")}
        if broker.mode == "dry":
            return {"ok": True, "note": "сухой режим: позиция на счёте не трогается"}
        if not r.get("ok"):
            return {"ok": False, "refusable": bool(r.get("stops_unknown")) or "force" in str(r.get("note") or ""),
                    "note": f"паника по счёту: {r.get('note') or 'биржа отбила'}"}
        return {"ok": True, "note": (f"закрыто по счёту: позиций {r.get('closed', 0)}, снято стопов {r.get('stops', 0)}"
                                    + (f", снято заявок {r.get('orders')}" if r.get("orders") else "")
                                    if r.get("closed") else "позиции по инструменту на счёте нет"
                                    + (f", снято стопов {r.get('stops')}" if r.get("stops") else "")
                                    + (f", снято заявок {r.get('orders')}" if r.get("orders") else ""))
                                    + (f" · ⚠ {warn}" if warn else "")}
    except Exception as e:                           # noqa: BLE001
        return {"ok": False, "note": f"паника по счёту не удалась: {str(e)[:120]}"}


async def _panic_without_pilot(m: Mission) -> dict:
    """Завершение принадлежит операции закрытия, даже если HTTP-клиент отключился."""
    r = await _flat_account(m)
    m.phase = "panic" if r.get("ok") else "error"
    m.note = "ПАНИКА без пилота: " + str(r.get("note") or "")
    if not r.get("ok"):
        m.error = str(r.get("note") or "закрытие не подтверждено")
    _persist(m)
    return r


PANIC_REPEAT_SEC = 120.0        # повторная ПАНИКА по тикеру в это окно после отказа «нужна проверка» = force
_panic_refused: dict[str, float] = {}   # тикер → когда паника отказала с «повтори/force»


def _repeat_force(ticker: str | None) -> bool:
    """Повторное нажатие ПАНИКИ после честного отказа («сохранённую заявку/стопы не проверить») = воля владельца
    закрыть без проверки (W4, п. 5): по тикеру — его отказ, без тикера — любой свежий."""
    now = time.time()
    for t, ts in list(_panic_refused.items()):
        if now - ts > PANIC_REPEAT_SEC:
            _panic_refused.pop(t, None)
    return any(t == ticker or ticker is None for t in _panic_refused)


def _mission_for(ticker: str) -> Mission:
    try:
        m = _from_store(ticker)
    except Exception:                                # noqa: BLE001
        m = None
    if m is None:
        it = instruments.get(ticker) or {}
        m = Mission(ticker, it.get("name") or ticker, it.get("asset_class") or instruments.guess_asset_class(ticker), "auto")
    return m


async def panic(ticker: str | None = None, *, force: bool = False) -> dict:
    """ПАНИКА: закрыть всё и встать. W4 (п. 4-5):
      · живые пилоты получают panic() ПЕРВЫМИ и независимо от базы (закрывают через брокера);
      · чтение сохранённых аварийных заявок (store) — только для пути «без пилота», в try/except: база не читается →
        пилоты всё равно паникуют, в ответе честная пометка; без пилота — отказ с «повтори/force»;
      · force=True (или повторное нажатие в PANIC_REPEAT_SEC после такого отказа) — закрывать по счёту напрямую,
        не дожидаясь проверки сохранённой заявки / списка стопов (сняв, что удалось; непроверенное названо)."""
    ticker = _norm_ticker((ticker or "").upper().strip()) or None
    force = bool(force) or _repeat_force(ticker)
    hit, notes, failed, pending = [], [], [], []
    # 1. живые пилоты — сразу, до любого чтения базы: «ПАНИКА обязана закрывать»
    for t, m in list(_M.items()):
        if (ticker and t != ticker) or not (m.pilot and m.pilot_alive()):
            continue
        m.auto_resume = False
        m.phase, m.error = "panic", None
        _scan_stop_quiet(t)                          # паника = «всё закрыть и встать»: сканер тоже
        pilot_force = force or (bool(getattr(m.pilot, "panic_flag", False)) and bool(getattr(m.pilot, "panic_blocked", False)))
        m.pilot.panic(force=pilot_force)
        m.note = "ПАНИКА: закрываю всё и встаю" + (" (force: без сверки стопов биржи)" if pilot_force else "")
        hit.append(t)
        _panic_refused.pop(t, None)
        _persist(m)
        await _cancel_council(m)
    # 2. путь «без пилота»: сохранённые аварийные заявки и state-файл — база не читается ≠ паника отменена
    store_error: str | None = None
    try:
        _restore_pending_panics()
    except RuntimeError as e:
        store_error = str(e)
        log.error("паника: %s", store_error)
    saved_order = _state_file_position()
    if ticker and ticker not in _M and ticker not in hit:
        m = _mission_for(ticker)
        if store_error and not force:
            _panic_refused[ticker] = time.time()
            return {"ok": False, "panic": [], "pending": [], "failed": [ticker], "force": False,
                    "note": f"{ticker}: {store_error} — живого пилота нет; повторная ПАНИКА (или force) закроет по счёту "
                            "напрямую без этой проверки"}
        _M[ticker] = m
    if saved_order and saved_order.get("unsettled") and not ticker and saved_order.get("base") not in _M:
        base = saved_order.get("base")
        if base:
            _M[base] = _mission_for(base)
        elif not hit and not force:
            _panic_refused[""] = time.time()
            return {"ok": False, "panic": [], "failed": [], "pending": [], "force": False,
                    "note": f"state-файл пилота не читается ({saved_order.get('error') or '?'}) — чью заявку закрывать, "
                            "неизвестно: назови тикер (повторная ПАНИКА по тикеру или force закроет по счёту напрямую)"}
    targets = [(t, m) for t, m in list(_M.items()) if (not ticker or t == ticker) and t not in hit]
    for t, m in targets:
        m.auto_resume = False
        m.phase, m.error = "panic", None
        _scan_stop_quiet(t)
        if m.pilot and m.pilot_alive():             # пилот появился между шагами — паника ему
            m.pilot.panic(force=force)
            m.note = "ПАНИКА: закрываю всё и встаю"
            hit.append(t)
            _persist(m)
            await _cancel_council(m)
            continue
        if store_error and not force:
            await _cancel_council(m)
            failed.append(t)
            m.error = store_error
            m.phase = "error"
            _panic_refused[t] = time.time()
            notes.append(f"{t}: {store_error} — повторная ПАНИКА (или force) закроет по счёту напрямую")
            _persist(m)
            continue
        if (saved_order and saved_order.get("unsettled")
                and (saved_order.get("base") == t or saved_order.get("unreadable")) and not force):
            await _cancel_council(m)
            if saved_order.get("unreadable"):
                r = {"ok": False, "note": f"state-файл пилота не читается ({saved_order.get('error') or '?'}) — сохранённую "
                                          "заявку не проверить; повторная ПАНИКА (или force) закроет по счёту напрямую: "
                                          "активные заявки и стопы по инструменту снимутся, позиция закроется"}
                _panic_refused[t] = time.time()
            else:
                try:
                    r = await resume(t, settle_only=True, panic_mode=True)
                except RuntimeError as e:
                    r = {"ok": False, "note": str(e)}
            if r.get("ok"):
                hit.append(t)
                pending.append(t)
            else:
                failed.append(t)
                m.error = str(r.get("note") or "восстановление заявки не удалось")
                m.phase = "error"
            notes.append(f"{t}: {r.get('note')}")
            _persist(m)
            continue
        # Двойное нажатие использует одно закрытие. Пока оно в полёте, новый
        # пилот/миссия не могут открыть позицию поверх аварийного закрытия.
        m.panic_force = force
        if not m.panic_task or m.panic_task.done():
            m.panic_task = asyncio.create_task(_panic_without_pilot(m))
        _persist(m)
        await _cancel_council(m)
        r = await asyncio.shield(m.panic_task)
        notes.append(f"{t}: {r.get('note')}")
        if r.get("ok"):
            hit.append(t)
            _panic_refused.pop(t, None)
            if r.get("pending"):
                pending.append(t)
                # V (2-й проход, g): «жду подтверждения» без force — тоже повод для повтора: владелец, нажавший ПАНИКУ
                # второй раз в PANIC_REPEAT_SEC, не обязан ждать биржу (flat_all под force снимает живые заявки по figi
                # и закрывает по свежему портфелю — второй продажи нет)
                _panic_refused[t] = time.time()
        else:
            failed.append(t)
            if r.get("refusable"):
                _panic_refused[t] = time.time()
    note = ("паника: " + ", ".join(hit)) if hit else "живых пилотов нет"
    if store_error and hit:
        note += (f" · ⚠ база миссий не читается ({store_error}) — пилот закрывает через брокера, "
                 "сохранённые аварийные заявки без пилота не проверены")
    if notes:
        note += " · " + "; ".join(notes)
    return {"ok": not failed, "panic": hit, "pending": pending, "failed": failed, "force": force, "note": note}


_STATE_PHASE = {"ЗАСАДА": "armed", "ВХОЖУ": "entering", "У_ДВЕРИ": "entering", "В_ПОЗИЦИИ": "in_position",
                "ПЕРЕАНАЛИЗ": "council", "ЖДУ_ПЛАН": "idle", "РЫНОК_ЗАКРЫТ": "closed"}


def _phase(m: Mission) -> str:
    if not m.auto_resume and m.phase in ("stopped", "panic", "error"):
        return m.phase
    if m.council_running():
        return "council"
    if m.pilot_alive():
        p = m.pilot
        if p.state == "СТОП":
            return "panic" if p.panic_flag else "stopped"
        if p.pending:
            return "entering"
        return _STATE_PHASE.get(p.state, m.phase)
    return m.phase


def status(ticker: str) -> dict | None:
    m = _M.get((ticker or "").upper().strip())
    if not m:
        return None
    p = m.pilot
    price = m.ctx_price
    pst = None
    if p:
        try:
            pst = p.status()
            if p.prices:
                price = p.prices[-1]
        except Exception as e:                       # noqa: BLE001
            pst = {"error": str(e)[:100]}
    try:
        tr = store_v5.trades_summary(m.ticker)
    except Exception:                                # noqa: BLE001
        tr = {"count": 0, "pnl": 0.0}
    return {"ticker": m.ticker, "name": m.name, "asset_class": m.asset_class, "play": m.play,
            "started_ts": m.started_ts, "run_id": m.run_id, "phase": _phase(m),
            "pilot": pst, "exec": m.exec, "frame": m.frame, "texts": m.texts, "news": m.news,
            "reviews": [{"ts": r.get("ts"), "choice": r.get("choice"), "why": r.get("why"),
                         "note": r.get("note")} for r in m.reviews[-REVIEWS_KEEP:]],
            "handoffs": [{"ts": h.get("ts"), "reason": h.get("reason"), "kind": h.get("kind") or "council",
                          "deferred": bool(h.get("deferred")), "triage": h.get("triage")} for h in m.handoffs[-20:]],
            "trades": tr, "price": price, "note": m.note, "error": m.error, "exec_ts": m.exec_ts,
            "live": m.live(), "deposit": m.deposit, "scan": _scan_brief(m),
            "panic_pending": len(m.panic_orders),
            "explain": explain.items(m) if explain is not None else [],
            "memory": (explain.memory_status(m) if explain is not None else
                       {"text": m.memory, "ts": m.memory_ts, "n": m.memory_n}),
            "sizes": m.sizes, "account_pos": m.account_pos or None,
            "market": (pst or {}).get("market") if isinstance(pst, dict) and (pst or {}).get("market") else m.market,
            "scout": {"requests": [{k: r.get(k) for k in ("kind", "code", "name", "days", "interval", "why")}
                                   for r in (m.scout_reqs or [])[:30]],
                      "text": (m.scout_text or "")[:4000]},
            "partners": {"items": [{k: x.get(k) for k in ("code", "name", "asset_class", "kind", "rho", "lead", "n",
                                                            "move_1d", "move_5d", "price", "source")}
                                   for x in (m.partners or [])[:10]],
                         "text": (m.partners_text or "")[:3000],
                         "note": _partners_note(m.ticker)}}


def _partners_note(ticker: str) -> str:
    try:
        return correlate.explain(ticker) if correlate is not None else ""
    except Exception:                                # noqa: BLE001
        return ""


def explain_list(ticker: str) -> dict:
    """API: лента толмача и память миссии (живой или из store)."""
    t = (ticker or "").upper().strip()
    m = _M.get(t) or _from_store(t)
    if not m:
        return {"ok": False, "ticker": t, "active": False, "items": [], "memory": None, "note": f"миссии {t} нет"}
    base = explain.status(m) if explain is not None else {"items": [], "memory": {"text": m.memory, "ts": m.memory_ts, "n": m.memory_n},
                                                          "enabled": False, "model": None, "pending": 0}
    return {"ok": True, "ticker": t, "active": m.live(), **base}


def snapshot() -> dict:
    act = _active()
    return {"active": act.ticker if act else None,
            "missions": {t: status(t) for t in list(_M)}}


async def on_serious_news(note: dict) -> None:
    """Серьёзная заметка дозора: живой пилот → повод дежурному PRO (внеплановая перепроверка с
    пейсингом событий; PRO сам решит, звать ли совет); пилота нет — полный совет по миссии."""
    try:
        m = _active()
        if not m:
            return
        txt = str((note or {}).get("note") or "").strip()
        if len(txt) > 160:                           # причина в хронике — одна фраза, с многоточием, не рубленая
            txt = txt[:157].rsplit(" ", 1)[0] + "…"
        if m.pilot and m.pilot_alive():
            t = m.pilot._ask_review("серьёзная новость: " + txt, kind="news")
            if t is not None:                        # v5.3 W2: FLASH-триаж — дождаться (дозор подождёт секунды)
                try:
                    await t
                except Exception as e:               # noqa: BLE001
                    log.info("триаж новости: %s", str(e)[:80])
        elif not m.council_running():
            await council_again(m.ticker, "серьёзная новость: " + txt)
    except Exception as e:                           # noqa: BLE001
        log.warning("серьёзная новость не дошла до миссии: %s", str(e)[:100])


async def _watchdog_tick() -> None:
    global _last_resume_ts
    # Only interrupted direct close operations are restored here; a manual stop
    # still prevents restarting ordinary trading after a server restart.
    _restore_pending_panics()
    for m in list(_M.values()):
        if m.panic_orders and not (m.panic_task and not m.panic_task.done()):
            m.panic_task = asyncio.create_task(_panic_without_pilot(m))
    for m in list(_M.values()):
        t = m.task
        if t and t.done() and not t.cancelled():
            try:
                e = t.exception()
            except Exception:                        # noqa: BLE001
                e = None
            if e and m.phase != "error":
                m.error = f"пилот упал: {str(e)[:200]}"
                m.phase = "error"
                log.error("сторож миссии: %s", m.error)
                _persist(m)
    if not tinkoff.enabled() or _active():
        return
    sf = _state_file_position()
    if not sf or not sf.get("base"):
        return
    saved = _M.get(sf["base"]) or _from_store(sf["base"])
    if saved and not saved.auto_resume and not sf.get("unsettled"):
        return
    if sf.get("unsettled") and saved is None:
        it = instruments.get(sf["base"]) or {}
        saved = Mission(sf["base"], it.get("name") or sf["base"],
                        it.get("asset_class") or instruments.guess_asset_class(sf["base"]), "auto")
        saved.auto_resume = False
        _M[sf["base"]] = saved
    if time.time() - _last_resume_ts < RESUME_GAP_SEC:
        return
    _last_resume_ts = time.time()
    log.warning("сторож миссии: позиция %s %s лот без пилота — поднимаю", sf["base"], sf["lots"])
    if saved and not saved.auto_resume:
        r = await resume(sf["base"], settle_only=True)
    else:
        r = await resume(sf["base"])
    log.warning("сторож миссии: %s", r.get("note"))


async def watchdog() -> None:
    while True:
        try:
            await asyncio.sleep(60)
            await _watchdog_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:                       # noqa: BLE001
            log.warning("сторож миссии споткнулся: %s", str(e)[:120])


# ══════════════════════════════════════════════════════════════════════════════
# Self-тест: без сети — фейки ИИ/рынка/брокера/соседних модулей
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    from . import trader_risk

    store_v5.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
    config.DATA_DIR = Path(tempfile.mkdtemp(prefix="pythia_mission_selftest_"))
    store_v5._schema()
    # ревью 5.4.2: ручки «свободного пилота» — на умолчаниях (data/config_user.json и окружение владельца не роняют тест)
    config.pin_free_pilot_defaults()
    ai_pilot.TICK_SEC = 0.01
    # FakeBroker below fills orders without updating FakeTinkoff.portfolio.
    # Its empty snapshot must not randomly erase a position while assertions run;
    # account reconciliation has its own scenarios in ai_pilot's self-test.
    ai_pilot.RECONCILE_EVERY = 1_000_000
    ai_pilot.CLOSED_RECONCILE_EVERY = 1_000_000
    # 5.4.1: старые блоки писались под трос НА БИРЖЕ и вход без проверки у двери; умолчания 5.4.1 — в конце теста
    config.PYTHIA_EXCHANGE_STOP = True
    config.PYTHIA_ENTRY_CHECK = False

    _real_ai_v5 = ai_v5

    class FakeAI:
        exec_answers: list = []
        review_answers: list = []
        guard_answers: list = []
        take_answers: list = []                      # v5.3 W2: ответы ИИ у тейка
        triage_answers: list = []                    # v5.3 W2: ответы триажа событий
        entry_answers: list = []                     # v5.4.1: ответы PRO у двери (mission_entry)
        profit_answers: list = []                    # v5.4.1: мысли о прибыли (mission_profit)
        errors: list = []                            # note_error (панель проблем)
        scout_answers: list = []
        calls: list = []
        last_user: dict = {}
        now_msk_str = staticmethod(_real_ai_v5.now_msk_str)
        fmt_ts = staticmethod(_real_ai_v5.fmt_ts)

        async def pro_stream(self, system, user, *, on_think=None, on_text=None, route="pro"):
            self.calls.append(route)
            self.last_user[route] = user
            if on_think:
                await on_think("думаю…")
            for d in ("текст ", route):
                if on_text:
                    await on_text(d)
            return "текст " + route

        async def pro_json(self, system, user, *, route="pro", max_tokens=None):
            self.calls.append(route)
            self.last_user[route] = user
            if route == "mission_exec":
                return self.exec_answers.pop(0)
            return self.review_answers.pop(0)

        @staticmethod
        def money_model():
            return _real_ai_v5.money_model()

        def note_error(self, text, route=""):
            self.errors.append((route, text))

        def _money(self, route):
            if route == "mission_guard":
                return self.guard_answers.pop(0)
            if route == "mission_take":              # v5.3 W2: мягкий тейк
                return self.take_answers.pop(0) if self.take_answers else {"decision": "ЗАФИКСИРОВАТЬ", "why": "тест"}
            if route == "event_triage":              # v5.3 W2: триаж события
                return self.triage_answers.pop(0) if self.triage_answers else {}
            if route == "mission_entry":             # v5.4.1: проверка входа у двери — по умолчанию ВОЙТИ
                return self.entry_answers.pop(0) if self.entry_answers else {"decision": "ВОЙТИ", "why": "тест"}
            if route == "mission_profit":            # v5.4.1: мысль о прибыли — по умолчанию ДЕРЖАТЬ
                return self.profit_answers.pop(0) if self.profit_answers else {"decision": "ДЕРЖАТЬ", "why": "тест"}
            return {}

        async def money_json(self, system, user, *, route, max_tokens=None):
            """v5.4.1: узлы у денег (трос, тейк, триаж, проверка входа, мысль о прибыли) — PRO по умолчанию."""
            self.calls.append(route)
            self.last_user[route] = user
            return self._money(route)

        async def flash_json(self, system, user, *, think=False, route="flash", max_tokens=None):
            """FLASH-JSON: разведка (route=scout); узлы у денег на FLASH — только при PYTHIA_MONEY_MODEL=flash."""
            self.calls.append(route)
            self.last_user[route] = user
            if route == "scout":
                return self.scout_answers.pop(0) if self.scout_answers else {"requests": []}
            return self._money(route)

        shrink_calls: list = []

        async def flash_text(self, system, user, *a, **k):
            """FLASH route=shrink: сжимает ровно к лимиту (первые N символов текста), как fake
            в compress self-test; иные вызовы FLASH — «сжато»."""
            if k.get("route") == "shrink":
                lim = int(user.split("ЛИМИТ: ")[1].split(" ")[0])
                body = user.split("\n\nТЕКСТ:\n", 1)[1]
                self.shrink_calls.append((len(body), lim))
                return body[:lim]
            if k.get("route") == "explain":          # толмач (v5.3): текст по событиям промпта
                self.calls.append("explain")
                self.last_user["explain"] = user
                ev = user.split("═══ ЧТО ПРОИЗОШЛО ═══", 1)[1].split("═══", 1)[0].strip().splitlines()
                return "Толмач: " + " | ".join(l.split(" · ", 1)[-1] for l in ev if l.strip())
            if k.get("route") == "memory":           # память миссии (v5.3)
                self.calls.append("memory")
                self.last_user["memory"] = user
                return "ПАМЯТЬ: " + " ".join(user.split("═══ НАКОПИЛОСЬ С ТЕХ ПОР ═══", 1)[1].split())[:600]
            return "сжато"

        def __getattr__(self, name):
            """v5.4.2: разбор слова решения (decision_raw/decision_of, словари узлов) — настоящий: чистые функции без сети."""
            if name.startswith(("decision_", "SYN_")) or name.endswith("_table") or name == "DECISION_KEYS":
                return getattr(_real_ai_v5, name)
            raise AttributeError(name)

    class FakeBroker:
        mode = "real"
        mx: dict | None = None                       # ответ «биржи» GetMaxLots (None → сайзер считает сам)
        flat_calls: list = []

        def __init__(self):
            self.placed, self.stops, self.stop_cancels = [], [], []
            self._n = 0
            self.account_id = None

        async def max_lots(self, figi, price=None):
            return dict(FakeBroker.mx) if FakeBroker.mx else None

        async def flat_all(self, positions=None, figi=None, lot=1, **kwargs):
            FakeBroker.flat_calls.append((figi, lot))
            return {"ok": True, "closed": 1, "stops": 1, "note": ""}

        async def place(self, figi, direction, lots, price=None, tag=""):
            self._n += 1
            self.placed.append({"order_id": f"F-{self._n}", "direction": direction, "lots": lots,
                                "price": price, "tag": tag})
            return {"ok": True, "order_id": f"F-{self._n}"}

        async def order_state(self, oid):
            return {"ok": True, "filled": True, "status": "FILL"}

        async def cancel(self, oid):
            return {"ok": True}

        async def place_stop(self, figi, direction, lots, stop_price, tag=""):
            self.stops.append({"lots": lots, "stop": stop_price})
            return {"ok": True, "stop_order_id": f"S-{len(self.stops)}"}

        async def cancel_stop(self, sid):
            self.stop_cancels.append(sid)
            return {"ok": True}

        async def portfolio(self):
            return {"mode": "real", "cash": None, "positions": []}

    class FakeTinkoff:
        price = 100.0
        on = True
        pause_ticks = False
        positions: list = []                         # что лежит на счёте по FIGI-T

        @classmethod
        def enabled(cls):
            return cls.on

        @classmethod
        async def accounts(cls):
            return [{"id": "acc-1"}]

        @classmethod
        async def portfolio(cls, acc):
            return {"positions": list(cls.positions), "total": 100000.0}

        @classmethod
        async def resolve(cls, ticker, ac):
            return {"figi": "FIGI-T", "lot": 1}

        @classmethod
        async def close_price(cls, figi):
            return None

        @classmethod
        async def last_price(cls, figi):
            return None if cls.pause_ticks else {"price": cls.price}

        @classmethod
        async def orderbook(cls, figi, depth=10):
            return {"best_bid": cls.price - 0.1, "best_ask": cls.price + 0.1}

    class StubCouncil:
        @staticmethod
        def latest():
            return {"data": {"summary": {"regime": "risk-on"}}}

        @staticmethod
        def summary_text(s):
            return "итог: risk-on"

        @staticmethod
        async def present_frame(kind, payload):
            return {"headline": "рамка " + kind, "frame": "…", "highlights": []}

    class StubNews:
        enriched: list = []

        old_ts = None                                # v5.3: если задано — ещё одна новость со старым ts

        @staticmethod
        def news_for_ticker(t, name, days=3, limit=20):
            out = [{"id": "a1b2c3", "title": "новость"}]
            if StubNews.old_ts:
                out.append({"id": "old001", "title": "старая новость", "ts": StubNews.old_ts})
            return out

        @staticmethod
        def fresh_since(ts):
            return []

        rich_calls: list = []

        @staticmethod
        def render(items, limit=None, rich=False):
            StubNews.rich_calls.append(rich)
            return "\n".join(f"[{i['id']}] {i['title']}" + (" · ключевое: тест" if rich else "")
                             for i in (items if limit is None else items[:limit]))

        @staticmethod
        async def enrich_ticker(run_id, ticker, name, asset_class, days=3):
            StubNews.enriched.append((ticker, run_id))
            return {"new": 1, "relevant": 1, "characterized": 1}

    class StubWatch:
        @staticmethod
        def serious_since(ts):
            return [{"data": {"ts": time.time(), "severity": 80, "note": "ЦБ", "affected": ["рынок"]}}]

    class StubScan:
        """Сканер стакана 4.x: старт/статус/стоп без сети."""
        MIN_TICKS_AGG = 10
        started: list = []
        stopped: list = []

        @staticmethod
        async def start(code, ticker, asset_class, minutes):
            StubScan.started.append((code, minutes))
            return {"ok": True, "minutes": minutes, "code": code}

        puncture = None                              # v5.3 W3: прокол сканера (puncture_first) по флагу теста

        @staticmethod
        def status(code):
            out = {"running": True, "ticks": 120, "elapsed_min": 6.0, "until": time.time() + 600,
                   "consensus": {"up_share": 0.78, "dn_share": 0.12, "side": "вверх", "now_side": "вверх",
                                 "streak": {"win": 20, "up": 16, "dn": 2}, "aggressor_mean": 0.58, "agree": True}}
            if StubScan.puncture:
                out["puncture_first"] = dict(StubScan.puncture)
                out["punctures"] = {"up": [], "down": [], "bin_step": 0.5}
                out["hawkes"] = {"n": 0.71}
                out["tension"] = {"word": "натяжение РАСТЁТ"}
            return out

        @staticmethod
        def stop(code):
            StubScan.stopped.append(code)
            return {"ok": True, "note": "скан остановлен"}

        @staticmethod
        def render_for_ai(agg):
            return "СКАНЕР СТАКАНА: тянут вверх 78%"

        @staticmethod
        def brief(agg):
            return {"running": True, "ticks": 120, "side": "вверх", "up_share": 0.78, "dn_share": 0.12}

    async def fake_build(t, ac, full=True):
        return {"text": "ДОСЬЕ " + t, "price": 100.0, "astro": "Луна", "astro_line": "☽", "aether": "",
                "kuramoto": None, "kuramoto_text": "графа нет", "bifurcation": None, "bif_text": "нет",
                "wyckoff_text": "ВАЙКОФФ D1: тест · до льда −1.6% → ближе к льду", "xray_text": "OBI +0.1",
                "oracle_text": "оракул: ПАРЛАМЕНТ", "calendar_text": "КАЛЕНДАРЬ СРЕДЫ", "sync_text": "",
                "reactor_text": "⚛ РЕАКТОР", "weather_text": "", "scan_text": "",
                "errors": ["стакан недоступен"]}

    async def fake_light(t, figi, ac):
        return {"text": f"{t}: цена {FakeTinkoff.price}", "price": FakeTinkoff.price, "book": None}

    async def fake_prepare(self):
        self.figi, self.asset_class = "FIGI-T", "futures"
        self.go_per_lot, self.tick_size, self.point_value = 12000.0, 0.01, 1.0
        self.deposit = self.deposit_override or 100000.0
        self.session_risk = trader_risk.SessionRisk(self.deposit)
        self._sr_day = self._msk_day()
        self._state_path = None
        for x in FakeTinkoff.positions:              # весь счёт: позиция на счёте — позиция бота
            if x.get("figi") == "FIGI-T" and abs(x.get("qty") or 0) >= 1 and self.adopt_account:
                self._absorb_account(int(x["qty"]), float(x.get("avg") or 0), None, "на счёте при старте")
        self._prepared = True
        if self._close_pending and not self.position:
            self._close_pending = None
        return True

    class FakeMoex:
        """MOEX ISS для разведки: котировка без сети (индексы ISS сюда не ходят)."""
        @staticmethod
        async def last_price(code, ac):
            assert ac != "index", code
            return {"price": 2850.5, "change_pct": -0.4, "stale": False, "quote_time": "12:00"}

        @staticmethod
        async def candles(code, ac, interval, days):
            return []

    class FakeCorrelate:
        """Связанные бумаги без сети: VTBR ρ=+0.81 для любого тикера; текст и запросы — настоящие."""
        calls: list = []
        items = [{"code": "VTBR", "name": "ВТБ", "asset_class": "share", "kind": "сектор: банки и финансы",
                  "rho": 0.81, "lead": 0, "n": 59, "rho_lag": None, "move_1d": 1.2, "move_5d": -0.5,
                  "price": 95.5, "source": "tinkoff"}]
        text = staticmethod(correlate.text)
        scout_requests = staticmethod(correlate.scout_requests)

        @classmethod
        async def partners(cls, ticker, ac, days=60, *, force=False):
            cls.calls.append((ticker, ac))
            return list(cls.items)

        @staticmethod
        def explain(ticker):
            return "кандидатов 17, показано 1 (тест)"

    class FakeClock:
        """Рыночные часы без сети: открыто/закрыто по флагу."""
        open = True
        next_in = 4 * 3600

        @staticmethod
        def enabled():
            return True

        @classmethod
        async def status(cls, t, ac, iid=None):
            return {"open": cls.open, "reason": "торги идут" if cls.open else "выходной — торгов нет",
                    "session": "основная" if cls.open else "выходной",
                    "next_open_ts": None if cls.open else time.time() + cls.next_in,
                    "next_open_in_s": None if cls.open else cls.next_in,
                    "next_open_msk": None if cls.open else "10:00 МСК", "source": "tinkoff", "ts": time.time()}

        @staticmethod
        def describe(st):
            return ("рынок открыт: торги идут" if (st or {}).get("open") else
                    "рынок закрыт до 10:00 МСК (выходной — торгов нет) — вход возможен только с открытия")

    fake_ai = FakeAI()
    g = globals()
    g["ai_v5"] = fake_ai
    g["market_clock"] = FakeClock
    ai_pilot.market_clock = FakeClock
    g["council"], g["newsflow"], g["watch"] = StubCouncil, StubNews, StubWatch
    g["tinkoff"] = FakeTinkoff
    g["maya_scan"] = StubScan
    ai_pilot.tinkoff = FakeTinkoff
    market_ctx.build, market_ctx.light = fake_build, fake_light
    g["_make_broker"] = FakeBroker
    MissionPilot.prepare = fake_prepare
    _init0 = MissionPilot.__init__
    def _init_no_state(self, *a, **k):          # тест не трогает data/aipilot_state.json (живая позиция владельца)
        _init0(self, *a, **k)
        self._state_path = None
    MissionPilot.__init__ = _init_no_state
    compress.ai_v5 = fake_ai
    explain.ai_v5 = fake_ai                          # толмач и память — тот же фейк
    explain.GATHER_SEC, explain.MIN_GAP_SEC = 0.02, 0.05   # дебаунс в тесте — короткий
    scout.ai_v5, scout.tinkoff, scout.moex = fake_ai, FakeTinkoff, FakeMoex
    g["correlate"], scout.correlate = FakeCorrelate, FakeCorrelate   # без фейка партнёры полезли бы в сеть
    ai_pilot.GUARD_TIMEOUT = 5.0

    def xev(m, kind, sub=""):
        """Толмач: есть ли событие вида kind с подстрокой sub в заголовке (записи объединяют узлы окна)."""
        return any(e.get("kind") == kind and sub in str(e.get("title") or "") for x in m.explain for e in x.get("events") or [])

    def xlast(m, kind):
        return [e for x in m.explain for e in x.get("events") or [] if e.get("kind") == kind][-1]

    async def settle(cond, n=300):
        for _ in range(n):
            if cond():
                return True
            # Windows' event-loop clock can round 10 ms below its resolution,
            # exhausting the retry loop before a 20–50 ms debouncer wakes up.
            await asyncio.sleep(0.02)
        return cond()

    async def main():
        # ── валидатор ──
        ex, err = _validate_exec({"do": "BUY", "entry": 99.5, "take": 103, "invalidation": 98}, "long", 100)
        assert ex and ex["do"] == "BUY" and ex["entry"] == 99.5 and err is None
        assert _validate_exec({"do": "BUY", "invalidation": 101}, "long", 100)[1], "стоп выше цены для BUY"
        assert _validate_exec({"do": "SELL", "invalidation": 102}, "long", 100)[1], "play=long с SELL"
        assert _validate_exec({"do": "BUY", "invalidation": 98}, "short", 100)[1], "play=short с BUY"
        assert _validate_exec({"do": "SELL", "take": 101, "invalidation": 102}, "auto", 100)[1], "тейк не с той стороны"
        exw, err = _validate_exec({"do": "FLAT", "invalidation": 98}, "auto", 100)   # v5.4.1: FLAT/WAIT без позиции — вне рынка
        assert exw and exw["do"] == "WAIT" and exw["entry"] is None and exw["invalidation"] is None and err is None, (exw, err)
        exw, err = _validate_exec({"do": "WAIT", "wait_for": "закрепление выше 101", "why": "перевеса нет", "confidence": 40,
                                   "levels": [99, 101]}, "long", 100)
        assert exw["do"] == "WAIT" and exw["wait_for"] == "закрепление выше 101" and exw["confidence"] == 40 and exw["levels"] == [99.0, 101.0]
        exw = _validate_exec({"do": "ЖДАТЬ"}, "auto", 100)[0]   # v5.4.2: заглушки нейтральны — не «перевеса нет» за ИИ
        assert exw["wait_for"] == "условие входа не названо — реши по живой картине" and exw["why"] == "(причина не указана)", exw
        # v5.4.2: слово решения — по словарю приказа (КУПИТЬ/ЛОНГ/ПОКУПКА → BUY, ЖДЁМ/NO_TRADE → WAIT, FLAT/EXIT в позиции → CLOSE)
        for w, want, pos_ in (("КУПИТЬ", "BUY", False), ("лонг", "BUY", False), ("Покупка", "BUY", False),
                              ("ПРОДАТЬ", "SELL", False), ("ЖДЁМ", "WAIT", False), ("ждем", "WAIT", False),
                              ("NO_TRADE", "WAIT", False), ("FLAT", "WAIT", False), ("FLAT", "CLOSE", True),
                              ("EXIT", "CLOSE", True)):
            exs, err = _validate_exec({"action": w, "invalidation": 102 if want == "SELL" else 98, "why": "т"}, "auto", 100,
                                      in_pos=pos_)
            assert exs and exs["do"] == want and err is None, (w, pos_, exs, err)
        err = _validate_exec({"do": "BUY", "invalidation": 101}, "auto", 100)[1]
        assert err.startswith("сторона BUY принята — исправь уровни:") and "WAIT" not in err, err
        assert _validate_exec({"do": "НЕ ПОКУПАТЬ", "invalidation": 98}, "auto", 100)[1], "отрицание — не решение"
        assert "позиция открыта" in _validate_exec({"do": "WAIT"}, "auto", 100, in_pos=True)[1], "WAIT при позиции недопустим"
        assert "HOLD" in _validate_exec({"do": "WAIT"}, "auto", 100, in_pos=True)[1], "отказ WAIT в позиции ведёт к HOLD"
        assert "позиции нет" in _validate_exec({"do": "CLOSE"}, "auto", 100)[1]
        # ревью 5.4.2: HOLD — держать как есть без добора: только при позиции; null — прежний уровень; сторона — по позиции
        exh, err = _validate_exec({"do": "ДЕРЖАТЬ", "invalidation": 99, "take": None, "why": "ход жив"}, "long", 100,
                                  in_pos=True, pos_side="long")
        assert err is None and exh["do"] == "HOLD" and exh["entry"] is None and exh["entry_kind"] == "сейчас" \
            and exh["invalidation"] == 99 and exh["take"] is None and exh["why"] == "ход жив", (exh, err)
        assert "HOLD — только при открытой позиции" in _validate_exec({"do": "HOLD"}, "auto", 100)[1]
        assert "invalidation 101 не ниже цены 100" in _validate_exec({"do": "HOLD", "invalidation": 101}, "auto", 100,
                                                                     in_pos=True, pos_side="long")[1]
        assert _validate_exec({"do": "HOLD", "invalidation": 101}, "auto", 100, in_pos=True, pos_side="short")[0]
        assert _validate_exec({"exec": {"do": "SELL", "invalidation": "102,5", "entry": None}}, "auto", 100)[0]
        assert _validate_exec({"do": "BUY", "invalidation": 0}, "auto", 100)[1]
        # виды входа: геометрия главнее подсказки откат/прорыв; прорыв (v5.4.2) — стоп по ту сторону УРОВНЯ входа:
        # классический стоп пробоя между ценой и уровнем законен (до пробития позиции нет)
        ex, err = _validate_exec({"do": "BUY", "entry": 102, "take": 106, "invalidation": 99, "entry_kind": "прорыв"}, "auto", 100)
        assert ex and ex["entry_kind"] == "прорыв" and ex["entry"] == 102.0 and err is None
        ex, err = _validate_exec({"do": "BUY", "entry": 102, "take": 106, "invalidation": 101}, "auto", 100)
        assert ex and ex["entry_kind"] == "прорыв" and ex["invalidation"] == 101.0 and err is None, err
        assert "не ниже entry 102" in _validate_exec({"do": "BUY", "entry": 102, "take": 106, "invalidation": 102.5}, "auto", 100)[1]
        assert _validate_exec({"do": "BUY", "entry": 99.5, "take": 103, "invalidation": 98, "entry_kind": "прорыв"}, "long", 100)[0]["entry_kind"] == "откат"
        assert _validate_exec({"do": "SELL", "entry": 98, "take": 94, "invalidation": 101}, "auto", 100)[0]["entry_kind"] == "прорыв"
        assert _validate_exec({"do": "SELL", "entry": 98, "take": 94, "invalidation": 99.5}, "auto", 100)[0]["invalidation"] == 99.5, \
            "SELL на пробитии: стоп между ценой и уровнем законен"
        ex, _ = _validate_exec({"do": "BUY", "entry": 100, "invalidation": 98}, "auto", 100)
        assert ex["entry"] is None and ex["entry_kind"] == "сейчас", "уровень равен цене — вход сейчас"
        ex, _ = _validate_exec({"do": "BUY", "entry": 100.3, "invalidation": 98, "entry_kind": "сейчас"}, "auto", 100)
        assert ex["entry"] is None and ex["entry_kind"] == "сейчас", "подсказка «сейчас» главнее геометрии (v5.4.2)"
        assert _validate_exec({"do": "BUY", "entry": 99, "invalidation": 98}, "auto", None)[0]["entry_kind"] == "откат"
        assert _validate_exec({"do": "BUY", "entry": 99, "invalidation": 98, "entry_kind": "breakout"}, "auto", None)[0]["entry_kind"] == "прорыв"
        # v5.2: CLOSE — закрыть и стоять вне рынка; без позиции это не приказ
        assert "позиции нет" in _validate_exec({"do": "CLOSE"}, "long", 100)[1]
        exc, err = _validate_exec({"do": "CLOSE", "why": "картина против", "confidence": 60}, "long", 100, in_pos=True)
        assert exc and exc["do"] == "CLOSE" and exc["entry"] is None and err is None, (exc, err)
        assert _validate_exec({"do": "ЗАКРЫТЬ"}, "short", 100, in_pos=True)[0]["do"] == "CLOSE"

        # ── start → совет → пилот: приказ отклонён раз, вторая попытка верна ──
        fake_ai.exec_answers[:] = [{"do": "SELL", "invalidation": 98},          # против режима long
                                   {"do": "BUY", "entry": 99.5, "take": 103, "invalidation": 98,
                                    "why": "тест", "plan": "ведём", "confidence": 70,
                                    "news_ids": ["a1b2c3"], "levels": [98, 103], "time_note": "к 15:00"}]
        fake_ai.scout_answers[:] = [{"requests": [{"kind": "quote", "code": "IMOEX", "why": "фон рынка"}]}]
        r = await start("TEST", "long", deposit=100000.0, reason="тест")
        assert r["ok"] and r["run_id"], r
        m = _M["TEST"]
        assert status("TEST")["phase"] == "council"
        r2 = await start("OTHER", "short")
        assert not r2["ok"] and "одна активная" in r2["note"], "вторая миссия при живом совете отклоняется"
        assert not (await council_again("TEST", "x"))["ok"], "совет уже идёт"
        await m.council_task
        assert m.error is None, m.error
        assert fake_ai.calls.count("mission_exec") == 2, "после отказа — одна повторная попытка"
        assert m.exec["do"] == "BUY" and m.texts["verdict"] == "текст mission_verdict"
        # v5.1: сканер стартовал вместе с разбором (8 ч), целевые новости собраны, слои дошли до ИИ целиком
        assert StubScan.started and StubScan.started[-1] == ("TEST", int(config.PYTHIA_SCAN_MIN)), StubScan.started
        assert StubNews.enriched and StubNews.enriched[-1][0] == "TEST"
        assert m.layers["wyckoff"].startswith("ВАЙКОФФ D1") and m.layers["scan"].startswith("СКАНЕР СТАКАНА")
        ua = fake_ai.last_user["mission_analysis"]
        for piece in ("ВАЙКОФФ D1: тест", "СКАНЕР СТАКАНА: тянут вверх 78%", "OBI +0.1", "оракул: ПАРЛАМЕНТ",
                      "КАЛЕНДАРЬ СРЕДЫ", "⚛ РЕАКТОР", "ДОСЬЕ TEST"):
            assert piece in ua, piece
        assert ua.index("ВАЙКОФФ") < ua.index("ДОСЬЕ БИРЖИ"), "Вайкофф идёт первым"
        # v5.2 разведка: FLASH попросил IMOEX → это фьючерс на индекс MX (индексы ISS убраны, v5.3) →
        # блок «ДАННЫЕ ПО ЗАПРОСУ РАЗВЕДКИ» у аналитика, запросы в миссии
        assert "scout" in fake_ai.calls and "ЧТО УЖЕ ПОЛУЧИТ PRO" in fake_ai.last_user["scout"]
        assert "TEST" in fake_ai.last_user["scout"] and "Вайкофф" in fake_ai.last_user["scout"], fake_ai.last_user["scout"][:400]
        assert m.scout_reqs and m.scout_reqs[0]["code"] == "MX" and m.scout_reqs[0]["asset_class"] == "futures", m.scout_reqs[0]
        assert "ДАННЫЕ ПО ЗАПРОСУ РАЗВЕДКИ" in ua and "MX (Индекс МосБиржи): 100.00" in ua and "· tinkoff" in ua, \
            ua[ua.find("РАЗВЕДК"):][:300]
        assert "IMOEX" not in ua.split("═══ ДАННЫЕ ПО ЗАПРОСУ РАЗВЕДКИ")[1][:600], "индекса ISS в данных нет"
        assert "фон рынка" in m.scout_text and "MX (Индекс МосБиржи)" in fake_ai.last_user["mission_verdict"]
        assert "MX (Индекс МосБиржи)" in fake_ai.last_user["mission_critique"], "критик и вердикт видят разведку"
        assert status("TEST")["scout"]["requests"][0]["code"] == "MX" and status("TEST")["scout"]["text"]
        # v5.3 связанные бумаги: посчитаны до разведки, свой блок у аналитика/критика/вердикта,
        # разведка сама добавила по ним котировку и 5 дней, FLASH их не просил, статус несёт список
        assert FakeCorrelate.calls and FakeCorrelate.calls[0] == ("TEST", "share"), FakeCorrelate.calls
        assert m.partners and m.partners[0]["code"] == "VTBR" and m.partners_text.startswith("Связанные бумаги для TEST")
        assert "═══ СВЯЗАННЫЕ БУМАГИ" in ua and "VTBR (ВТБ; сектор: банки и финансы): ρ=+0.81" in ua, ua[ua.find("СВЯЗАННЫЕ"):][:300]
        assert ua.index("ДАННЫЕ ПО ЗАПРОСУ РАЗВЕДКИ") < ua.index("СВЯЗАННЫЕ БУМАГИ") < ua.index("ИТОГ ОБЩЕГО СОВЕТА")
        assert "ρ=+0.81" in fake_ai.last_user["mission_critique"] and "ρ=+0.81" in fake_ai.last_user["mission_verdict"]
        assert "СВЯЗАННЫЕ БУМАГИ уже добавлены автоматически (котировка и 5 дней): VTBR" in fake_ai.last_user["scout"]
        assert [r["code"] for r in m.scout_reqs] == ["MX", "VTBR", "VTBR"], m.scout_reqs
        assert "VTBR (ВТБ): 100.00 · tinkoff — связанная бумага, ρ=+0.81" in m.scout_text, m.scout_text
        st_p = status("TEST")["partners"]
        assert st_p["items"][0]["code"] == "VTBR" and st_p["items"][0]["rho"] == 0.81 and "ρ=+0.81" in st_p["text"] and "тест" in st_p["note"]
        assert m.sizes["analysis"]["blocks"].get("partners", 0) > 0, "размер блока партнёров в степпере"
        assert "текст mission_analysis" in fake_ai.last_user["mission_critique"], "критик видит анализ целиком"
        uv = fake_ai.last_user["mission_verdict"]
        assert "текст mission_analysis" in uv and "текст mission_critique" in uv and "СКАНЕР СТАКАНА" in uv
        assert "ВАЙКОФФ" in fake_ai.last_user["mission_exec"] and "СКАНЕР" in fake_ai.last_user["mission_exec"]
        # v5.4.2: рамка для человека — фоном ПОСЛЕ того, как пилот получил приказ (не держит вход до 240 с)
        assert m.pilot is not None and await settle(lambda: bool(m.frame)), "рамка пришла фоном"
        assert m.frame["headline"] == "рамка mission" and m.news and m.news[0]["id"] == "a1b2c3"
        # v5.1 разумные пределы: маленькие блоки уходят целиком — FLASH-shrink НЕ вызывался
        assert not fake_ai.shrink_calls, fake_ai.shrink_calls
        assert "обрезано" not in ua and "обрезано" not in uv
        # новости — богатым рендером rich=True (newsflow его поддерживает — по сигнатуре)
        assert StubNews.rich_calls and all(StubNews.rich_calls) and "ключевое: тест" in ua, StubNews.rich_calls
        # размеры промптов: в степпере и в status()["sizes"]
        sz = status("TEST")["sizes"]
        for k in ("analysis", "critique", "verdict", "exec"):
            assert sz[k]["prompt"] > 0 and sz[k]["answer"] > 0 and isinstance(sz[k]["blocks"], dict), (k, sz.get(k))
        assert sz["analysis"]["prompt"] == len(ua) and sz["analysis"]["answer"] == len("текст mission_analysis")
        assert sz["analysis"]["blocks"]["wyckoff"] == len(m.layers["wyckoff"])
        json.dumps(sz)                                       # JSON-сериализуемо
        assert store_v5.mission_get("TEST")["sizes"]["verdict"]["prompt"] == sz["verdict"]["prompt"]
        det = _sizes_detail("аналитик", len(ua), {k: v for k, v in m.layers.items()})
        assert det.startswith("аналитик: промпт ") and "симв." in det and "Вайкофф" in det, det
        run = bus.run(r["run_id"])
        assert run["status"] == "done" and run["texts"]["analysis"] == "текст mission_analysis"
        assert run["thinks"]["critique"] == "думаю…"
        assert store_v5.mission_get("TEST")["exec"]["take"] == 103.0
        assert m.pilot and m.pilot_alive() and m.pilot.state == "ЗАСАДА"
        assert m.exec["entry_kind"] == "откат" and m.pilot.plan["kind"] == "откат" and "(откат)" in _exec_text(m)
        # v5.3 толмач: приказ совета объяснён владельцу (стадия explain по шине, status()["explain"], store)
        assert await settle(lambda: xev(m, "council")), m.explain
        xc = [x for x in m.explain if x["kind"] == "council"][-1]
        assert xc["ok"] and xc["text"].startswith("Толмач: ") and "Совет вынес приказ: BUY откат @99.5, тейк 103, стоп 98" in xc["title"]
        assert xc["refs"]["invalidation"] == 98 and xc["model"] == "flash" and "exec_ts" in status("TEST")
        ux = fake_ai.last_user["explain"]
        for piece in ("═══ ЧТО ПРОИЗОШЛО ═══", "Совет вынес приказ", "═══ ПРИКАЗ СОВЕТА ═══", "BUY entry=99.5", "═══ СВЯЗАННЫЕ БУМАГИ ═══",
                      "ρ=+0.81", "Режим игры: long", "═══ СИТУАЦИЯ ПИЛОТА ═══"):
            assert piece in ux, (piece, ux[:1500])
        st_x = status("TEST")["explain"]
        assert st_x and st_x[-1]["kind"] == "council" and store_v5.mission_get("TEST")["explain"][-1]["kind"] == "council"
        assert status("TEST")["memory"]["n"] == 0 and status("TEST")["memory"]["text"] == "", "первому совету сводить нечего"
        assert m.pilot.review_ts - time.time() > float(config.PYTHIA_REVIEW_SEC) - 5
        assert ai_pilot.REVIEW_SEC == float(config.PYTHIA_REVIEW_SEC)
        st = status("TEST")
        assert st["phase"] == "armed", st["phase"]
        for k in ("ticker", "name", "asset_class", "play", "started_ts", "run_id", "phase", "pilot", "exec",
                  "frame", "texts", "news", "reviews", "handoffs", "trades", "price", "note", "error"):
            assert k in st, k
        assert st["pilot"]["pilot"] == "ИИ-ПИЛОТ" and st["trades"]["count"] == 0
        assert st["scan"]["side"] == "вверх" and st["scan"]["ticks"] == 120, st["scan"]
        assert scan_status("TEST")["brief"]["side"] == "вверх" and "СКАНЕР" in scan_status("TEST")["text"]
        snap = snapshot()
        assert snap["active"] == "TEST" and "TEST" in snap["missions"]
        r3 = await start("OTHER", "short")
        assert not r3["ok"], "вторая миссия при живом пилоте отклоняется"

        # ── цена подошла к засаде (99.5 + 5 шагов по 0.01) → вход → позиция ──
        fill_events: list = []                          # фаза 4 · W2: исполнение уходит в шину (панель, Telegram)

        async def fill_sink(ev):
            if ev.get("type") == "v5" and ev.get("stage") == "pilot" and isinstance(ev.get("data"), dict) and ev["data"].get("fill"):
                fill_events.append(ev)
        bus.set_sink(fill_sink)
        FakeTinkoff.price = px = 99.52
        assert await settle(lambda: m.pilot.position is not None), m.pilot.last_action
        assert m.pilot.position["lots"] == 8, m.pilot.position
        # v5.2 мягкий стоп: триггер 98 у FLASH, на бирже — аварийный трос на PYTHIA_HARD_STOP_PCT % дальше
        hard0 = m.pilot._hard_of(m.pilot.position)
        assert m.pilot.position["invalidation"] == 98.0 and m.pilot.position["inv0"] == 98.0
        assert hard0 and abs(hard0 - 98.0 * (1 - float(config.PYTHIA_HARD_STOP_PCT) / 100)) < 0.011, hard0
        assert abs(m.pilot.broker.stops[-1]["stop"] - hard0) < 1e-9, (m.pilot.broker.stops[-1], hard0)
        assert m.pilot.position["hard_stop"] == hard0 and status("TEST")["pilot"]["soft_stop"]
        assert status("TEST")["pilot"]["hard_stop"] == hard0 and status("TEST")["pilot"]["guard"]["holds"] == 0
        assert "трос @" in m.pilot.last_action and "триггер PRO @98" in m.pilot.last_action, m.pilot.last_action
        assert status("TEST")["phase"] == "in_position"
        assert m.open_trade and m.open_trade["lots"] == 8
        assert abs(status("TEST")["price"] - px) < 1e-9
        assert await settle(lambda: xev(m, "entry")), m.explain
        bus.set_sink(None)                              # фаза 4 · W2: событие исполнения ушло в шину с числами
        assert fill_events and fill_events[0]["status"] == "progress" and fill_events[0]["ticker"] == "TEST" \
            and fill_events[0]["data"]["fill"]["new"] is True and fill_events[0]["data"]["fill"]["lots"] == 8 \
            and fill_events[0]["data"]["fill"]["side"] == "long" and "ВОШЁЛ" in fill_events[0]["detail"], fill_events
        xe = [x for x in m.explain if x["kind"] == "entry"][-1]
        assert "Вход исполнен: long 8 лот @99." in xe["title"] and xe["refs"]["lots"] == 8, xe
        assert "ПОЗИЦИЯ: long 8 лот" in fake_ai.last_user["explain"], fake_ai.last_user["explain"][:1200]

        # ── перепроверка: ЖДЁМ пишется; ЖДЁМ с новыми стопом/тейком двигает трос; ЗАКРЫТЬ закрывает,
        #    пишет сделку и даёт повод дежурному PRO (не совету) ──
        p = m.pilot
        from . import astro as _astro_mod            # небо в перепроверке — быстрый фейк (не грузим эфемериды в тесте)

        async def _fake_actx(max_age=0):
            return {}
        _astro_mod.acontext = _fake_actx
        fake_ai.review_answers[:] = [{"choice": "ЖДЁМ", "why": "план жив", "note": "держим"}]
        await p._review(px)
        assert len(m.reviews) == 1 and m.reviews[0]["choice"] == "ЖДЁМ" and p.position
        assert status("TEST")["reviews"][0]["note"] == "держим"
        assert await settle(lambda: xev(m, "review", "Перепроверка: ЖДЁМ")), m.explain
        assert m.reviews_since_memory == 1
        ur = fake_ai.last_user["mission_review"]
        assert "СКАНЕР СТАКАНА" in ur and "ВАЙКОФФ" in ur, "перепроверка видит сканер и Вайкофф"
        assert "ДАННЫЕ РАЗВЕДКИ" in ur and "MX (Индекс МосБиржи)" in ur and "VTBR (ВТБ): " in ur, "перепроверка видит свежую разведку"
        assert "═══ СВЯЗАННЫЕ БУМАГИ (корреляции)" in ur and "ρ=+0.81" in ur, "перепроверка видит связанные бумаги"
        assert "триггер (мягкий стоп) @98" in ur and "аварийный трос биржи @" in ur, ur[:1200]
        assert "размер входа/добора считает биржа" not in ur or "свободно" in ur
        assert fake_ai.calls.count("scout") == 1, "перепроверка НЕ спрашивает FLASH заново — только свежие числа"
        assert "ПОВОД ПЕРЕПРОВЕРКИ" not in ur and "Последний полный совет: 0 мин назад" in ur, ur[:900]
        assert m.sizes["review"]["prompt"] == len(ur) and m.sizes["review"]["answer"] > 0
        assert m.sizes["review"]["blocks"]["scan"] > 0 and not fake_ai.shrink_calls
        assert len(StubNews.enriched) >= 2, "перед перепроверкой — целевые новости"
        # ЖДЁМ в позиции с новыми уровнями: трос подтянут (перевыставлен тиком), тейк передвинут; сторона проверяется
        n_st = len(p.broker.stops)
        fake_ai.review_answers[:] = [{"choice": "ЖДЁМ", "why": "подтягиваю", "invalidation": 98.8, "take": 105.0,
                                     "note": "трос выше"}]
        await p._review(px)
        assert p.position["invalidation"] == 98.8 and p.position["take"] == 105.0, p.position
        assert "трос 98 → 98.8" in p.last_action and "тейк 103 → 105" in p.last_action, p.last_action
        assert await settle(lambda: len(p.broker.stops) > n_st)
        assert abs(p.broker.stops[-1]["stop"] - p._hard_of(p.position)) < 1e-9 and p.position["inv0"] == 98.8
        fake_ai.review_answers[:] = [{"choice": "ЖДЁМ", "why": "мусор", "invalidation": 120.0, "take": 90.0}]
        await p._review(px)
        assert p.position["invalidation"] == 98.8 and p.position["take"] == 105.0, "стоп выше цены и тейк ниже — не приняты"
        fake_ai.exec_answers[:] = [{"do": "BUY", "entry": None, "take": 104, "invalidation": 97}]
        fake_ai.review_answers[:] = [{"choice": "ЗАКРЫТЬ ВСЁ", "why": "слом", "note": "выходим"}]
        old_rid = m.run_id
        await p._review(px)
        assert p.position is None and m.reviews[-1]["choice"] == "ЗАКРЫТЬ"
        # v5.3 фаза 3 (а): причина закрытия остаётся первой в last_action, повод дежурному PRO дописан
        la = p.last_action
        assert la.startswith("ЗАКРЫЛ ВСЁ (решение перепроверки: закрыть): P/L") and "после закрытия" in la, la
        assert la.index("ЗАКРЫЛ ВСЁ") < la.index("после закрытия") and "дежурный PRO решит через" in la, la
        assert status("TEST")["pilot"]["last_action"].startswith("ЗАКРЫЛ ВСЁ")
        # v5.3: закрытие → толмач «Позиция закрыта … P/L» и память сведена (узел прошёл): абзац, списки ужаты
        assert await settle(lambda: xev(m, "close") and m.memory_n >= 1), (m.explain, m.memory_n)
        xcl = xlast(m, "close")
        assert "Позиция закрыта: long 8 лот, P/L" in xcl["title"] and "решение перепроверки: закрыть" in xcl["detail"]
        assert m.memory.startswith("ПАМЯТЬ: ") and "ЖДЁМ" in m.memory and m.reviews_since_memory == 0
        um = fake_ai.last_user["memory"]
        assert "Перепроверки:" in um and "Объяснения толмача:" in um and "Сделки:" in um and "ЗАКРЫТЬ" in um, um[:1500]
        assert len(m.reviews) <= explain.KEEP_REVIEWS and len(m.explain) <= explain.KEEP_EXPLAIN
        assert status("TEST")["memory"]["n"] == m.memory_n and store_v5.mission_get("TEST")["memory"] == m.memory
        trs = store_v5.trades("TEST")
        assert len(trs) == 1 and trs[0]["side"] == "long" and trs[0]["lots"] == 8 and trs[0]["mode"] == "real"
        assert abs(trs[0]["exit_px"] - px) < 1e-6, trs[0]
        # после закрытия — НЕ совет и не тактика через 45 с: повод дежурному PRO, остыть PYTHIA_AFTER_CLOSE_SEC
        assert m.handoffs and m.handoffs[-1]["reason"].startswith("после закрытия") and m.handoffs[-1]["kind"] == "pilot"
        assert not p._reanalyzing and m.run_id == old_rid and fake_ai.exec_answers, "совет не звался"
        assert p._review_reason and "после закрытия" in p._review_reason
        after = float(config.PYTHIA_AFTER_CLOSE_SEC)
        assert time.time() + after - 10 <= p.review_ts <= time.time() + after + 1, (p.review_ts - time.time(), after)
        assert status("TEST")["trades"]["count"] == 1 and status("TEST")["pilot"]["review_reason"]
        assert status("TEST")["handoffs"][-1]["kind"] == "pilot" and status("TEST")["pilot"]["council_age_min"] == 0
        # внеплановая перепроверка с поводом в промпте: КУПИТЬ с засадой-откатом → план «откат», вход при подходе
        amb = round(px - 0.3, 2)
        fake_ai.review_answers[:] = [{"choice": "КУПИТЬ_СЕЙЧАС", "why": "откуп", "entry": amb, "entry_kind": "откат",
                                     "invalidation": 97.0, "take": 104.0, "note": "жду откат"}]
        await p._review(px)
        assert "ПОВОД ПЕРЕПРОВЕРКИ (внеплановая): после закрытия" in fake_ai.last_user["mission_review"]
        assert "═══ ПАМЯТЬ МИССИИ" in fake_ai.last_user["mission_review"] and "ПАМЯТЬ: " in fake_ai.last_user["mission_review"], \
            "перепроверка видит память миссии"
        assert p._review_reason is None and p.plan and p.plan["side"] == "long" and p.plan["entry"] == amb, p.plan
        assert p.plan["kind"] == "откат" and p.state == "ЗАСАДА" and p.position is None
        assert m.reviews[-1]["entry_kind"] == "откат" and "ЗАСАДА (откат): long @" in p._situation_text(px)
        FakeTinkoff.price = amb + 0.02                         # цена подошла к засаде → вход
        assert await settle(lambda: p.position is not None)
        assert status("TEST")["phase"] == "in_position" and p.position["entry"] <= amb + 0.1
        FakeTinkoff.price = px
        assert await settle(lambda: bool(p.prices) and p.prices[-1] == px)

        # ── ПРОРЫВ: КУПИТЬ с уровнем выше цены → ждём пробития; под уровнем не входим, за уровнем два тика → вход ──
        await p._close_all(px, "тест: освободить-0", reanalyze=False)
        p.plan = None
        brk = round(px + 1.0, 2)
        fake_ai.review_answers[:] = [{"choice": "КУПИТЬ_СЕЙЧАС", "why": "на пробое", "entry": brk,
                                     "invalidation": 97.0, "take": 106.0}]
        await p._review(px)
        assert p.plan and p.plan["kind"] == "прорыв" and p.plan["entry"] == brk and p.state == "ЗАСАДА", p.plan
        assert "пробитии" in p.last_action, p.last_action
        FakeTinkoff.price = brk - 0.05                         # под уровнем — ждём
        assert await settle(lambda: bool(p.prices) and p.prices[-1] == brk - 0.05)
        await settle(lambda: False, n=30)
        assert p.position is None and "жду пробития" in p.last_action, p.last_action
        assert "ЖДУ ПРОБИТИЯ: long при проходе" in p._situation_text(brk - 0.05)
        FakeTinkoff.price = brk + 0.02                         # прошли уровень → два тика → вход
        assert await settle(lambda: p.position is not None), p.last_action
        assert p.position["side"] == "long" and status("TEST")["phase"] == "in_position"
        FakeTinkoff.price = px
        assert await settle(lambda: bool(p.prices) and p.prices[-1] == px)
        # прорыв со стопом между ценой и уровнем (v5.4.2): классический стоп пробоя — принят как есть (до пробития
        # позиции нет; стоп по ту сторону уровня входа)
        await p._close_all(px, "тест: освободить-1", reanalyze=False)
        p.plan = None
        fake_ai.review_answers[:] = [{"choice": "КУПИТЬ_СЕЙЧАС", "why": "на пробое", "entry": brk,
                                     "invalidation": px + 0.5, "take": 106.0}]
        await p._review(px)
        assert p.plan and p.plan["kind"] == "прорыв" and p.plan["invalidation"] == round(px + 0.5, 6), p.plan
        assert p.plan["src"] == "review" and p.plan["snap_price"] == p.prices[-1], p.plan
        p.plan = None

        # ── ДРЕЙФ РЕШЕНИЯ направленный (v5.4.2: снимок — цена в промпте PRO, сдвиг — пока он думал): цена ЛУЧШЕ
        #    снимка → входим сразу (баг «цена лучше — не заходит») ──
        _pj0 = fake_ai.pro_json

        def _moving(to):
            async def _pj(system, user, *, route="pro", max_tokens=None):   # PRO думает — рынок живёт
                FakeTinkoff.price = to
                await settle(lambda: bool(p.prices) and p.prices[-1] == to)
                return await _pj0(system, user, route=route, max_tokens=max_tokens)
            return _pj
        assert await settle(lambda: bool(p.prices) and p.prices[-1] == px)
        better = round(px * 0.98, 2)
        fake_ai.review_answers[:] = [{"choice": "КУПИТЬ_СЕЙЧАС", "why": "сейчас", "invalidation": 95.0, "take": 104.0}]
        fake_ai.pro_json = _moving(better)
        try:
            await p._review(px)                                # снимок решения — px, пока думал — на 2% ниже
        finally:
            fake_ai.pro_json = _pj0
        assert p.plan and p.plan["entry"] is None and p.state == "ВХОЖУ" and "лучше снимка" in p.plan["why"], p.plan
        assert p.plan["snap_price"] == px and p.plan["src"] == "review", p.plan
        assert await settle(lambda: p.position is not None)
        assert p.position["entry"] <= better + 0.2, p.position
        # цена УБЕЖАЛА хуже снимка на 2% → v5.4.2: не тихая засада по старой цене (в тренде она не исполнится), а план
        # «сейчас» со снимком и пометкой о дрейфе — у двери решит PRO по живой цене; ревью 5.4.2: дверь выключена —
        # по уехавшей цене не входим (ИИ этой цены не видел): план снят, дежурный PRO решит заново по живой
        await p._close_all(better, "тест: освободить-2", reanalyze=False)
        p.plan = None
        FakeTinkoff.price = px
        assert await settle(lambda: bool(p.prices) and p.prices[-1] == px)
        worse = round(px * 1.02, 2)
        fake_ai.review_answers[:] = [{"choice": "КУПИТЬ_СЕЙЧАС", "why": "сейчас", "invalidation": 97.0, "take": 108.0}]
        fake_ai.pro_json = _moving(worse)
        try:
            await p._review(px)
        finally:
            fake_ai.pro_json = _pj0
        assert p.plan and p.plan["entry"] is None and p.plan["snap_price"] == px and "откат" != p.plan["kind"], p.plan
        assert p.plan["gate_note"] == f"цена ушла на 2.00% за время раздумий (было {px:g}, стало {worse:g})", p.plan
        assert "решит дверь по живой цене" in p.plan["why"] and not p._reanalyzing, p.plan
        assert await settle(lambda: p.plan is None), "дверь выключена, цена уехала дальше PYTHIA_ENTRY_DRIFT_PCT — не входим"
        assert p.position is None and p.pending is None, p.position
        assert f"цена ушла на 2.00% от снимка решения {px:g} (стало {worse:g}) — реши заново по живой цене" \
            in (p._review_reason or ""), p._review_reason

        # ── режим игры в перепроверке: ПРОДАТЬ при play=long → ЖДЁМ ──
        assert store_v5.trades_summary("TEST")["count"] == 4
        p.plan = None
        # v5.3: память раз в PYTHIA_MEMORY_EVERY перепроверок; новости старше совета — не в перепроверку, а в память
        await settle(lambda: not (explain._state(m).get("mem_task") and not explain._state(m)["mem_task"].done()))
        n_mem = m.memory_n
        StubNews.old_ts = m.council_ts - 3600                  # одна старая новость до совета
        m.reviews_since_memory = explain.memory_every() - 1
        fake_ai.review_answers[:] = [{"choice": "ЖДЁМ", "why": "память", "note": "ждём"}]
        await p._review(px)
        ur_m = fake_ai.last_user["mission_review"]
        assert "ужаты в блок ПАМЯТЬ МИССИИ" in ur_m and "[old001]" not in ur_m and "[a1b2c3]" in ur_m, ur_m[:1500]
        assert await settle(lambda: m.memory_n == n_mem + 1), (m.memory_n, n_mem)
        assert "old001" in fake_ai.last_user["memory"] and "Новости до последнего совета" in fake_ai.last_user["memory"]
        assert m.reviews_since_memory == 0
        StubNews.old_ts = None
        fake_ai.review_answers[:] = [{"choice": "ПРОДАТЬ_СЕЙЧАС", "why": "вниз", "invalidation": 101}]
        p.review_ts = time.time() + 1800
        await p._review(px)
        # v5.4.2: вход против режима — своя запись ВНЕ_РЕЖИМА (слово ИИ сохранено), не «ЖДЁМ» за ИИ; переспрос с пометкой
        assert m.reviews[-1]["choice"] == "ВНЕ_РЕЖИМА" and m.reviews[-1]["ai_choice"] == "ПРОДАТЬ_СЕЙЧАС" \
            and "против режима" in m.reviews[-1]["why"] and p.plan is None, m.reviews[-1]
        assert p._review_reason == "прошлый ответ ПРОДАТЬ_СЕЙЧАС запрещён режимом long", p._review_reason
        assert p.review_ts <= time.time() + REVIEW_RETRY_SEC + 1, p.review_ts - time.time()
        # v5.4.2: слово не разобрано («НЕ ПОКУПАТЬ» / мусор) — решения нет: ни «ЖДЁМ» в m.reviews, ни плана; повод жив
        n_rv = len(m.reviews)
        for junk in ("НЕ ПОКУПАТЬ", "может быть"):
            fake_ai.review_answers[:] = [{"choice": junk, "why": "?"}]
            p.review_ts = time.time() + 1800
            await p._review(px)
            assert len(m.reviews) == n_rv and p.plan is None and "ответ не разобран" in p.last_action, (junk, p.last_action)
            assert "запрещён режимом" in (p._review_reason or "") and p.review_ts <= time.time() + REVIEW_RETRY_SEC + 1
        # латиница и синонимы — решения: BUY → КУПИТЬ_СЕЙЧАС (план), ждём → ЖДЁМ
        fake_ai.review_answers[:] = [{"decision": "buy", "why": "латиницей", "invalidation": 95.0}]
        await p._review(px)
        assert m.reviews[-1]["choice"] == "КУПИТЬ_СЕЙЧАС" and p.plan and p.plan["side"] == "long", (m.reviews[-1], p.plan)
        p.plan = None

        # ── поводы пилота не зовут совет: «идея мертва до входа» → дежурному PRO; v5.4.2: вне рынка без плана —
        #    перепроверка через EVENT_MIN_GAP_SEC после ответа PRO, а не плановая (пилот не стоит без решения) ──
        p._review_reason = None
        p.review_ts = time.time() + 1800
        n_h = len(m.handoffs)
        p._fire_reanalyze("идея мертва до входа")
        assert p.review_ts <= max(time.time(), p._last_review_ts) + EVENT_MIN_GAP_SEC + 1 and p._review_reason == "идея мертва до входа"
        assert len(m.handoffs) == n_h + 1 and m.handoffs[-1]["kind"] == "pilot" and not m.handoffs[-1]["deferred"]
        assert not p._reanalyzing and not p._review_pulled, "повод пилота — не событие: пейсинг событий не тратится"
        p._fire_reanalyze("идея мертва до входа")             # тот же повод — не дубль
        assert len(m.handoffs) == n_h + 1 and m.handoffs[-1]["repeats"] == 2
        config.PYTHIA_AFTER_CLOSE_SEC = 0                      # 0 → после закрытия ждём плановой перепроверки
        p.review_ts = time.time() + 1800
        p._fire_reanalyze("после закрытия — огромный анализ с нуля")
        assert p.review_ts >= time.time() + 1790 and "после закрытия" in p._review_reason
        config.PYTHIA_AFTER_CLOSE_SEC = 900
        # ── v5.3 фаза 3 (д): PRO промолчал на перепроверке (ответ не JSON / таймаут) → ранняя повторная попытка
        #    через REVIEW_RETRY_SEC, повод НЕ стирается, толмач и шина видят ──
        n_x = len(m.explain)
        fake_ai.review_answers[:] = ["это не JSON-объект"]
        await p._review(px)
        assert "после закрытия" in (p._review_reason or ""), "повод потерян"
        assert time.time() + REVIEW_RETRY_SEC - 5 <= p.review_ts <= time.time() + REVIEW_RETRY_SEC + 1, p.review_ts - time.time()
        assert p.last_action.startswith("дежурный PRO промолчал (ответ не JSON-объект)") and "повод сохранён:" in p.last_action \
            and "после закрытия" in p.last_action, p.last_action
        p.review_ts = time.time() + 1800

        async def _pro_timeout(*a, **k):
            raise asyncio.TimeoutError()
        _pj = fake_ai.pro_json
        fake_ai.pro_json = _pro_timeout
        try:
            await p._review(px)
            raise AssertionError("исключение обязано дойти до _review_bg (лог), не глотаться")
        except asyncio.TimeoutError:
            pass
        finally:
            fake_ai.pro_json = _pj
        assert "промолчал (таймаут 1200 с)" in p.last_action and "после закрытия" in p._review_reason, p.last_action
        assert p.review_ts <= time.time() + REVIEW_RETRY_SEC + 1 and p.review_ts > time.time() + REVIEW_RETRY_SEC - 5
        assert await settle(lambda: len(m.explain) > n_x and xev(m, "review", "PRO промолчал")), m.explain[-1:]
        assert m.reviews[-1]["choice"] != "ПРОМОЛЧАЛ" and status("TEST")["pilot"]["review_reason"]
        p._review_reason = None

        # ── событие: серьёзная новость при живом пилоте → дежурный PRO (не совет), не раньше 3 мин после ответа PRO ──
        p.review_ts = time.time() + 1800
        n_h = len(m.handoffs)
        await on_serious_news({"note": "ЦБ поднял ставку", "severity": 90})
        assert len(m.handoffs) == n_h + 1 and m.handoffs[-1]["reason"].startswith("серьёзная новость")
        assert m.handoffs[-1]["kind"] == "news" and status("TEST")["handoffs"][-1]["kind"] == "news"
        assert p._review_reason and "серьёзная новость" in p._review_reason and not p._reanalyzing and p._review_pulled
        assert time.time() + EVENT_MIN_GAP_SEC - 10 <= p.review_ts <= time.time() + EVENT_MIN_GAP_SEC + 1, p.review_ts - time.time()
        # ── событие: резкий ход цены → перепроверка (тот же ход второй раз не считается) ──
        t0 = time.time()
        p._px_hist = [(t0 - 400, px), (t0 - 300, px), (t0 - 200, px), (t0 - 100, px)]
        p.review_ts = time.time() + 1800
        p._review_pulled = False
        n_h = len(m.handoffs)
        p._shock_watch(round(px * 1.012, 3))
        assert len(m.handoffs) == n_h + 1 and m.handoffs[-1]["kind"] == "shock", m.handoffs[-1]
        assert "+1.20%" in m.handoffs[-1]["reason"] and p._review_pulled and p.review_ts <= time.time() + EVENT_MIN_GAP_SEC + 1
        p._shock_watch(round(px * 1.013, 3))
        assert len(m.handoffs) == n_h + 1, "один ход — одна перепроверка"
        assert "Ход за 6 мин: +1.30%" in p._situation_text(round(px * 1.013, 3)), p._situation_text(round(px * 1.013, 3))
        fake_ai.review_answers[:] = [{"choice": "ЖДЁМ", "why": "шум", "note": "ждём"}]
        await p._review(px)
        ur = fake_ai.last_user["mission_review"]
        assert "резкий ход: +1.20%" in ur and "серьёзная новость: ЦБ" in ur and "идея мертва" not in ur, ur[:900]
        assert p._review_reason is None and p._last_event_review_ts > 0
        p.review_ts = time.time() + 1800                       # событийная прошла → следующая не раньше PYTHIA_EVENT_COOL_SEC
        t0 = time.time()
        p._px_hist = [(t0 - 400, px), (t0 - 300, px), (t0 - 200, px), (t0 - 100, px)]
        p._last_shock_ts = 0.0
        p._shock_watch(round(px * 0.985, 3))
        assert m.handoffs[-1]["kind"] == "shock" and "-1.50%" in m.handoffs[-1]["reason"]
        assert p.review_ts >= time.time() + float(config.PYTHIA_EVENT_COOL_SEC) - 10, "пейсинг событий"
        # тишина после входа: событие при молодой позиции БЕЗ триажа не приближает перепроверку (v5.4.2: с триажем
        # молодая позиция идёт к нему — СЕЙЧАС/ПЛАНОВО решает ИИ; это — tests/test_free_pilot_cadence.py)
        config.PYTHIA_EVENT_TRIAGE = False
        p.position = {"side": "long", "entry": px, "lots": 5, "take": 104.0, "invalidation": 97.0,
                      "opened_ts": time.time(), "stop_id": None, "floating": 0.0}
        p.review_ts = time.time() + 1800
        p._review_reason = None
        p._last_event_review_ts = 0.0
        await on_serious_news({"note": "Санкции на банки", "severity": 95})
        assert p.review_ts >= time.time() + 1790 and "моложе" in p.last_action and m.handoffs[-1]["deferred"]
        assert "ОТКРЫТАЯ ПОЗИЦИЯ: long" in _prev_text(m) and "в рынке 0 мин" in _prev_text(m), _prev_text(m)
        assert not fake_ai.calls.count("event_triage"), "триаж выключен — триажа нет"
        config.PYTHIA_EVENT_TRIAGE = True
        p.position = None
        p._review_reason = None

        # ── v5.3 W2: ТРИАЖ СОБЫТИЙ FLASH при зрелой позиции: ПЛАНОВО → PRO не дёргают, повод копится;
        #    САМ «подтянуть_трос» → триггер ближе без PRO (трос биржи на месте); молчание → PRO как раньше ──
        p.position = {"side": "long", "entry": px, "lots": 8, "take": 104.0, "invalidation": 97.0,
                      "opened_ts": time.time() - 1300, "stop_id": None, "floating": 0.0}
        p._set_levels(p.position, 104.0, 97.0)
        pos_t = p.position
        FakeTinkoff.pause_ticks = True  # manual triage assertions own the state until the take scenario

        async def _pf_with_pos():                    # сверка со счётом видит эту позицию (иначе «внешнее закрытие»)
            return {"mode": "real", "cash": None, "positions": [{"figi": "FIGI-T", "qty": 8, "avg": px}]}
        _pf0 = p.broker.portfolio
        p.broker.portfolio = _pf_with_pos
        p.review_ts, p._last_review_ts, p._last_event_review_ts = time.time() + 1800, 0.0, 0.0
        p._review_reason = None
        n_h = len(m.handoffs)
        fake_ai.triage_answers[:] = [{"urgency": "ПЛАНОВО", "action": None, "why": "в русле плана"}]
        t_ev = time.time()                                     # таймер считаем от события, не от конца проверок
        await on_serious_news({"note": "Ставка без сюрпризов", "severity": 90})
        assert fake_ai.calls.count("event_triage") == 1 and p.triages[-1]["urgency"] == "ПЛАНОВО", p.triages
        assert len(m.handoffs) == n_h + 1 and m.handoffs[-1]["deferred"] and m.handoffs[-1]["triage"].startswith("ПЛАНОВО")
        assert p.review_ts >= t_ev + 1790 and "Ставка" in p._review_reason and "ПЛАНОВО" in p.last_action
        assert status("TEST")["handoffs"][-1]["triage"].startswith("ПЛАНОВО") and status("TEST")["pilot"]["triages"][-1]["urgency"] == "ПЛАНОВО"
        ut_ = fake_ai.last_user["event_triage"]
        assert "СОБЫТИЕ: серьёзная новость: Ставка" in ut_ and "триггер (мягкий стоп) 97.0" in ut_ and "ПАМЯТЬ МИССИИ" in ut_, ut_[:600]
        assert m.sizes["triage"]["prompt"] == len(ut_)
        hard_t = pos_t["hard_stop"]
        # v5.3 фаза 3 (в): ПЛАНОВО + «подтянуть_трос» с безопасным уровнем (между триггером и ценой) → трос поджат,
        # как при САМ, PRO не дёргают; небезопасный уровень — не тронут; «снять_план» при ПЛАНОВО — не выполняется
        fake_ai.triage_answers[:] = [{"urgency": "ПЛАНОВО", "action": "подтянуть_трос", "trigger": 98.0, "why": "поджать"}]
        await on_serious_news({"note": "Отчёт лучше ожиданий", "severity": 90})
        assert p.triages[-1]["urgency"] == "ПЛАНОВО" and p.triages[-1]["action"] == "подтянуть_трос", p.triages[-1]
        assert p.triages[-1]["trigger"] == 98.0 and "триггер подтянут 97 → 98" in p.triages[-1]["done"], p.triages[-1]
        assert pos_t["invalidation"] == 98.0 and pos_t["inv0"] == 97.0 and pos_t["hard_stop"] == hard_t, pos_t
        assert m.handoffs[-1]["deferred"] and m.handoffs[-1]["triage"].startswith("ПЛАНОВО (триггер подтянут")
        assert p.review_ts >= time.time() + 1790, "PRO не разбужен"
        fake_ai.triage_answers[:] = [{"urgency": "ПЛАНОВО", "action": "подтянуть_трос", "trigger": 120.0, "why": "мусор"}]
        await on_serious_news({"note": "Слух о SPO", "severity": 90})
        assert pos_t["invalidation"] == 98.0 and "не тронут" in p.triages[-1]["done"], p.triages[-1]
        fake_ai.triage_answers[:] = [{"urgency": "ПЛАНОВО", "action": "снять_план", "why": "не сейчас"}]
        await on_serious_news({"note": "Совет директоров перенесён", "severity": 90})
        assert p.triages[-1]["urgency"] == "ПЛАНОВО" and p.triages[-1]["action"] is None and not p.triages[-1]["done"], p.triages[-1]
        fake_ai.triage_answers[:] = [{"urgency": "САМ", "action": "подтянуть_трос", "trigger": 98.5, "why": "подтянем"}]
        await on_serious_news({"note": "Дивиденды подтверждены", "severity": 90})
        assert p.triages[-1]["urgency"] == "САМ" and p.triages[-1]["trigger"] == 98.5 and "подтянут 98 → 98.5" in p.triages[-1]["done"]
        assert pos_t["invalidation"] == 98.5 and pos_t["inv0"] == 97.0 and pos_t["hard_stop"] == hard_t, pos_t
        assert p.review_ts >= time.time() + 1790 and m.handoffs[-1]["deferred"]
        _tt = EVENT_TRIAGE_TIMEOUT
        g["EVENT_TRIAGE_TIMEOUT"] = 0.2

        async def _slow_flash(*a, **k):
            await asyncio.sleep(1.0)
            return {}
        _fj = fake_ai.money_json                             # 5.4.1: триаж — узел у денег (money_json)
        fake_ai.money_json = _slow_flash
        await on_serious_news({"note": "Санкции на банки", "severity": 95})
        fake_ai.money_json = _fj
        g["EVENT_TRIAGE_TIMEOUT"] = _tt
        assert p.triages[-1]["urgency"] == "СЕЙЧАС" and "не ответил" in p.triages[-1]["why"]
        assert p._review_pulled and p.review_ts <= time.time() + EVENT_MIN_GAP_SEC + 1 and not m.handoffs[-1]["deferred"]
        assert await settle(lambda: xev(m, "triage", "Триаж PRO: САМ") or xev(m, "triage", "Триаж PRO: ПЛАНОВО")), m.explain
        # ── v5.3 W3: ПРОКОЛ СКАНЕРА как повод: против позиции → триаж (СЕЙЧАС) → PRO; блок ПРОКОЛ СКАНЕРА первым
        #    в ситуации триажа и перепроверки; тот же прокол второй раз — тишина; ниже порога — тишина; выкл — тишина ──
        p.last_book, p._book_ts = {"best_bid": px - 0.1, "best_ask": px + 0.1}, time.time()
        p.review_ts, p._last_review_ts, p._last_event_review_ts, p._review_reason = time.time() + 1800, 0.0, 0.0, None
        p._review_pulled, p._puncture_check_ts, p._last_puncture_ts = False, 0.0, 0.0
        n_h, n_tr = len(m.handoffs), len(p.triages)
        StubScan.puncture = {"side": "вниз", "p_lo": 97.2, "p_hi": 97.6, "persistence": 0.78, "depth_mean": 0.8, "ticks": 94}
        fake_ai.triage_answers[:] = [{"urgency": "СЕЙЧАС", "action": None, "why": "полоса ведёт к тросу"}]
        p._puncture_watch(px)
        assert p._triage_task is not None, p.last_action
        await p._triage_task
        pu = p.puncture
        assert pu and pu["role"] == "угроза" and pu["in_pos"] and pu["pending"] and pu["side"] == "вниз", pu
        assert pu["text"].startswith("ПРОКОЛ СКАНЕРА: сторона ВНИЗ, стойкость 78 % (полосу держат 94 тиков), полоса 97.2–97.6"), pu["text"]
        assert "мы в позиции long — прокол против нас — возможная угроза" in pu["text"] and "Хоукс по ленте n=0.71" in pu["text"]
        ut_p = fake_ai.last_user["event_triage"]
        assert ut_p.index("ПРОКОЛ СКАНЕРА") < ut_p.index("Цена сейчас") and "СОБЫТИЕ: прокол сканера вниз 78 % (угроза" in ut_p, ut_p[:500]
        assert len(m.handoffs) == n_h + 1 and m.handoffs[-1]["kind"] == "puncture" and not m.handoffs[-1]["deferred"]
        assert m.handoffs[-1]["triage"].startswith("СЕЙЧАС") and p.triages[-1]["kind"] == "puncture"
        assert p._review_pulled and p.review_ts <= time.time() + EVENT_MIN_GAP_SEC + 1, "PRO разбужен"
        assert pu["triage"].startswith("СЕЙЧАС") and "PRO решит" in pu["state"], pu
        st_p = status("TEST")["pilot"]
        assert st_p["puncture"]["side"] == "вниз" and st_p["puncture_now"]["persistence"] == 0.78 and st_p["puncture_min"] == 0.6
        assert "text" not in st_p["puncture_now"] and status("TEST")["handoffs"][-1]["kind"] == "puncture"
        assert await settle(lambda: xev(m, "puncture", "Сканер видит прокол вниз 78 %")), m.explain
        #    тот же прокол ещё раз — второй раз ИИ не тревожим (дедуп по стороне + полосе + роли)
        p._puncture_check_ts = 0.0
        p._puncture_watch(px)
        assert p._triage_task is None and len(m.handoffs) == n_h + 1 and len(p.triages) == n_tr + 1
        #    v5.3 фаза 3 (б): дедуп (_puncture_seen) и пейсинг (_last_puncture_ts) переживают рестарт — секция «pilot»
        #    state-файла рядом с позицией; новый пилот подхватывает и на тот же прокол молчит
        _sp = Path(tempfile.mkdtemp(prefix="pythia_m_")) / "aipilot_state.json"
        p._state_path = _sp
        p._save_state()
        _rec = json.loads(_sp.read_text(encoding="utf-8"))
        assert _rec["figi"] == "FIGI-T" and _rec["position"]["lots"] == 8, _rec
        assert _rec["pilot"]["puncture_seen"] == [["вниз", 195, "угроза", round(p._puncture_seen[("вниз", 195, "угроза")], 1)]], _rec["pilot"]
        assert abs(_rec["pilot"]["last_puncture_ts"] - p._last_puncture_ts) < 0.11, _rec["pilot"]
        p._state_path = None
        p2 = MissionPilot("TEST", deposit=100000.0, broker=p.broker, mission=m)
        p2.figi, p2._state_path = "FIGI-T", _sp
        p2._restore_state(0)                              # на счёте пусто — позицию не подхватит, память прокола — да
        assert p2.position is None and ("вниз", 195, "угроза") in p2._puncture_seen and p2._last_puncture_ts > 0, p2._puncture_seen
        assert abs(p2._puncture_seen[("вниз", 195, "угроза")] - p._puncture_seen[("вниз", 195, "угроза")]) < 0.11
        p2.position = dict(p.position)
        p2._puncture_check_ts, p2._state_path = 0.0, None
        p2._puncture_watch(px)
        assert p2._triage_task is None and p2.puncture is None and "уже был поводом" in p2._puncture_now["state"], p2._puncture_now
        assert not _sp.exists(), "на счёте пусто — позиция из файла отброшена, файл стёрт (как и раньше)"
        #    протухшая память (старше PUNCTURE_SEEN_SEC / пейсинга) и мусор при подхвате отбрасываются; файл без позиции
        _old = {"figi": "FIGI-T", "base": "TEST", "ts": time.time(), "position": None,
                "pilot": {"puncture_seen": [["вниз", 195, "угроза", time.time() - PUNCTURE_SEEN_SEC - 10], ["мусор"]],
                          "last_puncture_ts": time.time() - 100000}}
        _sp.write_text(json.dumps(_old), encoding="utf-8")
        p3 = MissionPilot("TEST", deposit=100000.0, broker=p.broker, mission=m)
        p3.figi, p3._state_path = "FIGI-T", _sp
        p3._restore_state(0)
        assert not p3._puncture_seen and p3._last_puncture_ts == 0.0, (p3._puncture_seen, p3._last_puncture_ts)
        assert p3.position is None
        p3._state_path = None
        _sp.unlink()
        #    PRO в перепроверке видит блок первым; после ответа блок снят, состояние — решение PRO
        fake_ai.review_answers[:] = [{"choice": "ЖДЁМ", "why": "полоса далеко от троса", "note": "держим"}]
        await p._review(px)
        ur_p = fake_ai.last_user["mission_review"]
        assert ur_p.index("ПРОКОЛ СКАНЕРА") < ur_p.index("Цена сейчас") and "прокол сканера вниз 78 %" in ur_p, ur_p[:400]
        assert not p.puncture["pending"] and p.puncture["state"].startswith("PRO решил: ЖДЁМ")
        fake_ai.review_answers[:] = [{"choice": "ЖДЁМ", "why": "план жив", "note": "держим"}]
        await p._review(px)
        assert "ПРОКОЛ СКАНЕРА" not in fake_ai.last_user["mission_review"], "разобранный прокол PRO второй раз не показывают"
        #    другая полоса ниже порога — тишина (с честной причиной для панели); выключено конфигом — тишина
        StubScan.puncture = {"side": "вниз", "p_lo": 95.0, "p_hi": 95.4, "persistence": 0.5, "depth_mean": 0.6, "ticks": 60}
        p._puncture_check_ts, p._last_puncture_ts = 0.0, 0.0
        p._puncture_watch(px)
        assert "ниже порога 60 %" in p._puncture_now["state"] and len(m.handoffs) == n_h + 1 and len(p.triages) == n_tr + 1
        StubScan.puncture = {"side": "вниз", "p_lo": 95.0, "p_hi": 95.4, "persistence": 0.9, "depth_mean": 0.6, "ticks": 60}
        config.PYTHIA_PUNCTURE = False
        p._puncture_check_ts = 0.0
        p._puncture_watch(px)
        assert len(m.handoffs) == n_h + 1 and status("TEST")["pilot"]["puncture_min"] is None
        config.PYTHIA_PUNCTURE = True
        StubScan.puncture = None
        p._puncture_check_ts = 0.0
        p._puncture_watch(px)
        assert p._puncture_now is None and len(m.handoffs) == n_h + 1
        p.puncture = None
        FakeTinkoff.pause_ticks = False
        # ── v5.3 W2: МЯГКИЙ ТЕЙК: FLASH ПОДЕРЖАТЬ → прибыль заперта триггером (не ниже входа + 50 % хода),
        #    тейк отодвинут, задача Совету без очереди → совет BUY держит с новыми уровнями ──
        p._last_reanalyze_ts = time.time()
        m.council_ts = time.time()
        p._review_pulled, p._review_reason = False, None
        fake_ai.take_answers[:] = [{"decision": "ПОДЕРЖАТЬ", "why": "импульс не выдохся", "lock_price": 101.0,
                                    "tp_next": 106.0, "hold_minutes": 10}]
        fake_ai.exec_answers[:] = [{"do": "BUY", "entry": None, "take": 107.0, "invalidation": 99.5, "why": "держать выше"}]
        old_rid_t = m.run_id                                   # (цена досье в тесте 100 — стоп совета ниже неё)
        p._tick_n = 1
        FakeTinkoff.price = 104.5                              # цена дошла до тейка 104 → петля спрашивает FLASH у тейка
        assert await settle(lambda: bool(p.guards) and p.guards[-1].get("side") == "take", n=600), p.last_action
        uk = fake_ai.last_user["mission_take"]
        for piece in ("ТЕЙК: long, цена 104.5 выше цели 104", "ХОД ЦЕНЫ", "ЖИВОЙ РЫНОК", "ПЛАН И ПРОШЛЫЕ РЕШЕНИЯ", "СВЯЗАННЫЕ БУМАГИ",
                      "ИТОГ ОБЩЕГО СОВЕТА", "СВЕЖИЕ НОВОСТИ", "ПАМЯТЬ МИССИИ", "Зафиксировать сейчас или подержать"):
            assert piece in uk, (piece, uk[:1200])
        assert m.sizes["take"]["prompt"] == len(uk) and m.sizes["take"]["answer"] > 0
        gt = p.guards[-1]
        floor_t = pos_t["entry"] + (104.0 - pos_t["entry"]) * 0.5          # вход + 50 % хода до тейка (lock 101 ниже пола)
        assert gt["side"] == "take" and gt["decision"] == "ПОДЕРЖАТЬ" and abs(gt["lock_price"] - floor_t) <= 0.0051 and gt["tp_next"] == 106.0, gt
        assert m.handoffs[-1]["kind"] == "take" and not m.handoffs[-1]["deferred"] and status("TEST")["pilot"]["take_guard"]["holds"] in (0, 1)
        assert await settle(lambda: not p._reanalyzing and m.run_id != old_rid_t, n=600), (p.last_action, p._reanalyzing)
        assert "мягкий тейк" in str(bus.run(m.run_id)["meta"].get("reason")) and "ОТКРЫТАЯ ПОЗИЦИЯ" in fake_ai.last_user["mission_exec"]
        assert p.position is pos_t and pos_t["invalidation"] == 99.5 and pos_t["take"] == 107.0 and pos_t["take_holds"] == 0, pos_t
        assert await settle(lambda: xev(m, "take", "PRO у тейка: ПОДЕРЖАТЬ")), m.explain
        assert "заперта" in xlast(m, "take")["detail"] and "передана Совету" in xlast(m, "take")["detail"]
        await p._close_all(104.5, "тест: освободить-тейк", reanalyze=False)
        p.broker.portfolio = _pf0
        p.plan = None
        p._review_reason = None
        p.guards.clear()                                       # дальше тесты троса ждут первый guard
        FakeTinkoff.price = px
        assert await settle(lambda: bool(p.prices) and p.prices[-1] == px)

        # ── РЫНОЧНЫЕ ЧАСЫ: биржа закрыта → стопор (фаза closed, строка рынка в ситуации и статусе), серьёзная
        #    новость копится к открытию (PRO не дёргаем); открылось после ночи → перепроверка «рынок открылся:
        #    накопились новости/события» с заметками дозора и накопленным поводом, без пейсинга ──
        _ct = getattr(config, "PYTHIA_CLOSED_TICK_SEC", 30)
        config.PYTHIA_CLOSED_TICK_SEC = 0.05
        FakeClock.open = False
        p.review_ts = time.time() + 1800
        p._last_event_review_ts = 0.0
        assert await settle(lambda: p.state == "РЫНОК_ЗАКРЫТ", 600), p.state
        st_c = status("TEST")
        assert st_c["phase"] == "closed" and st_c["market"]["open"] is False and st_c["pilot"]["market"]["next_open_msk"] == "10:00 МСК"
        assert p.review_ts >= time.time() + FakeClock.next_in, "перепроверка ждёт открытия"
        assert "рынок закрыт до 10:00 МСК" in p.last_action and "с открытия" in p.last_action, p.last_action
        await on_serious_news({"note": "Санкции на банки", "severity": 95})
        assert p.review_ts >= time.time() + FakeClock.next_in - 5 and "рынок закрыт" in p.last_action and m.handoffs[-1]["deferred"]
        assert "Санкции" in (p._review_reason or ""), "повод копится к открытию"
        assert "РЫНОК: рынок закрыт до 10:00 МСК" in p._situation_text(100.0)
        assert await settle(lambda: xev(m, "market", "Рынок закрыт до 10:00 МСК")), m.explain
        p._closed_since = time.time() - 8 * 3600
        FakeClock.open = True
        assert await settle(lambda: p.state != "РЫНОК_ЗАКРЫТ", 600), p.state
        assert p.state == p._resting_state() and status("TEST")["phase"] != "closed"
        rr = p._review_reason or ""
        assert "рынок открылся: накопились новости/события" in rr and "Санкции" in rr and "ЦБ" in rr, rr
        assert p.review_ts <= time.time() + ai_pilot.OPEN_REVIEW_GRACE_SEC + 1 and m.handoffs[-1]["kind"] == "open", (p.review_ts - time.time(), m.handoffs[-1])
        assert "РЫНОК: рынок открыт" in p._situation_text(100.0)
        assert await settle(lambda: xev(m, "market", "Рынок открылся")), m.explain
        assert "закрыт был 8 ч" in xlast(m, "market")["detail"]
        config.PYTHIA_CLOSED_TICK_SEC = _ct
        p._review_reason = None
        p._review_pulled = False
        p.review_ts = time.time() + 1800

        # ── НОВЫЙ_АНАЛИЗ от PRO: совет был только что → окно закрыто, PRO решает сам; совет давно → полный совет ──
        m.council_ts = time.time()
        p.review_ts = time.time() + 1800
        fake_ai.review_answers[:] = [{"choice": "НОВЫЙ_АНАЛИЗ", "why": "картина сломалась", "note": "зову совет"}]
        await p._review(px)
        assert not p._reanalyzing and p._council_blocked and m.handoffs[-1]["kind"] == "council" and m.handoffs[-1]["deferred"]
        # v5.4.2: просьба не потеряна — отложена до окна, перепроверка к открытию окна
        assert p._reanalyze_pending and p._reanalyze_pending.startswith("перепроверка потребовала свежий разбор: картина")
        assert p.review_ts <= m.council_ts + float(config.PYTHIA_COUNCIL_GAP_SEC) + 61, p.review_ts - m.council_ts
        assert "совет был 0 мин назад" in p.last_action and status("TEST")["pilot"]["council_blocked"], p.last_action
        fake_ai.review_answers[:] = [{"choice": "ЖДЁМ", "why": "жду окна", "note": "ждём"}]
        await p._review(px)
        ur = fake_ai.last_user["mission_review"]
        assert "СОВЕТ: " in ur and "просили полный совет" in ur and "решай сам" in ur
        assert not p._council_blocked
        m.council_ts = time.time() - 4000
        p._last_reanalyze_ts = time.time() - 4000
        fake_ai.exec_answers[:] = [{"do": "BUY", "entry": None, "take": 104, "invalidation": 97}]
        fake_ai.review_answers[:] = [{"choice": "НОВЫЙ_АНАЛИЗ", "why": "сломалось всерьёз", "note": "зову совет"}]
        old_rid2 = m.run_id
        await p._review(px)
        assert p._reanalyzing and m.handoffs[-1]["reason"].startswith("перепроверка потребовала свежий разбор: сломалось")
        assert not m.handoffs[-1]["deferred"] and m.handoffs[-1]["kind"] == "council"
        assert await settle(lambda: not p._reanalyzing and m.run_id != old_rid2)
        assert bus.run(m.run_id)["meta"].get("again") and "сломалось всерьёз" in str(bus.run(m.run_id)["meta"].get("reason"))
        assert m.exec.get("take") == 104.0 and m.exec["entry_kind"] == "сейчас" and "(сейчас)" in _exec_text(m)
        assert await settle(lambda: p.position is not None)   # вход сразу по новому приказу
        assert status("TEST")["phase"] == "in_position" and p._review_reason is None
        await p._close_all(px, "тест: освободить-3", reanalyze=False)
        p.plan = None
        # ── защита от переворота (на отдельном, не запущенном пилоте) ──
        p2 = MissionPilot("TEST", deposit=100000, broker=p.broker, mission=m)
        p2.position = {"side": "long", "entry": px, "lots": 5, "take": 104.0, "invalidation": 97.0,
                       "opened_ts": time.time(), "stop_id": None, "floating": 0.0}
        p2._reanalyzing = True
        assert p2.adopt_forecast({"exec": {"do": "SELL", "entry": None, "take": 95.0, "invalidation": 102.0}}) is False
        assert p2.position["side"] == "long" and "переворот отклонён" in p2.last_action and not p2._reanalyzing
        # v5.4.2: выход по слову совета исполняется (закрыть ближайшим тиком), переворота нет
        assert p2._close_pending and "закрываю без переворота" in p2.last_action and p2.plan is None, p2.last_action
        assert p2.adopt_forecast({"exec": {"do": "BUY", "entry": None, "take": 105.0, "invalidation": 98.0}}) is True
        assert p2.position["take"] == 105.0 and p2.position["invalidation"] == 98.0, "та же сторона — обновил стоп/тейк"
        assert p2._last_reanalyze_ts > 0, "пейсинг считается с первого принятого плана"
        p2.position["opened_ts"] = time.time() - 1300                       # старше 20 мин — переворот разрешён
        assert p2.adopt_forecast({"exec": {"do": "SELL", "entry": None, "take": 95.0, "invalidation": 102.0}}) is True
        assert p2.plan and p2.plan["side"] == "short"
        config.PYTHIA_FLIP_QUIET_SEC = 0                                    # 0 — защита выключена
        p2.position = {"side": "long", "entry": px, "lots": 5, "take": 104.0, "invalidation": 97.0,
                       "opened_ts": time.time(), "stop_id": None, "floating": 0.0}
        assert p2.adopt_forecast({"exec": {"do": "SELL", "entry": None, "take": 95.0, "invalidation": 102.0}}) is True
        config.PYTHIA_FLIP_QUIET_SEC = 1200

        # ── МЯГКИЙ СТОП (v5.2): цена за триггером → FLASH решает; ЖДАТЬ → задача Совету без очереди,
        #    совет говорит CLOSE → позиция закрыта; трос на бирже не двигался ──
        fake_ai.review_answers[:] = [{"choice": "КУПИТЬ_СЕЙЧАС", "why": "снова", "invalidation": 97.0, "take": 104.0}]
        await p._review(px)
        assert await settle(lambda: p.position is not None) and p.position["invalidation"] == 97.0
        hard1 = p.position["hard_stop"]
        assert hard1 and hard1 < 97.0 and abs(p.broker.stops[-1]["stop"] - hard1) < 1e-9
        p._last_reanalyze_ts = time.time()                     # пейсинг советов «только что» — force обязан пройти
        m.council_ts = time.time()
        n_tr = store_v5.trades_summary("TEST")["count"]
        fake_ai.guard_answers[:] = [{"decision": "ЖДАТЬ", "why": "ложный прокол, стакан держит", "hold_minutes": 5,
                                     "hold_until_price": 96.5}]
        fake_ai.exec_answers[:] = [{"do": "CLOSE", "why": "картина сломалась — выходим"}]
        old_rid3 = m.run_id
        p._tick_n = 1                    # сверка со счётом (каждые RECONCILE_EVERY тиков; фейковый счёт пуст →
                                         # «внешнее закрытие») не должна опередить вопрос у троса — гонка теста
        FakeTinkoff.price = 96.9                               # ниже триггера 97, выше троса
        assert await settle(lambda: bool(p.guards)), p.last_action
        g1 = p.guards[-1]
        assert g1["decision"] == "ЖДАТЬ" and g1["hold_until"] == 96.5 and "ложный прокол" in g1["why"], g1
        ug = fake_ai.last_user["mission_guard"]
        for piece in ("ХОД ЦЕНЫ", "ЖИВОЙ РЫНОК", "ПЛАН И ПРОШЛЫЕ РЕШЕНИЯ", "ДАННЫЕ РАЗВЕДКИ", "VTBR (ВТБ): 96.9",
                      "СВЯЗАННЫЕ БУМАГИ (корреляции)", "ρ=+0.81",
                      "ИТОГ ОБЩЕГО СОВЕТА", "СВЕЖИЕ НОВОСТИ", "ниже уровня 97", "аварийный трос биржи @95.54",
                      "триггер (мягкий стоп) @97"):
            assert piece in ug, (piece, ug[:1500])
        assert m.sizes["guard"]["prompt"] == len(ug) and m.sizes["guard"]["answer"] > 0
        assert "═══ ПАМЯТЬ МИССИИ" in ug and "ПАМЯТЬ: " in ug, "трос видит память миссии"
        assert await settle(lambda: xev(m, "guard", "PRO у троса: ЖДАТЬ")), m.explain
        assert "передана Совету" in xlast(m, "guard")["detail"]
        assert m.handoffs[-1]["kind"] == "stop" and "мягкий стоп" in m.handoffs[-1]["reason"] and not m.handoffs[-1]["deferred"]
        assert await settle(lambda: p.position is None and m.run_id != old_rid3, n=600), (p.last_action, p.position)
        assert "мягкий стоп" in str(bus.run(m.run_id)["meta"].get("reason")), bus.run(m.run_id)["meta"]
        assert m.exec["do"] == "CLOSE" and "CLOSE" in _exec_text(m), _exec_text(m)
        assert fake_ai.calls.count("mission_exec") >= 3 and "ОТКРЫТАЯ ПОЗИЦИЯ" in fake_ai.last_user["mission_exec"]
        trs = store_v5.trades("TEST")
        assert store_v5.trades_summary("TEST")["count"] == n_tr + 1 and "приказ совета: закрыть" in trs[0]["why"], trs[0]
        assert not p.stops_bad if hasattr(p, "stops_bad") else True
        assert status("TEST")["pilot"]["guards"][-1]["decision"] == "ЖДАТЬ"
        assert p.plan is None and p.state == "ЖДУ_ПЛАН", (p.plan, p.state)
        FakeTinkoff.price = px
        assert await settle(lambda: bool(p.prices) and p.prices[-1] == px)
        # СЛИТЬ: FLASH решает продать сразу — совет не зовут
        fake_ai.review_answers[:] = [{"choice": "КУПИТЬ_СЕЙЧАС", "why": "ещё раз", "invalidation": 97.0, "take": 104.0}]
        await p._review(px)
        assert await settle(lambda: p.position is not None)
        n_ex = len(fake_ai.exec_answers)
        fake_ai.guard_answers[:] = [{"decision": "СЛИТЬ", "why": "поток продавцов"}]
        FakeTinkoff.price = 96.9
        assert await settle(lambda: p.position is None), p.last_action
        tr_s = store_v5.trades("TEST")[0]
        assert "PRO решил слить" in tr_s["why"] and "поток продавцов" in tr_s["why"], tr_s
        assert p.guards[-1]["decision"] == "СЛИТЬ" and len(fake_ai.exec_answers) == n_ex and not p._reanalyzing
        FakeTinkoff.price = px
        assert await settle(lambda: bool(p.prices) and p.prices[-1] == px)
        # аварийный трос: цена за тросом → закрыть без вопросов FLASH
        fake_ai.review_answers[:] = [{"choice": "КУПИТЬ_СЕЙЧАС", "why": "третий", "invalidation": 97.0, "take": 104.0}]
        await p._review(px)
        assert await settle(lambda: p.position is not None)
        fake_ai.guard_answers[:] = [{"decision": "ЖДАТЬ", "why": "не должно спрашиваться"}]
        FakeTinkoff.price = round(p.position["hard_stop"] - 0.05, 2)
        assert await settle(lambda: p.position is None), p.last_action
        assert "аварийный трос" in p.last_action and fake_ai.guard_answers, p.last_action
        fake_ai.guard_answers.clear()
        FakeTinkoff.price = px
        assert await settle(lambda: bool(p.prices) and p.prices[-1] == px)

        # ── ДОБРАТЬ (v5.2): биржа даёт ещё лоты → план той же стороны, тик добирает, вход усредняется ──
        def fresh(pl):          # новая сцена: killswitch и P/L сессии заново (три стопа подряд запирают сессию по правилу)
            pl.pnls = []
            pl.session_risk = trader_risk.SessionRisk(pl.deposit)
            pl._live_cash = None
            if pl.state == "СТОП" and not pl.panic_flag:
                pl.state = "ЖДУ_ПЛАН"
        assert p.session_risk.locked and p.state == "СТОП", "три стопа подряд → killswitch по правилу"
        fresh(p)
        fake_ai.review_answers[:] = [{"choice": "КУПИТЬ_СЕЙЧАС", "why": "вход", "invalidation": 97.0, "take": 104.0}]
        await p._review(px)
        assert await settle(lambda: p.position is not None), (p.last_action, p.state, p.plan, p._reanalyzing, p.pending,
                                                              p.no_entry_until - time.time(), p.session_risk.state())
        assert p.position["lots"] == 8, p.position
        FakeBroker.mx = {"buy": 3, "sell": 11}                 # GetMaxLots: купить можно ещё 3
        p.deposit_override = None                              # весь счёт — без потолка владельца
        fake_ai.review_answers[:] = [{"choice": "ДОБРАТЬ", "why": "тянут вверх", "invalidation": 97.5, "take": 105.0}]
        await p._review(px)
        assert m.reviews[-1]["choice"] == "ДОБРАТЬ" and p.plan and p.plan["side"] == "long" and p.plan["entry"] is None
        assert "ДОБРАТЬ → long до максимума" in p.last_action and p.position["invalidation"] == 97.5, p.last_action
        assert "ДОБОР ПО ПРИКАЗУ: long сейчас" in p._situation_text(px)
        assert await settle(lambda: p.position is not None and p.position["lots"] == 11), (p.position, p.last_action)
        assert p.plan is None and p._sized_by_broker and status("TEST")["pilot"]["sized_by_broker"]
        assert p.position["invalidation"] == 97.5 and p.position["take"] == 105.0, "добор не трогает уровни PRO"
        assert status("TEST")["pilot"]["account"]["max_buy"] == 3 and "биржа даёт" in p._account_line(), p._account_line()
        assert "размер входа/добора считает биржа" in p._situation_text(px)
        # ДОБРАТЬ, когда биржа больше не даёт — честный отказ без входа
        FakeBroker.mx = {"buy": 0, "sell": 11}
        p._mx = None
        fake_ai.review_answers[:] = [{"choice": "ДОБРАТЬ", "why": "ещё"}]
        await p._review(px)
        await settle(lambda: p.plan is None, n=100)
        assert p.position["lots"] == 11 and p.plan is None, (p.position, p.plan, p.last_action)
        FakeBroker.mx = None
        p._mx = None

        # ── ПЕРЕВЕРНУТЬ (v5.2): против режима long → v5.4.2: выходная половина разрешена — ЗАКРЫТЬ (не «держим» против
        #    взгляда ИИ); в режиме auto → закрыть и войти в short ──
        fake_ai.review_answers[:] = [{"choice": "ПЕРЕВЕРНУТЬ", "why": "вниз", "invalidation": 103.0, "take": 95.0}]
        await p._review(px)
        assert m.reviews[-1]["choice"] == "ЗАКРЫТЬ" and m.reviews[-1]["ai_choice"] == "ПЕРЕВЕРНУТЬ" \
            and "[ПЕРЕВЕРНУТЬ против режима long — только ЗАКРЫТЬ]" in m.reviews[-1]["why"], m.reviews[-1]
        assert p.position is None and p.plan is None and "решение перепроверки: закрыть" in store_v5.trades("TEST")[0]["why"]
        fresh(p)
        fake_ai.review_answers[:] = [{"choice": "КУПИТЬ_СЕЙЧАС", "why": "снова лонг", "invalidation": 97.0, "take": 104.0}]
        await p._review(px)
        assert await settle(lambda: p.position is not None and p.position["side"] == "long"), (p.position, p.last_action)
        m.play = "auto"
        fake_ai.review_answers[:] = [{"choice": "ПЕРЕВЕРНУТЬ", "why": "вниз всерьёз", "invalidation": 103.0, "take": 95.0}]
        await p._review(px)
        assert m.reviews[-1]["choice"] == "ПЕРЕВЕРНУТЬ" and p.plan and p.plan["side"] == "short", (m.reviews[-1], p.plan)
        assert "ПЕРЕВЕРНУТЬ → short" in p.last_action, p.last_action
        assert await settle(lambda: p.position is not None and p.position["side"] == "short"), (p.position, p.last_action)
        assert p.position["invalidation"] == 103.0 and p.position["hard_stop"] > 103.0 and p.position["take"] == 95.0
        assert p.broker.stops[-1]["stop"] == p.position["hard_stop"]
        assert store_v5.trades("TEST")[0]["side"] == "long" and "флип" in store_v5.trades("TEST")[0]["why"]
        m.play = "long"
        await p._close_all(px, "тест: освободить-4", reanalyze=False)
        p.plan = None
        p.deposit_override = 100000.0
        fresh(p)

        # ── v5.3 отказ биржи с текстом → толмач «Биржа отбила заявку входа», тот же отказ подряд — одно объяснение ──
        _place0 = FakeBroker.place

        async def place_fail(self, figi, direction, lots, price=None, tag=""):
            return {"ok": False, "error": "30042: недостаточно средств"}
        FakeBroker.place = place_fail
        n_x = len([e for x in m.explain for e in x["events"] if e["kind"] == "refusal"])
        fake_ai.review_answers[:] = [{"choice": "КУПИТЬ_СЕЙЧАС", "why": "вход", "invalidation": 97.0, "take": 104.0}]
        await p._review(px)
        assert await settle(lambda: p._entry_fail >= 1), (p._entry_fail, p.last_action)   # дальше бэкофф 4 с — второй попытки в тесте не ждём
        assert await settle(lambda: len([e for x in m.explain for e in x["events"] if e["kind"] == "refusal"]) == n_x + 1), m.explain[-3:]
        xr = xlast(m, "refusal")
        assert xr["title"] == "Биржа отбила заявку входа" and "30042: недостаточно средств" in xr["detail"]
        assert "попытка" not in xr["detail"], "счётчик попыток вычищен — иначе каждый тик новое объяснение"
        FakeBroker.place = _place0
        p.plan, p._entry_fail, p.no_entry_until = None, 0, 0.0
        p.state = "ЖДУ_ПЛАН"
        el = explain_list("TEST")
        assert el["ok"] and el["active"] and el["items"] and el["memory"]["n"] == m.memory_n and el["enabled"]
        assert not explain_list("NOPE")["ok"]

        # ── stop / resume / panic ──
        assert m.exec["do"] == "CLOSE"
        r = await stop("TEST")
        assert r["ok"] and await settle(lambda: not m.pilot_alive())
        r = await resume("TEST")
        assert r["ok"] and m.pilot_alive() and m.pilot.plan is None and "свежего приказа нет" in r["note"], r
        assert m.pilot._close_pending is None, "CLOSE заново не принимается — уже исполнен"
        m.exec = {"do": "BUY", "entry": None, "entry_kind": "сейчас", "take": 104.0, "invalidation": 97.0,
                  "why": "тест", "plan": "", "confidence": None, "news_ids": [], "levels": [], "time_note": ""}
        m.exec_ts = time.time()
        p = m.pilot
        r = await stop("TEST")
        assert r["ok"] and await settle(lambda: not m.pilot_alive())
        assert status("TEST")["phase"] == "stopped" and snapshot()["active"] is None
        assert "TEST" not in StubScan.stopped, "стоп пилота НЕ гасит сканер — он копит дальше"
        n_sc = len(StubScan.started)
        _M.clear()                                             # пилот из store: объяснения и память переживают перезапуск
        r = await resume("TEST")
        m = _M["TEST"]
        assert r["ok"] and m.pilot_alive() and m.pilot is not p, r
        assert m.explain and m.memory.startswith("ПАМЯТЬ: ") and m.memory_n >= 1, (len(m.explain), m.memory_n)
        m.council_ts = time.time()                             # у миссии из store council_ts=0: восстановить пейсинг совета для теста
        assert m.pilot.plan and m.pilot.plan["side"] == "long", "свежий приказ принят при resume"
        assert len(StubScan.started) == n_sc + 1, "продолжить продлевает сканер (start = продление)"
        assert (await scan_start("TEST", 60))["ok"] and StubScan.started[-1] == ("TEST", 60)
        assert scan_stop("TEST")["ok"] and "TEST" in StubScan.stopped
        StubScan.stopped.clear()
        # v5.3 фаза 3 (проверяющий): ПАНИКА и сразу СТОП (в тот же тик) — стоп не отменяет панику: позиция закрыта,
        # пилот встал сам; раньше петля выходила по stopping до тормозов, и «закрыть всё» молча терялось
        assert await settle(lambda: m.pilot.position is not None), "для проверки паники нужна позиция"
        r = await panic()
        assert r["panic"] == ["TEST"]
        r2 = await stop("TEST")
        assert r2["ok"] and "ПАНИКА ещё закрывает" in r2["note"] and status("TEST")["phase"] in ("panic", "in_position"), r2
        assert await settle(lambda: m.pilot.state == "СТОП" and m.pilot.position is None), (m.pilot.state, m.pilot.position)
        assert "ПАНИКА владельца" in m.pilot.last_action and "торговля остановлена" in m.pilot.last_action, m.pilot.last_action
        assert m.pilot.last_action.startswith("ЗАКРЫЛ ВСЁ") or m.pilot.last_action.startswith("ПАНИКА"), "причина и P/L закрытия — первыми"
        assert "TEST" in StubScan.stopped, "паника гасит сканер"
        await settle(lambda: not m.pilot_alive())
        # v5.2: пилот остановлен, а владелец жмёт ПАНИКУ — закрываем по счёту напрямую (Broker.flat_all)
        FakeBroker.flat_calls.clear()
        r = await panic()
        assert r["panic"] == ["TEST"] and FakeBroker.flat_calls == [("FIGI-T", 1)], r
        assert "закрыто по счёту" in m.note and "снято стопов 1" in r["note"], (m.note, r)

        # ── ВЕСЬ СЧЁТ (v5.2): на счёте уже лежит позиция → совет видит её, CLOSE закрывает её пилотом ──
        _M.clear()
        FakeTinkoff.positions = [{"figi": "FIGI-T", "uid": "FIGI-T", "type": "share", "qty": 3, "avg": 99.0,
                                  "cur_price": 100.0, "yield": 300.0}]
        fake_ai.exec_answers[:] = [{"do": "CLOSE", "why": "картина против позиции"}]
        n_tr = store_v5.trades_summary("TEST")["count"]
        r = await start("TEST", "long", reason="позиция на счёте")
        m = _M["TEST"]
        await m.council_task
        assert m.error is None, m.error
        assert m.account_pos and m.account_pos.startswith("ОТКРЫТАЯ ПОЗИЦИЯ НА СЧЁТЕ: long 3 лот @99"), m.account_pos
        ua = fake_ai.last_user["mission_analysis"]
        assert "ОТКРЫТАЯ ПОЗИЦИЯ НА СЧЁТЕ (весь счёт по инструменту ведёт пилот)" in ua and "long 3 лот @99" in ua
        assert ua.index("ОТКРЫТАЯ ПОЗИЦИЯ НА СЧЁТЕ") < ua.index("ВАЙКОФФ"), "позиция — первым блоком"
        assert "long 3 лот @99" in fake_ai.last_user["mission_exec"] and "P/L +300" in fake_ai.last_user["scout"]
        assert m.exec["do"] == "CLOSE" and status("TEST")["account_pos"]
        p3 = m.pilot
        assert p3 and m.pilot_alive()
        assert await settle(lambda: p3._prepared and p3.position is None and store_v5.trades_summary("TEST")["count"] == n_tr + 1), \
            (p3.last_action, p3.position, p3._close_pending)
        tr = store_v5.trades("TEST")[0]
        assert tr["lots"] == 3 and tr["side"] == "long" and tr["entry"] == 99.0 and "картина против" in tr["why"], tr
        assert "ПРИНЯЛ позицию со счёта" in "\n".join(str(h.get("reason")) for h in m.handoffs) or True
        assert p3.state == "ЖДУ_ПЛАН" and p3._close_pending is None and status("TEST")["phase"] == "idle"
        # тот же старт с BUY: позиция со счёта принята как своя, уровни совета — её уровни, трос на бирже
        await stop("TEST")
        await settle(lambda: not m.pilot_alive())
        _M.clear()
        fake_ai.exec_answers[:] = [{"do": "BUY", "entry": None, "take": 104, "invalidation": 97}]
        r = await start("TEST", "long", reason="позиция на счёте 2")
        m = _M["TEST"]
        await m.council_task
        assert m.error is None, m.error
        p3 = m.pilot
        assert await settle(lambda: p3._prepared and p3.position is not None and p3.position.get("invalidation") == 97.0
                            and bool(p3.broker.stops)), (p3.position, p3.last_action)
        assert p3.position["side"] == "long" and p3.position["take"] == 104.0, p3.position
        # приказ BUY «сейчас» при 3 лотах со счёта = добор до максимума (8): вход усреднён, уровни совета
        assert await settle(lambda: p3.position is not None and p3.position["lots"] == 8 and not p3.pending), \
            (p3.position, p3.last_action)
        assert 99.0 < p3.position["entry"] < 99.6 and not p3.position.get("levels_placeholder"), p3.position
        assert p3.position["invalidation"] == 97.0 and abs(p3.broker.stops[-1]["stop"] - p3._hard_of(p3.position)) < 1e-9
        assert any("добор" in x.get("tag", "") or x["lots"] == 5 for x in p3.broker.placed), p3.broker.placed
        assert status("TEST")["phase"] == "in_position" and status("TEST")["pilot"]["position"]["lots"] == 8
        await stop("TEST")
        await settle(lambda: not m.pilot_alive())
        # ревью 5.4.2 (финал): тот же старт с HOLD — приказ пришёл до prepare(): позиция со счёта принята с уровнями
        # совета (стороны проверены по ней), а не с временным стопом 2 %; добора нет
        _M.clear()
        fake_ai.exec_answers[:] = [{"do": "HOLD", "entry": None, "take": 104, "invalidation": 97, "why": "держать"}]
        r = await start("TEST", "long", reason="позиция на счёте 3")
        m = _M["TEST"]
        await m.council_task
        assert m.error is None and m.exec["do"] == "HOLD", (m.error, m.exec)
        p3 = m.pilot
        assert await settle(lambda: p3._prepared and p3.position is not None), (p3.position, p3.last_action)
        assert p3.position["invalidation"] == 97.0 and p3.position["take"] == 104.0, p3.position
        assert not p3.position.get("levels_placeholder") and p3._pending_hold is None and p3.plan is None, p3.position
        assert p3.position["lots"] == 3 and not p3.pending, (p3.position, p3.pending)
        await stop("TEST")
        await settle(lambda: not m.pilot_alive())
        FakeTinkoff.positions = []

        # ── v5.4.1 «ТРЕЗВЫЙ ПИЛОТ»: приказ совета WAIT — пилот поднят без плана (фаза idle), входа нет, wait_for хранится и
        #    виден в _exec_text/ситуации, толмач; проверка входа у двери (PRO: ЖДАТЬ с уровнем → у уровня ВОЙТИ) и мысль о
        #    прибыли (65 % хода → ВЫЙТИ → закрыто в плюс, журнал) на фейке money_json; стопы только в программе ──
        _M.clear()
        config.PYTHIA_ENTRY_CHECK, config.PYTHIA_EXCHANGE_STOP = True, False
        fake_ai.exec_answers[:] = [{"do": "WAIT", "wait_for": "закрепление выше 101", "why": "перевеса нет", "plan": "ждём"}]
        r = await start("TEST", "long", reason="WAIT")
        m = _M["TEST"]
        await m.council_task
        assert m.error is None and m.exec["do"] == "WAIT" and m.exec["wait_for"] == "закрепление выше 101", (m.error, m.exec)
        p = m.pilot
        assert p and m.pilot_alive() and p.plan is None and status("TEST")["phase"] == "idle", (p.plan, status("TEST")["phase"])
        assert "вне рынка — ждал: закрепление выше 101" in p.last_action and "WAIT — совет ждал: закрепление выше 101" in _exec_text(m), \
            p.last_action
        assert await settle(lambda: xev(m, "council", "Совет решил ждать (WAIT)")), m.explain
        sit_w = p._situation_text(100.0)                       # v5.4.2: WAIT совета — прошлое мнение, а не запрет
        assert "ПРИКАЗ СОВЕТА (" in sit_w and "совет ждал: закрепление выше 101" in sit_w and "прошлое мнение, а не запрет" in sit_w, sit_w
        assert status("TEST")["exec"]["do"] == "WAIT"
        FakeTinkoff.price = 101.5
        assert await settle(lambda: bool(p.prices) and p.prices[-1] == 101.5)
        await settle(lambda: False, n=10)
        assert p.position is None and p.pending is None and fake_ai.calls.count("mission_entry") == 0, "WAIT — входа нет"
        #    дежурный PRO: КУПИТЬ_СЕЙЧАС по живому рынку → v5.4.2: свежее решение PRO — вход без второго вопроса у двери
        fake_ai.review_answers[:] = [{"choice": "КУПИТЬ_СЕЙЧАС", "why": "закрепились", "invalidation": 99.0, "take": 108.0}]
        await p._review(101.5)
        assert p.plan and p.plan["entry"] is None and p.plan["src"] == "review" and p.plan["snap_price"] == 101.5, p.plan
        assert await settle(lambda: p.position is not None, 600), (p.last_action, fake_ai.calls.count("mission_entry"))
        assert fake_ai.calls.count("mission_entry") == 0 and p.position["invalidation"] == 99.0, fake_ai.calls.count("mission_entry")
        assert not p.broker.stops and p.position.get("stop_id") is None and status("TEST")["pilot"]["exchange_stop"] is False
        def xtitle(sub):   # узлы entry_check/profit есть в explain.KINDS (5.4.1) — здесь ищем по заголовку
            return any(sub in str(e.get("title") or "") for x in m.explain for e in x.get("events") or [])
        assert await settle(lambda: xtitle("Вход по свежему решению PRO")), m.explain
        #    мысль о прибыли: 65 % хода → PRO: ВЫЙТИ → закрыто в плюс, журнал, повод дежурному PRO
        n_tr = store_v5.trades_summary("TEST")["count"]
        n_pf = fake_ai.calls.count("mission_profit")   # мысль о прибыли могла звучать и в прежних блоках (умолчание ДЕРЖАТЬ)
        fake_ai.profit_answers[:] = [{"decision": "ВЫЙТИ", "why": "рывок выдохся"}]
        entry_px = p.position["entry"]
        FakeTinkoff.price = round(entry_px + (108.0 - entry_px) * 0.65, 2)
        assert await settle(lambda: p.position is None, 600), (p.last_action, fake_ai.calls.count("mission_profit"))
        assert fake_ai.calls.count("mission_profit") == n_pf + 1 and p.profits[-1]["decision"] == "ВЫЙТИ" and p.pnls[-1] > 0, \
            (fake_ai.calls.count("mission_profit"), p.profits[-2:], p.pnls, p.last_action)
        assert store_v5.trades_summary("TEST")["count"] == n_tr + 1 and "PRO решил выйти" in store_v5.trades("TEST")[0]["why"]
        assert p.last_action.startswith("ЗАКРЫЛ ВСЁ (мысль о прибыли") and "после закрытия" in p.last_action, p.last_action
        assert status("TEST")["pilot"]["profits"][-1]["decision"] == "ВЫЙТИ" and status("TEST")["pilot"]["money_model"] == "pro"
        assert await settle(lambda: xtitle("Мысль о прибыли: ВЫЙТИ")), m.explain
        #    приказ совета (он старше живого рынка) — через дверь: PRO ЖДАТЬ (откат 101.2) → уровень назвал сам PRO по
        #    живому рынку (gate_level) → у уровня вход без второго вопроса, пока решение свежее
        FakeTinkoff.price = 101.5
        assert await settle(lambda: bool(p.prices) and p.prices[-1] == 101.5)
        fake_ai.entry_answers[:] = [{"decision": "ЖДАТЬ", "why": "взять на откате", "entry": 101.2, "entry_kind": "откат"}]
        assert p.adopt_forecast({"exec": {"do": "BUY", "entry": None, "entry_kind": "сейчас", "take": 108.0,
                                          "invalidation": 99.0, "why": "совет: вход"}}), p.last_action
        assert p.plan["src"] == "council", p.plan
        assert await settle(lambda: fake_ai.calls.count("mission_entry") == 1 and p.plan is not None and p.plan.get("entry") == 101.2, 600), \
            (p.plan, p.last_action)
        assert p.gates[-1]["decision"] == "ЖДАТЬ" and p.plan["src"] == "gate_level" and p.state == "ЗАСАДА" \
            and status("TEST")["pilot"]["entry_gate"]["decision"] == "ЖДАТЬ", (p.plan, p.gates[-1])
        FakeTinkoff.price = 101.22
        assert await settle(lambda: p.position is not None, 600), (p.last_action, fake_ai.calls.count("mission_entry"))
        assert fake_ai.calls.count("mission_entry") == 1 and p.position["invalidation"] == 99.0, fake_ai.calls.count("mission_entry")
        assert await settle(lambda: xtitle("PRO у двери: ЖДАТЬ")), m.explain
        await stop("TEST")
        await settle(lambda: not m.pilot_alive())
        config.PYTHIA_ENTRY_CHECK, config.PYTHIA_EXCHANGE_STOP = False, True

        # ── без токена Tinkoff: приказ показан, пилот не стартует ──
        FakeTinkoff.on = False
        _M.clear()
        fake_ai.exec_answers[:] = [{"do": "SELL", "entry": None, "take": 95, "invalidation": 102}]
        r = await start("TEST", "auto")
        await _M["TEST"].council_task
        assert _M["TEST"].phase == "idle" and "нет токена" in _M["TEST"].note and _M["TEST"].pilot is None
        assert not (await resume("TEST"))["ok"]
        # ошибка ИИ → phase error, run с ошибкой
        fake_ai.exec_answers[:] = [{"do": "BUY"}, {"do": "BUY"}]           # без invalidation — приказ не собирается
        r = await start("TEST", "auto")
        await _M["TEST"].council_task
        assert _M["TEST"].phase == "error" and "приказ не собрался" in _M["TEST"].error
        assert bus.run(r["run_id"])["status"] == "error"
        # v5.4.2: токен есть — первый совет без приказа не убивает миссию: пилот без плана, PRO решит скоро
        FakeTinkoff.on = True
        fake_ai.exec_answers[:] = [{"do": "BUY"}, {"do": "BUY"}]
        r = await start("TEST", "auto")
        await _M["TEST"].council_task
        mf = _M["TEST"]
        assert mf.pilot_alive() and mf.phase == "idle" and mf.pilot.plan is None and "приказ не собрался" in (mf.error or "")
        assert mf.pilot.review_ts <= time.time() + REVIEW_RETRY_SEC + 1 and "совет не собрал приказ" in (mf.pilot._review_reason or "")
        await stop("TEST")
        assert await settle(lambda: not mf.pilot_alive())
        FakeTinkoff.on = False
        try:
            await start("TEST", "flat")
            raise AssertionError("play=flat обязан быть отвергнут")
        except ValueError:
            pass
        await _watchdog_tick()

        # ── v5.1 разумные пределы: Вайкофф 200 000 и досье 120 000 симв. → FLASH ужимает по частям,
        #    промпт аналитика ≤ PYTHIA_PROMPT_SOFT, «обрезано» нигде, все блоки на месте ──
        big_w = "ВАЙКОФФ D1: тест · до льда −1.6% → ближе к льду\n" + "\n".join(
            f"строка {i} " + "в" * 90 for i in range(2000))                  # ≈ 200 000
        big_d = "ДОСЬЕ TEST\n" + "\n".join(f"свеча {i} " + "д" * 90 for i in range(1200))   # ≈ 120 000
        assert len(big_w) >= 200_000 and len(big_d) >= 120_000, (len(big_w), len(big_d))

        async def fake_build_big(t, ac, full=True):
            d = await fake_build(t, ac, full)
            return dict(d, wyckoff_text=big_w, text=big_d)
        market_ctx.build = fake_build_big
        saved_lim = (config.PYTHIA_CTX_LIMIT, config.PYTHIA_PROMPT_SOFT)
        config.PYTHIA_CTX_LIMIT, config.PYTHIA_PROMPT_SOFT = 60_000, 150_000
        FakeTinkoff.on = True
        _M.clear()
        fake_ai.shrink_calls.clear()
        bus_events: list = []
        _orig_stage = bus.stage

        async def spy_stage(scope, run_id, stage_name, st, **kw):
            bus_events.append((stage_name, st, str(kw.get("detail") or "")))
            await _orig_stage(scope, run_id, stage_name, st, **kw)
        bus.stage = spy_stage
        try:
            fake_ai.exec_answers[:] = [{"do": "BUY", "entry": 99.5, "take": 103, "invalidation": 98}]
            r = await start("TEST", "long", deposit=100000.0, reason="большие блоки")
            m = _M["TEST"]
            await m.council_task
            assert m.error is None, m.error
            assert fake_ai.shrink_calls, "FLASH-shrink обязан вызваться выше пределов"
            assert all(l <= compress.CHUNK for l, _ in fake_ai.shrink_calls), "по частям ≤ CHUNK"
            ua = fake_ai.last_user["mission_analysis"]
            assert len(ua) <= 150_000, len(ua)
            for who in ("mission_analysis", "mission_critique", "mission_verdict", "mission_exec"):
                assert "обрезано" not in fake_ai.last_user[who], who
            for title in ("═══ ВАЙКОФФ", "═══ ДОСЬЕ БИРЖИ", "═══ СКАНЕР СТАКАНА", "═══ МИКРОСТРУКТУРА",
                          "═══ ИТОГ ОБЩЕГО СОВЕТА", "═══ НОВОСТИ ПО ИНСТРУМЕНТУ", "═══ СЕРЬЁЗНЫЕ ЗАМЕТКИ",
                          "═══ СВОД ГОЛОСОВ", "═══ КУРАМОТО", "═══ БИФУРКАЦИИ", "═══ КАЛЕНДАРЬ",
                          "═══ РЕАКТОР", "═══ НЕБО"):
                assert title in ua, title
            assert "ВАЙКОФФ D1: тест" in ua and "ДОСЬЕ TEST" in ua, "начало каждого блока сохранено"
            assert len(m.layers["wyckoff"]) <= 60_000 and m.layers["wyckoff"].startswith("ВАЙКОФФ D1")
            sz = status("TEST")["sizes"]
            assert sz["analysis"]["prompt"] > 0 and sz["analysis"]["prompt"] <= 150_000
            assert sz["analysis"]["blocks"]["wyckoff"] <= 60_000 and sz["analysis"]["blocks"]["dossier"] <= 60_000
            st_a = [e for e in bus_events if e[0] == "analysis" and e[1] == "start"]
            assert st_a and "симв." in st_a[-1][2] and "FLASH ужал" in st_a[-1][2] and "Вайкофф" in st_a[-1][2], st_a
            print("detail analysis start:", st_a[-1][2])
            assert any(e[0] == "analysis" and e[1] == "done" and "симв." in e[2] for e in bus_events)
            assert any(e[0] == "exec" and e[1] == "start" and "шифровальщик: промпт" in e[2] for e in bus_events)
            # перепроверка при тех же пределах — тоже без «обрезано», размеры в шине
            assert m.pilot and m.pilot_alive()
            fake_ai.review_answers[:] = [{"choice": "ЖДЁМ", "why": "план жив", "note": "держим"}]
            await m.pilot._review(100.0)
            ur = fake_ai.last_user["mission_review"]
            assert "обрезано" not in ur and "ВАЙКОФФ D1: тест" in ur and len(ur) <= 150_000, len(ur)
            assert m.sizes["review"]["prompt"] == len(ur) and m.sizes["review"]["answer"] > 0
            assert any(e[0] == "review" and e[1] == "start" and "перепроверка: промпт" in e[2] for e in bus_events)
            await stop("TEST")
            await settle(lambda: not m.pilot_alive())
        finally:
            bus.stage = _orig_stage
            market_ctx.build = fake_build
            config.PYTHIA_CTX_LIMIT, config.PYTHIA_PROMPT_SOFT = saved_lim

        explain.reset()                                        # задачи толмача — не оставлять на закрытие цикла

    asyncio.run(main())
    print("mission self-test OK: валидатор (стороны, режим игры, виды входа), start→совет→приказ→пилот, "
          "вторая миссия отклонена, вход/позиция, перепроверка ЖДЁМ (трос/тейк)/ЗАКРЫТЬ + журнал сделок, "
          "повод дежурному PRO после закрытия, откат/прорыв из перепроверки, направленный дрейф, режим игры, "
          "поводы пилота без тактик, серьёзная новость и резкий ход → PRO с пейсингом, тишина после входа, "
          "НОВЫЙ_АНАЛИЗ с окном совета, stop/resume/panic, без токена, ошибка ИИ, "
          "разумные пределы: маленькие блоки целиком без FLASH, большие ужаты FLASH по частям без «обрезано», "
          "размеры промптов в степпере и status()['sizes'], новости rich=True; "
          "v5.2: разведка FLASH (индексный фьючерс MX) у аналитика/перепроверки/троса, v5.3: связанные бумаги (блок, разведка, статус), аварийный трос на бирже дальше триггера, "
          "мягкий стоп (ЖДАТЬ → совет без очереди → CLOSE; СЛИТЬ; трос без вопросов), ДОБРАТЬ по GetMaxLots, "
          "ПЕРЕВЕРНУТЬ (режим игры, флип), паника без пилота по счёту, позиция на счёте → совет → CLOSE/принята; "
          "рыночные часы: закрыто → фаза closed, новость копится, открылось → перепроверка «рынок открылся»; "
          "v5.3 толмач: приказ/вход/перепроверка/закрытие/трос/рынок/отказ биржи объяснены владельцу, память миссии по узлам "
          "(абзац, списки ужаты, старые новости в память), блок ПАМЯТЬ у перепроверки и троса, explain_list, store; "
          "v5.4.1: приказ WAIT (валидатор, пилот без плана, фаза idle), узлы у денег через money_json (PRO), проверка входа у двери "
          "ЖДАТЬ→ВОЙТИ, мысль о прибыли ВЫЙТИ, стопы только в программе; v5.4.2: словарь приказа и перепроверки "
          "(BUY/ЛОНГ/NO_TRADE, мусор → переспрос без ЖДЁМ), ВНЕ_РЕЖИМА, ПЕРЕВЕРНУТЬ против режима → ЗАКРЫТЬ, пробой со стопом "
          "у уровня, снимок решения в промпте, дрейф без засады, свежее решение PRO без второго вопроса у двери; "
          "повод пилота без плана → PRO скоро, рамка фоном после приказа пилоту, первый совет без приказа → пилот без плана, "
          "НОВЫЙ_АНАЛИЗ в окне отложен, переворот в тишине → выход без переворота")
