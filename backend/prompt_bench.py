# -*- coding: utf-8 -*-
"""ПИФИЯ v5.4.3 — СТЕНД ПРОМПТОВ: «как DeepSeek торгует сам».

Воля владельца (30.09.2026): «всё ещё играет ответами ждать… создай иллюзорные ситуации как на бирже и дай итог».
Стенд берёт НАСТОЯЩИЕ промпты миссии (prompts_mission.review / entry_check / profit_think / verdict), подаёт им
14 выдуманных, но согласованных биржевых ситуаций (SBER ≈ 300 ₽, режим игры auto) и спрашивает ИИ теми же вызовами,
что боевой код (mission.py): перепроверка — ai_v5.pro_json(route="mission_review"), дверь и мысль о прибыли —
ai_v5.money_json(route="mission_entry"/"mission_profit", attempt_timeout=…), вердикт — ai_v5.pro_text(route=
"mission_verdict") (в бою — pro_stream того же маршрута; стрим стенду не нужен). Слово решения разбирает тот же
ai_v5.decision_of по словарям узлов (review_table / door_table / profit_table); у вердикта (свободный текст) —
первое слово решения из exec_table в первых строках ответа (строки «Вердикт/Решение/Сторона/Итог» — первыми).
Итог — таблица: сколько раз ИИ действует (вход, добор, переворот, выход), а сколько ждёт, в одинаковых
воспроизводимых ситуациях.

5.4.3: стенд даёт модели РОВНО то, что дал бы боевой код в той же ситуации. Каждая ситуация — сцена (Scene) на
настоящих mission.Mission + MissionPilot (брокер-пустышка, без state-файла и базы), время заморожено (frozen): приказ
совета проходит mission._validate_exec и adopt_forecast; история сцены (тики цены) идёт через настоящие наблюдатели
тика — _shock_watch (окно хода цены, резкий ход), _wait_watch (уровни будильника WAIT), _flat_track («вне рынка с»),
_wake_watch (будильник ЖДЁМ); прокол сканера — настоящий _puncture_watch (в позиции — через _apply_triage), ответ у
двери — настоящий _apply_gate, повод «серьёзная новость» — настоящий _ask_review; ответ перепроверки — настоящие
MissionPilot._review_answered и _review_record (ревью 5.4.3 после F1: запись с in_pos/pos_side, метка модели в
last_review, ДЕРЖАТЬ в позиции — _retune, будильник — _wake_after_review, серия ЖДЁМ — m.wait_streak через
_streak_after_review, цена за серию — по тикам _flat_track). Из боевого кода идут: ситуация пилота (_review_situation /
_situation_for_ai: ПРИКАЗ СОВЕТА / ВХОД ВЗВЕДЁН / ПРОБОЙ ПРОЙДЕН / Прошлая перепроверка с ценой и меткой модели / ЖДЁМ
подряд / БУДИЛЬНИК / Последний полный совет: приказ пришёл …), приказ (_exec_text), план и прошлые ответы у двери
(_plan_text, _gates_text), ход цены (_history_text), прошлые ответы у троса/тейка и мысли о прибыли, повод и план мысли о
прибыли (_profit_trigger, _profit_plan_text), новости перепроверки / двери / совета (_gather_news / _news_quick /
_news_for над newsflow.render; свежая новость по тикеру — один раз), заметки дозора (_watch_text), итог общего совета
(_council_text → council.summary_text), отчёт сканера (_scan_text → maya_scan.render_for_ai), блок prev совета
(_prev_text), связанные бумаги (correlate.text), Вайкофф (market_ctx.render_wyckoff; перепроверка видит слой времени
совета — m.layers — под подписью «на момент совета HH:MM, цена тогда P» и со строкой расстояний от текущей цены:
_review_wyckoff по m.wy_at, как в бою). Вручную, в формате источника: шапка живого рынка (market_ctx.light: цена, стакан, стены,
лента, крупные; рентген и Майя — настоящие microstructure.classify + _xray_line / _maya_line), разведка (scout.fetch),
досье (market_ctx.build), память миссии (абзац FLASH по правилам explain 5.4.3 — «в чью пользу»), анализ и критика
(тексты PRO). Склейку боевых узлов стенд не повторяет на веру: combat_prompt(id) прогоняет настоящие
MissionPilot._review / _entry_check / _profit_think на данных сцены (подменено только внешнее: живой рынок, разведка,
связанные бумаги, небо, шина; вызов ИИ перехвачен) и сверяет промпт со стендом байт в байт — self-тест и
tests/test_prompt_bench.py. Ручки механики сцен (прокол, резкий ход, трос в программе, триаж, дверь, мысль о прибыли —
SCENE_KNOBS) на время сборки сцен — умолчания: ситуации те же при любых настройках владельца; ручки промптов и вызовов
(PYTHIA_REVIEW_SEC, PYTHIA_COUNCIL_GAP_SEC, PYTHIA_ENTRY_TIMEOUT_SEC, PYTHIA_MONEY_MODEL …) — живые, как в бою. Числа в
ситуациях выдуманы (закон 2 о боевом коде здесь не нарушается: это стенд, в бой ничего не течёт); expect у ситуации —
только для чтения отчёта, не для подгонки.

Запуск (одно правило: без аргументов — self-тест, флаг прогона — прогон):
  python3 -m backend.prompt_bench                  self-тест на фейковом ИИ (asserts, без сети и ключей) → «self-test OK»
  python3 -m backend.prompt_bench --selftest       то же явно
  python3 -m backend.prompt_bench --list           список ситуаций
  python3 -m backend.prompt_bench --dump DIR       промпты в DIR (<id>.system.txt / <id>.user.txt), ИИ не зовётся
  python3 -m backend.prompt_bench --run            прогон: 14 ситуаций × 1 ответ
      [--runs N] [--only id1,id2] [--nodes review_flat,entry] [--out отчёт.md] [--json отчёт.json]
      [--par 3] [--timeout S]                     любой из этих флагов тоже означает прогон
Прогон без ключа DeepSeek и без PYTHIA_MOCK_AI=1 — «нет ключа DeepSeek: …» и код выхода 2. С PYTHIA_MOCK_AI=1 ИИ
подменяется ответами backend/mock_ai.py (как во всей системе; рынок стенду не нужен — подменяется только ИИ, база
мока не создаётся). Стенд сам ничего не пишет в data/ — только туда, куда указали --out / --json / --dump (импорт
mission поднимает схему data/pythia_v5.db — штатно, как у любого модуля над store_v5).
Доли в отчёте считаются от ответов с решением: молчание и неразобранный ответ — не решение ИИ (закон 3), они — «молч.».
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import (ai_pilot, ai_v5, astro, bus, config, correlate, hawkes, market_clock, market_ctx, microstructure,
               mission, newsflow, trader_risk)
from . import council as council_mod
from . import prompts_mission as pm

log = logging.getLogger("pythia.prompt_bench")

# ── сцена ────────────────────────────────────────────────────────────────────────────────────────────────
TICKER, NAME, PLAY = "SBER", "Сбербанк", "auto"
LOT = 10                                    # акций в лоте SBER (у акции point_value пилота = лот)
DEPOSIT = 100_000.0
N30 = int(1800 / ai_pilot.TICK_SEC)         # тиков за ≈30 мин: окно «Ход за окно наблюдения» и «За ≈30 мин»
NORM_BPS = 3.0                              # норма спреда market_ctx (NORM_SPREAD_MIN_BPS) — вход рентгена
SCAN_TICK_S = 3.0                           # maya_scan.SCAN_INTERVAL_S по умолчанию
SCAN_MIN = 480                              # сканер «8 ч» (PYTHIA_SCAN_MIN): стартует с советом, повторный совет продлевает
BOOK_DEPTH = 50                             # market_ctx.LIGHT_DEPTH
MARKET_ST = {"open": True, "reason": "торги идут", "session": "основная"}
MARKET = market_clock.describe(MARKET_ST)
NEWS_EMPTY = "(свежих новостей нет или сбор недоступен — НЕ выдумывай их, решай по цене и плану)"   # MissionPilot._review

# ручки механики сцен (что наблюдатели и учёт считают поводом и уровнем; трос — в программе, как с 5.4.1) — на время
# сборки сцен и сверки с боем умолчания config: ситуации одни и те же при любых настройках владельца
SCENE_KNOBS: dict[str, Any] = dict(
    config.FREE_PILOT_DEFAULTS,
    PYTHIA_PUNCTURE=True, PYTHIA_PUNCTURE_MIN=0.6, PYTHIA_PUNCTURE_COOL_SEC=600, PYTHIA_SHOCK_PCT=1.0,
    PYTHIA_SHOCK_WIN_SEC=600, PYTHIA_EVENT_COOL_SEC=900, PYTHIA_QUIET_SEC=900, PYTHIA_SOFT_STOP=True,
    PYTHIA_HARD_STOP_PCT=1.5, PYTHIA_EXCHANGE_STOP=False, PYTHIA_EVENT_TRIAGE=True, PYTHIA_ENTRY_CHECK=True,
    PYTHIA_ENTRY_CHECK_COOL_SEC=300, PYTHIA_PROFIT_THINK=True, PYTHIA_PROFIT_THINK_PCT=60.0,
    PYTHIA_PROFIT_THINK_MIN_PCT=1.0)

NODES = ("review_flat", "review_pos", "entry", "profit", "verdict")
JSON_NODES = ("review_flat", "review_pos", "entry", "profit")
COMBAT_NODES = JSON_NODES                   # узлы, чей промпт стенд сверяет с настоящим узлом mission.py
KINDS = ("action", "exit", "wait", "hold", "council", "silent")
EXPECTS = ("action", "wait_ok", "exit_ok")
EXPECT_TEXT = {"action": "разумно действовать", "wait_ok": "ждать разумно", "exit_ok": "разумен выход"}
KIND_TEXT = {"action": "действие", "exit": "выход", "wait": "ждать", "hold": "держать", "council": "совет",
             "silent": "молчит"}

REVIEW_TIMEOUT = 1200.0                     # mission._review: asyncio.wait_for(pro_json, 1200)
VERDICT_TIMEOUT = 1800.0                    # в бою вердикт — стрим без внешнего срока; стенду нужен потолок
PAR = 3                                     # параллельных вызовов (как ai_v5.SEM_MONEY)
VERDICT_SCAN_LINES = 15                     # в скольких первых строках вердикта ищем слово решения
NO_KEY = "нет ключа DeepSeek: задай ключ в панели или PYTHIA_MOCK_AI=1 для сухого прогона"


# ── время (фиксированное: стенд воспроизводим) ────────────────────────────────────────────────────────────
def _dt(hhmm: str, day: int = 30) -> datetime:
    x = [int(v) for v in hhmm.split(":")] + [0]
    return datetime(2026, 9, day, x[0], x[1], x[2], tzinfo=ai_v5.MSK)


def ts(hhmm: str, day: int = 30) -> float:
    """Метка времени момента «ЧЧ:ММ» (или «ЧЧ:ММ:СС») МСК 30.09.2026; day — другой день сентября."""
    return _dt(hhmm, day).timestamp()


def msk(hhmm: str, day: int = 30) -> str:
    """«30.09.2026 11:20 МСК, среда» — как ai_v5.now_msk_str()."""
    return ai_v5.now_msk_str(_dt(hhmm, day))


class _Clock:
    """time для боевых модулей: time() — замороженный момент, всё остальное — настоящий модуль time."""

    def __init__(self, t: float):
        self._t = float(t)

    def time(self) -> float:
        return self._t

    def __getattr__(self, name: str):
        return getattr(time, name)


class frozen:
    """Контекст: mission / ai_pilot / council видят time.time() = t, ai_v5.fmt_ts — без сдвига к часам биржи
    (_skew_s = 0): «N мин назад», «ЧЧ:ММ» и «протухнет через …» в текстах боевого кода воспроизводимы байт в байт."""

    def __init__(self, t: float):
        self.t = float(t)
        self._saved: list[tuple[Any, str, Any]] = []

    def __enter__(self) -> "frozen":
        clock = _Clock(self.t)
        for mod in (mission, ai_pilot, council_mod):
            self._saved.append((mod, "time", mod.time))
            mod.time = clock
        self._saved.append((ai_v5, "_skew_s", ai_v5._skew_s))
        ai_v5._skew_s = lambda: 0.0
        return self

    def __exit__(self, *exc) -> bool:
        while self._saved:
            mod, name, val = self._saved.pop()
            setattr(mod, name, val)
        return False


class _patched:
    """Контекст: (объект, имя, значение) — подменить на время, на выходе вернуть как было (в обратном порядке)."""

    def __init__(self, pairs: list[tuple[Any, str, Any]]):
        self.pairs = pairs
        self._saved: list[tuple[Any, str, Any]] = []

    def __enter__(self) -> "_patched":
        for obj, name, val in self.pairs:
            self._saved.append((obj, name, getattr(obj, name, None)))
            setattr(obj, name, val)
        return self

    def __exit__(self, *exc) -> bool:
        while self._saved:
            obj, name, val = self._saved.pop()
            setattr(obj, name, val)
        return False


def _drive(coro):
    """Корутина боевого кода, которой на данных сцены нечего ждать, — до конца без цикла событий. Если она всё же
    ждёт (сеть, таймер), это ошибка сборки сцены: стенд в сеть не ходит."""
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    coro.close()
    raise RuntimeError("боевой код ждёт сеть или таймер — сцена так не собирается")


# ══ форматтеры источников, которых mission.py не собирает сам ═══════════════════════════════════════════════
def _who(ar: float) -> str:
    return "покупатели" if ar > 0.55 else "продавцы" if ar < 0.45 else "паритет"


def light_text(price: float, *, at: str, close: float, bid: float, ask: float, bid_vol: int, ask_vol: int,
               buy: int, sell: int, count: int, big: tuple = (), wall_bid: tuple | None = None,
               wall_ask: tuple | None = None, my: dict | None = None) -> str:
    """Блок ЖИВОЙ РЫНОК — формат market_ctx.light (путь Tinkoff): шапка, стакан, стены, лента 15 мин, крупные;
    рентген — настоящий microstructure.classify с теми же входами, что даёт light() (OBI, CVD, VPIN ленты, Казимир,
    Хёрст 0.5, отмен нет) → market_ctx._xray_line; Майя — market_ctx._maya_line."""
    spread = round(ask - bid, 2)
    bps = round(spread / price * 1e4, 2)
    imb = (bid_vol - ask_vol) / (bid_vol + ask_vol)
    L = [f"{TICKER}: цена {price} ({msk(at)}), к закрытию {(price / close - 1) * 100:+.2f}%",
         f"Стакан({BOOK_DEPTH}): bid {bid} / ask {ask}, спред {spread} ({bps} bps), объём bid {bid_vol} / "
         f"ask {ask_vol}, дисбаланс {imb:+.2f}"]
    if wall_bid or wall_ask:
        L.append("Стены: " + ", ".join(x for x in ((f"bid {wall_bid[0]}×{wall_bid[1]}" if wall_bid else ""),
                                                    (f"ask {wall_ask[0]}×{wall_ask[1]}" if wall_ask else "")) if x))
    tot = buy + sell
    ar = buy / tot
    L.append(f"Лента 15 мин: {count} сделок, объём {tot}, агрессор — {_who(ar)} ({ar * 100:.0f}% покупок), "
             f"дельта {buy - sell:+d}, средняя сделка {round(tot / count, 1)}")
    if big:
        L.append("Крупные: " + ", ".join(f"{s} {q}@{p}" for s, q, p in big[:3]))
    feats = {"obi": imb, "cvd": float(buy - sell), "vpin": abs(buy - sell) / tot, "cancel_vel": 0.0,
             "casimir": microstructure.casimir(bps, NORM_BPS), "hurst": 0.5, "spread_bad": bps > NORM_BPS * 3.0}
    xr = microstructure.classify(feats)
    xr["available"] = True
    L.append(market_ctx._xray_line(xr, {"spread_bps": bps}, NORM_BPS))
    L.append(market_ctx._maya_line(dict(my, available=True) if my else None))
    return "\n".join(L)


def maya(pull: str | None, up_dn: tuple[float, float], *, ar: float, vac_up: tuple | None = None,
         vac_dn: tuple | None = None, wall_ask: tuple | None = None, wall_bid: tuple | None = None) -> dict:
    """Словарь maya.analyze: тяга, вакуум (p_from, p_to, depth, уровней от лучшей), стены (p, ×медианы, лотов),
    агрессор ленты — по правилам maya.analyze (0.55 / 0.45, согласие с тягой)."""
    who = "агрессор — покупатель" if ar > 0.55 else "агрессор — продавец" if ar < 0.45 else "агрессия сбалансирована"
    agree = (ar > 0.55) if pull == "вверх" else (ar < 0.45) if pull == "вниз" else None
    out: dict[str, Any] = {"pull": {"side": pull, "score_up": up_dn[0], "score_dn": up_dn[1]},
                           "aggressor": {"ratio": round(ar, 3), "word": who, "with_pull": agree}}
    for key, v in (("vacuum_up", vac_up), ("vacuum_down", vac_dn)):
        out[key] = {"p_from": v[0], "p_to": v[1], "depth": v[2], "dist_levels": v[3]} if v else None
    for key, w in (("wall_ask", wall_ask), ("wall_bid", wall_bid)):
        out[key] = {"p": w[0], "mult": w[1], "q": w[2]} if w else None
    return out


def _hawkes(n: float, bins: int = 40) -> dict:
    word = ("КРИТИЧНОСТЬ: поток питает сам себя — лавина рядом" if n >= hawkes.N_CRITICAL else
            "рефлексивность разогрета: сделки рожают сделки" if n >= hawkes.N_WARM else
            "поток докритичен: сделки в основном внешние")
    return {"n": n, "fano": round(1.0 / (1.0 - n) ** 2, 2), "bins": bins, "word": word}


def scan_agg(*, now: str, since: str, ext: str | None = None, bid: float, ask: float, imb: float, pull: str | None,
             up: float, dn: float, word: str, streak: tuple[int, int], ar: float, agree: bool | None,
             pf: tuple | None = None, pz_up: tuple = (), pz_dn: tuple = (), wall_ask: tuple | None = None,
             wall_bid: tuple | None = None, tension: tuple | None = None, synth: tuple | None = None,
             hawkes_n: float | None = None) -> dict:
    """Агрегаты maya_scan.status() — их отчёт для ИИ рисует настоящий mission._scan_text (maya_scan.render_for_ai), а
    прокол из них поднимает настоящий _puncture_watch. Сканер стартует с советом (since) и тикает раз в 3 с; повторный
    совет (ext) продлевает срок «8 ч». Стены: (p_mode, ×медианы | None, лотов | None, присутствие, устойчивость, призрак?)."""
    price = (bid + ask) / 2
    ticks = int((ts(now) - ts(since)) / SCAN_TICK_S)
    left = round(SCAN_MIN - (ts(now) - ts(ext or since)) / 60)
    agg: dict[str, Any] = {
        "ticks": ticks, "running": True, "interval_s": SCAN_TICK_S, "elapsed_min": round(ticks * SCAN_TICK_S / 60),
        "left_min": left,
        "last": {"bid": bid, "ask": ask, "spread_bps": round((ask - bid) / price * 1e4, 2), "imb": imb,
                 "pull": pull},
        "consensus": {"up_share": up, "dn_share": dn, "word": word,
                      "streak": {"win": 20, "up": streak[0], "dn": streak[1]}, "aggressor_mean": ar, "agree": agree}}
    if pf:
        agg["puncture_first"] = {"side": pf[0], "p_lo": pf[1], "p_hi": pf[2], "persistence": pf[3],
                                 "depth_mean": pf[4], "ticks": pf[5]}
    if pz_up or pz_dn:
        agg["punctures"] = {k: [{"p_lo": a, "p_hi": b, "persistence": c, "depth_mean": d} for a, b, c, d in rows]
                            for k, rows in (("up", pz_up), ("down", pz_dn))}
    for key, w in (("wall_ask", wall_ask), ("wall_bid", wall_bid)):
        if w:
            nw = {"mult": w[1], "q": w[2]} if w[1] else {}
            agg[key] = {"p_mode": w[0], "now": nw, "present": w[3], "stability": w[4], "ghost": w[5]}
    if tension:
        agg["tension"] = {"word": tension[0], "first": tension[1], "last": tension[2]}
    if synth:
        agg["synth"] = {"word": synth[0], "jump_max": synth[1]}
    if hawkes_n is not None:
        agg["hawkes"] = _hawkes(hawkes_n)
    return agg


T_FLAT = ("натяжение ровное", 0.52, 0.54)
T_UP = "натяжение РАСТЁТ — вакуум углубляется, взрыв зреет"
T_DOWN = "натяжение спадает — вакуум заполняют"
S_CALM = ("производная спокойна — вакуум держат без рывков", 0.04)


def wy_tf(bars: int, read: str, phase: str, bias: int, lo: float | None, hi: float | None, atr: float,
          vol: str, events: list[tuple[str, str, float, str]]) -> dict:
    """Словарь wyckoff.analyze для одного TF (бокс, фаза, события) → вход market_ctx.render_wyckoff."""
    return {"bars": bars, "read": read, "phase": phase, "bias": bias, "box_low": lo, "box_high": hi, "box_atr": atr,
            "vol_character": vol, "events_all": [{"t": t, "name": n, "price": p, "note": note} for t, n, p, note in events]}


def wyckoff_text(d1: dict, h1: dict, price: float) -> str:
    """Блок ВАЙКОФФ — настоящий market_ctx.render_wyckoff (в бою — ctx["wyckoff_text"] досье совета)."""
    return market_ctx.render_wyckoff({"daily": d1, "hourly": h1}, price)


def wyckoff_ctx(d1: dict, h1: dict, price: float, at: float) -> dict:
    """Слой Вайкоффа совета так, как его отдаёт market_ctx.build: текст (render_wyckoff), края боксов (wyckoff_box),
    цена расчёта и время сборки — из них mission._council кладёт m.layers["wyckoff"] и m.wy_at."""
    wy = {"daily": d1, "hourly": h1}
    return {"wyckoff_text": market_ctx.render_wyckoff(wy, price), "wyckoff_box": market_ctx.wyckoff_box(wy),
            "price": float(price), "ts": float(at)}


def scout_text(at: str, mx: tuple, vtbr: tuple, si: tuple) -> str:
    """ДАННЫЕ РАЗВЕДКИ — формат scout.fetch (котировка и изменение за день по каждому запросу)."""
    rows = [("MX (фьючерс на индекс MOEX)", mx, "рынок в целом"), ("VTBR (ВТБ)", vtbr, "связанная бумага, ρ=+0.78"),
            ("Si (фьючерс USD/RUB)", si, "рубль")]
    return (f"Разведка FLASH ({msk(at)}): 3 запрос(ов), из них по связанным бумагам 1\n"
            + "\n".join(f"- {lab}: {v[0]:g} ({v[1]:+.2f}% за день) · tinkoff — {why}" for lab, v, why in rows))


PARTNER_META = (("SBERP", "Сбербанк-п", "сектор: банки и финансы", 0.97, 60, 0, None),
                ("VTBR", "ВТБ", "сектор: банки и финансы", 0.78, 58, 1, 0.31),
                ("MX", "фьючерс на индекс MOEX", "макро", 0.71, 60, 0, None),
                ("SI", "рубль: фьючерс USD/RUB (Si)", "макро", -0.34, 60, 0, None))


def partners_text(moves: dict[str, tuple[float, float, float]]) -> str:
    """СВЯЗАННЫЕ БУМАГИ — настоящий correlate.text по записям correlate.partners (ρ, n, лаг, ход за день и 5 дней, цена)."""
    return correlate.text([{"code": code, "name": name, "kind": kind, "rho": rho, "n": n, "lead": lead, "rho_lag": lag,
                            "move_1d": moves[code][0], "move_5d": moves[code][1], "price": moves[code][2]}
                           for code, name, kind, rho, n, lead, lag in PARTNER_META], TICKER)


def _range_blocks(now: str, price: float) -> tuple[str, str]:
    """Разведка и связанные бумаги «коридорного» дня."""
    return (scout_text(now, (2912.0, 0.18), (81.35, 0.42), (93150.0, -0.05)),
            partners_text({"SBERP": (0.10, 0.85, round(price * 0.998, 1)), "VTBR": (0.42, 1.10, 81.35),
                           "MX": (0.18, 0.60, 2912.0), "SI": (-0.05, 0.30, 93150.0)}))


# ── новости: размеченные записи хранилища (строки рисует настоящий newsflow.one_line_rich) ─────────────────────
def news(nid: str, at: str, src: str, one: str, tone: int, honesty: int, hype: int, imp: int, nov: int, eff: str,
         strength: int, kind: str, horizon: str, assets: tuple = (), sectors: tuple = (), facts: tuple = (),
         gist: str = "", day: int = 30) -> dict:
    """Новость в форме store_v5 (разметка FLASH в ai); added_ts — когда легла в базу (≈2 мин: RSS + разметка) — по нему
    newsflow.fresh_since отбирает «свежие с прошлой перепроверки»."""
    t = ts(at, day)
    return {"id": nid + "0" * 26, "ts": t, "added_ts": t + 120.0, "source": src, "title": one, "relevant": 1,
            "ai": {"one_liner": one, "tone": tone, "honesty": honesty, "hype": hype, "importance": imp, "novelty": nov,
                   "expected_effect": eff, "effect_strength": strength, "kind": kind, "horizon": horizon,
                   "assets": list(assets), "sectors": list(sectors), "key_facts": list(facts), "gist": gist}}


N_PROFIT = news("3f9a2c", "08:15", "interfax", "Сбербанк за 8 мес. по РСБУ: чистая прибыль +6% г/г", 30, 80, 20, 55, 45,
                "вверх", 25, "факт", "дни", ("SBER",), ("банки",))
N_OFZ = news("b71e04", "09:30", "rbc", "Минфин разместит ОФЗ на 50 млрд ₽ в среду", 0, 85, 10, 35, 40, "неясно", 10,
             "факт", "дни", (), ("ОФЗ", "рынок"))
N_MORTGAGE = news("5d0b7a", "10:52", "prime", "Сбербанк: выдачи ипотеки в сентябре +18% м/м (оценка менеджмента)", 45,
                  70, 35, 60, 65, "вверх", 40, "факт", "дни", ("SBER",), ("банки",))
N_TAX = news("c4d8e1", "17:20", "kommersant", "Минфин обсуждает разовый налог на сверхприбыль банков", -45, 60, 55, 75,
             70, "вниз", 60, "слух", "дни", ("SBER", "VTBR"), ("банки",),
             facts=("обсуждение в рабочей группе Минфина", "параметры не названы", "решение не принято"),
             gist="риск изъятия части прибыли банков, пока на уровне обсуждения", day=29)
N_CBR = news("7c21e0", "10:40", "interfax", "ЦБ внепланово снизил ключевую ставку на 100 б.п.", 70, 90, 25, 95, 95,
             "вверх", 85, "факт", "часы", ("SBER",), ("банки", "рынок"),
             facts=("ставка снижена на 100 б.п. вне графика заседаний", "решение объявлено в 10:40 МСК",
                    "ЦБ допускает дальнейшее снижение"),
             gist="неожиданное смягчение ДКП: дешевле фондирование, позитив для банков и акций в целом")
N_OVERDUE = news("e93a55", "10:05", "rbc", "ЦБ: просрочка по потребкредитам выросла на 1.9 п.п. за квартал", -40, 85,
                 30, 70, 60, "вниз", 45, "факт", "дни", ("SBER", "VTBR"), ("банки",),
                 facts=("доля проблемных потребкредитов 11.2%", "рост за квартал 1.9 п.п.", "ЦБ говорит о мерах"),
                 gist="рост плохих долгов в рознице — давление на резервы банков")
N_BADDEBT = news("a4c9f2", "11:02", "rbc", "ЦБ: доля проблемных потребкредитов выросла до 11.2%", -40, 85, 30, 60, 55,
                 "вниз", 45, "факт", "дни", ("SBER", "VTBR"), ("банки",))
WATCH_CBR = ("ЦБ внепланово снизил ключевую ставку на 100 б.п.: сюрприз для рынка, первая реакция — банки и индекс "
             "вверх")


# ── итог общего совета: строка council_latest (текст рисует настоящий council.summary_text) ──────────────────
def pick(ticker: str, side: str, conv: int, horizon: str, why: str, trigger: str, risk: str) -> dict:
    return {"ticker": ticker, "side": side, "conviction": conv, "horizon": horizon, "why": why, "trigger": trigger,
            "risk": risk}


def council_row(regime: str, summary: str, picks: list[dict], *, avoid: tuple = (), watch: tuple = (),
                key_times: tuple = (), when: str = "08:50") -> dict:
    return {"ts": ts(when), "kind": "daily",
            "data": {"summary": {"regime": regime, "summary": summary, "picks": picks,
                                 "avoid": [{"ticker": t, "why": w} for t, w in avoid],
                                 "watch": [{"ticker": t, "why": w} for t, w in watch], "key_times": list(key_times)}}}


COUNCIL_RANGE = council_row(
    "нейтральный, выборочно risk-on",
    "Фьючерс на индекс (MX) третью сессию в коридоре 2 880–2 940; рубль стабилен (Si 92 900–93 600), Brent 71–73 $. "
    "Банки сильнее рынка на ожиданиях снижения ставки к концу года; объёмы ниже средних за 20 дней.",
    [pick("SBER", "BUY", 55, "1-3 дня", "коридор 296–304, покупатель защищает 296",
          "закрепление над 304 или отбой от 296 с объёмом", "закрытие дня ниже 296"),
     pick("LKOH", "SELL", 50, "сессия", "нефть у нижней границы 71 $", "Brent ниже 70.8 $", "решение ОПЕК+")],
    watch=(("VTBR", "догоняет SBER"), ("MX", "верх коридора 2 940")),
    key_times=("10:00–11:00 МСК — основная ликвидность", "19:00 МСК — недельная инфляция Росстата"))


def council_bear(p: dict) -> dict:
    return council_row(
        "risk-off",
        "Третий день продаж в банках: слух о разовом налоге на сверхприбыль (29.09 17:20), MX 2 850–2 880 у минимумов "
        "месяца; рубль слабеет (Si 93 400), нерезидентов на рынке нет — отскоки выкупают вяло.",
        [p], avoid=(("VTBR", "слабее сектора, −4.3% за 5 дней"),), watch=(("MX", "2 850 — поддержка месяца"),),
        key_times=("16:00 МСК — брифинг Минфина", "19:00 МСК — недельная инфляция Росстата"))


# ── приказы совета (JSON шифровальщика; проходят настоящий mission._validate_exec) ─────────────────────────────
WAIT_RAW = {"do": "WAIT", "confidence": 55, "levels": [304.0, 296.0],
            "wait_for": "закрепление над 304 (объём ×2 к среднему, 15 мин над уровнем) — тогда BUY к 310; "
                        "пробой 296 вниз с объёмом — SELL к 290",
            "why": "коридор 296–304 второй день, края держат плиты по 17–22 тыс. лотов, середина — без хода",
            "plan": "вне рынка; пробой 304 с объёмом — лонг к 310, стоп под 302; пробой 296 — шорт к 290, стоп над 298",
            "time_note": "выход из коридора вероятнее после 11:00 МСК, до 16:00"}
BUY_0940_RAW = {"do": "BUY", "entry": None, "entry_kind": "сейчас", "take": 305.0, "invalidation": 296.4, "confidence": 64,
                "why": "спринг 295.8 в 09:00 на объёме ×1.9 и возврат в бокс, покупатель в ленте 0.6",
                "plan": "вход сейчас; стоп 296.4 под базой спринга; цель 305 (перед 305.8); над 304 — держать, подтянуть "
                        "триггер к 302",
                "time_note": "ход к 304 вероятнее до 13:00 МСК"}
BUY_1035_RAW = {"do": "BUY", "entry": None, "entry_kind": "сейчас", "take": 306.0, "invalidation": 298.5, "confidence": 58,
                "why": "выход из 300 вверх на объёме ×1.6, покупатель в ленте 0.6",
                "plan": "вход сейчас; стоп 298.5 под 299 (база выхода); цель 306; над 304 — держать",
                "time_note": "ход к 304 вероятнее до 14:00 МСК"}
BUY_1108_RAW = {"do": "BUY", "entry": None, "entry_kind": "сейчас", "take": 306.0, "invalidation": 297.8, "confidence": 64,
                "why": "спринг 295.8 отработан: возврат в бокс на объёме ×1.9, покупатель держит 299.5 (лента 0.58)",
                "plan": "вход сейчас; стоп 297.8 под базой отката; цель 306 (+1.8%); при закреплении над 304 — держать к 309",
                "time_note": "ход к 304 вероятнее до 15:00 МСК"}
BUY_BRK_RAW = {"do": "BUY", "entry": 304.0, "entry_kind": "прорыв", "take": 310.0, "invalidation": 301.8, "confidence": 60,
               "why": "бокс 296–304 третий день, покупатель давит к верху, откаты на сухом объёме",
               "plan": "вход на пробитии 304 (подтверждение тиками за уровнем); стоп 301.8 — возврат в бокс глубже 2 ₽; "
                       "цель 310 перед 309.6–310 (верх дневного диапазона)",
               "time_note": "пробой вероятен до 13:00 МСК"}
SELL_1005_RAW = {"do": "SELL", "entry": None, "entry_kind": "сейчас", "take": 296.0, "invalidation": 305.2, "confidence": 63,
                 "why": "марк-даун третий день, LPSY 301.9, продавец в ленте 0.38",
                 "plan": "вход сейчас; стоп 305.2 над верхом часового бокса; цель 296, дальше 295 (кульминация 11.09); "
                         "под 299 — держать, триггер к 302",
                 "time_note": "продажи вероятнее до 16:00 МСК (брифинг Минфина)"}
SELL_1010_RAW = {"do": "SELL", "entry": 302.0, "entry_kind": "откат", "take": 292.0, "invalidation": 304.6, "confidence": 62,
                 "why": "третий день марк-дауна: SOW 300.1, LPSY 301.9 — продаём откат к бывшей опоре 302",
                 "plan": "засада short @302 на откате; стоп 304.6 над LPSY; цель 292 (ширина бокса 5.6 ₽ вниз от 300.2); "
                         "при уходе ниже 296 без отката — перепроверка",
                 "time_note": "откат вероятнее до 13:00 МСК"}

# ── память миссии: абзац FLASH (explain.memory_prompt 5.4.3 — что дали решения и в чью пользу; действующий приказ в
#    абзац не пишется; сводится после совета, закрытия и каждых PYTHIA_MEMORY_EVERY перепроверок) ──────────────────
_M_HEAD = ("Миссия SBER идёт с 29.09 10:05 МСК, режим auto. 29.09: совет дал BUY от 298.6 со стопом 296.4 — вход 11:02 "
           "по 298.7, выход 14:40 по мысли о прибыли у 301.9 (+960 ₽ на 30 лотах; вход за лонг +1.07 %). После выхода "
           "цена час стояла у 302, к закрытию 29.09 — 299.6: выход в нашу пользу (лонг от 301.9 −0.76 %). ")
_M_NEWS = (" Из новостей держит рынок отчёт Сбербанка за 8 мес. по РСБУ: чистая прибыль +6 % г/г (30.09 08:15).")
MEMORY_0 = _M_HEAD + "Вечером 29.09 и утром 30.09 цена ходила в коридоре 296–304." + _M_NEWS
MEMORY_LONG = (_M_HEAD + "Вечером 29.09 цена ходила в коридоре 296–304; 30.09 в 09:00 — спринг 295.8 (вынос под 296 на "
               "объёме ×1.9) и возврат в коридор." + _M_NEWS)
MEMORY_WAIT = " 30.09 09:40 совет: WAIT при 299.8 (ориентир — выход из коридора 296–304)"
MEMORY_BEAR = ("Миссия SBER идёт с 28.09 10:05 МСК, режим auto. 28.09: совет дал SELL на откате к 305 — засада не "
               "исполнилась, цена ушла вниз без отката (к 303.4 за 2 часа — ход в сторону идеи без нас), приказ протух в "
               "12:40. 29.09: совет WAIT; дежурный PRO вошёл в short по 300.4 в 13:10 и закрыл в 16:20 по 299.1 по мысли о "
               "прибыли (+390 ₽ на 30 лотах; вход за шорт +0.43 %). Слух о разовом налоге на сверхприбыль банков (29.09 "
               "17:20) рынок встретил продажами вечером; 30.09 09:30 — Минфин разместит ОФЗ на 50 млрд ₽.")

ACC_FLAT = (100000, 100000, 52, 47)         # свободно, ликвидный портфель, биржа даёт купить / продать лот

# Вайкофф: дневной диапазон с 11.09 (общий для «коридорных» ситуаций) и часовые картины
D1_RANGE = wy_tf(250, "СКЛОНЯЕТСЯ К НАКОПЛЕНИЮ", "A/B — диапазон", 12, 288.4, 309.6, 3.1,
                 "объём на росте ×1.12 к объёму на падении — паритет",
                 [("2026-09-11", "КУЛЬМИНАЦИЯ ПРОДАЖ", 288.4, "объём ×3.1, размах ×2.4 — паника слита в чьи-то руки"),
                  ("2026-09-15", "АВТО-РАЛЛИ", 309.6, "отскок после кульминации — верх будущего диапазона"),
                  ("2026-09-22", "ВТОРИЧНЫЙ ТЕСТ", 291.2, "ретест дна на сухом объёме — предложение иссякло")])
H1_EVENTS = [("2026-09-28T10:00", "КУЛЬМИНАЦИЯ ПРОДАЖ", 296.0, "объём ×2.8, размах ×2.1 — паника слита в чьи-то руки"),
             ("2026-09-28T12:00", "АВТО-РАЛЛИ", 304.0, "отскок от кульминации до 304.0 — верх бокса"),
             ("2026-09-29T11:00", "ВТОРИЧНЫЙ ТЕСТ (верх)", 302.1, "ретест верха без объёма — спрос не дожимает"),
             ("2026-09-29T15:00", "ВТОРИЧНЫЙ ТЕСТ", 296.2, "ретест дна на сухом объёме — предложение иссякло"),
             ("2026-09-30T10:00", "ВТОРИЧНЫЙ ТЕСТ (верх)", 303.8, "ретест верха без объёма — спрос не дожимает")]
VOL_PAR = "объём на росте ×1.04 к объёму на падении — паритет"
H1_SPRING = ("2026-09-30T09:00", "СПРИНГ", 295.8, "вынос стопов под лёд и возврат — объём ×1.9; топливо для роста")


def h1_range(read: str, phase: str, bias: int, events: list) -> dict:
    return wy_tf(300, read, phase, bias, 296.0, 304.0, 2.6, VOL_PAR, events)


D1_TREND_DOWN = wy_tf(250, "МАРК-ДАУН", "тренд (диапазон не сформирован)", -34, None, None, 0.0,
                      "объём на росте ×0.78 к объёму на падении — предложение доминирует",
                      [("2026-09-25", "ПОСЛЕДНЕЕ ПРЕДЛОЖЕНИЕ (LPSY)", 309.0, "откат к бывшей опоре на малом объёме"),
                       ("2026-09-28", "ЗНАК СЛАБОСТИ (SOW)", 303.4, "закрытие под боксом на расширении (объём ×2.2) — "
                                                                     "знак слабости"),
                       ("2026-09-29", "ПРОБОЙ ВНИЗ", 300.2, "закрытие ниже 301 на объёме ×1.6")])
D1_BEAR_RANGE = wy_tf(250, "СКЛОНЯЕТСЯ К РАСПРЕДЕЛЕНИЮ", "B — построение причины", -18, 295.0, 309.6, 3.4,
                      "объём на росте ×0.82 к объёму на падении — предложение доминирует",
                      [("2026-09-11", "КУЛЬМИНАЦИЯ ПРОДАЖ", 295.0, "объём ×3.1, размах ×2.4 — паника слита в чьи-то руки"),
                       ("2026-09-15", "АВТО-РАЛЛИ", 309.6, "отскок после кульминации — верх диапазона"),
                       ("2026-09-25", "АПТРАСТ", 310.4, "прокол над криком и возврат — ловушка для покупателей"),
                       ("2026-09-28", "ЗНАК СЛАБОСТИ (SOW)", 303.4, "закрытие на расширении вниз (объём ×2.2) — знак "
                                                                    "слабости")])
H1_DOWN_EVENTS = [("2026-09-29T12:00", "ЗНАК СЛАБОСТИ (SOW)", 300.1, "закрытие под боксом на расширении (объём ×2.1) — "
                                                                       "знак слабости"),
                  ("2026-09-29T17:00", "ПОСЛЕДНЕЕ ПРЕДЛОЖЕНИЕ (LPSY)", 301.9, "откат к бывшей опоре на малом объёме")]
VOL_SUPPLY = "объём на росте ×0.71 к объёму на падении — предложение доминирует"
H1_BEAR = wy_tf(300, "РАСПРЕДЕЛЕНИЕ", "D — знак слабости (откаты к LPSY)", -33, 300.2, 305.8, 2.1, VOL_SUPPLY,
                H1_DOWN_EVENTS)

# слой Вайкоффа времени совета: (D1, H1, цена расчёта) — перепроверка видит m.layers["wyckoff"] досье последнего совета
# (с его ценой и событиями) под подписью «на момент совета HH:MM, цена тогда P» и строкой расстояний от текущей цены
# (m.wy_at — MissionPilot._review_wyckoff); время расчёта — начало совета сцены (как ctx["ts"] market_ctx.build)
WY_RANGE_0922 = (D1_RANGE, h1_range("ХАРАКТЕР НЕ ОПРЕДЕЛЁН", "B — построение причины", 4, H1_EVENTS[:4]), 299.8)
WY_LONG_0922 = (D1_RANGE, h1_range("СКЛОНЯЕТСЯ К НАКОПЛЕНИЮ", "C — спринг (тест предложения)", 20,
                                   H1_EVENTS[:4] + [H1_SPRING]), 298.0)
WY_RANGE_1017 = (D1_RANGE, h1_range("ХАРАКТЕР НЕ ОПРЕДЕЛЁН", "B — построение причины", 2, H1_EVENTS), 300.1)
WY_BEAR_0952 = (D1_TREND_DOWN, H1_BEAR, 300.3)
WY_BEAR_0947 = (D1_BEAR_RANGE, H1_BEAR, 303.0)


# ══ бой на данных сцены ══════════════════════════════════════════════════════════════════════════════════════
class _Broker:
    """Брокер-пустышка: тексты боевого кода к бирже не ходят."""
    mode = "dry"


class _NewsDB:
    """newsflow без базы: fresh_since — размеченные новости, легшие в базу позже ts (новые первыми, как
    store_v5.news_since), news_for_ticker — новости инструмента в порядке сцены, render — настоящий newsflow.render.
    enrich_ticker нет — целевой сбор Google News (mission._enrich_news) молчит, как при PYTHIA_GNEWS=0."""

    def __init__(self, pool: list[dict], own: list[dict]):
        self.pool, self.own = pool, own

    def fresh_since(self, since: float) -> list[dict]:
        return sorted((x for x in self.pool if x["added_ts"] > float(since or 0) and x.get("ai")), key=lambda x: -x["ts"])

    def news_for_ticker(self, ticker: str, name: str, days: float = 3, limit: int | None = None) -> list[dict]:
        return list(self.own) if limit is None else list(self.own)[:max(0, int(limit))]

    @staticmethod
    def render(items: list[dict], limit: int | None = None, rich: bool = False) -> str:
        return newsflow.render(items, limit=limit, rich=rich)


class _Watch:
    """watch без базы: серьёзные заметки дозора позже ts (новые первыми)."""

    def __init__(self, notes: list[dict]):
        self.notes = notes

    def serious_since(self, since: float) -> list[dict]:
        return sorted((n for n in self.notes if n["ts"] > float(since or 0)), key=lambda n: -n["ts"])


class _Council:
    """council без базы: latest — строка council_latest сцены, summary_text — настоящий council.summary_text."""

    def __init__(self, row: dict | None):
        self.row = row

    def latest(self) -> dict | None:
        return self.row

    @staticmethod
    def summary_text(row: dict) -> str:
        return council_mod.summary_text(row)


class _Combat:
    """Контекст боевого кода сцены: замороженное время t, ручки механики SCENE_KNOBS, данные сцены вместо базы и
    биржи (newsflow / watch / council / статус сканера); запись миссии в базу (mission._persist) и толмач (explain)
    выключены — стенд ничего не пишет и FLASH не зовёт."""

    def __init__(self, sc: "Scene", t: float):
        self.sc, self.t = sc, float(t)
        self._fz = frozen(self.t)
        self._pt: _patched | None = None

    def __enter__(self) -> "Scene":
        sc = self.sc
        self._fz.__enter__()
        pairs = [(config, k, v) for k, v in SCENE_KNOBS.items()]
        pairs += [(mission, "newsflow", _NewsDB(sc.pool, sc.own)), (mission, "watch", _Watch(sc.notes)),
                  (mission, "council", _Council(sc.council_row)), (mission, "_scan_status_raw", lambda ticker: sc.agg),
                  (mission, "_persist", lambda m: True), (mission, "explain", None)]
        self._pt = _patched(pairs).__enter__()
        return sc

    def __exit__(self, *exc) -> bool:
        if self._pt is not None:
            self._pt.__exit__(*exc)
        self._fz.__exit__(*exc)
        return False


class Scene:
    """Одна биржевая ситуация на НАСТОЯЩИХ mission.Mission + MissionPilot. История проигрывается по времени: совет и
    приказ (mission._validate_exec → adopt_forecast), тики цены через наблюдатели тика (_shock_watch, _wait_watch,
    _flat_track, _wake_watch), перепроверки (запись той же формы, что пишет MissionPilot._review), ответы у двери
    (_apply_gate), прокол сканера (_puncture_watch), серьёзная новость (_ask_review). Тексты для ИИ — args(node) —
    собирают боевые сборщики mission.py в момент now."""

    def __init__(self, now: str, price: float, *, start: str = "09:22", agg: dict | None = None,
                 council_row: dict | None = None):
        self.now, self.price, self.t = now, float(price), ts(now)
        self.pool: list[dict] = []           # все размеченные новости базы (fresh_since)
        self.own: list[dict] = []            # новости инструмента (news_for_ticker)
        self.notes: list[dict] = []          # серьёзные заметки дозора
        self.council_row = council_row
        self.agg = agg                       # статус сканера (maya_scan.status)
        self.light = self.scout = self.partners = ""
        self.verdict: dict[str, str] = {}    # узлу verdict: анализ, критика, досье, Вайкофф совета
        with self.combat(ts(start)):
            self.m = mission.Mission(TICKER, NAME, "share", PLAY, DEPOSIT)
            self.p = mission.MissionPilot(TICKER, deposit=DEPOSIT, broker=_Broker(), mission=self.m)
        p = self.p
        p._state_path = None                 # state-файл data/aipilot_state.json не трогаем
        p.figi, p.asset_class, p.tick_size, p.point_value = "BBG004730N88", "share", 0.01, float(LOT)
        p.deposit = DEPOSIT
        p.session_risk = trader_risk.SessionRisk(DEPOSIT)
        p._prepared = True
        p.market = dict(MARKET_ST)
        self.m.pilot = p
        self.account(*ACC_FLAT)

    def combat(self, t: float | None = None) -> _Combat:
        return _Combat(self, self.t if t is None else t)

    # ── история сцены ──
    def news(self, pool: list[dict], own: list[dict]) -> "Scene":
        self.pool, self.own = list(pool), list(own)
        return self

    def blocks(self, *, light: str, scout: str, partners: str) -> "Scene":
        self.light, self.scout, self.partners = light, scout, partners
        return self

    def account(self, free: float, liquid: float, buy: int, sell: int) -> "Scene":
        self.p.account = {"free": free, "liquid": liquid, "max_buy": buy, "max_sell": sell}
        return self

    def council(self, start: str, end: str | None = None) -> "Scene":
        """Полный совет: m.council_ts — начало; конец — длительность (m.council_dur), как в mission._council."""
        self.m.council_ts = ts(start)
        if end:
            self.m.council_dur = ts(end) - ts(start)
        return self

    def council_now(self, start: str) -> "Scene":
        """Совет идёт прямо сейчас (узел verdict): начат в start, пилот ждёт его приказа."""
        self.m.council_ts = ts(start)
        self.p._reanalyzing = True
        return self

    def order(self, raw: dict, at: str, price: float, adopt: bool = True) -> "Scene":
        """Приказ совета: настоящий mission._validate_exec, цена при принятии — ex["price"] (как _council); adopt —
        пилот принимает его настоящим adopt_forecast (план входа, WAIT без плана)."""
        p = self.p
        ex, err = mission._validate_exec(dict(raw), PLAY, price, in_pos=p.position is not None,
                                         pos_side=(p.position or {}).get("side"))
        if err:
            raise ValueError(f"приказ сцены не прошёл mission._validate_exec: {err}")
        ex["price"] = float(price)
        self.m.exec, self.m.exec_ts, self.m.ctx_price = ex, ts(at), float(price)
        if adopt:
            with self.combat(ts(at)):
                if not p.adopt_forecast({"exec": ex}):
                    raise ValueError(f"пилот не принял приказ сцены: {p.last_action}")
        return self

    def memory(self, text: str, at: str, wyckoff: tuple | None = None) -> "Scene":
        """Память миссии (абзац FLASH) и время её сведения; wyckoff — (D1, H1, цена) слоя Вайкоффа досье этого совета:
        m.layers["wyckoff"] и метка m.wy_at — настоящим mission._wyckoff_at по контексту формы market_ctx.build
        (время расчёта — начало совета m.council_ts, как в _council)."""
        self.m.memory, self.m.memory_ts = text, ts(at)
        if wyckoff is not None:
            ctx = wyckoff_ctx(*wyckoff, at=self.m.council_ts)
            self.m.layers = {"wyckoff": ctx["wyckoff_text"]}
            self.m.wy_at = mission._wyckoff_at(ctx, self.m.council_ts)
        return self

    def hold(self, side: str, lots: int, entry: float, opened: str, inv: float, take: float) -> "Scene":
        """Позиция пилота: уровни — настоящим _set_levels (триггер, тейк и аварийный трос по PYTHIA_HARD_STOP_PCT)."""
        p = self.p
        pos = {"side": side, "lots": int(lots), "entry": float(entry), "opened_ts": ts(opened), "stop_id": None,
               "floating": 0.0}
        with self.combat(ts(opened)):
            p._set_levels(pos, take, inv)
        p.position, p.plan, p._flat, p.state = pos, None, None, "В_ПОЗИЦИИ"
        return self

    def tick(self, at: str, price: float) -> "Scene":
        """Тик цены через наблюдатели MissionPilot.tick в том же порядке: резкий ход (ведёт окно хода цены),
        уровни WAIT, отсчёт «вне рынка», будильник ЖДЁМ. Плавающий P/L позиции — как AIPilot.tick."""
        p, t, px = self.p, ts(at), float(price)
        with self.combat(t):
            p.prices.append(px)
            p.last_book, p._book_ts = {"best_bid": round(px - 0.01, 2), "best_ask": round(px + 0.01, 2)}, t
            p._shock_watch(px)
            p._wait_watch(px)
            p._flat_track(px)
            p._wake_watch(px)
            pos = p.position
            if pos:
                sgn = 1.0 if pos["side"] == "long" else -1.0
                pos["floating"] = round((px - pos["entry"]) * sgn * pos["lots"] * p.point_value, 2)
        return self

    def ticks(self, *points: tuple[str, float]) -> "Scene":
        for at, px in points:
            self.tick(at, px)
        return self

    def review_start(self, at: str) -> "Scene":
        """Перепроверка ушла к PRO (он думает): поводы до ответа копятся в неё же, уровни и прокол ждут ответа."""
        self.p._review_busy, self.p._review_started_ts = True, ts(at)
        return self

    def review(self, at: str, choice: str, why: str, price: float, *, entry: float | None = None,
               entry_kind: str | None = None, inv: float | None = None, take: float | None = None) -> "Scene":
        """Ответ перепроверки (choice — канон кода: КУПИТЬ_СЕЙЧАС, ЖДЁМ …) — теми же функциями, что MissionPilot._review
        после разбора слова: тик цены ответа, _review_answered (повод и пейсинг сброшены) и _review_record (last_review с
        ценой решения, разбор прокола, запись m.reviews с in_pos/pos_side, ДЕРЖАТЬ в позиции — _retune, будильник ЖДЁМ
        — _wake_after_review, серия ЖДЁМ — m.wait_streak через _streak_after_review). Сверка с настоящим _review —
        check_review_record и tests/test_prompt_bench.py."""
        self.tick(at, price)
        p = self.p
        with self.combat(ts(at)):
            p._review_busy = False                   # AIPilot._review_bg: PRO ответил
            p._review_answered()
            in_pos = p.position is not None
            obj = {"choice": choice, "why": why, "entry": entry, "entry_kind": entry_kind, "invalidation": inv,
                   "take": take}
            p._review_record(obj, choice, choice, str(why)[:300], "", float(price), in_pos,
                             (p.position or {}).get("side"))
        return self

    def handoff(self, at: str, reason: str, kind: str = "council") -> "Scene":
        """Передача совету (НОВЫЙ_АНАЛИЗ перепроверки) — запись MissionPilot._fire_reanalyze (окно совета открыто)."""
        self.m.handoffs.append({"ts": ts(at), "reason": reason, "deferred": False, "kind": kind})
        return self

    def news_event(self, at: str, note: str, affected: list[str], severity: int = 5) -> "Scene":
        """Серьёзная заметка дозора: в базу дозора (для _watch_text) и повод пилоту — как mission.on_serious_news."""
        t = ts(at)
        self.notes.append({"ts": t, "severity": severity, "note": note, "affected": list(affected)})
        txt = note.strip()
        if len(txt) > 160:
            txt = txt[:157].rsplit(" ", 1)[0] + "…"
        with self.combat(t):
            if self.p._ask_review("серьёзная новость: " + txt, kind="news") is not None:
                raise ValueError("сцена: новость ушла в триаж (нужен цикл событий)")
        return self

    def gate(self, at: str, answer: dict) -> "Scene":
        """Ответ PRO у двери — настоящий MissionPilot._apply_gate (ЖДАТЬ: срок gate_after, причина — gate_wait_why)."""
        p = self.p
        with self.combat(ts(at)):
            _drive(p._apply_gate(dict(answer), p.prices[-1], p.plan))
        return self

    def puncture(self, triage_why: str = "") -> "Scene":
        """Прокол сканера из статуса сцены — настоящий _puncture_watch в момент now; в позиции повод идёт через
        _apply_triage с ответом триажа СЕЙЧАС (triage_why), вне рынка — сразу дежурному PRO."""
        p = self.p
        with self.combat():
            p._puncture_check_ts = 0.0
            if p.position:
                def _via_triage(why: str, kind: str = "pilot"):
                    p._apply_triage({"urgency": "СЕЙЧАС", "why": triage_why, "action": None}, why, kind)
                    return None
                p._ask_review = _via_triage
            try:
                p._puncture_watch(self.price)
            finally:
                p.__dict__.pop("_ask_review", None)
        if not (p.puncture and p.puncture.get("pending")):
            raise ValueError(f"прокол сцены не стал поводом: {(p._puncture_now or {}).get('state')}")
        return self

    def settle(self, base30: float, bid: float, ask: float, imb: float) -> "Scene":
        """Момент now: последний тик — цена сцены; окно ≈30 мин тиков (N30) от base30; стакан с дисбалансом."""
        p = self.p
        if not p._px_hist or p._px_hist[-1] != (self.t, self.price):
            raise ValueError("сцена: последний тик должен быть now по цене сцены")
        p.prices = [float(base30)] + [self.price] * (N30 - 1)
        p.last_book, p._book_ts = {"best_bid": bid, "best_ask": ask, "imbalance": imb}, self.t
        return self

    # ── тексты узлов (боевые сборщики) ──
    def side(self) -> str | None:
        return (self.p.position or self.p.plan or {}).get("side")

    def review_args(self) -> dict:
        """Аргументы prompts_mission.review — как собирает MissionPilot._review."""
        p, m = self.p, self.m
        with self.combat():
            news_txt = _drive(p._gather_news())
            if not news_txt.strip():
                news_txt = NEWS_EMPTY
            price = p.prices[-1]
            situation = p._review_situation(price)
            wy_at, wy_txt = p._review_wyckoff(price, m.layers.get("wyckoff") or "")
            return {"situation": situation, "light": self.light, "council_text": mission._council_text(),
                    "prev_exec": mission._exec_text(m), "news": news_txt,
                    "watch": mission._watch_text(p._last_review_ts or p.started_ts), "astro_line": "",
                    "scan": mission._scan_text(m), "wyckoff": wy_txt, "wyckoff_at": wy_at, "scout": self.scout,
                    "partners": self.partners, "memory": m.memory or ""}

    def door_args(self) -> dict:
        """Аргументы prompts_mission.entry_check — как собирает MissionPilot._entry_check (у двери без строки ПРОВЕРКА
        ВХОДА, пометка кода — «ПОМЕТКА КОДА»)."""
        p, m, plan = self.p, self.m, self.p.plan
        with self.combat():
            src = {"plan": p._plan_text(plan, self.price), "history": p._history_text(self.price),
                   "news": p._news_quick(), "checks": p._gates_text(plan), "guards": p._guards_text(),
                   "scan": mission._scan_text(m), "council_text": mission._council_text()}
            p._door_asking = True
            try:
                situation = p._situation_for_ai(p.prices[-1])
            finally:
                p._door_asking = False
            if plan.get("gate_note"):
                situation += f"\nПОМЕТКА КОДА: {plan['gate_note']}"
        return {"situation": situation, "plan": src["plan"], "history": src["history"], "light": self.light,
                "news": src["news"], "council_text": src["council_text"], "scan": src["scan"], "scout": self.scout,
                "partners": self.partners, "memory": m.memory or "", "guards": src["guards"], "checks": src["checks"]}

    def profit_args(self) -> dict:
        """Аргументы prompts_mission.profit_think — как собирает MissionPilot._profit_think (повод — _profit_trigger)."""
        p, m, pos = self.p, self.m, self.p.position
        with self.combat():
            reason = p._profit_trigger(self.price, pos)
            if not reason:
                raise ValueError("сцена: повода мысли о прибыли нет")
            return {"profit": reason, "situation": p._situation_for_ai(p.prices[-1]),
                    "history": p._history_text(self.price), "light": self.light, "plan": p._profit_plan_text(pos),
                    "news": p._news_quick(), "council_text": mission._council_text(), "scan": mission._scan_text(m),
                    "scout": self.scout, "partners": self.partners, "memory": m.memory or "",
                    "guards": p._guards_text(), "thoughts": p._profits_text(pos)}

    def verdict_args(self) -> dict:
        """Вердикт совета: анализ и критика (тексты PRO сцены) + контекст, как его собирает mission._council — новости
        (_news_for), итог общего совета, сканер, память и prev (_prev_text) из боевого кода, досье — текст сцены."""
        m, v = self.m, self.verdict
        with self.combat():
            _items, news_txt = _drive(mission._news_for(m))
            ctx = {"time_msk": msk(self.now), "price": self.price, "asset_class": "share", "market": MARKET,
                   "reason": v["reason"], "wyckoff": v["wyckoff"], "dossier": v["dossier"], "scan": mission._scan_text(m),
                   "scout": self.scout, "partners": self.partners, "council": mission._council_text(),
                   "news": news_txt, "memory": m.memory or "", "prev": mission._prev_text(m)}
        return {"analysis_text": v["analysis"], "critique_text": v["critique"], "ctx": ctx}

    def args(self, node: str) -> dict:
        if node in ("review_flat", "review_pos"):
            return self.review_args()
        if node == "entry":
            return self.door_args()
        if node == "profit":
            return self.profit_args()
        if node == "verdict":
            return self.verdict_args()
        raise ValueError(f"узел {node!r} не знаю")


# ══ 14 ситуаций ═════════════════════════════════════════════════════════════════════════════════════════
R_1014 = ("ретест верха 303.8 в 10:00 без объёма — спрос не дожимает; цена 302.2 откатывает к середине коридора, "
          "лента паритет 0.50")


def _range(now: str, price: float, agg: dict, pool: tuple = (N_PROFIT, N_OFZ), own: tuple = (N_OFZ, N_PROFIT)) -> Scene:
    """Мир «коридор 296–304»: совет 09:22–09:40 дал WAIT при 299.8 (будильник кода у 304 и 296), память и слой
    Вайкоффа — от этого совета; в 10:00 ретест верха 303.8 без объёма."""
    sc = Scene(now, price, agg=agg, council_row=COUNCIL_RANGE).news(list(pool), list(own))
    sc.council("09:22", "09:40").order(WAIT_RAW, "09:40", 299.8).memory(MEMORY_0, "09:41", WY_RANGE_0922)
    return sc.ticks(("09:40", 299.8), ("10:00", 303.8))


def _w_range_mid_wait() -> Scene:
    now, px = "11:20", 300.0
    sc = _range(now, px, scan_agg(now=now, since="09:22", bid=299.99, ask=300.01, imb=0.02, pull=None, up=0.46, dn=0.44,
                                  word="выраженной тяги за окно нет", streak=(9, 8), ar=0.51, agree=None,
                                  wall_ask=(300.4, 3.2, 9800, 0.71, 0.64, False),
                                  wall_bid=(299.6, 3.0, 9100, 0.66, 0.6, False), tension=T_FLAT, synth=S_CALM,
                                  hawkes_n=0.34))
    sc.review("10:14", "ЖДЁМ", R_1014, 302.2).ticks(("10:35", 299.4))
    sc.review("10:50", "ЖДЁМ", "цена 300.1 посередине коридора 296–304, лента паритет 0.51, до краёв по 1.3%", 300.1,
              entry=304.0)
    sc.ticks(("11:11", 299.91), ("11:14", 299.8), ("11:17", 300.2), ("11:20", px)).settle(299.79, 299.99, 300.01, 0.02)
    return sc.blocks(light=light_text(px, at=now, close=299.6, bid=299.99, ask=300.01, bid_vol=61200, ask_vol=58400,
                                      buy=48300, sell=46900, count=2140, big=(("buy", 2600, 300.05), ("sell", 2100, 300.12)),
                                      my=maya(None, (0.41, 0.38), ar=0.507, vac_up=(300.18, 300.31, 0.55, 17),
                                              vac_dn=(299.8, 299.69, 0.5, 19), wall_ask=(300.4, 3.2, 9800),
                                              wall_bid=(299.6, 3.0, 9100))),
                     **dict(zip(("scout", "partners"), _range_blocks(now, px))))


def _w_range_low_buyer() -> Scene:
    now, px = "11:20", 296.4
    sc = _range(now, px, scan_agg(now=now, since="09:22", bid=296.39, ask=296.41, imb=0.31, pull="вверх", up=0.58, dn=0.31,
                                  word="тянут вверх", streak=(13, 4), ar=0.62, agree=True,
                                  pz_up=((296.46, 296.7, 0.41, 0.66),), wall_ask=(296.8, 2.1, 6300, 0.38, 0.3, False),
                                  wall_bid=(296.0, 7.4, 22100, 0.93, 0.9, False), tension=(T_UP, 0.41, 0.63),
                                  hawkes_n=0.57))
    sc.review("10:14", "ЖДЁМ", R_1014, 302.2)
    sc.review("10:50", "ЖДЁМ", "цена 299.0 в нижней половине коридора 296–304, лента паритет 0.49", 299.0)
    sc.ticks(("11:10", 297.8), ("11:15", 297.0), ("11:19", 296.3), ("11:20", px)).settle(299.0, 296.39, 296.41, 0.31)
    return sc.blocks(light=light_text(px, at=now, close=299.6, bid=296.39, ask=296.41, bid_vol=142600, ask_vol=74800,
                                      buy=101800, sell=60400, count=3900, wall_bid=(296.0, 22100),
                                      big=(("buy", 5200, 296.35), ("buy", 4100, 296.3), ("sell", 2300, 296.5)),
                                      my=maya("вверх", (0.66, 0.18), ar=0.628, vac_up=(296.46, 296.7, 0.72, 3),
                                              vac_dn=(296.2, 296.05, 0.21, 12), wall_ask=(296.8, 2.1, 6300),
                                              wall_bid=(296.0, 7.4, 22100))),
                     **dict(zip(("scout", "partners"), _range_blocks(now, px))))


def _w_breakout_hold() -> Scene:
    now, px = "11:20", 305.1
    sc = _range(now, px, scan_agg(now=now, since="09:22", bid=305.09, ask=305.11, imb=0.22, pull="вверх", up=0.64, dn=0.22,
                                  word="тянут вверх", streak=(14, 3), ar=0.65, agree=True,
                                  pf=("вверх", 305.3, 305.7, 0.61, 0.8, 240), pz_up=((305.3, 305.7, 0.61, 0.8),),
                                  wall_ask=(305.9, 2.3, 7100, 0.3, 0.2, True),
                                  wall_bid=(304.9, 4.4, 12800, 0.58, 0.55, False), tension=(T_UP, 0.44, 0.8),
                                  hawkes_n=0.71),
                pool=(N_PROFIT, N_OFZ, N_MORTGAGE), own=(N_MORTGAGE, N_OFZ, N_PROFIT))
    sc.review("10:14", "ЖДЁМ", R_1014, 302.2)
    sc.review("10:50", "ЖДЁМ", "цена 300.3 посередине коридора 296–304, лента паритет 0.52, пробоя нет", 300.3)
    sc.tick("11:00", 304.1).review_start("11:00")          # уровень 304 из приказа WAIT будит дежурного PRO
    sc.review("11:02", "ЖДЁМ", "пробой 304 на объёме ×2.4, но над уровнем только 2 мин (цена 304.4) — удержания ещё нет",
              304.4)
    sc.ticks(("11:10", 304.4), ("11:13", 304.2), ("11:18", 305.3), ("11:20", px)).settle(300.3, 305.09, 305.11, 0.22)
    sc.puncture()                                           # прокол вверх 61 % — вне рынка без плана
    return sc.blocks(light=light_text(px, at=now, close=299.6, bid=305.09, ask=305.11, bid_vol=118000, ask_vol=75400,
                                      buy=172900, sell=89100, count=6100, wall_bid=(304.9, 12800),
                                      big=(("buy", 7400, 305.0), ("buy", 6100, 304.8), ("sell", 3900, 305.2)),
                                      my=maya("вверх", (0.71, 0.2), ar=0.66, vac_up=(305.3, 305.7, 0.8, 19),
                                              wall_ask=(305.9, 2.3, 7100), wall_bid=(304.9, 4.4, 12800))),
                     scout=scout_text(now, (2929.0, 0.62), (82.3, 1.2), (93050.0, -0.15)),
                     partners=partners_text({"SBERP": (1.3, 2.1, 304.6), "VTBR": (1.2, 1.9, 82.3),
                                             "MX": (0.62, 0.9, 2929.0), "SI": (-0.15, 0.2, 93050.0)}))


def _w_false_breakout() -> Scene:
    now, px = "11:20", 302.9
    sc = _range(now, px, scan_agg(now=now, since="09:22", bid=302.89, ask=302.91, imb=-0.29, pull="вниз", up=0.52, dn=0.33,
                                  word="за всё окно тянули вверх, последние тики — вниз (перекладка?)", streak=(3, 14),
                                  ar=0.37, agree=True, pz_dn=((302.6, 302.84, 0.44, 0.7),),
                                  wall_ask=(303.3, 5.2, 15600, 0.62, 0.58, False),
                                  wall_bid=(302.4, 2.0, 6000, 0.25, 0.18, True), tension=(T_UP, 0.39, 0.6),
                                  synth=("∂P/∂t > 0: вакуум вниз углубляют — синтетическую сингулярность готовят", 0.21),
                                  hawkes_n=0.64))
    sc.review("10:14", "ЖДЁМ", R_1014, 302.2)
    sc.review("10:55", "ЖДЁМ", "цена 301.2 в верхней половине коридора, лента паритет 0.52, пробоя 304 нет", 301.2)
    sc.ticks(("11:10", 303.81), ("11:16", 304.1))           # проход 304 будит дежурного PRO (уровень приказа WAIT) …
    sc.review_start("11:16")                                # … пока PRO собирает данные, цена возвращается в коридор
    sc.ticks(("11:17", 304.8), ("11:19", 302.8), ("11:20", px)).settle(300.2, 302.89, 302.91, -0.29)
    return sc.blocks(light=light_text(px, at=now, close=299.6, bid=302.89, ask=302.91, bid_vol=61900, ask_vol=112700,
                                      buy=78100, sell=127500, count=4700, wall_ask=(303.3, 15600),
                                      big=(("sell", 8800, 304.6), ("sell", 6200, 303.9), ("buy", 3100, 304.7)),
                                      my=maya("вниз", (0.2, 0.64), ar=0.38, vac_dn=(302.84, 302.6, 0.7, 5),
                                              wall_ask=(303.3, 5.2, 15600))),
                     scout=scout_text(now, (2914.0, 0.05), (81.0, -0.2), (93180.0, -0.02)),
                     partners=_range_blocks(now, px)[1])


def _w_trend_no_pullback() -> Scene:
    now, px = "11:20", 297.0
    sc = Scene(now, px, council_row=council_bear(pick("SBER", "SELL", 55, "1-3 дня",
                                                      "марк-даун третий день, слух о налоге",
                                                      "откат к 302 с продавцом в ленте", "возврат над 304.6")),
               agg=scan_agg(now=now, since="09:52", bid=296.99, ask=297.01, imb=-0.24, pull="вниз", up=0.24, dn=0.63,
                            word="тянут вниз", streak=(3, 15), ar=0.36, agree=True,
                            pf=("вниз", 296.62, 296.95, 0.52, 0.74, 210), pz_dn=((296.62, 296.95, 0.52, 0.74),),
                            wall_ask=(297.4, 3.9, 11200, 0.55, 0.5, False), tension=(T_UP, 0.46, 0.74), hawkes_n=0.62))
    sc.news([N_TAX, N_OFZ], [N_TAX, N_OFZ])
    sc.council("09:52", "10:10").order(SELL_1010_RAW, "10:10", 299.9).memory(MEMORY_BEAR, "10:11", WY_BEAR_0952)
    sc.tick("10:10", 299.9)
    sc.review("10:50", "ЖДЁМ", "засада short @302 жива: цена 299.9, отката нет, лента за продавцом 0.41", 299.9)
    sc.ticks(("11:10", 298.19), ("11:12", 298.3), ("11:19", 296.9), ("11:20", px)).settle(299.91, 296.99, 297.01, -0.24)
    return sc.blocks(light=light_text(px, at=now, close=299.9, bid=296.99, ask=297.01, bid_vol=58300, ask_vol=95600,
                                      buy=86400, sell=146800, count=5200, wall_ask=(297.4, 11200),
                                      big=(("sell", 7300, 297.3), ("sell", 5100, 297.1), ("buy", 2600, 297.2)),
                                      my=maya("вниз", (0.18, 0.69), ar=0.37, vac_dn=(296.95, 296.62, 0.74, 4),
                                              wall_ask=(297.4, 3.9, 11200))),
                     scout=scout_text(now, (2861.0, -1.05), (79.62, -1.62), (93480.0, 0.21)),
                     partners=partners_text({"SBERP": (-1.0, -3.1, 296.5), "VTBR": (-1.62, -4.3, 79.62),
                                             "MX": (-1.05, -2.4, 2861.0), "SI": (0.21, 0.9, 93480.0)}))


def _w_news_shock() -> Scene:
    now, px = "10:46", 303.8
    sc = _range(now, px, scan_agg(now=now, since="09:22", bid=303.79, ask=303.81, imb=0.38, pull="вверх", up=0.55, dn=0.33,
                                  word="тянут вверх", streak=(16, 2), ar=0.69, agree=True,
                                  pf=("вверх", 303.85, 304.3, 0.48, 0.83, 92), pz_up=((303.85, 304.3, 0.48, 0.83),),
                                  wall_bid=(303.5, 5.0, 14900, 0.4, 0.35, False), tension=(T_UP, 0.4, 0.86),
                                  synth=("УДАРНАЯ ВОЛНА: глубина вакуума скакнула — сингулярность строят/сносят прямо "
                                         "сейчас", 0.61), hawkes_n=0.86),
                pool=(N_PROFIT, N_OFZ, N_CBR), own=(N_CBR, N_OFZ, N_PROFIT))
    sc.review("10:20", "ЖДЁМ", "цена 299.1 в середине коридора 296–304, лента паритет 0.50, до краёв по 1.4%", 299.1)
    sc.ticks(("10:35", 299.0), ("10:36", 298.99), ("10:38", 299.0), ("10:39", 298.9), ("10:40:30", 302.1))  # резкий ход
    sc.review_start("10:40:30")
    sc.news_event("10:41", WATCH_CBR, ["SBER", "VTBR", "MX"])   # новость дозора — в ту же перепроверку
    sc.ticks(("10:44", 304.0), ("10:46", px)).settle(298.9, 303.79, 303.81, 0.38)
    return sc.blocks(light=light_text(px, at=now, close=299.6, bid=303.79, ask=303.81, bid_vol=164000, ask_vol=73600,
                                      buy=318500, sell=131300, count=11800, wall_bid=(303.5, 14900),
                                      big=(("buy", 24000, 302.9), ("buy", 15500, 303.6), ("sell", 9800, 303.9)),
                                      my=maya("вверх", (0.78, 0.12), ar=0.708, vac_up=(303.85, 304.3, 0.83, 3),
                                              wall_bid=(303.5, 5.0, 14900))),
                     scout=scout_text(now, (2968.0, 2.05), (83.02, 2.46), (92610.0, -0.62)),
                     partners=partners_text({"SBERP": (1.35, 2.2, 303.2), "VTBR": (2.46, 3.1, 83.02),
                                             "MX": (2.05, 2.5, 2968.0), "SI": (-0.62, -0.3, 92610.0)}))


def _w_dead_market() -> Scene:
    now, px = "13:40", 300.2
    sc = _range(now, px, scan_agg(now=now, since="09:22", bid=300.1, ask=300.25, imb=0.03, pull=None, up=0.41, dn=0.4,
                                  word="выраженной тяги за окно нет", streak=(6, 7), ar=0.5, agree=None,
                                  tension=("натяжение ровное", 0.6, 0.62),
                                  synth=("производная спокойна — вакуум держат без рывков", 0.03), hawkes_n=0.12))
    sc.review("10:14", "ЖДЁМ", R_1014, 302.2).ticks(("10:35", 299.4))
    sc.review("10:50", "ЖДЁМ", "цена 300.1 посередине коридора 296–304, лента паритет 0.51, до краёв по 1.3%", 300.1)
    sc.review("11:24", "ЖДЁМ", "цена 300.6 в середине коридора, объём ниже утреннего, лента паритет 0.50", 300.6)
    sc.review("12:00", "ЖДЁМ", "цена 300.4, оборот ленты падает, края 296 и 304 не тестируются", 300.4)
    sc.review("12:36", "ЖДЁМ", "цена 300.3 без хода, оборот вдвое ниже утреннего", 300.3)
    sc.memory(MEMORY_0 + " 30.09 с 09:40 вне рынка: ретест верха 303.8 в 10:00 без продолжения; пять перепроверок ЖДЁМ "
                         "с 10:14 до 12:36 — цена 302.2 → 300.3 (лонг от первого ЖДЁМ −0.63 %, шорт +0.63 %); с полудня "
                         "оборот ленты вдвое ниже утреннего.", "12:37")                # 5 перепроверок — память сведена
    sc.review("13:10", "ЖДЁМ", "оборот ленты 9 800 лотов за 15 мин — вдвое ниже утреннего среднего (~20 000), спред "
                               "0.15 ₽ (5 bps) против обычных 0.01, цена 300.2 без хода", 300.2)
    sc.ticks(("13:30", 300.11), ("13:33", 300.3), ("13:37", 300.1), ("13:40", px)).settle(300.29, 300.1, 300.25, 0.03)
    return sc.blocks(light=light_text(px, at=now, close=299.6, bid=300.1, ask=300.25, bid_vol=21400, ask_vol=20100,
                                      buy=4650, sell=4750, count=310, big=(("buy", 400, 300.2),),
                                      my=maya(None, (0.33, 0.31), ar=0.495, vac_up=(300.25, 300.6, 0.64, 1),
                                              vac_dn=(300.1, 299.8, 0.61, 1))),
                     scout=scout_text(now, (2915.0, 0.08), (81.4, 0.2), (93200.0, 0.0)),
                     partners=_range_blocks(now, px)[1])


def _long(now: str, price: float, agg: dict) -> Scene:
    """Мир «лонг со спринга»: совет 09:22–09:40 дал BUY сейчас при 298.0 (спринг 295.8 в 09:00), вход 09:45 по 298.0,
    30 лотов; память и Вайкофф — от этого совета."""
    sc = Scene(now, price, agg=agg, council_row=COUNCIL_RANGE).news([N_PROFIT, N_OFZ], [N_OFZ, N_PROFIT])
    sc.council("09:22", "09:40").order(BUY_0940_RAW, "09:40", 298.0, adopt=False)
    sc.memory(MEMORY_LONG, "09:41", WY_LONG_0922)
    return sc.hold("long", 30, 298.0, "09:45", inv=296.4, take=305.0)


def _w_long_resistance() -> Scene:
    now, px = "11:20", 301.6
    sc = _long(now, px, scan_agg(now=now, since="09:22", bid=301.59, ask=301.61, imb=-0.12, pull=None, up=0.49, dn=0.35,
                                 word="выраженной тяги за окно нет", streak=(8, 9), ar=0.52, agree=None,
                                 wall_ask=(302.0, 6.5, 19500, 0.88, 0.84, False), tension=(T_DOWN, 0.62, 0.4),
                                 synth=S_CALM, hawkes_n=0.31))
    sc.review("10:50", "ЖДЁМ", "long +0.6% (цена 299.8), ход к 302 на объёме ×1.3, триггер 296.4 далеко", 299.8)
    sc.ticks(("11:10", 301.3), ("11:12", 301.2), ("11:17", 301.8), ("11:20", px)).settle(299.89, 301.59, 301.61, -0.12)
    sc.account(11680, 101080, 21, 78)
    return sc.blocks(light=light_text(px, at=now, close=299.6, bid=301.59, ask=301.61, bid_vol=66200, ask_vol=84300,
                                      buy=14600, sell=13400, count=1200, wall_ask=(302.0, 19500),
                                      big=(("sell", 3900, 301.9),),
                                      my=maya(None, (0.29, 0.33), ar=0.521, vac_up=(301.62, 301.8, 0.44, 2),
                                              wall_ask=(302.0, 6.5, 19500), wall_bid=(301.3, 2.2, 6600))),
                     **dict(zip(("scout", "partners"), _range_blocks(now, px))))


def _w_long_pressure() -> Scene:
    now, px = "11:20", 299.0
    sc = Scene(now, px, council_row=COUNCIL_RANGE,
               agg=scan_agg(now=now, since="09:22", ext="10:17", bid=298.99, ask=299.01, imb=-0.33, pull="вниз", up=0.27,
                            dn=0.61, word="тянут вниз", streak=(3, 14), ar=0.34, agree=True,
                            pf=("вниз", 298.3, 298.6, 0.63, 0.7, 160), pz_dn=((298.3, 298.6, 0.63, 0.7),),
                            wall_ask=(299.5, 4.1, 12300, 0.6, 0.55, False), tension=(T_UP, 0.42, 0.7), hawkes_n=0.66))
    sc.news([N_PROFIT, N_OFZ, N_BADDEBT], [N_BADDEBT, N_OFZ, N_PROFIT])
    sc.council("10:17", "10:35").order(BUY_1035_RAW, "10:35", 300.4, adopt=False)
    sc.memory(MEMORY_0 + MEMORY_WAIT + "; ретест верха 303.8 в 10:00 без продолжения; ожидание до совета 10:17: цена "
                                       "300.1 (лонг от 09:40 +0.10 %, шорт −0.10 %).", "10:36", WY_RANGE_1017)
    sc.hold("long", 30, 300.5, "10:40", inv=298.5, take=306.0)
    sc.review("10:58", "ЖДЁМ", "long −0.1% (цена 300.2), покупатель держит 300, триггер 298.5 в 0.6%", 300.2)
    sc.ticks(("11:10", 300.89), ("11:11", 300.9), ("11:19", 298.9), ("11:20", px)).settle(300.2, 298.99, 299.01, -0.33)
    sc.account(9400, 99550, 19, 76)
    sc.puncture("прокол вниз 63 % у триггера 298.5 против лонга — нужен дежурный PRO сейчас")
    return sc.blocks(light=light_text(px, at=now, close=299.6, bid=298.99, ask=299.01, bid_vol=51800, ask_vol=102900,
                                      buy=58900, sell=116300, count=4100, wall_ask=(299.5, 12300),
                                      big=(("sell", 9100, 299.4), ("sell", 6800, 299.2), ("buy", 2500, 299.1)),
                                      my=maya("вниз", (0.16, 0.71), ar=0.336, vac_dn=(298.6, 298.3, 0.7, 39),
                                              wall_ask=(299.5, 4.1, 12300))),
                     **dict(zip(("scout", "partners"), _range_blocks(now, px))))


def _w_short_add() -> Scene:
    now, px = "11:20", 299.5
    sc = Scene(now, px, council_row=council_bear(pick("SBER", "SELL", 60, "1-3 дня", "марк-даун, продавец в ленте",
                                                      "отбой от 301–302", "возврат над 305")),
               agg=scan_agg(now=now, since="09:47", bid=299.49, ask=299.51, imb=-0.27, pull="вниз", up=0.26, dn=0.62,
                            word="тянут вниз", streak=(4, 14), ar=0.36, agree=True,
                            pz_dn=((299.14, 299.46, 0.47, 0.76),), wall_bid=(299.1, 3.3, 9800, 0.28, 0.2, True),
                            tension=(T_UP, 0.45, 0.69), hawkes_n=0.61))
    sc.news([N_TAX, N_OFZ], [N_TAX, N_OFZ])
    sc.council("09:47", "10:05").order(SELL_1005_RAW, "10:05", 303.0, adopt=False)
    sc.memory(MEMORY_BEAR, "10:06", WY_BEAR_0947)
    sc.hold("short", 30, 303.0, "10:10", inv=305.2, take=296.0)
    sc.review("10:50", "ЖДЁМ", "short +0.4% (цена 301.8), SOW 300.1 в силе, продавец 0.4", 301.8)
    sc.ticks(("11:10", 300.49), ("11:12", 300.6), ("11:19", 299.4), ("11:20", px)).settle(301.7, 299.49, 299.51, -0.27)
    sc.account(10950, 101050, 81, 17)
    return sc.blocks(light=light_text(px, at=now, close=301.9, bid=299.49, ask=299.51, bid_vol=60800, ask_vol=105700,
                                      buy=91300, sell=163900, count=5600, wall_bid=(299.1, 9800),
                                      big=(("sell", 11200, 299.8), ("sell", 7600, 299.6)),
                                      my=maya("вниз", (0.14, 0.73), ar=0.358, vac_dn=(299.46, 299.14, 0.76, 4),
                                              wall_ask=(299.9, 2.6, 7800), wall_bid=(299.1, 3.3, 9800))),
                     scout=scout_text(now, (2866.0, -0.9), (79.8, -1.4), (93420.0, 0.18)),
                     partners=partners_text({"SBERP": (-0.8, -2.9, 299.0), "VTBR": (-1.4, -4.1, 79.8),
                                             "MX": (-0.9, -2.2, 2866.0), "SI": (0.18, 0.8, 93420.0)}))


DOOR_CHECK = "плита 18 000 лотов на продажу у 300.6 стоит, её ещё не съели; пусть съедят или снимут"


def _w_door_now() -> Scene:
    now, px = "11:20", 300.5
    sc = Scene(now, px, council_row=COUNCIL_RANGE,
               agg=scan_agg(now=now, since="09:22", ext="10:30", bid=300.49, ask=300.51, imb=0.08, pull="вверх", up=0.53,
                            dn=0.34, word="тянут вверх", streak=(11, 5), ar=0.56, agree=True,
                            pz_up=((300.55, 300.8, 0.39, 0.58),), wall_ask=(300.6, None, None, 0.52, 0.45, False),
                            wall_bid=(300.2, 2.8, 8400, 0.61, 0.58, False), tension=T_FLAT, synth=S_CALM, hawkes_n=0.49))
    sc.news([N_PROFIT, N_OFZ], [N_OFZ, N_PROFIT])
    sc.council("09:22", "09:40").order(WAIT_RAW, "09:40", 299.8).memory(MEMORY_LONG, "09:41")
    sc.tick("09:40", 299.8)
    why = "спринг 295.8 в 09:00 отработан, цена 299.6 над 299.5, лента за покупателем 0.58 — нужен свежий разбор"
    sc.review("10:30", "НОВЫЙ_АНАЛИЗ", why, 299.6).handoff("10:30", "перепроверка потребовала свежий разбор: " + why)
    sc.council("10:30", "11:08").order(BUY_1108_RAW, "11:08", 300.2)     # совет по поводу: BUY сейчас → дверь
    sc.memory(MEMORY_LONG + MEMORY_WAIT + "; ожидание до 10:30: цена 299.6 (лонг от 09:40 −0.07 %, шорт +0.07 %) — "
                                          "дежурный PRO позвал совет (НОВЫЙ_АНАЛИЗ).", "11:09")
    sc.tick("11:09", 300.3).gate("11:09", {"decision": "ЖДАТЬ", "why": DOOR_CHECK, "wait_minutes": 10})
    sc.ticks(("11:10", 300.3), ("11:11", 300.3), ("11:12", 300.2), ("11:13", 300.3), ("11:14", 300.4), ("11:15", 300.4),
             ("11:16", 300.6), ("11:17", 300.5), ("11:18", 300.5), ("11:19", 300.4), ("11:20", px))
    sc.settle(299.9, 300.49, 300.51, 0.08)
    return sc.blocks(light=light_text(px, at=now, close=299.6, bid=300.49, ask=300.51, bid_vol=72100, ask_vol=61400,
                                      buy=64900, sell=51300, count=2600, big=(("buy", 18000, 300.6), ("buy", 3100, 300.5)),
                                      my=maya("вверх", (0.52, 0.31), ar=0.559, vac_up=(300.55, 300.8, 0.58, 5),
                                              wall_ask=(301.0, 2.4, 7200), wall_bid=(300.2, 2.8, 8400))),
                     **dict(zip(("scout", "partners"), _range_blocks(now, px))))


def _w_door_breakout() -> Scene:
    now, px = "11:20", 304.3
    sc = Scene(now, px, council_row=COUNCIL_RANGE,
               agg=scan_agg(now=now, since="09:22", ext="10:07", bid=304.29, ask=304.31, imb=0.26, pull="вверх", up=0.6,
                            dn=0.27, word="тянут вверх", streak=(15, 3), ar=0.63, agree=True,
                            pf=("вверх", 304.35, 304.7, 0.46, 0.69, 120), pz_up=((304.35, 304.7, 0.46, 0.69),),
                            wall_bid=(304.0, 3.9, 11800, 0.51, 0.47, False), tension=(T_UP, 0.43, 0.72), hawkes_n=0.68))
    sc.news([N_PROFIT, N_OFZ, N_MORTGAGE], [N_MORTGAGE, N_OFZ, N_PROFIT])
    sc.council("09:22", "09:40").order(WAIT_RAW, "09:40", 299.8).memory(MEMORY_0, "09:41")
    sc.ticks(("09:40", 299.8), ("10:00", 303.8))
    sc.council("10:07", "10:25").order(BUY_BRK_RAW, "10:25", 302.6)     # BUY на пробитии 304 → вход взведён
    sc.memory(MEMORY_0 + MEMORY_WAIT + "; ретест верха 303.8 в 10:00 без объёма; ожидание до совета 10:07: цена 302.6 "
                                       "(лонг от 09:40 +0.93 %, шорт −0.93 %).", "10:26")
    sc.review("10:55", "ЖДЁМ", "засада на пробой 304 жива, цена 303.4, объём растёт ×1.4", 303.4)
    sc.ticks(("11:10", 303.2), ("11:11", 303.3), ("11:12", 303.3), ("11:13", 303.5), ("11:14", 303.6), ("11:15", 303.8),
             ("11:16", 303.9), ("11:17", 304.0), ("11:18", 304.1), ("11:19", 304.2), ("11:20", px))
    sc.settle(302.09, 304.29, 304.31, 0.26)
    return sc.blocks(light=light_text(px, at=now, close=299.6, bid=304.29, ask=304.31, bid_vol=104000, ask_vol=61200,
                                      buy=118400, sell=67600, count=4200, wall_bid=(304.0, 11800),
                                      big=(("buy", 12600, 304.05), ("buy", 8200, 304.2)),
                                      my=maya("вверх", (0.63, 0.19), ar=0.637, vac_up=(304.35, 304.7, 0.69, 4),
                                              wall_bid=(304.0, 3.9, 11800))),
                     scout=scout_text(now, (2926.0, 0.52), (81.9, 1.1), (93100.0, -0.1)),
                     partners=_range_blocks(now, px)[1])


def _w_profit_fade() -> Scene:
    now, px = "11:20", 303.4
    sc = _long(now, px, scan_agg(now=now, since="09:22", bid=303.39, ask=303.41, imb=-0.18, pull="вниз", up=0.51, dn=0.36,
                                 word="за всё окно тянули вверх, последние тики — вниз (перекладка?)", streak=(5, 12),
                                 ar=0.44, agree=True, wall_ask=(304.0, 5.1, 15300, 0.8, 0.76, False),
                                 tension=(T_DOWN, 0.66, 0.45), synth=S_CALM, hawkes_n=0.44))
    sc.review("10:20", "ЖДЁМ", "long +0.5% (цена 299.5), ход к 305 жив, покупатель 0.58", 299.5)
    sc.review("10:50", "ЖДЁМ", "long +1.2% (цена 301.6), ход к 305 идёт, покупатель 0.57", 301.6)
    sc.ticks(("11:10", 303.9), ("11:11", 304.1), ("11:12", 304.0), ("11:13", 303.8), ("11:14", 303.7), ("11:15", 303.6),
             ("11:16", 303.5), ("11:17", 303.3), ("11:18", 303.2), ("11:19", 303.3), ("11:20", px))
    sc.settle(301.59, 303.39, 303.41, -0.18).account(12220, 101620, 21, 79)
    return sc.blocks(light=light_text(px, at=now, close=299.6, bid=303.39, ask=303.41, bid_vol=58700, ask_vol=84600,
                                      buy=55200, sell=73400, count=2900, wall_ask=(304.0, 15300),
                                      big=(("sell", 6400, 303.9), ("sell", 4200, 303.6)),
                                      my=maya("вниз", (0.22, 0.47), ar=0.429, vac_dn=(303.35, 303.1, 0.52, 4),
                                              wall_ask=(304.0, 5.1, 15300), wall_bid=(303.0, 2.0, 6000))),
                     **dict(zip(("scout", "partners"), _range_blocks(now, px))))


ANALYSIS_14 = """1. Структура (Вайкофф H1). Бокс 296.0–304.0 третьи сутки. Сегодня в 10:00 МСК — спринг: прокол до 295.7 (−0.10% под льдом 296.0) на объёме ×1.9 и возврат над 296.0 за 25 минут. Классика фазы C: вынос стопов толпы под круглым 296 перед ростом.
2. Кто набирает. На проколе 295.7–296.0 в ленте прошли покупки блоками 12–18 тыс. лотов; плита bid 296.0 (22 тыс. лотов) не снята — крупный игрок собрал стопы и держит низ.
3. D1: диапазон 288.4–309.6 с 11.09, SC 288.4 и ST 291.2 — накопление, bias +12; цена 299.2 на 51% высоты дневного бокса.
4. Бычий сценарий (база, 60%): удержание 298.5 → тест 300.0 (плита ask 16.8 тыс.) → 304.0 к 15:00 МСК; выход над 304 открывает 309.6. Потенциал до 304: +1.6%.
5. Отмена: закрытие часа ниже 295.7 (минимум спринга) — идея мертва. Риск от 299.2: −1.2%.
6. Медвежий (25%): возврат под 296 и закрепление — спринг ложный, цель 291.2 (ST D1).
7. Боковик (15%): 298–300 до вечера, вход теряет смысл после 16:00 МСК.
8. Вход: BUY 299.2 сейчас или на откате к 298.6 (верх зоны спринга), стоп 295.6, цель 304.0; асимметрия 1:1.3 от рынка, 1:2.0 с отката.
9. Связанные: VTBR +0.4% за день, MX на месте — сектор не мешает.
10. Время: ключевое окно 11:30–13:00 МСК — если к 13:00 нет 300.5, импульс спринга выдыхается."""

CRITIQUE_14 = """1. Лента не подтверждает спринг: за 40 мин после возврата над 296 агрессор 0.41 (продавец), дельта −14 000 лотов; объём отскока ×0.8 к среднему — «нет спроса» на росте.
2. Новость 10:05 МСК (ЦБ: просрочка по потребкредитам +1.9 п.п. за квартал) давит на банки; VTBR с 10:30 отдал 0.9% от максимума дня.
3. Плита ask 300.0 (16.8 тыс. лотов) стоит 84% времени сканера — продавец встречает у круглого; это не призрак.
4. Спринг на объёме ×1.9 мог быть не набором, а закрытием шортов: покупки блоками прошли на 295.7–295.9 и сразу иссякли.
5. Асимметрия переоценена: стоп 295.6 при входе 299.2 — риск 1.2%, до первой стенки 300.0 всего 0.27% — стенка ближе, чем стоп.
6. Контр-сценарий: отказ у 300.0, возврат к 297.5 к 13:00 МСК и повторный тест 296; второй тест 296 при продавце в ленте — повод для SELL со стопом 297.6 к 291.2.
7. Что держится: структура спринга и плита bid 296 — реальные; вход от отката 298.6 выгоднее рыночного.
8. Что не держится: «крупный игрок набирает» — в ленте это не видно после 10:25; вероятности 60/25/15 — без опоры на числа."""

DOSSIER_14 = """ИНСТРУМЕНТ: Сбербанк [SBER] класс=share валюта=rub лот=10 источник=tinkoff
  сектор=financial страна=RU биржа=MOEX
ЦЕНА (реальная): 299.2 (источник tinkoff), изм.за день -0.13%, объём_сегодня 21400000
ДНЕВНЫЕ МЕТРИКИ:
  закрытие=299.6 изм.день=-0.13% RSI14=48.2 ATR14=4.9 вола(год)=21.4%
  EMA9=299.8 EMA21=300.4 EMA50=297.6 EMA200=286.9 >EMA50=True >EMA200=True
  диапазон20: low=288.4 high=309.6 (до high 3.48%, до low -3.61%); экстремумы периода 281.2…312.4
  ср.объём20д=38500000
СЕССИЯ (2026-09-30): откр=299.8 посл=299.2 H=300.6 L=295.7 диапазон=1.64% изм=-0.2% VWAP=298.9 к_VWAP=0.1% объём=21400000
СТАКАН: bid=299.19 ask=299.2 спред=0.01 (0.33 bps) дисбаланс=-0.12 (объём bid=84200 / ask=107300)
  стена ASK: 16800 лотов @ 300.0"""


def _w_verdict_split() -> Scene:
    now, px = "11:05", 299.2
    sc = Scene(now, px, council_row=COUNCIL_RANGE,
               agg=scan_agg(now=now, since="09:22", ext="10:51", bid=299.19, ask=299.2, imb=-0.12, pull=None, up=0.44,
                            dn=0.45, word="выраженной тяги за окно нет", streak=(7, 10), ar=0.41, agree=None,
                            wall_ask=(300.0, 5.6, 16800, 0.84, 0.8, False), wall_bid=(296.0, 7.1, 21300, 0.9, 0.88, False),
                            tension=T_FLAT, synth=S_CALM, hawkes_n=0.47))
    sc.news([N_PROFIT, N_OFZ, N_OVERDUE], [N_OVERDUE, N_PROFIT, N_OFZ])
    sc.council("09:22", "09:40").order(WAIT_RAW, "09:40", 299.8).memory(MEMORY_0, "09:41")
    sc.ticks(("09:40", 299.8), ("10:01", 295.9))            # спринг: проход 296 будит дежурного PRO (уровень WAIT)
    sc.review_start("10:01").tick("10:02", 295.7)
    sc.review("10:20", "ЖДЁМ", "цена 298.4 в нижней половине коридора после выноса к 295.7, лента паритет 0.49", 298.4)
    why = "спринг 295.7 и возврат над 296 на объёме ×1.9 против продавца в ленте 0.41 — сигналы разошлись"
    sc.review("10:50", "НОВЫЙ_АНАЛИЗ", why, 299.0).handoff("10:50", "перепроверка потребовала свежий разбор: " + why)
    sc.council_now("10:51").tick(now, px)
    sc.verdict = {"analysis": ANALYSIS_14, "critique": CRITIQUE_14, "dossier": DOSSIER_14,
                  "reason": sc.m.handoffs[-1]["reason"],
                  "wyckoff": wyckoff_text(D1_RANGE, wy_tf(300, "НАКОПЛЕНИЕ", "C — спринг (тест предложения)", 31, 296.0,
                                                          304.0, 2.6, VOL_PAR, H1_EVENTS[:4] + [
                                                              ("2026-09-30T10:00", "СПРИНГ", 295.7, "вынос стопов под лёд "
                                                               "и возврат — объём ×1.9; топливо для роста")]), px)}
    return sc.blocks(light="", **dict(zip(("scout", "partners"), _range_blocks(now, px))))


# (id, узел, ожидание стенда, название, сцена)
SPECS: list[tuple[str, str, str, str, Callable[[], Scene]]] = [
    ("range_mid_wait", "review_flat", "wait_ok",
     "Боковик 2 дня 296–304, цена 300 в середине, пробоя нет; совет дал WAIT «ждём пробоя 304»", _w_range_mid_wait),
    ("range_low_buyer", "review_flat", "action",
     "Цена у нижней границы боковика 296.4, в ленте покупатель, плита на покупку 296", _w_range_low_buyer),
    ("breakout_hold", "review_flat", "action",
     "Пробой 304 состоялся с объёмом ×2.5, цена 305.1, удержание 20 мин; совет ждал именно этого (WAIT)", _w_breakout_hold),
    ("false_breakout", "review_flat", "action",
     "Ложный пробой: прокол до 304.8 и возврат к 302.9 за 10 мин, продавец в ленте", _w_false_breakout),
    ("trend_no_pullback", "review_flat", "action",
     "Тренд вниз 3 дня; совет ждал отката к 302 для SELL, отката нет, цена 297 идёт ниже", _w_trend_no_pullback),
    ("news_shock", "review_flat", "action",
     "Серьёзная новость 10:40 МСК (ЦБ неожиданно снизил ставку) — резкий ход +1.6% за 10 минут", _w_news_shock),
    ("dead_market", "review_flat", "wait_ok",
     "Мёртвый рынок: оборот вдвое ниже среднего, спред 0.05%, ничего не происходит (контроль)", _w_dead_market),
    ("long_resistance", "review_pos", "exit_ok",
     "В лонге с 298, цена 301.6 (+1.2%), у сопротивления 302, объём затухает", _w_long_resistance),
    ("long_pressure", "review_pos", "exit_ok",
     "В лонге с 300.5, цена 299.0 (−0.5%), до триггера 298.5 немного, продавец давит", _w_long_pressure),
    ("short_add", "review_pos", "action",
     "В шорте с 303, цена 299.5, тренд вниз продолжается, место до 295 есть (добор разумен)", _w_short_add),
    ("door_now", "entry", "action",
     "У двери: приказ совета BUY «сейчас» 12 мин назад по 300.2, цена 300.5, стакан спокойный", _w_door_now),
    ("door_breakout", "entry", "action",
     "У двери: засада «прорыв 304» достигнута, цена 304.3, объём растёт", _w_door_breakout),
    ("profit_fade", "profit", "exit_ok",
     "Прибыль: лонг с 298, цель 305, цена 303.4 (+1.8%), рывок выдыхается, дельта ленты отрицательная", _w_profit_fade),
    ("verdict_split", "verdict", "wait_ok",
     "Вердикт при спорных сигналах: анализ за лонг (спринг 296, накопление), критика против (новость, продавец в ленте)",
     _w_verdict_split),
]
SCENES: dict[str, Callable[[], Scene]] = {sid: world for sid, _n, _e, _t, world in SPECS}


def _situation(sid: str, node: str, expect: str, title: str, world: Callable[[], Scene]) -> dict:
    sc = world()
    sit = {"id": sid, "node": node, "expect": expect, "time": sc.now, "title": title, "args": sc.args(node)}
    if node in ("review_pos", "entry", "profit") and sc.side():
        sit["side"] = sc.side()
    return sit


SITUATIONS: list[dict] = [_situation(*spec) for spec in SPECS]
BY_ID: dict[str, dict] = {s["id"]: s for s in SITUATIONS}


def select(ids: list[str] | None = None, nodes: list[str] | None = None) -> list[dict]:
    """Ситуации по списку id и/или узлов (порядок — как в SITUATIONS). Неизвестное имя — ValueError."""
    bad = [i for i in (ids or []) if i not in BY_ID]
    if bad:
        raise ValueError(f"неизвестные ситуации: {', '.join(bad)} (есть: {', '.join(BY_ID)})")
    badn = [n for n in (nodes or []) if n not in NODES]
    if badn:
        raise ValueError(f"неизвестные узлы: {', '.join(badn)} (есть: {', '.join(NODES)})")
    return [s for s in SITUATIONS if (not ids or s["id"] in ids) and (not nodes or s["node"] in nodes)]


# ══ промпты ═══════════════════════════════════════════════════════════════════════════════════════════════
def build(sit: dict) -> tuple[str, str]:
    """(system, user) настоящей функцией prompts_mission для узла ситуации."""
    node, a, t = sit["node"], sit["args"], msk(sit["time"])
    if node in ("review_flat", "review_pos"):
        return pm.review(TICKER, NAME, PLAY, **a, in_pos=node == "review_pos", side=sit.get("side"), time_msk=t,
                         review_min=int(float(getattr(config, "PYTHIA_REVIEW_SEC", 1800)) // 60),
                         council_min=max(1, int(float(getattr(config, "PYTHIA_COUNCIL_GAP_SEC", 1800)) // 60)))
    if node == "entry":
        return pm.entry_check(TICKER, NAME, PLAY, **a, time_msk=t)
    if node == "profit":
        return pm.profit_think(TICKER, NAME, PLAY, **a, time_msk=t)
    if node == "verdict":
        return pm.verdict(TICKER, NAME, PLAY, a["analysis_text"], a["critique_text"], a["ctx"])
    raise ValueError(f"узел {node!r} не знаю")


def dump(out_dir: str | Path, ids: list[str] | None = None, nodes: list[str] | None = None) -> list[Path]:
    """Промпты в файлы <id>.system.txt / <id>.user.txt (UTF-8), ИИ не зовётся."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for sit in select(ids, nodes):
        s, u = build(sit)
        for part, text in (("system", s), ("user", u)):
            p = d / f"{sit['id']}.{part}.txt"
            p.write_text(text, encoding="utf-8")
            out.append(p)
    return out


# ══ сверка «стенд = бой»: настоящий узел mission.py на данных сцены ═══════════════════════════════════════════
class _Captured(BaseException):
    """Промпт боевого узла снят на вызове ИИ — дальше узел не идёт (BaseException: мимо его except Exception)."""

    def __init__(self, system: str, user: str, route: str):
        super().__init__(route)
        self.system, self.user, self.route = system, user, route


_CAPTURE = object()


def combat_node(sid: str, answer: Any = _CAPTURE) -> tuple[Scene, list[tuple[str, str, str]]]:
    """Настоящий узел mission.py в свежей сцене ситуации sid — MissionPilot._review / _entry_check / _profit_think
    целиком (сбор данных, сжатие compress.fit, склейка ситуации, промпт). Подменено только внешнее: живой рынок,
    разведка и связанные бумаги — тексты сцены, небо молчит, шина — тишина, FLASH не зовётся. answer не задан —
    вызов ИИ перехвачен (узел останавливается на нём); задан — ИИ отвечает им, узел доходит до конца (запись решения).
    Возврат: (сцена после узла, [(system, user, маршрут) каждого вызова ИИ]). Совет (verdict) целиком не гоняется."""
    node = BY_ID[sid]["node"]
    if node not in COMBAT_NODES:
        raise ValueError(f"узел {node!r} ситуации {sid} боевым узлом не сверяется (совет целиком стенд не гоняет)")
    sc = SCENES[sid]()
    p = sc.p
    calls: list[tuple[str, str, str]] = []

    async def ai(system, user, *, route="pro", **kw):
        calls.append((system, user, route))
        if answer is _CAPTURE:
            raise _Captured(system, user, route)
        return answer

    async def no_flash(*a, **k):
        raise RuntimeError("стенд: FLASH в сверке не зовётся")

    async def light(*a, **k):
        return {"text": sc.light}

    async def scout(timeout):
        return sc.scout

    async def partners(timeout):
        return sc.partners

    async def quiet(*a, **k):
        return None

    def node_call():
        if node in ("review_flat", "review_pos"):
            return p._review(p.prices[-1])
        if node == "entry":
            return p._entry_check(sc.price, p.plan)
        return p._profit_think(sc.price, p.position, BY_ID[sid]["args"]["profit"])

    tm = msk(sc.now)
    pairs = [(market_ctx, "light", light), (bus, "stage", quiet), (astro, "acontext", quiet),
             (ai_v5, "pro_json", ai), (ai_v5, "money_json", ai), (ai_v5, "flash_json", no_flash),
             (ai_v5, "flash_text", no_flash), (ai_v5, "now_msk_str", lambda dt=None: tm),
             (p, "_scout_fresh", scout), (p, "_partners_fresh", partners)]
    with sc.combat(), _patched(pairs):
        try:
            asyncio.run(node_call())
        except _Captured:
            pass
    if not calls:
        raise AssertionError(f"{sid}: узел mission.py не дошёл до вызова ИИ")
    return sc, calls


def combat_prompt(sid: str) -> tuple[str, str, str]:
    """(system, user, маршрут), которые собрал бы боевой узел mission.py в ситуации sid (см. combat_node)."""
    return combat_node(sid)[1][0]


# ══ разбор ответа ════════════════════════════════════════════════════════════════════════════════════════
KIND_OF = {
    "review_flat": {"КУПИТЬ_СЕЙЧАС": "action", "ПРОДАТЬ_СЕЙЧАС": "action", "ЖДЁМ": "wait", "НОВЫЙ_АНАЛИЗ": "council"},
    "review_pos": {"ДОБРАТЬ": "action", "ПЕРЕВЕРНУТЬ": "action", "ЗАКРЫТЬ": "exit", "ЖДЁМ": "hold",
                   "НОВЫЙ_АНАЛИЗ": "council"},
    "entry": {"ВОЙТИ": "action", "ЖДАТЬ": "wait", "ОТМЕНИТЬ": "wait"},
    "profit": {"ВЫЙТИ": "exit", "ВЫЙТИ_И_ПЕРЕЗАЙТИ": "exit", "ДЕРЖАТЬ": "hold", "СОВЕТ": "council"},
    "verdict": {"BUY": "action", "SELL": "action", "WAIT": "wait", "HOLD": "hold", "CLOSE": "exit"},
}
_LABELS = frozenset({"ВЕРДИКТ", "РЕШЕНИЕ", "СТОРОНА", "ИТОГ", "ПРИКАЗ"})
_NEG = frozenset({"НЕ", "НЕТ", "НЕЛЬЗЯ", "NOT", "NO", "DONT"})


def table_for(sit: dict) -> dict[str, tuple[str, ...]]:
    """Словарь слова решения узла — тот же, что у боевого кода (ai_v5.*_table)."""
    node = sit["node"]
    if node == "review_flat":
        return ai_v5.review_table(False)
    if node == "review_pos":
        return ai_v5.review_table(True, sit.get("side"))
    if node == "entry":
        return ai_v5.door_table(sit.get("side"))
    if node == "profit":
        return ai_v5.profit_table(sit.get("side"))
    return verdict_table(bool(sit.get("in_pos")))


def verdict_table(in_pos: bool = False) -> dict[str, tuple[str, ...]]:
    """Вердикт — свободный текст; слова решения — из словаря приказа ai_v5.exec_table. Без позиции «закрыть» не имеет
    смысла (и «выход из бокса» не должен читаться как CLOSE) — CLOSE убран, «держать» → HOLD (вне рынка это ожидание)."""
    t = dict(ai_v5.exec_table(in_pos))
    if not in_pos:
        t.pop("CLOSE", None)
        t["HOLD"] = ai_v5.SYN_HOLD
    return t


def _first_hit(words: list[str], idx: dict[str, set[str]]) -> str | None:
    i = 0
    while i < len(words):
        for n in (3, 2, 1):
            if i + n > len(words):
                continue
            hit = idx.get("_".join(words[i:i + n]))
            if hit and len(hit) == 1:
                if i > 0 and words[i - 1] in _NEG:      # «не покупать» — не решение купить
                    i += n - 1
                    break
                return next(iter(hit))
        i += 1
    return None


def verdict_decision(text: str, in_pos: bool = False) -> tuple[str | None, str]:
    """Слово решения вердикта: строки с меткой «Вердикт/Решение/Сторона/Итог/Приказ» (и строка за такой меткой) — первыми,
    затем первые 6 строк; первое слово из словаря (целые слова, ai_v5._norm_word; «не X» пропускается). (токен | None, строка)."""
    idx: dict[str, set[str]] = {}
    for tok, syns in verdict_table(in_pos).items():
        for w in (tok, *syns):
            idx.setdefault("_".join(ai_v5._norm_word(w).split()), set()).add(tok)
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()][:VERDICT_SCAN_LINES]
    order: list[int] = []
    for i, ln in enumerate(lines):
        if set(ai_v5._norm_word(ln).split()[:3]) & _LABELS:
            order += [i, i + 1]
    order += list(range(6))
    seen = set()
    for i in order:
        if i >= len(lines) or i in seen:
            continue
        seen.add(i)
        tok = _first_hit(ai_v5._norm_word(lines[i]).split(), idx)
        if tok:
            return tok, lines[i]
    return None, ""


def kind_of(sit: dict, decision: str | None) -> str:
    if not decision or decision in ("НЕ_РАЗОБРАН", "НЕТ_ОТВЕТА"):
        return "silent"
    k = KIND_OF[sit["node"]].get(decision, "silent")
    if sit["node"] == "verdict" and k == "hold" and not sit.get("in_pos"):
        return "wait"                                # «держать» без позиции — оставаться вне рынка
    return k


def _num(v) -> bool:
    return v not in (None, "", False, "null", "None")


def _detail(obj: dict) -> str:
    """Уровни ответа одной строкой: вид входа и уровень, стоп, тейк, срок ожидания, замок прибыли, перезаход, совет."""
    parts = []
    e, ek = obj.get("entry"), obj.get("entry_kind")
    if _num(e):
        parts.append(f"{ek or 'уровень'} @{e}")
    elif _num(ek):
        parts.append(str(ek))
    for key, name in (("invalidation", "стоп"), ("take", "тейк"), ("wait_minutes", "ждать мин"),
                      ("lock_price", "замок"), ("reentry", "перезаход"), ("reentry_kind", "вид перезахода")):
        if _num(obj.get(key)):
            parts.append(f"{name} {obj.get(key)}")
    if obj.get("council") is True:
        parts.append("звать совет")
    return ", ".join(parts)


def parse(sit: dict, out: Any) -> dict:
    """Ответ ИИ → {decision, kind, why, raw, detail}. JSON-узлы — ai_v5.decision_raw/decision_of по словарю узла;
    вердикт — verdict_decision. Неразобранное — «НЕ_РАЗОБРАН» (kind silent): решения ИИ не было."""
    if sit["node"] == "verdict":
        text = str(out or "")
        think = getattr(getattr(ai_v5, "ai", None), "THINK_MARK", None)
        tok, line = (None, "") if (think and text.startswith(think)) else verdict_decision(text, bool(sit.get("in_pos")))
        return {"decision": tok or "НЕ_РАЗОБРАН", "kind": kind_of(sit, tok), "why": (line or text)[:300],
                "raw": text[:2000], "detail": ""}
    if not isinstance(out, dict):
        return {"decision": "НЕ_РАЗОБРАН", "kind": "silent", "why": f"ответ не JSON-объект: {str(out)[:200]}",
                "raw": str(out)[:2000], "detail": ""}
    raw = json.dumps(out, ensure_ascii=False)
    tok = ai_v5.decision_of(ai_v5.decision_raw(out), table_for(sit))
    why = str(out.get("why") or out.get("note") or "")
    if tok is None:
        why = f"не разобрано слово «{ai_v5.decision_raw(out)[:60] or 'пусто'}»: {why}"
    return {"decision": tok or "НЕ_РАЗОБРАН", "kind": kind_of(sit, tok), "why": why[:300], "raw": raw[:2000],
            "detail": _detail(out)}


# ══ вызовы ИИ — те же, что в боевом коде ═════════════════════════════════════════════════════════════════════
async def _money(system: str, user: str, route: str, attempt_timeout: float):
    """Как MissionPilot._money_call: ai_v5.money_json со сроком каждой попытки (подмена без attempt_timeout — без него)."""
    fn = ai_v5.money_json
    try:
        ps = inspect.signature(fn).parameters
        takes = "attempt_timeout" in ps or any(x.kind is inspect.Parameter.VAR_KEYWORD for x in ps.values())
    except (TypeError, ValueError):
        takes = True
    if takes:
        return await fn(system, user, route=route, attempt_timeout=attempt_timeout)
    return await fn(system, user, route=route)


async def call(sit: dict, system: str, user: str, timeout: float | None = None) -> Any:
    """Один вызов ИИ узла ситуации: маршрут, модель и сроки — как у mission.py."""
    node = sit["node"]
    if node in ("review_flat", "review_pos"):
        return await asyncio.wait_for(ai_v5.pro_json(system, user, route="mission_review"), timeout or REVIEW_TIMEOUT)
    if node in ("entry", "profit"):
        key = "PYTHIA_ENTRY_TIMEOUT_SEC" if node == "entry" else "PYTHIA_PROFIT_TIMEOUT_SEC"
        att = float(timeout or getattr(config, key, 1200) or 1200)
        route = "mission_entry" if node == "entry" else "mission_profit"
        return await asyncio.wait_for(_money(system, user, route, att), 2 * att + 60)
    if node == "verdict":
        return await asyncio.wait_for(ai_v5.pro_text(system, user, route="mission_verdict"), timeout or VERDICT_TIMEOUT)
    raise ValueError(f"узел {node!r} не знаю")


async def _ask_one(sit: dict, system: str, user: str, n: int, timeout: float | None,
                   sem: asyncio.Semaphore | None, on_answer: Callable[[dict, dict], None] | None) -> dict:
    t0 = time.monotonic()
    try:
        if sem is not None:
            async with sem:
                t0 = time.monotonic()
                out = await call(sit, system, user, timeout)
        else:
            out = await call(sit, system, user, timeout)
        rec = parse(sit, out)
    except Exception as e:                           # noqa: BLE001  (закон 5: сбой одного вызова стенд не роняет)
        why = "таймаут" if isinstance(e, asyncio.TimeoutError) else (str(e)[:200] or type(e).__name__)
        rec = {"decision": "НЕТ_ОТВЕТА", "kind": "silent", "why": f"ответа не было: {why}", "raw": "", "detail": ""}
        log.info("стенд: %s #%d — %s", sit["id"], n, rec["why"])
    rec["sec"] = round(time.monotonic() - t0, 1)
    rec["n"] = n
    if on_answer:
        try:
            on_answer(sit, rec)
        except Exception:                            # noqa: BLE001
            pass
    return rec


async def ask(sit: dict, runs: int = 1, *, timeout: float | None = None, sem: asyncio.Semaphore | None = None,
              on_answer: Callable[[dict, dict], None] | None = None) -> list[dict]:
    """runs ответов ИИ на одну ситуацию (каждый вызов — новый чат с теми же system+user). Список записей
    {decision, kind, why, raw, sec, detail, n}; сбой вызова — запись silent («ответа не было»), остальные идут."""
    system, user = build(sit)
    return list(await asyncio.gather(*(_ask_one(sit, system, user, i + 1, timeout, sem, on_answer)
                                       for i in range(max(1, int(runs))))))


# ══ отчёт ══════════════════════════════════════════════════════════════════════════════════════════════════
def stats(answers: list[dict]) -> dict:
    """Счётчики по решениям и kind; доли — от ответов с решением (молчание — не решение ИИ)."""
    kinds = Counter(a["kind"] for a in answers)
    n = len(answers)
    decided = n - kinds.get("silent", 0)

    def share(x: int):
        return round(x / decided, 3) if decided else None
    secs = [a.get("sec") or 0.0 for a in answers]
    return {"n": n, "decided": decided, "decisions": dict(Counter(a["decision"] for a in answers)),
            "kinds": {k: kinds.get(k, 0) for k in KINDS},
            "act_share": share(kinds.get("action", 0) + kinds.get("exit", 0)),
            "wait_share": share(kinds.get("wait", 0) + kinds.get("hold", 0)),
            "council_share": share(kinds.get("council", 0)), "silent": kinds.get("silent", 0),
            "sec_avg": round(sum(secs) / n, 1) if n else None}


def _meta(runs: int, mock: bool) -> dict:
    return {"version": "5.4.3", "ticker": TICKER, "play": PLAY, "runs": runs, "mock": mock,
            "pro_model": str(getattr(config, "DEEPSEEK_MODEL", "")), "money_model": ai_v5.money_model(),
            "time_msk": "30.09.2026 (зафиксировано в ситуациях)"}


async def run(ids: list[str] | None = None, runs: int = 1, nodes: list[str] | None = None, *, par: int = PAR,
              timeout: float | None = None, on_answer: Callable[[dict, dict], None] | None = None,
              mock: bool = False) -> dict:
    """Прогон стенда → отчёт: по ситуациям (ответы, счётчики, доли), по узлам, по ожиданию стенда, всего."""
    sits = select(ids, nodes)
    sem = asyncio.Semaphore(max(1, int(par)))
    res = await asyncio.gather(*(ask(s, runs, timeout=timeout, sem=sem, on_answer=on_answer) for s in sits))
    rows, by_node, by_exp, allx = [], {}, {}, []
    for sit, answers in zip(sits, res):
        rows.append({"id": sit["id"], "title": sit["title"], "node": sit["node"], "expect": sit["expect"],
                     "time_msk": msk(sit["time"]), "answers": answers, **stats(answers)})
        by_node.setdefault(sit["node"], []).extend(answers)
        by_exp.setdefault(sit["expect"], []).extend(answers)
        allx.extend(answers)
    return {"meta": _meta(runs, mock), "situations": rows,
            "nodes": {k: stats(v) for k, v in by_node.items()},
            "expect": {k: stats(v) for k, v in by_exp.items()}, "total": stats(allx)}


def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def _decs(d: dict) -> str:
    return " ".join(f"{k}×{v}" for k, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0])))


def _line(label: str, st: dict) -> str:
    return (f"{label}: действие {_pct(st['act_share'])} · ожидание {_pct(st['wait_share'])} · совет "
            f"{_pct(st['council_share'])} · молчание {st['silent']} (ответов {st['n']}, с решением {st['decided']})")


def report_text(rep: dict) -> str:
    """Таблица для терминала (русский)."""
    m = rep.get("meta") or {}
    L = [f"СТЕНД ПРОМПТОВ {m.get('version', '')} — как DeepSeek торгует сам: {TICKER}, режим {PLAY}, "
         f"ответов на ситуацию {m.get('runs')}, PRO {m.get('pro_model') or '?'}, узлы у денег {str(m.get('money_model')).upper()}"
         + (" (МОК)" if m.get("mock") else ""),
         "действ. = вход / добор / переворот / выход; ждать = ждать / держать; доли — от ответов с решением "
         "(молчание и неразобранный ответ — не решение ИИ); ожидание стенда — только для чтения",
         f"{'id':<19}{'узел':<13}{'ожидание':<20}{'действ.':>8}{'ждать':>7}{'совет':>7}{'молч.':>6}{'с':>7}  ответы"]
    for r in rep["situations"]:
        L.append(f"{r['id']:<19}{r['node']:<13}{EXPECT_TEXT.get(r['expect'], r['expect']):<20}"
                 f"{_pct(r['act_share']):>8}{_pct(r['wait_share']):>7}{_pct(r['council_share']):>7}{r['silent']:>6}"
                 f"{(r['sec_avg'] if r['sec_avg'] is not None else 0):>7}  {_decs(r['decisions'])}")
    L.append("")
    L.append("ПО УЗЛАМ:")
    L += [_line(f"  {k}", v) for k, v in rep["nodes"].items()]
    L.append("ПО ОЖИДАНИЮ СТЕНДА (только для чтения):")
    L += [_line(f"  {EXPECT_TEXT.get(k, k)}", v) for k, v in rep["expect"].items()]
    L.append(_line("ВСЕГО", rep["total"]))
    return "\n".join(L)


def _md(s: str) -> str:
    return str(s or "").replace("|", "\\|").replace("\n", " ")


def report_md(rep: dict) -> str:
    """Markdown-отчёт владельцу: сводка, по узлам, по ожиданию, ответы по ситуациям с «почему»."""
    m = rep.get("meta") or {}
    L = [f"# Стенд промптов ПИФИИ {m.get('version', '')}: как DeepSeek торгует сам", "",
         f"Инструмент {TICKER}, режим игры {PLAY}; ответов на ситуацию: {m.get('runs')}; PRO: `{m.get('pro_model') or '?'}`; "
         f"узлы у денег: {str(m.get('money_model')).upper()}" + ("; **мок-ответы (PYTHIA_MOCK_AI=1)**" if m.get("mock") else "")
         + ". Промпты — настоящие (`prompts_mission`), ситуации выдуманы и зафиксированы (30.09.2026, МСК).", "",
         "«Действие» — вход, добор, переворот или выход; «ждать» — ждать или держать позицию; «совет» — звать полный "
         "совет. Доли — от ответов с решением: молчание и неразобранный ответ — не решение ИИ. «Ожидание стенда» — "
         "подсказка для чтения, а не правильный ответ.", "",
         "## Сводка", "",
         "| id | узел | ситуация | ожидание | действие | ждать | совет | молч. | с/ответ | ответы |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["situations"]:
        L.append(f"| `{r['id']}` | {r['node']} | {_md(r['title'])} | {EXPECT_TEXT.get(r['expect'], r['expect'])} | "
                 f"{_pct(r['act_share'])} | {_pct(r['wait_share'])} | {_pct(r['council_share'])} | {r['silent']} | "
                 f"{r['sec_avg']} | {_md(_decs(r['decisions']))} |")
    L += ["", "## По узлам", "", "| узел | действие | ждать | совет | молч. | ответов |", "|---|---|---|---|---|---|"]
    L += [f"| {k} | {_pct(v['act_share'])} | {_pct(v['wait_share'])} | {_pct(v['council_share'])} | {v['silent']} | "
          f"{v['n']} |" for k, v in rep["nodes"].items()]
    L += ["", "## По ожиданию стенда", "", "| ожидание | действие | ждать | совет | молч. | ответов |",
          "|---|---|---|---|---|---|"]
    L += [f"| {EXPECT_TEXT.get(k, k)} | {_pct(v['act_share'])} | {_pct(v['wait_share'])} | {_pct(v['council_share'])} | "
          f"{v['silent']} | {v['n']} |" for k, v in rep["expect"].items()]
    t = rep["total"]
    L += ["", f"**Всего:** действие {_pct(t['act_share'])}, ожидание {_pct(t['wait_share'])}, совет "
              f"{_pct(t['council_share'])}, молчание {t['silent']} из {t['n']} ответов.", "", "## Ответы по ситуациям"]
    for r in rep["situations"]:
        L += ["", f"### `{r['id']}` — {r['title']}", "",
              f"Узел `{r['node']}`, {r['time_msk']}; ожидание стенда: {EXPECT_TEXT.get(r['expect'], r['expect'])}.", ""]
        for a in r["answers"]:
            L.append(f"{a.get('n')}. **{a['decision']}** ({KIND_TEXT.get(a['kind'], a['kind'])}, {a.get('sec')} с)"
                     + (f" [{_md(a['detail'])}]" if a.get("detail") else "") + f" — {_md(a['why'])}")
    return "\n".join(L) + "\n"


def list_text() -> str:
    return "\n".join(f"{s['id']:<19}{s['node']:<13}{EXPECT_TEXT[s['expect']]:<20}{s['title']}" for s in SITUATIONS)


# ══ CLI ════════════════════════════════════════════════════════════════════════════════════════════════════
def _install_mock_ai() -> None:
    """PYTHIA_MOCK_AI=1: ответы ИИ — из backend/mock_ai.py (те же подмены ai.*, что делает mock_ai.install), ключ
    мока — только в памяти. Рынок, новости и база мока стенду не нужны — их не трогаем (data/ не пишется)."""
    from . import ai, mock_ai
    keys = ["sk-mock"]
    config.deepseek_keys = lambda: list(keys)
    config.pick_pool_key = lambda exclude=None: keys[0]
    ai.ask, ai.ask_json, ai.stream = mock_ai._m_ask, mock_ai._m_ask_json, mock_ai._m_stream
    ai.chat, ai.health = mock_ai._m_chat, mock_ai._m_health
    ai.reset_client = lambda key=None: None


def _csv(s: str | None) -> list[str] | None:
    xs = [x.strip() for x in (s or "").split(",") if x.strip()]
    return xs or None


def _write(path: str, text: str) -> Path:
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m backend.prompt_bench",
                                 description="Стенд промптов ПИФИИ: настоящие промпты миссии × выдуманные ситуации → "
                                             "сколько раз DeepSeek действует, а сколько ждёт. Без аргументов — self-тест.")
    ap.add_argument("--selftest", action="store_true", help="self-тест на фейковом ИИ (так же без аргументов)")
    ap.add_argument("--list", action="store_true", help="список ситуаций")
    ap.add_argument("--run", action="store_true", help="прогон через ИИ")
    ap.add_argument("--runs", type=int, help="ответов на ситуацию (по умолчанию 1)")
    ap.add_argument("--only", help="id ситуаций через запятую")
    ap.add_argument("--nodes", help=f"узлы через запятую: {','.join(NODES)}")
    ap.add_argument("--out", help="markdown-отчёт")
    ap.add_argument("--json", dest="json_path", help="отчёт JSON (с сырыми ответами)")
    ap.add_argument("--dump", help="папка для промптов (<id>.system.txt / <id>.user.txt); ИИ не зовётся")
    ap.add_argument("--par", type=int, help=f"параллельных вызовов (по умолчанию {PAR})")
    ap.add_argument("--timeout", type=float, help="срок одного вызова, с (по умолчанию — как в бою)")
    a = ap.parse_args(argv)
    try:
        ids, nodes = _csv(a.only), _csv(a.nodes)
        select(ids, nodes)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.list:
        print(list_text())
        return 0
    if a.dump:
        paths = dump(a.dump, ids, nodes)
        print(f"промпты записаны: {len(paths)} файлов в {Path(a.dump).resolve()}")
        return 0
    run_mode = a.run or any(x is not None for x in (a.runs, a.only, a.nodes, a.out, a.json_path, a.par, a.timeout))
    if a.selftest or not run_mode:
        _selftest()
        return 0
    runs = int(a.runs or 1)
    if runs < 1:
        print("--runs должно быть ≥ 1", file=sys.stderr)
        return 2
    mock = os.getenv("PYTHIA_MOCK_AI") == "1"
    if mock:
        _install_mock_ai()
    elif not ai_v5.has_key():
        print(NO_KEY, file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    total = len(select(ids, nodes)) * runs
    done = [0]

    def _progress(sit: dict, rec: dict) -> None:
        done[0] += 1
        print(f"[{done[0]}/{total}] {sit['id']} #{rec['n']}: {rec['decision']} ({rec['kind']}, {rec['sec']} с)",
              file=sys.stderr, flush=True)

    rep = asyncio.run(run(ids, runs, nodes, par=a.par or PAR, timeout=a.timeout, on_answer=_progress, mock=mock))
    print(report_text(rep))
    if a.out:
        print(f"отчёт: {_write(a.out, report_md(rep)).resolve()}")
    if a.json_path:
        print(f"JSON: {_write(a.json_path, json.dumps(rep, ensure_ascii=False, indent=1)).resolve()}")
    return 0


# ══ self-тест (закон 6): фейковый ИИ, без сети, без записи в data/ ══════════════════════════════════════════
class FakeAI:
    """Подмена ai_v5.pro_json / money_json / pro_text: ответ по id ситуации (узнаётся по user-тексту промпта);
    Exception в ответах — сбой вызова. Записывает маршруты и сроки попыток."""

    def __init__(self, answers: dict[str, Any]):
        self.answers = answers
        self.by_user = {build(s)[1]: s["id"] for s in SITUATIONS}
        self.calls: list[tuple[str, str, Any]] = []

    def _answer(self, route: str, user: str, att=None):
        sid = self.by_user.get(user, "?")
        self.calls.append((sid, route, att))
        a = self.answers.get(sid)
        if isinstance(a, Exception):
            raise a
        return a

    async def pro_json(self, system, user, *, route="pro", max_tokens=None, attempt_timeout=None):
        return self._answer(route, user)

    async def money_json(self, system, user, *, route, max_tokens=None, attempt_timeout=None):
        return self._answer(route, user, attempt_timeout)

    async def pro_text(self, system, user, *, route="pro", max_tokens=None):
        return self._answer(route, user)


class patched_ai:
    """Контекст: ai_v5.pro_json / money_json / pro_text → методы фейка; на выходе — как было."""
    NAMES = ("pro_json", "money_json", "pro_text")

    def __init__(self, fake: FakeAI):
        self.fake, self.saved = fake, {}

    def __enter__(self):
        for n in self.NAMES:
            self.saved[n] = getattr(ai_v5, n)
            setattr(ai_v5, n, getattr(self.fake, n))
        return self.fake

    def __exit__(self, *exc):
        for n, f in self.saved.items():
            setattr(ai_v5, n, f)
        return False


# ответы фейка на все 14 ситуаций (метки 5.4.3: КУПИТЬ / ПРОДАТЬ / ДЕРЖАТЬ, латиница и старые — тоже слова решения)
# и ожидаемые (решение — канон кода, kind)
FAKE_ANSWERS: dict[str, Any] = {
    "range_mid_wait": {"choice": "ЖДЁМ", "why": "середина коридора 300.0"},
    "range_low_buyer": {"choice": "КУПИТЬ", "why": "плита 296", "entry": None, "entry_kind": "сейчас",
                        "invalidation": 295.6, "take": 303.5},
    "breakout_hold": {"choice": "BUY", "why": "пробой удержан", "invalidation": 303.4, "take": 310},
    "false_breakout": {"choice": "ПРОДАТЬ", "why": "аптраст 304.8", "invalidation": 305.1, "take": 298},
    "trend_no_pullback": {"choice": "НОВЫЙ_АНАЛИЗ", "why": "план протух"},
    "news_shock": {"choice": "ЖДЁМ или КУПИТЬ", "why": "сомневаюсь"},
    "dead_market": {"choice": "ЖДЕМ", "why": "мёртвый рынок"},
    "long_resistance": {"choice": "ЗАКРЫТЬ", "why": "стена 302"},
    "long_pressure": {"choice": "HOLD", "why": "триггер 298.5 держит", "invalidation": 298.6},
    "short_add": {"choice": "ДОБРАТЬ", "why": "место до 295"},
    "door_now": {"decision": "ВОЙТИ", "why": "плиту съели", "entry": None},
    "door_breakout": {"decision": "ЖДАТЬ", "why": "ретест 304", "entry": 304.05, "entry_kind": "откат",
                      "wait_minutes": 10},
    "profit_fade": {"decision": "ВЫЙТИ_И_ПЕРЕЗАЙТИ", "why": "рывок выдохся", "reentry": 302.2, "reentry_kind": "откат"},
    "verdict_split": "**ВЕРДИКТ СОВЕТА ПО SBER (11:05 МСК)**\nРешение: вне рынка — ждать закрепления над 300.5.\n"
                     "План: BUY только над 300.5.",
}
FAKE_WANT = {
    "range_mid_wait": ("ЖДЁМ", "wait"), "range_low_buyer": ("КУПИТЬ_СЕЙЧАС", "action"),
    "breakout_hold": ("КУПИТЬ_СЕЙЧАС", "action"), "false_breakout": ("ПРОДАТЬ_СЕЙЧАС", "action"),
    "trend_no_pullback": ("НОВЫЙ_АНАЛИЗ", "council"), "news_shock": ("НЕ_РАЗОБРАН", "silent"),
    "dead_market": ("ЖДЁМ", "wait"), "long_resistance": ("ЗАКРЫТЬ", "exit"), "long_pressure": ("ЖДЁМ", "hold"),
    "short_add": ("ДОБРАТЬ", "action"), "door_now": ("ВОЙТИ", "action"), "door_breakout": ("ЖДАТЬ", "wait"),
    "profit_fade": ("ВЫЙТИ_И_ПЕРЕЗАЙТИ", "exit"), "verdict_split": ("WAIT", "wait"),
}


ROUTE_OF = {"review_flat": "mission_review", "review_pos": "mission_review", "entry": "mission_entry",
            "profit": "mission_profit", "verdict": "mission_verdict"}

# строки, которые в бою собирает mission.py 5.4.3, — в промптах стенда (ситуация, приказ, дверь, прибыль, prev совета)
FORMAT_PIECES: tuple[tuple[str, str], ...] = (
    ("range_mid_wait", "ПРИКАЗ СОВЕТА (100 мин назад): вне рынка — прошлое мнение, не запрет; с тех пор цена +0.07 %. "
                       "Реши заново — КУПИТЬ | ПРОДАТЬ | НОВЫЙ_АНАЛИЗ | ЖДЁМ"),
    ("range_mid_wait", "БУДИЛЬНИК: уровень 304 из твоего ЖДЁМ 10:50 (цена тогда 300.1) — при проходе цены код разбудит "
                       "дежурного PRO; это не вход"),
    ("range_mid_wait", "Прошлая перепроверка (30 мин назад, цена 300.1): ЖДЁМ — цена 300.1 посередине коридора"),
    ("range_mid_wait", "; с тех пор 300 (-0.03 %)"),
    # серия ЖДЁМ — по счётчику m.wait_streak (настоящий _streak_after_review), цена за серию — с первого ЖДЁМ 10:14
    ("range_mid_wait", "ЖДЁМ подряд: 2 за 66 мин (с 30.09 10:14); вне рынка с 30.09 09:40; цена за серию 299.4–302.2 "
                       "(размах 0.94 %), от первого ЖДЁМ (302.2) -0.73 %"),
    # находка стенда (3): возраст совета — от приказа (09:40), а не от начала (09:22)
    ("range_mid_wait", "Последний полный совет: приказ пришёл 100 мин назад (совет шёл 18 мин, начат 09:22); пока совет "
                       "идёт"),
    # находка стенда (1): Вайкофф совета — с подписью времени и цены расчёта и расстояниями от текущей цены
    ("range_mid_wait", "═══ ВАЙКОФФ (на момент совета 09:22, 118 мин назад, цена тогда 299.8) ═══\nСЕЙЧАС от цены 300 "
                       "(края бокса — расчёт совета): D1 бокс 288.4 … 309.6: цена на 55% высоты бокса; до льда -3.87% "
                       "(-11.6, ~1.7 ATR), до крика +3.20% (+9.6, ~1.4 ATR) → ближе к крику; H1 бокс 296 … 304: цена на "
                       "50% высоты бокса"),
    ("range_mid_wait", "С тех пор цена 299.8 → 300 (+0.07 %). Будильник кода у уровней 304, 296 — проход цены будит"),
    ("range_mid_wait", "\nКак совет видел ведение 100 мин назад (прошлое мнение): план — вне рынка;"),
    ("range_mid_wait", "— новости до совета 30.09 09:22 (1 шт.) ужаты в блок ПАМЯТЬ МИССИИ"),
    ("range_mid_wait", "Совет daily от 30.09 08:50 МСК (2 ч назад) — общий по рынку"),
    ("range_mid_wait", "СКАНЕР СТАКАНА (онлайн, тик 3 с): 2360 тиков за 118 мин, идёт ещё 362 мин."),
    ("range_mid_wait", "Killswitch: ок (дневной лимит -6000.0)"),
    ("breakout_hold", "ПОВОД ПЕРЕПРОВЕРКИ (внеплановая): прокол сканера вверх 61 % (вне рынка: полоса 305.3–305.7, "
                      "без плана)"),
    ("breakout_hold", "ЖДЁМ подряд: 3 за 66 мин (с 30.09 10:14); вне рынка с 30.09 09:40; цена за серию 300.3–305.3"),
    ("breakout_hold", "H1 бокс 296 … 304: цена на 114% высоты бокса — НАД криком, вне бокса; до льда -2.98%"),
    ("false_breakout", "ПОВОД ПЕРЕПРОВЕРКИ (внеплановая): WAIT: цена 304.1 прошла уровень 304 из приказа совета — реши "
                       "по живой картине"),
    ("trend_no_pullback", "ВХОД ВЗВЕДЁН (лимит на откате): short @302 — до уровня +1.68 %, стоп 304.6, тейк 292.0; "
                          "взведён 70 мин, протухнет через 20 мин"),
    ("news_shock", "ПОВОД ПЕРЕПРОВЕРКИ (внеплановая): резкий ход: +1.07% за 10 мин (цена 302.1); серьёзная новость: ЦБ "
                   "внепланово снизил"),
    ("news_shock", "30.09 10:41 · серьёзность 5 · ЦБ внепланово снизил"),
    # находка стенда (2): свежая новость по тикеру — один раз (в «свежих»), «по инструменту» — остальные
    ("news_shock", "— по инструменту (кроме свежих выше):\n[b71e04] 30.09 09:30 · rbc · Минфин разместит ОФЗ"),
    ("dead_market", "ЖДЁМ подряд: 6 за 206 мин (с 30.09 10:14)"),
    ("dead_market", "Последний полный совет: приказ пришёл 240 мин назад (совет шёл 18 мин, начат 09:22)"),
    ("long_resistance", "ПОЗИЦИЯ: long 30 лот @298, в рынке 95 мин, плавающий P/L +1080 ₽, триггер (мягкий стоп) @296.4"),
    # метка модели: ЖДЁМ в позиции — «ДЕРЖАТЬ» (в записи канон прежний)
    ("long_resistance", "Прошлая перепроверка (30 мин назад, цена 299.8): ДЕРЖАТЬ — long +0.6%"),
    ("long_resistance", "═══ ВАЙКОФФ (на момент совета 09:22, 118 мин назад, цена тогда 298) ═══\nСЕЙЧАС от цены 301.6"),
    ("long_pressure", "ПРОКОЛ СКАНЕРА: сторона ВНИЗ, стойкость 63 %"),
    ("long_pressure", "плавающий P/L -450 ₽"),
    ("long_pressure", "Прошлая перепроверка (22 мин назад, цена 300.2): ДЕРЖАТЬ — long −0.1%"),
    ("long_pressure", "— по инструменту: только свежие выше"),
    ("short_add", "ПОЗИЦИЯ: short 30 лот @303, в рынке 70 мин, плавающий P/L +1050 ₽"),
    ("door_now", "ВХОЖУ: long сейчас, стоп 297.8, тейк 306.0"),
    # находка стенда (3): приказ пришёл 12 мин назад — раньше модель видела «50 мин назад» (от начала совета 10:30)
    ("door_now", "Последний полный совет: приказ пришёл 12 мин назад (совет шёл 38 мин, начат 10:30)"),
    ("door_now", "ПЛАН ПИЛОТА (по нему готов войти сейчас, цена 300.5): long сейчас"),
    ("door_now", "ЖДАТЬ по этому плану: 1 раз за 11 мин; цена 300.3 → 300.5 (+0.07 % в сторону плана); план "
                 "протухнет через 78 мин"),
    ("door_now", "30.09 11:09 @300.3: ЖДАТЬ — плита 18 000 лотов на продажу у 300.6 стоит"),
    ("door_now", "→ сейчас 300.5 (+0.07 % в сторону плана)"),
    ("door_breakout", "ПРОБОЙ ПРОЙДЕН: long, уровень 304, цена 304.3 (+0.10 % за уровнем)"),
    ("door_breakout", "long прорыв @304 — уровень достигнут"),
    ("door_breakout", "За 10 мин: 11:10 303.2 → 11:11 303.3"),
    ("profit_fade", "ПРИБЫЛЬ: пройдено 77 % хода от входа 298 до тейка 305 (порог 60 %)"),
    ("profit_fade", "УРОВНИ ПОЗИЦИИ: вход 298, триггер (мягкий стоп) 296.4"),
    ("profit_fade", "Перепроверки: 30.09 10:20 ДЕРЖАТЬ — long +0.5% (цена 299.5)"),
    ("verdict_split", "Перепроверки: 30.09 10:20 ЖДЁМ @298.4 → сейчас 299.2 (+0.27 %)"),
    ("verdict_split", "Передачи совету: 30.09 10:01 WAIT: цена 295.9 прошла уровень 296 из приказа совета"),
    ("verdict_split", "Пилот: вне рынка; результат сессии +0 ₽ за 0 сделок"),
    ("verdict_split", "═══ АНАЛИЗ ═══"),
)

# ответы ИИ, на которых запись перепроверки сцены (Scene.review) сверяется с записью настоящего MissionPilot._review
REVIEW_ANSWERS: tuple[tuple[str, dict], ...] = (
    ("range_mid_wait", {"choice": "ЖДЁМ", "why": "середина коридора, будильник у 304.5", "entry": 304.5,
                        "entry_kind": "прорыв"}),
    ("long_pressure", {"choice": "ДЕРЖАТЬ", "why": "триггер 298.5 держит"}),
)


def check_review_record(sid: str, answer: dict) -> None:
    """Запись решения перепроверки, которую сцена кладёт в историю (Scene.review), — та же, что пишет настоящий
    MissionPilot._review на том же ответе ИИ: запись m.reviews, last_review с ценой решения, будильник ЖДЁМ, прокол."""
    sc, calls = combat_node(sid, dict(answer))
    assert len(calls) == 1 and calls[0][2] == "mission_review", calls
    ref = SCENES[sid]()
    choice = ai_v5.decision_of(ai_v5.decision_raw(answer), ai_v5.review_table(ref.p.position is not None, ref.side()))
    ref.review(ref.now, choice, answer["why"], ref.price, entry=answer.get("entry"), entry_kind=answer.get("entry_kind"),
               inv=answer.get("invalidation"), take=answer.get("take"))
    got, want = sc.m.reviews[-1], ref.m.reviews[-1]
    assert got == want, (sid, got, want)
    assert sc.p.last_review == ref.p.last_review and sc.p._wake == ref.p._wake, (sid, sc.p._wake, ref.p._wake)
    assert sc.m.wait_streak == ref.m.wait_streak, (sid, sc.m.wait_streak, ref.m.wait_streak)
    assert sc.p._last_review_ts == ref.p._last_review_ts and sc.p._review_reason == ref.p._review_reason
    pu, pr = sc.p.puncture or {}, ref.p.puncture or {}
    assert (pu.get("pending"), pu.get("state")) == (pr.get("pending"), pr.get("state")), (sid, pu, pr)


def check_prompts() -> None:
    """Закон 3 и §7 на всех ситуациях: голос, ≤ SYSTEM_MAX_LINES строк, BANNED нет, «json» у JSON-узлов, время МСК."""
    assert len(SITUATIONS) == 14 and len(BY_ID) == 14, "14 ситуаций с уникальными id"
    for s in SITUATIONS:
        assert s["node"] in NODES and s["expect"] in EXPECTS and s["title"] and s["id"].isascii(), s["id"]
        system, user = build(s)
        low = system.lower()
        assert pm.VOICE in system and pm.FREEDOM in system, (s["id"], "голос 4.5.4 и свобода слоёв")
        assert pm.system_lines(system) <= pm.SYSTEM_MAX_LINES, (s["id"], pm.system_lines(system))
        assert not [b for b in pm.BANNED if b in low], (s["id"], [b for b in pm.BANNED if b in low])
        if s["node"] in JSON_NODES:
            assert "json" in low and "строго один JSON-объект" in system, (s["id"], "слово json в JSON-стадии")
        assert msk(s["time"]) in system and msk(s["time"]) in user and TICKER in user, (s["id"], "время МСК и объект")
        assert "обрезано" not in user, (s["id"], "аварийный потолок не сработал")
    assert {s["node"] for s in SITUATIONS} == set(NODES), "все пять узлов покрыты"


def _selftest() -> None:
    t0 = time.time()
    data_dir = Path(getattr(config, "DATA_DIR", "data"))

    def _snap() -> dict:
        try:
            return {p.name: p.stat().st_mtime_ns for p in data_dir.iterdir()} if data_dir.exists() else {}
        except OSError:
            return {}
    before = _snap()
    check_prompts()
    # промпты воспроизводимы байт в байт (время зафиксировано, datetime.now нет)
    assert [build(s) for s in SITUATIONS] == [build(s) for s in SITUATIONS]
    # тексты mission.py — из боевых сборщиков в формате 5.4.3 (цена ожидания, взведённый вход, будильник, дверь)
    for sid, piece in FORMAT_PIECES:
        system, user = build(BY_ID[sid])
        assert piece in user or piece in system, (sid, piece)
    s1 = BY_ID["range_mid_wait"]["args"]
    assert s1["prev_exec"].startswith("Приказ совета 30.09 09:40 (100 мин назад, цена тогда 299.8): WAIT — вне рынка. "
                                      "Ориентир совета (не условие): закрепление над 304"), s1["prev_exec"][:200]
    assert s1["situation"].startswith("Цена сейчас: 300\nРЫНОК: рынок открыт: торги идут (основная сессия)")
    assert BY_ID["breakout_hold"]["args"]["situation"].startswith("ПРОКОЛ СКАНЕРА: сторона ВВЕРХ, стойкость 61 %")
    assert "ПРОВЕРКА ВХОДА:" not in build(BY_ID["door_now"])[1], "у двери — без дубля строки о проверке входа"
    # история сцены — настоящими функциями mission.py: серия ЖДЁМ по счётчику, запись с pos_side, метка модели
    sc0 = SCENES["range_mid_wait"]()
    assert sc0.m.wait_streak["n"] == 2 and sc0.m.wait_streak["price"] == 302.2, sc0.m.wait_streak
    assert SCENES["long_pressure"]().m.reviews[-1]["pos_side"] == "long"
    # находки стенда в бою: Вайкофф совета с подписью и от текущей цены, новость — один раз, возраст — от приказа
    assert s1["wyckoff_at"].startswith("на момент совета 09:22") and s1["wyckoff"].startswith("СЕЙЧАС от цены 300 ")
    assert build(BY_ID["news_shock"])[1].count("[7c21e0]") == 1
    assert "приказ пришёл 12 мин назад (совет шёл 38 мин" in BY_ID["door_now"]["args"]["situation"]
    lt = BY_ID["range_low_buyer"]["args"]["light"]
    assert lt.startswith("SBER: цена 296.4 (30.09.2026 11:20 МСК, среда)") and "Рентген: OBI +0.31" in lt and "Майя: тяга вверх" in lt
    assert "Лента 15 мин: 3900 сделок, объём 162200, агрессор — покупатели (63% покупок), дельта +41400" in lt
    # стенд = бой: настоящий узел mission.py в сцене собирает тот же промпт байт в байт
    for s in SITUATIONS:
        if s["node"] in COMBAT_NODES:
            cs, cu, route = combat_prompt(s["id"])
            assert (cs, cu) == build(s), (s["id"], "промпт стенда разошёлся с боевым узлом")
            assert route == ROUTE_OF[s["node"]], (s["id"], route)
    for sid, answer in REVIEW_ANSWERS:
        check_review_record(sid, answer)
    # разбор вердикта
    vt = verdict_table(False)
    assert "CLOSE" not in vt and "HOLD" in vt
    for text, want in (("Вердикт: BUY от 299.2, стоп 295.6", "BUY"), ("## ВЕРДИКТ\nВне рынка до 300.5", "WAIT"),
                       ("ВЕРДИКТ. SBER — long от 299.2", "BUY"), ("Решение: не покупать, ждать 300.5", "WAIT"),
                       ("Итог: SELL от 300 к 296", "SELL"), ("1. Выход из бокса вверх\n2. Сторона: шорт", "SELL"),
                       ("Разбор без решения", None)):
        assert verdict_decision(text)[0] == want, (text, verdict_decision(text), want)
    assert verdict_decision("Вердикт: закрыть позицию", True)[0] == "CLOSE"

    async def _go() -> dict:
        fake = FakeAI(dict(FAKE_ANSWERS))
        with patched_ai(fake):
            rep = await run(runs=1)
            assert len(fake.calls) == 14, fake.calls
            routes = {sid: r for sid, r, _ in fake.calls}
            assert routes["range_mid_wait"] == routes["long_resistance"] == "mission_review"
            assert routes["door_now"] == "mission_entry" and routes["profit_fade"] == "mission_profit"
            assert routes["verdict_split"] == "mission_verdict"
            att = {sid: a for sid, r, a in fake.calls if r in ("mission_entry", "mission_profit")}
            assert all(a and a > 0 for a in att.values()), att
            # сбой одного вызова — silent, стенд идёт дальше
            fake.answers["door_now"] = RuntimeError("сеть упала")
            fake.answers["profit_fade"] = asyncio.TimeoutError()
            rep2 = await run(ids=["door_now", "door_breakout", "profit_fade"], runs=2)
        return {"rep": rep, "rep2": rep2}
    got = asyncio.run(_go())
    rep, rep2 = got["rep"], got["rep2"]
    for r in rep["situations"]:
        a = r["answers"][0]
        assert (a["decision"], a["kind"]) == FAKE_WANT[r["id"]], (r["id"], a["decision"], a["kind"])
    assert rep["total"]["n"] == 14 and rep["total"]["silent"] == 1 and rep["total"]["decided"] == 13
    assert rep["nodes"]["review_flat"]["kinds"]["action"] == 3 and rep["nodes"]["entry"]["kinds"]["wait"] == 1
    assert "стоп 295.6" in rep["situations"][1]["answers"][0]["detail"]
    r2 = {r["id"]: r for r in rep2["situations"]}
    assert [a["decision"] for a in r2["door_now"]["answers"]] == ["НЕТ_ОТВЕТА", "НЕТ_ОТВЕТА"]
    assert r2["door_now"]["silent"] == 2 and r2["door_now"]["act_share"] is None and "сеть упала" in r2["door_now"]["answers"][0]["why"]
    assert r2["profit_fade"]["answers"][0]["why"] == "ответа не было: таймаут"
    assert r2["door_breakout"]["decided"] == 2 and r2["door_breakout"]["wait_share"] == 1.0, "стенд шёл дальше"
    json.dumps(rep, ensure_ascii=False)
    txt, md = report_text(rep), report_md(rep)
    assert all(s["id"] in txt and s["id"] in md for s in SITUATIONS)
    assert "ПО УЗЛАМ" in txt and "ВСЕГО" in txt and "## Сводка" in md and "ВЫЙТИ_И_ПЕРЕЗАЙТИ" in md
    with tempfile.TemporaryDirectory(prefix="pythia_bench_") as tmp:
        files = dump(tmp)
        assert len(files) == 28 and all(p.stat().st_size > 0 for p in files), len(files)
        assert sorted(p.name for p in Path(tmp).iterdir())[:2] == ["breakout_hold.system.txt", "breakout_hold.user.txt"]
        assert main(["--dump", str(Path(tmp) / "d2"), "--only", "door_now"]) == 0
        assert len(list((Path(tmp) / "d2").iterdir())) == 2
    try:
        select(["нет_такой"])
        raise AssertionError("неизвестный id — ошибка")
    except ValueError as e:
        assert "нет_такой" in str(e)
    assert _snap() == before, "self-тест не пишет в data/"
    print(report_text(rep))
    n_combat = sum(1 for s in SITUATIONS if s["node"] in COMBAT_NODES)
    print(f"prompt_bench self-test OK: {len(SITUATIONS)} ситуаций, промпты по закону 3 (голос, ≤ {pm.SYSTEM_MAX_LINES} "
          f"строк, без BANNED, json у JSON-узлов), {n_combat} промптов = настоящий узел mission.py байт в байт, запись "
          f"перепроверки = _review, kind на фейковом ИИ верны, сбой вызова — silent, отчёты и dump (28 файлов) — "
          f"{time.time() - t0:.1f} с")


if __name__ == "__main__":
    sys.exit(main())
