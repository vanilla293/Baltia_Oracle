# -*- coding: utf-8 -*-
"""ПИФИЯ v5.4.2 — СТЕНД ПРОМПТОВ: «как DeepSeek торгует сам».

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

Блоки ситуаций написаны в формате настоящих сборщиков: где есть чистая функция-форматтер — зовётся она
(market_ctx.render_wyckoff, market_ctx._xray_line / _maya_line поверх microstructure.classify, maya_scan.render_for_ai,
prompts_mission.puncture_block, market_clock.describe, ai_v5.now_msk_str), где нет (mission._situation_text,
_exec_text, _plan_text, _history_text, newsflow.one_line_rich, correlate.text, scout.fetch, council.summary_text) —
формат повторён вручную: те модули тянут store_v5 (база в data/) или берут time.time(). Время МСК фиксировано
(30.09.2026, среда), datetime.now нет — промпты воспроизводимы байт в байт. Ручки config (PYTHIA_REVIEW_SEC,
PYTHIA_ENTRY_FRESH_SEC, PYTHIA_MONEY_MODEL …) — живые, как в бою. Числа в ситуациях выдуманы (закон 2 о боевом коде
здесь не нарушается: это стенд, в бой ничего не течёт); expect у ситуации — только для чтения отчёта, не для подгонки.

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
мока не создаётся). Стенд сам ничего не пишет в data/ — только туда, куда указали --out / --json / --dump.
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

from . import ai_v5, config, hawkes, maya_scan, market_clock, market_ctx, microstructure
from . import prompts_mission as pm

log = logging.getLogger("pythia.prompt_bench")

# ── сцена ────────────────────────────────────────────────────────────────────────────────────────────────
TICKER, NAME, PLAY = "SBER", "Сбербанк", "auto"
LOT = 10                                    # акций в лоте SBER
DEPOSIT = 100_000.0
DAY_LIMIT = -3000.0                         # trader_risk.state()["day_loss_limit"] (депозит × 3 %)
HARD = "аварийный трос в программе"         # ai_pilot._hard_name() при PYTHIA_EXCHANGE_STOP=0 (умолчание 5.4.1)
PLAN_TTL_MIN = 90                           # ai_pilot.PLAN_TTL_SEC 5400 с
N30 = 1200                                  # тиков за ≈30 мин (1800 / ai_pilot.TICK_SEC 1.5)
NORM_BPS = 3.0                              # норма спреда market_ctx (NORM_SPREAD_MIN_BPS) — вход рентгена
SCAN_TICK_S = 3.0                           # maya_scan.SCAN_INTERVAL_S по умолчанию
BOOK_DEPTH = 50                             # market_ctx.LIGHT_DEPTH
MARKET = market_clock.describe({"open": True, "reason": "торги идут", "session": "основная"})

NODES = ("review_flat", "review_pos", "entry", "profit", "verdict")
JSON_NODES = ("review_flat", "review_pos", "entry", "profit")
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
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 9, day, h, m, tzinfo=ai_v5.MSK)


def msk(hhmm: str, day: int = 30) -> str:
    """«30.09.2026 11:20 МСК, среда» — как ai_v5.now_msk_str()."""
    return ai_v5.now_msk_str(_dt(hhmm, day))


def hm(hhmm: str, day: int = 30) -> str:
    """«30.09 11:20» — как ai_v5.fmt_ts()."""
    return f"{day:02d}.09 {hhmm}"


def _mins(a: str, b: str) -> int:
    return int((_dt(b) - _dt(a)).total_seconds() // 60)


# ══ форматтеры в формате боевых сборщиков ════════════════════════════════════════════════════════════════
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


def scan_text(*, bid: float, ask: float, imb: float, pull: str | None, up: float, dn: float, word: str,
              streak: tuple[int, int], ar: float, agree: bool | None, ticks: int = 1260, left: float = 177,
              pf: tuple | None = None, pz_up: tuple = (), pz_dn: tuple = (), wall_ask: tuple | None = None,
              wall_bid: tuple | None = None, tension: tuple | None = None, synth: tuple | None = None,
              hawkes_n: float | None = None) -> str:
    """СКАНЕР СТАКАНА — настоящий maya_scan.render_for_ai по агрегатам status(). Стены: (p_mode, ×медианы | None,
    лотов | None, присутствие, устойчивость, призрак?)."""
    price = (bid + ask) / 2
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
            now = {"mult": w[1], "q": w[2]} if w[1] else {}
            agg[key] = {"p_mode": w[0], "now": now, "present": w[3], "stability": w[4], "ghost": w[5]}
    if tension:
        agg["tension"] = {"word": tension[0], "first": tension[1], "last": tension[2]}
    if synth:
        agg["synth"] = {"word": synth[0], "jump_max": synth[1]}
    if hawkes_n is not None:
        agg["hawkes"] = _hawkes(hawkes_n)
    return maya_scan.render_for_ai(agg)


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
    return market_ctx.render_wyckoff({"daily": d1, "hourly": h1}, price)


def exec_text(o: dict) -> str:
    """Формат mission._exec_text (приказ совета: WAIT / HOLD / BUY|SELL с уровнями)."""
    head = f"Приказ ({hm(o['at'])}): "
    tail = f"\nПлан: {o.get('plan')}\nТайминг: {o.get('time_note')}"
    if o["do"] == "WAIT":
        return (head + f"WAIT — совет ждал: {o.get('wait_for') or o.get('why') or '—'} (уверенность {o.get('conf')}) — "
                f"{o.get('why')}" + tail)
    kind = o.get("kind") or ("сейчас" if o.get("entry") is None else "откат")
    return (head + f"{o['do']} entry={o.get('entry')} ({kind}) take={o.get('take')} invalidation={o.get('inv')} "
            f"уверенность {o.get('conf')} — {o.get('why')}" + tail)


def reviews_line(reviews: list[tuple[str, str, str]]) -> str:
    """«Перепроверки: 30.09 10:20 ЖДЁМ — …; …» — как в mission._prev_text / _plan_text."""
    return "Перепроверки: " + "; ".join(f"{hm(t)} {c} — {w}" for t, c, w in reviews)


def situation_text(price: float, *, move: tuple | None = None, win30: float | None = None,
                   book: tuple | None = None, pos: dict | None = None, plan: dict | None = None,
                   wait: tuple | None = None, gate: str = "", pnl: float = 0.0, deals: int = 0,
                   account: str = "", prev_review: tuple | None = None, council_min: int | None = None,
                   puncture: str = "", reason: str = "", note: str = "") -> str:
    """СИТУАЦИЯ ПИЛОТА — формат mission._situation_text (+ блок ПРОКОЛ СКАНЕРА первым, как _situation_for_ai;
    + «ПОВОД ПЕРЕПРОВЕРКИ» как в _review; + «ПОМЕТКА К ЭТОМУ ВОПРОСУ» как в _entry_check)."""
    L = [f"Цена сейчас: {price:g}", "РЫНОК: " + MARKET]
    if move:
        L.append(f"Ход за {move[0]} мин: {move[1]:+.2f}% (мин {move[2]:g}, макс {move[3]:g})")
    if win30 is not None:
        L.append(f"Ход за окно наблюдения (≈30 мин): {win30:+.2f}%")
    if book:
        L.append(f"Стакан: bid {book[0]:g} / ask {book[1]:g}, дисбаланс {book[2]}")
    if pos:
        sgn = 1 if pos["side"] == "long" else -1
        fl = (price - pos["entry"]) * pos["lots"] * LOT * sgn
        L.append(f"ПОЗИЦИЯ: {pos['side']} {pos['lots']} лот @{pos['entry']:g}, в рынке {pos['held']} мин, "
                 f"плавающий P/L {fl:+.0f} ₽, триггер (мягкий стоп) @{pos['inv']}, {HARD} @{pos['hard']}, "
                 f"тейк {pos['take']}")
    elif plan:
        if plan.get("entry") is None:
            L.append(f"ВХОЖУ: {plan['side']} сейчас, стоп {plan['inv']}, тейк {plan['take']}")
        elif plan.get("kind") == "прорыв":
            L.append(f"ЖДУ ПРОБИТИЯ: {plan['side']} при проходе {plan['entry']}, стоп {plan['inv']}, "
                     f"тейк {plan['take']} (приказ {plan['age']} мин назад, срок {PLAN_TTL_MIN} мин)")
        else:
            L.append(f"ЗАСАДА (откат): {plan['side']} @{plan['entry']}, стоп {plan['inv']}, "
                     f"тейк {plan['take']} (приказ {plan['age']} мин назад, срок {PLAN_TTL_MIN} мин)")
        if gate:
            L.append(gate)
    else:
        L.append("Позиции нет, засады нет — полностью вне рынка")
        if wait:
            L.append(f"ПРИКАЗ СОВЕТА ({wait[0]} мин назад): вне рынка; совет ждал: {wait[1]}. Это прошлое мнение, "
                     f"а не запрет: реши заново — {pm.review_options(PLAY, False)}")
    L.append(f"Депозит {DEPOSIT:.0f} ₽, результат сессии {pnl:+.0f} ₽ за {deals} сделок")
    if account:
        L.append(account + " — размер входа/добора считает биржа, ты решаешь только сторону")
    if prev_review:
        L.append(f"Прошлая перепроверка ({prev_review[0]} мин назад): {prev_review[1]} — {prev_review[2]}")
    if council_min is not None:
        L.append(f"Последний полный совет: {council_min} мин назад")
    L.append(f"Killswitch: ок (дневной лимит {DAY_LIMIT})")
    s = "\n".join(L)
    if puncture:
        s = puncture + "\n" + s
    if reason:
        s += f"\nПОВОД ПЕРЕПРОВЕРКИ (внеплановая): {reason}"
    if note:
        s += f"\nПОМЕТКА К ЭТОМУ ВОПРОСУ: {note}"
    return s


def history_text(points: list[tuple[str, float]], price: float, pct30: float) -> str:
    """ХОД ЦЕНЫ — формат mission._history_text (точки окна резкого хода + ≈30 мин по тикам)."""
    lo, hi = min(p for _, p in points), max(p for _, p in points)
    return "\n".join([f"За {_mins(points[0][0], points[-1][0])} мин: " + " → ".join(f"{t} {p:g}" for t, p in points),
                      f"мин {lo:g}, макс {hi:g}, сейчас {price:g} ({(price / points[0][1] - 1) * 100:+.2f}% от начала окна)",
                      f"За ≈30 мин: {pct30:+.2f}% (тиков {N30})"])


def news_line(nid: str, at: str, src: str, one: str, tone: int, honesty: int, hype: int, imp: int, nov: int,
              eff: str, strength: int, kind: str, horizon: str, who: str, facts: str = "", gist: str = "",
              day: int = 30) -> str:
    """Формат newsflow.one_line_rich: строка с характеристиками, у важной — «факты: … · суть: …»."""
    line = (f"[{nid}] {hm(at, day)} · {src} · {one} · тон {tone:+d} · честн {honesty} · разд {hype} · важн {imp} · "
            f"нов {nov} · эффект {eff}/{strength} · {kind} · {horizon} · {who}")
    extra = " · ".join(x for x in ((f"факты: {facts}" if facts else ""), (f"суть: {gist}" if gist else "")) if x)
    return line + ("\n    " + extra if extra and (imp >= 70 or strength >= 60) else "")


def news_text(own: list[str], fresh: list[str] | None = None, since: str = "", hidden: str = "") -> str:
    """Новости перепроверки — формат mission._gather_news: «— свежие с …», «— новости до совета … ужаты …»,
    «— по инструменту:»."""
    blocks = []
    if fresh:
        blocks.append(f"— свежие с {hm(since)}:\n" + "\n".join(fresh))
    if hidden:
        blocks.append(hidden)
    if own:
        blocks.append("— по инструменту:\n" + "\n".join(own))
    return "\n\n".join(blocks)


NEWS_EMPTY = "(свежих новостей нет или сбор недоступен — НЕ выдумывай их, решай по цене и плану)"

N_PROFIT = news_line("3f9a2c", "08:15", "interfax", "Сбербанк за 8 мес. по РСБУ: чистая прибыль +6% г/г", 30, 80, 20,
                     55, 45, "вверх", 25, "факт", "дни", "SBER,банки")
N_OFZ = news_line("b71e04", "09:30", "rbc", "Минфин разместит ОФЗ на 50 млрд ₽ в среду", 0, 85, 10, 35, 40, "неясно",
                  10, "факт", "дни", "ОФЗ,рынок")
N_MORTGAGE = news_line("5d0b7a", "10:52", "prime", "Сбербанк: выдачи ипотеки в сентябре +18% м/м (оценка менеджмента)",
                       45, 70, 35, 60, 65, "вверх", 40, "факт", "дни", "SBER,банки")
N_TAX = news_line("c4d8e1", "17:20", "kommersant", "Минфин обсуждает разовый налог на сверхприбыль банков", -45, 60, 55,
                  75, 70, "вниз", 60, "слух", "дни", "SBER,VTBR,банки",
                  facts="обсуждение в рабочей группе Минфина; параметры не названы; решение не принято",
                  gist="риск изъятия части прибыли банков, пока на уровне обсуждения", day=29)
N_CBR = news_line("7c21e0", "10:40", "interfax", "ЦБ внепланово снизил ключевую ставку на 100 б.п.", 70, 90, 25, 95, 95,
                  "вверх", 85, "факт", "часы", "SBER,банки,рынок",
                  facts="ставка снижена на 100 б.п. вне графика заседаний; решение объявлено в 10:40 МСК; ЦБ допускает "
                        "дальнейшее снижение",
                  gist="неожиданное смягчение ДКП: дешевле фондирование, позитив для банков и акций в целом")
N_OVERDUE = news_line("e93a55", "10:05", "rbc", "ЦБ: просрочка по потребкредитам выросла на 1.9 п.п. за квартал", -40, 85,
                      30, 70, 60, "вниз", 45, "факт", "дни", "SBER,VTBR,банки",
                      facts="доля проблемных потребкредитов 11.2%; рост за квартал 1.9 п.п.; ЦБ говорит о мерах",
                      gist="рост плохих долгов в рознице — давление на резервы банков")
NEWS_RANGE = news_text([N_PROFIT, N_OFZ])


def scout_text(at: str, mx: tuple, vtbr: tuple, si: tuple) -> str:
    """ДАННЫЕ РАЗВЕДКИ — формат scout.fetch (котировка и изменение за день по каждому запросу)."""
    rows = [("MX (фьючерс на индекс MOEX)", mx, "рынок в целом"), ("VTBR (ВТБ)", vtbr, "связанная бумага, ρ=+0.78"),
            ("Si (фьючерс USD/RUB)", si, "рубль")]
    return (f"Разведка FLASH ({msk(at)}): 3 запрос(ов), из них по связанным бумагам 1\n"
            + "\n".join(f"- {lab}: {v[0]:g} ({v[1]:+.2f}% за день) · tinkoff — {why}" for lab, v, why in rows))


def _px(v: float) -> str:
    return f"{v:,.1f}".replace(",", " ") if abs(v) >= 1000 else (f"{v:.2f}" if abs(v) >= 100 else f"{v:.4g}")


def partners_text(moves: dict[str, tuple[float, float, float]]) -> str:
    """СВЯЗАННЫЕ БУМАГИ — формат correlate.text (ρ, лаг, ход за день и 5 дней, цена) + абзац «обычно читают так»."""
    meta = (("SBERP", "Сбербанк-п", "сектор: банки и финансы", 0.97, 60, ""),
            ("VTBR", "ВТБ", "сектор: банки и финансы", 0.78, 58, ", опережает на день (ρ с лагом +0.31)"),
            ("MX", "фьючерс на индекс MOEX", "макро", 0.71, 60, ""),
            ("SI", "рубль: фьючерс USD/RUB (Si)", "макро", -0.34, 60, ""))
    L = [f"Связанные бумаги для {TICKER} (ρ Пирсона по дневным лог-доходностям, общих дней n; ход за день и за 5 дней, "
         f"цена по последней дневной свече):"]
    for code, name, kind, rho, n, lag in meta:
        d1, d5, px = moves[code]
        L.append(f"- {code} ({name}; {kind}): ρ={rho:+.2f} n={n}{lag}; день {d1:+.2f}%, 5 дн. {d5:+.2f}%; цена {_px(px)}")
    L.append("Обычно читают так: при ρ выше +0.5 бумаги ходят вместе — расхождение сегодняшнего хода "
             "(партнёр уже пошёл, наша нет) читают как догоняющий потенциал или как слабость, если "
             "расхождение держится; при ρ ниже −0.5 партнёр — зеркало (рубль против экспортёров); "
             "«опережает на день» — вчерашний ход партнёра часто отзывается сегодня. Это статистика "
             "60 дней, не закон: связь рвётся на своих новостях.")
    return "\n".join(L)


def council_text(now: str, regime: str, summary: str, picks: list[str], *, avoid: str = "", watch: str = "",
                 key_times: str = "", when: str = "08:50") -> str:
    """ИТОГ ОБЩЕГО СОВЕТА — формат council.summary_text со шапкой council_latest (вид, время МСК, возраст)."""
    age = _mins(when, now)
    L = [f"Совет daily от {hm(when)} МСК ({f'{age} мин назад' if age < 60 else f'{age // 60} ч назад'}) — общий по рынку",
         f"Режим: {regime}", summary, "Входы:", *("- " + p for p in picks)]
    if avoid:
        L.append("Избегать: " + avoid)
    if watch:
        L.append("Смотреть: " + watch)
    if key_times:
        L.append("Ключевое время: " + key_times)
    return "\n".join(L)


def council_range(now: str) -> str:
    return council_text(
        now, "нейтральный, выборочно risk-on",
        "Фьючерс на индекс (MX) третью сессию в коридоре 2 880–2 940; рубль стабилен (Si 92 900–93 600), Brent 71–73 $. "
        "Банки сильнее рынка на ожиданиях снижения ставки к концу года; объёмы ниже средних за 20 дней.",
        ["SBER BUY 55% 1-3 дня — коридор 296–304, покупатель защищает 296 | триггер: закрепление над 304 или отбой от "
         "296 с объёмом | риск: закрытие дня ниже 296",
         "LKOH SELL 50% сессия — нефть у нижней границы 71 $ | триггер: Brent ниже 70.8 $ | риск: решение ОПЕК+"],
        watch="VTBR (догоняет SBER); MX (верх коридора 2 940)",
        key_times="10:00–11:00 МСК — основная ликвидность; 19:00 МСК — недельная инфляция Росстата")


def council_bear(now: str, pick: str) -> str:
    return council_text(
        now, "risk-off",
        "Третий день продаж в банках: слух о разовом налоге на сверхприбыль (29.09 17:20), MX 2 850–2 880 у минимумов "
        "месяца; рубль слабеет (Si 93 400), нерезидентов на рынке нет — отскоки выкупают вяло.",
        [pick], avoid="VTBR (слабее сектора, −4.3% за 5 дней)", watch="MX (2 850 — поддержка месяца)",
        key_times="16:00 МСК — брифинг Минфина; 19:00 МСК — недельная инфляция Росстата")


WAIT_EXEC = {"do": "WAIT", "at": "09:40", "conf": 55,
             "wait_for": "закрепление над 304 (объём ×2 к среднему, 15 мин над уровнем) — тогда BUY к 310; "
                         "пробой 296 вниз с объёмом — SELL к 290",
             "why": "коридор 296–304 второй день, края держат плиты по 17–22 тыс. лотов, середина — без хода",
             "plan": "вне рынка; пробой 304 с объёмом — лонг к 310, стоп под 302; пробой 296 — шорт к 290, стоп над 298",
             "time_note": "выход из коридора вероятнее после 11:00 МСК, до 16:00"}

MEMORY_0 = ("Миссия SBER идёт с 29.09 10:05 МСК, режим auto. 29.09: совет дал BUY от 298.6 со стопом 296.4 — вход 11:02 "
            "по 298.7, выход 14:40 по мысли о прибыли у 301.9 (+960 ₽ на 30 лотах). Вечером 29.09 и утром 30.09 цена "
            "ходила в коридоре 296–304; совет 30.09 09:40 — вне рынка до выхода из коридора. Что дало ожидание 29.09 "
            "после выхода: цена ещё час стояла у 302, затем вернулась к 299.")
MEMORY_BEAR = ("Миссия SBER идёт с 28.09 10:05 МСК, режим auto. 28.09: совет дал SELL на откате к 305 — засада не "
               "исполнилась, цена ушла вниз без отката (к 303.4 за 2 часа), приказ протух в 12:40. 29.09: совет WAIT; "
               "дежурный PRO вошёл в short по 300.4 в 13:10 и закрыл в 16:20 по 299.1 по мысли о прибыли (+390 ₽ на 30 "
               "лотах). Слух о налоге (29.09 17:20) рынок встретил продажами вечером.")

ACC_FLAT = "Счёт: свободно 100000 ₽, ликвидный портфель 100000 ₽, биржа даёт купить 52 / продать 47 лот"

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


def h1_range(read: str, phase: str, bias: int, extra: list | None = None, vol: str = VOL_PAR) -> dict:
    return wy_tf(300, read, phase, bias, 296.0, 304.0, 2.6, vol, H1_EVENTS + list(extra or []))


D1_TREND_DOWN = wy_tf(250, "МАРК-ДАУН", "тренд (диапазон не сформирован)", -34, None, None, 0.0,
                      "объём на росте ×0.78 к объёму на падении — предложение доминирует",
                      [("2026-09-25", "ПОСЛЕДНЕЕ ПРЕДЛОЖЕНИЕ (LPSY)", 309.0, "откат к бывшей опоре на малом объёме"),
                       ("2026-09-28", "ЗНАК СЛАБОСТИ (SOW)", 303.4, "закрытие под боксом на расширении (объём ×2.2) — "
                                                                     "знак слабости"),
                       ("2026-09-29", "ПРОБОЙ ВНИЗ", 300.2, "закрытие ниже 301 на объёме ×1.6")])
H1_DOWN_EVENTS = [("2026-09-29T12:00", "ЗНАК СЛАБОСТИ (SOW)", 300.1, "закрытие под боксом на расширении (объём ×2.1) — "
                                                                       "знак слабости"),
                  ("2026-09-29T17:00", "ПОСЛЕДНЕЕ ПРЕДЛОЖЕНИЕ (LPSY)", 301.9, "откат к бывшей опоре на малом объёме"),
                  ("2026-09-30T11:00", "ПРОБОЙ ВНИЗ", 299.8, "закрытие под 300.2 на объёме ×1.7")]
VOL_SUPPLY = "объём на росте ×0.71 к объёму на падении — предложение доминирует"
H1_DOWN = wy_tf(300, "РАСПРЕДЕЛЕНИЕ ЗАВЕРШЕНО", "D/E — марк-даун (выход из диапазона вниз)", -41, 300.2, 305.8, 2.1,
                VOL_SUPPLY, H1_DOWN_EVENTS)


def _time_block(now: str, price: float) -> dict:
    """Общие поля JSON-узлов: время, разведка и связанные бумаги «коридорного» дня."""
    return {"scout": scout_text(now, (2912.0, 0.18), (81.35, 0.42), (93150.0, -0.05)),
            "partners": partners_text({"SBERP": (0.10, 0.85, round(price * 0.998, 1)), "VTBR": (0.42, 1.10, 81.35),
                                       "MX": (0.18, 0.60, 2912.0), "SI": (-0.05, 0.30, 93150.0)})}


# ══ 14 ситуаций ═════════════════════════════════════════════════════════════════════════════════════════
def _review_args(*, situation: str, light: str, scan: str, wyckoff: str, prev_exec: str, news: str,
                 council: str, memory: str, scout: str, partners: str, watch: str = "") -> dict:
    return {"situation": situation, "light": light, "council_text": council, "prev_exec": prev_exec, "news": news,
            "watch": watch, "astro_line": "", "scan": scan, "wyckoff": wyckoff, "scout": scout, "partners": partners,
            "memory": memory}


def _s_range_mid_wait() -> dict:
    now, px = "11:20", 300.0
    return {"id": "range_mid_wait", "node": "review_flat", "expect": "wait_ok", "time": now,
            "title": "Боковик 2 дня 296–304, цена 300 в середине, пробоя нет; совет дал WAIT «ждём пробоя 304»",
            "args": _review_args(
                situation=situation_text(px, move=(9, 0.03, 299.8, 300.2), win30=0.07, book=(299.99, 300.01, 0.02),
                                         wait=(_mins("09:40", now), WAIT_EXEC["wait_for"]), account=ACC_FLAT,
                                         prev_review=(30, "ЖДЁМ", "цена 300.1 посередине коридора 296–304, лента "
                                                                 "паритет 0.51, до краёв по 1.3%"),
                                         council_min=_mins("09:40", now)),
                light=light_text(px, at=now, close=299.6, bid=299.99, ask=300.01, bid_vol=61200, ask_vol=58400,
                                 buy=48300, sell=46900, count=2140, big=(("buy", 2600, 300.05), ("sell", 2100, 300.12)),
                                 my=maya(None, (0.41, 0.38), ar=0.507, vac_up=(300.18, 300.31, 0.55, 17),
                                           vac_dn=(299.8, 299.69, 0.5, 19), wall_ask=(300.4, 3.2, 9800),
                                           wall_bid=(299.6, 3.0, 9100))),
                scan=scan_text(bid=299.99, ask=300.01, imb=0.02, pull=None, up=0.46, dn=0.44,
                               word="выраженной тяги за окно нет", streak=(9, 8), ar=0.51, agree=None,
                               wall_ask=(300.4, 3.2, 9800, 0.71, 0.64, False), wall_bid=(299.6, 3.0, 9100, 0.66, 0.6, False),
                               tension=T_FLAT, synth=S_CALM, hawkes_n=0.34),
                wyckoff=wyckoff_text(D1_RANGE, h1_range("ХАРАКТЕР НЕ ОПРЕДЕЛЁН", "B — построение причины", 4), px),
                prev_exec=exec_text(WAIT_EXEC), news=NEWS_RANGE, council=council_range(now), memory=MEMORY_0,
                **_time_block(now, px))}


def _s_range_low_buyer() -> dict:
    now, px = "11:20", 296.4
    return {"id": "range_low_buyer", "node": "review_flat", "expect": "action", "time": now,
            "title": "Цена у нижней границы боковика 296.4, в ленте покупатель, плита на покупку 296",
            "args": _review_args(
                situation=situation_text(px, move=(10, -0.47, 296.3, 297.8), win30=-0.87, book=(296.39, 296.41, 0.31),
                                         wait=(_mins("09:40", now), WAIT_EXEC["wait_for"]), account=ACC_FLAT,
                                         prev_review=(30, "ЖДЁМ", "цена 299.0 в верхней половине коридора 296–304, лента "
                                                                 "паритет 0.49"),
                                         council_min=_mins("09:40", now)),
                light=light_text(px, at=now, close=299.6, bid=296.39, ask=296.41, bid_vol=142600, ask_vol=74800,
                                 buy=101800, sell=60400, count=3900, wall_bid=(296.0, 22100),
                                 big=(("buy", 5200, 296.35), ("buy", 4100, 296.3), ("sell", 2300, 296.5)),
                                 my=maya("вверх", (0.66, 0.18), ar=0.628, vac_up=(296.46, 296.7, 0.72, 3),
                                           vac_dn=(296.2, 296.05, 0.21, 12), wall_ask=(296.8, 2.1, 6300),
                                           wall_bid=(296.0, 7.4, 22100))),
                scan=scan_text(bid=296.39, ask=296.41, imb=0.31, pull="вверх", up=0.58, dn=0.31, word="тянут вверх",
                               streak=(13, 4), ar=0.62, agree=True, pz_up=((296.46, 296.7, 0.41, 0.66),),
                               wall_ask=(296.8, 2.1, 6300, 0.38, 0.3, False), wall_bid=(296.0, 7.4, 22100, 0.93, 0.9, False),
                               tension=(T_UP, 0.41, 0.63), hawkes_n=0.57),
                wyckoff=wyckoff_text(D1_RANGE, h1_range("СКЛОНЯЕТСЯ К НАКОПЛЕНИЮ", "B — построение причины", 16, [
                    ("2026-09-30T11:00", "НЕТ ПРЕДЛОЖЕНИЯ", 296.5, "узкий бар вниз на объёме ×0.6 у льда — продавцы "
                                                                   "иссякают")]), px),
                prev_exec=exec_text(WAIT_EXEC), news=NEWS_RANGE, council=council_range(now), memory=MEMORY_0,
                **_time_block(now, px))}


def _s_breakout_hold() -> dict:
    now, px = "11:20", 305.1
    cons = {"up_share": 0.64, "dn_share": 0.22, "word": "тянут вверх", "streak": {"win": 20, "up": 14, "dn": 3},
            "aggressor_mean": 0.65, "agree": True}
    punct = pm.puncture_block("вверх", 0.61, 305.3, 305.7, role="вне рынка", our_side=None, in_pos=False, price=px,
                              depth=0.8, ticks=240, consensus=cons, hawkes_n=0.71, tension=T_UP)
    tb = _time_block(now, px)
    tb["scout"] = scout_text(now, (2929.0, 0.62), (82.3, 1.2), (93050.0, -0.15))
    tb["partners"] = partners_text({"SBERP": (1.3, 2.1, 304.6), "VTBR": (1.2, 1.9, 82.3), "MX": (0.62, 0.9, 2929.0),
                                    "SI": (-0.15, 0.2, 93050.0)})
    return {"id": "breakout_hold", "node": "review_flat", "expect": "action", "time": now,
            "title": "Пробой 304 состоялся с объёмом ×2.5, цена 305.1, удержание 20 мин; совет ждал именно этого (WAIT)",
            "args": _review_args(
                situation=situation_text(px, move=(10, 0.23, 304.2, 305.3), win30=1.6, book=(305.09, 305.11, 0.22),
                                         wait=(_mins("09:40", now), WAIT_EXEC["wait_for"]), account=ACC_FLAT,
                                         prev_review=(18, "ЖДЁМ", "пробой 304 на объёме ×2.4, но над уровнем только 2 мин "
                                                                 "(цена 304.4) — удержания ещё нет"),
                                         council_min=_mins("09:40", now), puncture=punct,
                                         reason="прокол сканера вверх 61 % (вне рынка: полоса 305.3–305.7, без плана)"),
                light=light_text(px, at=now, close=300.8, bid=305.09, ask=305.11, bid_vol=118000, ask_vol=75400,
                                 buy=172900, sell=89100, count=6100, wall_bid=(304.9, 12800),
                                 big=(("buy", 7400, 305.0), ("buy", 6100, 304.8), ("sell", 3900, 305.2)),
                                 my=maya("вверх", (0.71, 0.2), ar=0.66, vac_up=(305.3, 305.7, 0.8, 19),
                                           wall_ask=(305.9, 2.3, 7100), wall_bid=(304.9, 4.4, 12800))),
                scan=scan_text(bid=305.09, ask=305.11, imb=0.22, pull="вверх", up=0.64, dn=0.22, word="тянут вверх",
                               streak=(14, 3), ar=0.65, agree=True, pf=("вверх", 305.3, 305.7, 0.61, 0.8, 240),
                               pz_up=((305.3, 305.7, 0.61, 0.8),), wall_ask=(305.9, 2.3, 7100, 0.3, 0.2, True),
                               wall_bid=(304.9, 4.4, 12800, 0.58, 0.55, False), tension=(T_UP, 0.44, 0.8), hawkes_n=0.71),
                wyckoff=wyckoff_text(D1_RANGE, h1_range(
                    "НАКОПЛЕНИЕ ЗАВЕРШЕНО", "D/E — марк-ап (выход из диапазона вверх)", 48,
                    [("2026-09-30T11:00", "ЗНАК СИЛЫ (SOS)", 305.0, "закрытие над боксом на расширении (объём ×2.5) — "
                                                                    "знак силы")],
                    vol="объём на росте ×1.46 к объёму на падении — спрос доминирует"), px),
                prev_exec=exec_text(WAIT_EXEC), news=news_text([N_PROFIT, N_OFZ], fresh=[N_MORTGAGE], since="10:50"),
                council=council_range(now), memory=MEMORY_0, **tb)}


def _s_false_breakout() -> dict:
    now, px = "11:20", 302.9
    tb = _time_block(now, px)
    tb["scout"] = scout_text(now, (2914.0, 0.05), (81.0, -0.2), (93180.0, -0.02))
    return {"id": "false_breakout", "node": "review_flat", "expect": "action", "time": now,
            "title": "Ложный пробой: прокол до 304.8 и возврат к 302.9 за 10 мин, продавец в ленте",
            "args": _review_args(
                situation=situation_text(px, move=(10, -0.30, 302.8, 304.8), win30=0.9, book=(302.89, 302.91, -0.29),
                                         wait=(_mins("09:40", now), WAIT_EXEC["wait_for"]), account=ACC_FLAT,
                                         prev_review=(25, "ЖДЁМ", "цена 303.7 у верха коридора, лента паритет 0.52, "
                                                                 "пробоя 304 нет"),
                                         council_min=_mins("09:40", now)),
                light=light_text(px, at=now, close=299.6, bid=302.89, ask=302.91, bid_vol=61900, ask_vol=112700,
                                 buy=78100, sell=127500, count=4700, wall_ask=(303.3, 15600),
                                 big=(("sell", 8800, 304.6), ("sell", 6200, 303.9), ("buy", 3100, 304.7)),
                                 my=maya("вниз", (0.2, 0.64), ar=0.38, vac_dn=(302.84, 302.6, 0.7, 5),
                                           wall_ask=(303.3, 5.2, 15600))),
                scan=scan_text(bid=302.89, ask=302.91, imb=-0.29, pull="вниз", up=0.52, dn=0.33,
                               word="за всё окно тянули вверх, последние тики — вниз (перекладка?)", streak=(3, 14),
                               ar=0.37, agree=True, pz_dn=((302.6, 302.84, 0.44, 0.7),),
                               wall_ask=(303.3, 5.2, 15600, 0.62, 0.58, False), wall_bid=(302.4, 2.0, 6000, 0.25, 0.18, True),
                               tension=(T_UP, 0.39, 0.6),
                               synth=("∂P/∂t > 0: вакуум вниз углубляют — синтетическую сингулярность готовят", 0.21),
                               hawkes_n=0.64),
                wyckoff=wyckoff_text(D1_RANGE, h1_range(
                    "РАСПРЕДЕЛЕНИЕ", "C — аптраст (тест спроса)", -14,
                    [("2026-09-30T11:00", "АПТРАСТ", 304.8, "прокол над криком и возврат в бокс на объёме ×1.8 — "
                                                            "ловушка для покупателей")]), px),
                prev_exec=exec_text(WAIT_EXEC), news=NEWS_RANGE, council=council_range(now), memory=MEMORY_0, **tb)}


def _s_trend_no_pullback() -> dict:
    now, px = "11:20", 297.0
    ex = {"do": "SELL", "at": "10:10", "entry": 302.0, "kind": "откат", "take": 292.0, "inv": 304.6, "conf": 62,
          "why": "третий день марк-дауна: SOW 300.1, LPSY 301.9 — продаём откат к бывшей опоре 302",
          "plan": "засада short @302 на откате; стоп 304.6 над LPSY; цель 292 (ширина бокса 5.6 ₽ вниз от 300.2); "
                  "при уходе ниже 296 без отката — перепроверка",
          "time_note": "откат вероятнее до 13:00 МСК"}
    return {"id": "trend_no_pullback", "node": "review_flat", "expect": "action", "time": now,
            "title": "Тренд вниз 3 дня; совет ждал отката к 302 для SELL, отката нет, цена 297 идёт ниже",
            "args": _review_args(
                situation=situation_text(px, move=(10, -0.40, 296.9, 298.3), win30=-0.97, book=(296.99, 297.01, -0.24),
                                         plan={"side": "short", "entry": 302.0, "kind": "откат", "inv": 304.6,
                                               "take": 292.0, "age": _mins("10:10", now)},
                                         account=ACC_FLAT, pnl=0.0,
                                         prev_review=(30, "ЖДЁМ", "засада short @302 жива: цена 298.9, отката нет, лента "
                                                                 "за продавцом 0.41"),
                                         council_min=_mins("10:10", now)),
                light=light_text(px, at=now, close=299.9, bid=296.99, ask=297.01, bid_vol=58300, ask_vol=95600,
                                 buy=86400, sell=146800, count=5200, wall_ask=(297.4, 11200),
                                 big=(("sell", 7300, 297.3), ("sell", 5100, 297.1), ("buy", 2600, 297.2)),
                                 my=maya("вниз", (0.18, 0.69), ar=0.37, vac_dn=(296.95, 296.62, 0.74, 4),
                                           wall_ask=(297.4, 3.9, 11200))),
                scan=scan_text(bid=296.99, ask=297.01, imb=-0.24, pull="вниз", up=0.24, dn=0.63, word="тянут вниз",
                               streak=(3, 15), ar=0.36, agree=True, pf=("вниз", 296.62, 296.95, 0.52, 0.74, 210),
                               pz_dn=((296.62, 296.95, 0.52, 0.74),), wall_ask=(297.4, 3.9, 11200, 0.55, 0.5, False),
                               tension=(T_UP, 0.46, 0.74), hawkes_n=0.62),
                wyckoff=wyckoff_text(D1_TREND_DOWN, H1_DOWN, px),
                prev_exec=exec_text(ex), news=news_text([N_TAX, N_OFZ]),
                council=council_bear(now, "SBER SELL 55% 1-3 дня — марк-даун третий день, слух о налоге | триггер: откат "
                                          "к 302 с продавцом в ленте | риск: возврат над 304.6"),
                memory=MEMORY_BEAR,
                scout=scout_text(now, (2861.0, -1.05), (79.62, -1.62), (93480.0, 0.21)),
                partners=partners_text({"SBERP": (-1.0, -3.1, 296.5), "VTBR": (-1.62, -4.3, 79.62),
                                        "MX": (-1.05, -2.4, 2861.0), "SI": (0.21, 0.9, 93480.0)}))}


def _s_news_shock() -> dict:
    now, px = "10:46", 303.8
    return {"id": "news_shock", "node": "review_flat", "expect": "action", "time": now,
            "title": "Серьёзная новость 10:40 МСК (ЦБ неожиданно снизил ставку) — резкий ход +1.6% за 5 минут",
            "args": _review_args(
                situation=situation_text(px, move=(10, 1.61, 298.9, 304.0), win30=1.64, book=(303.79, 303.81, 0.38),
                                         wait=(_mins("09:40", now), WAIT_EXEC["wait_for"]), account=ACC_FLAT,
                                         prev_review=(26, "ЖДЁМ", "цена 299.1 в середине коридора 296–304, лента паритет "
                                                                 "0.50, до краёв по 1.4%"),
                                         council_min=_mins("09:40", now),
                                         reason="резкий ход: +1.61% за 10 мин (цена 303.8); серьёзная новость: ЦБ "
                                                "внепланово снизил ключевую ставку на 100 б.п. — сюрприз для рынка: "
                                                "банки и индекс в первой реакции +1.5–2.5%"),
                light=light_text(px, at=now, close=299.6, bid=303.79, ask=303.81, bid_vol=164000, ask_vol=73600,
                                 buy=318500, sell=131300, count=11800, wall_bid=(303.5, 14900),
                                 big=(("buy", 24000, 302.9), ("buy", 15500, 303.6), ("sell", 9800, 303.9)),
                                 my=maya("вверх", (0.78, 0.12), ar=0.708, vac_up=(303.85, 304.3, 0.83, 3),
                                           wall_bid=(303.5, 5.0, 14900))),
                scan=scan_text(bid=303.79, ask=303.81, imb=0.38, pull="вверх", up=0.55, dn=0.33, word="тянут вверх",
                               streak=(16, 2), ar=0.69, agree=True, ticks=860, left=197,
                               pf=("вверх", 303.85, 304.3, 0.48, 0.83, 92), pz_up=((303.85, 304.3, 0.48, 0.83),),
                               wall_bid=(303.5, 5.0, 14900, 0.4, 0.35, False), tension=(T_UP, 0.4, 0.86),
                               synth=("УДАРНАЯ ВОЛНА: глубина вакуума скакнула — сингулярность строят/сносят прямо "
                                      "сейчас", 0.61), hawkes_n=0.86),
                wyckoff=wyckoff_text(D1_RANGE, h1_range("СКЛОНЯЕТСЯ К НАКОПЛЕНИЮ", "B — построение причины", 22), px),
                prev_exec=exec_text(WAIT_EXEC), news=news_text([N_PROFIT, N_OFZ], fresh=[N_CBR], since="10:20"),
                watch=f"{hm('10:41')} · серьёзность 5 · ЦБ внепланово снизил ключевую ставку на 100 б.п.: сюрприз для "
                      f"рынка, первая реакция — банки и индекс вверх · SBER, VTBR, MX",
                council=council_range(now), memory=MEMORY_0,
                scout=scout_text(now, (2968.0, 2.05), (83.02, 2.46), (92610.0, -0.62)),
                partners=partners_text({"SBERP": (1.35, 2.2, 303.2), "VTBR": (2.46, 3.1, 83.02), "MX": (2.05, 2.5, 2968.0),
                                        "SI": (-0.62, -0.3, 92610.0)}))}


def _s_dead_market() -> dict:
    now, px = "13:40", 300.2
    tb = _time_block(now, px)
    tb["scout"] = scout_text(now, (2915.0, 0.08), (81.4, 0.2), (93200.0, 0.0))
    return {"id": "dead_market", "node": "review_flat", "expect": "wait_ok", "time": now,
            "title": "Мёртвый рынок: оборот вдвое ниже среднего, спред 0.05%, ничего не происходит (контроль)",
            "args": _review_args(
                situation=situation_text(px, move=(10, 0.03, 300.1, 300.3), win30=-0.03, book=(300.1, 300.25, 0.03),
                                         wait=(_mins("09:40", now), WAIT_EXEC["wait_for"]), account=ACC_FLAT,
                                         prev_review=(30, "ЖДЁМ", "оборот ленты 9 800 лотов за 15 мин — вдвое ниже "
                                                                 "утреннего среднего (~20 000), спред 0.15 ₽ (5 bps) против "
                                                                 "обычных 0.01, цена 300.2 без хода"),
                                         council_min=_mins("09:40", now)),
                light=light_text(px, at=now, close=299.6, bid=300.1, ask=300.25, bid_vol=21400, ask_vol=20100,
                                 buy=4650, sell=4750, count=310, big=(("buy", 400, 300.2),),
                                 my=maya(None, (0.33, 0.31), ar=0.495, vac_up=(300.25, 300.6, 0.64, 1),
                                           vac_dn=(300.1, 299.8, 0.61, 1))),
                scan=scan_text(bid=300.1, ask=300.25, imb=0.03, pull=None, up=0.41, dn=0.4,
                               word="выраженной тяги за окно нет", streak=(6, 7), ar=0.5, agree=None, ticks=3000, left=90,
                               tension=("натяжение ровное", 0.6, 0.62), synth=("производная спокойна — вакуум держат без "
                                                                             "рывков", 0.03), hawkes_n=0.12),
                wyckoff=wyckoff_text(D1_RANGE, h1_range("ХАРАКТЕР НЕ ОПРЕДЕЛЁН", "B — построение причины", 2), px),
                prev_exec=exec_text(WAIT_EXEC),
                news=news_text([], hidden=f"— новости до совета {hm('09:40')} (2 шт.) ужаты в блок ПАМЯТЬ МИССИИ; ниже "
                                          f"только свежие после совета") or NEWS_EMPTY,
                council=council_range(now), memory=MEMORY_0, **tb)}


POS_LONG_298 = {"side": "long", "lots": 30, "entry": 298.0, "held": 95, "inv": 296.4, "hard": 294.9, "take": 305.0}
EXEC_BUY_0940 = {"do": "BUY", "at": "09:40", "entry": None, "take": 305.0, "inv": 296.4, "conf": 64,
                 "why": "спринг 295.8 в 09:00 на объёме ×1.9 и возврат в бокс, покупатель в ленте 0.6",
                 "plan": "вход сейчас; стоп 296.4 под базой спринга; цель 305 (перед 305.8); над 304 — держать, подтянуть "
                         "триггер к 302",
                 "time_note": "ход к 304 вероятнее до 13:00 МСК"}
MEMORY_LONG = MEMORY_0 + (" 30.09 09:40 совет: BUY сейчас от 298 (спринг 295.8), стоп 296.4, цель 305; вход 09:45 по "
                          "298.0, 30 лотов.")
H1_SPRING = ("2026-09-30T09:00", "СПРИНГ", 295.8, "вынос стопов под лёд и возврат — объём ×1.9; топливо для роста")


def _s_long_resistance() -> dict:
    now, px = "11:20", 301.6
    return {"id": "long_resistance", "node": "review_pos", "side": "long", "expect": "exit_ok", "time": now,
            "title": "В лонге с 298, цена 301.6 (+1.2%), у сопротивления 302, объём затухает",
            "args": _review_args(
                situation=situation_text(px, move=(10, 0.10, 301.2, 301.8), win30=0.57, book=(301.59, 301.61, -0.12),
                                         pos=POS_LONG_298,
                                         account="Счёт: свободно 11680 ₽, ликвидный портфель 101080 ₽, биржа даёт купить "
                                                 "21 / продать 78 лот",
                                         prev_review=(30, "ЖДЁМ", "long +0.6% (цена 299.8), ход к 302 на объёме ×1.3, "
                                                                 "триггер 296.4 далеко"),
                                         council_min=_mins("09:40", now)),
                light=light_text(px, at=now, close=299.6, bid=301.59, ask=301.61, bid_vol=66200, ask_vol=84300,
                                 buy=14600, sell=13400, count=1200, wall_ask=(302.0, 19500),
                                 big=(("sell", 3900, 301.9),),
                                 my=maya(None, (0.29, 0.33), ar=0.521, vac_up=(301.62, 301.8, 0.44, 2),
                                           wall_ask=(302.0, 6.5, 19500), wall_bid=(301.3, 2.2, 6600))),
                scan=scan_text(bid=301.59, ask=301.61, imb=-0.12, pull=None, up=0.49, dn=0.35,
                               word="выраженной тяги за окно нет", streak=(8, 9), ar=0.52, agree=None,
                               wall_ask=(302.0, 6.5, 19500, 0.88, 0.84, False), tension=(T_DOWN, 0.62, 0.4),
                               synth=S_CALM, hawkes_n=0.31),
                wyckoff=wyckoff_text(D1_RANGE, h1_range("СКЛОНЯЕТСЯ К НАКОПЛЕНИЮ", "C — спринг (тест предложения)", 20,
                                                        [H1_SPRING]), px),
                prev_exec=exec_text(EXEC_BUY_0940), news=NEWS_RANGE, council=council_range(now), memory=MEMORY_LONG,
                **_time_block(now, px))}


def _s_long_pressure() -> dict:
    now, px = "11:20", 299.0
    pos = {"side": "long", "lots": 30, "entry": 300.5, "held": 40, "inv": 298.5, "hard": 297.0, "take": 306.0}
    ex = {"do": "BUY", "at": "10:35", "entry": None, "take": 306.0, "inv": 298.5, "conf": 58,
          "why": "выход из 300 вверх на объёме ×1.6, покупатель в ленте 0.6",
          "plan": "вход сейчас; стоп 298.5 под 299 (база выхода); цель 306; над 304 — держать",
          "time_note": "ход к 304 вероятнее до 14:00 МСК"}
    cons = {"up_share": 0.27, "dn_share": 0.61, "word": "тянут вниз", "streak": {"win": 20, "up": 3, "dn": 14},
            "aggressor_mean": 0.34, "agree": True}
    punct = pm.puncture_block("вниз", 0.63, 298.3, 298.6, role="угроза", our_side="long", in_pos=True, price=px,
                              depth=0.7, ticks=160, consensus=cons, hawkes_n=0.66, tension=T_UP)
    return {"id": "long_pressure", "node": "review_pos", "side": "long", "expect": "exit_ok", "time": now,
            "title": "В лонге с 300.5, цена 299.0 (−0.5%), до триггера 298.5 немного, продавец давит",
            "args": _review_args(
                situation=situation_text(px, move=(10, -0.63, 298.9, 300.9), win30=-0.40, book=(298.99, 299.01, -0.33),
                                         pos=pos,
                                         account="Счёт: свободно 9400 ₽, ликвидный портфель 99550 ₽, биржа даёт купить "
                                                 "19 / продать 76 лот",
                                         prev_review=(22, "ЖДЁМ", "long −0.1% (цена 300.2), покупатель держит 300, "
                                                                 "триггер 298.5 в 0.6%"),
                                         council_min=_mins("10:35", now), puncture=punct,
                                         reason="прокол сканера вниз 63 % (угроза: полоса 298.3–298.6, в позиции long)"),
                light=light_text(px, at=now, close=299.6, bid=298.99, ask=299.01, bid_vol=51800, ask_vol=102900,
                                 buy=58900, sell=116300, count=4100, wall_ask=(299.5, 12300),
                                 big=(("sell", 9100, 299.4), ("sell", 6800, 299.2), ("buy", 2500, 299.1)),
                                 my=maya("вниз", (0.16, 0.71), ar=0.336, vac_dn=(298.6, 298.3, 0.7, 39),
                                           wall_ask=(299.5, 4.1, 12300))),
                scan=scan_text(bid=298.99, ask=299.01, imb=-0.33, pull="вниз", up=0.27, dn=0.61, word="тянут вниз",
                               streak=(3, 14), ar=0.34, agree=True, pf=("вниз", 298.3, 298.6, 0.63, 0.7, 160),
                               pz_dn=((298.3, 298.6, 0.63, 0.7),), wall_ask=(299.5, 4.1, 12300, 0.6, 0.55, False),
                               tension=(T_UP, 0.42, 0.7), hawkes_n=0.66),
                wyckoff=wyckoff_text(D1_RANGE, h1_range("ХАРАКТЕР НЕ ОПРЕДЕЛЁН", "B — построение причины", -6, [
                    ("2026-09-30T11:00", "НЕТ СПРОСА", 300.6, "узкий бар вверх на объёме ×0.6 — покупатели не дожимают")]),
                    px),
                prev_exec=exec_text(ex),
                news=news_text([N_PROFIT, N_OFZ], fresh=[news_line(
                    "a4c9f2", "11:02", "rbc", "ЦБ: доля проблемных потребкредитов выросла до 11.2%", -40, 85, 30, 60, 55,
                    "вниз", 45, "факт", "дни", "SBER,VTBR,банки")], since="10:58"),
                council=council_range(now),
                memory=MEMORY_0 + (" 30.09 10:35 совет: BUY сейчас от 300.4 (выход из 300 вверх), стоп 298.5, цель 306; "
                                   "вход 10:40 по 300.5, 30 лотов."),
                **_time_block(now, px))}


def _s_short_add() -> dict:
    now, px = "11:20", 299.5
    pos = {"side": "short", "lots": 30, "entry": 303.0, "held": 70, "inv": 305.2, "hard": 306.7, "take": 296.0}
    ex = {"do": "SELL", "at": "10:05", "entry": None, "take": 296.0, "inv": 305.2, "conf": 63,
          "why": "марк-даун третий день, LPSY 301.9, продавец в ленте 0.38",
          "plan": "вход сейчас; стоп 305.2 над верхом часового бокса; цель 296, дальше 295 (кульминация 11.09); под 299 — "
                  "держать, триггер к 302",
          "time_note": "продажи вероятнее до 16:00 МСК (брифинг Минфина)"}
    d1 = wy_tf(250, "СКЛОНЯЕТСЯ К РАСПРЕДЕЛЕНИЮ", "B — построение причины", -18, 295.0, 309.6, 3.4,
               "объём на росте ×0.82 к объёму на падении — предложение доминирует",
               [("2026-09-11", "КУЛЬМИНАЦИЯ ПРОДАЖ", 295.0, "объём ×3.1, размах ×2.4 — паника слита в чьи-то руки"),
                ("2026-09-15", "АВТО-РАЛЛИ", 309.6, "отскок после кульминации — верх диапазона"),
                ("2026-09-25", "АПТРАСТ", 310.4, "прокол над криком и возврат — ловушка для покупателей"),
                ("2026-09-28", "ЗНАК СЛАБОСТИ (SOW)", 303.4, "закрытие на расширении вниз (объём ×2.2) — знак слабости")])
    return {"id": "short_add", "node": "review_pos", "side": "short", "expect": "action", "time": now,
            "title": "В шорте с 303, цена 299.5, тренд вниз продолжается, место до 295 есть (добор разумен)",
            "args": _review_args(
                situation=situation_text(px, move=(10, -0.33, 299.4, 300.6), win30=-0.73, book=(299.49, 299.51, -0.27),
                                         pos=pos,
                                         account="Счёт: свободно 10950 ₽, ликвидный портфель 101050 ₽, биржа даёт купить "
                                                 "81 / продать 17 лот",
                                         prev_review=(30, "ЖДЁМ", "short +0.4% (цена 301.8), SOW 300.1 в силе, продавец 0.4"),
                                         council_min=_mins("10:05", now)),
                light=light_text(px, at=now, close=301.9, bid=299.49, ask=299.51, bid_vol=60800, ask_vol=105700,
                                 buy=91300, sell=163900, count=5600, wall_bid=(299.1, 9800),
                                 big=(("sell", 11200, 299.8), ("sell", 7600, 299.6)),
                                 my=maya("вниз", (0.14, 0.73), ar=0.358, vac_dn=(299.46, 299.14, 0.76, 4),
                                           wall_ask=(299.9, 2.6, 7800), wall_bid=(299.1, 3.3, 9800))),
                scan=scan_text(bid=299.49, ask=299.51, imb=-0.27, pull="вниз", up=0.26, dn=0.62, word="тянут вниз",
                               streak=(4, 14), ar=0.36, agree=True, pz_dn=((299.14, 299.46, 0.47, 0.76),),
                               wall_bid=(299.1, 3.3, 9800, 0.28, 0.2, True), tension=(T_UP, 0.45, 0.69), hawkes_n=0.61),
                wyckoff=wyckoff_text(d1, H1_DOWN, px),
                prev_exec=exec_text(ex), news=news_text([N_TAX, N_OFZ]),
                council=council_bear(now, "SBER SELL 60% 1-3 дня — марк-даун, продавец в ленте | триггер: отбой от "
                                          "301–302 | риск: возврат над 305"),
                memory=MEMORY_BEAR + " 30.09 10:05 совет: SELL сейчас от 303, стоп 305.2, цель 296; вход 10:10 по 303.0, "
                                     "30 лотов.",
                scout=scout_text(now, (2866.0, -0.9), (79.8, -1.4), (93420.0, 0.18)),
                partners=partners_text({"SBERP": (-0.8, -2.9, 299.0), "VTBR": (-1.4, -4.1, 79.8), "MX": (-0.9, -2.2, 2866.0),
                                        "SI": (0.18, 0.8, 93420.0)}))}


def _entry_args(*, situation: str, plan: str, history: str, light: str, news: str, council: str, scan: str,
                scout: str, partners: str, memory: str, checks: str = "", guards: str = "") -> dict:
    return {"situation": situation, "plan": plan, "history": history, "light": light, "news": news,
            "council_text": council, "scan": scan, "scout": scout, "partners": partners, "memory": memory,
            "guards": guards, "checks": checks}


def _plan_line(price: float, how: str, plan: dict, why: str, age: int, reached: bool) -> str:
    """«ПЛАН ПИЛОТА (…)» — формат mission._plan_text."""
    return (f"ПЛАН ПИЛОТА (по нему готов войти сейчас, цена {price:g}): {how}" + (" — уровень достигнут" if reached else "")
            + f", стоп {plan['inv']}, тейк {plan['take']}; повод: {why[:200]}; приказу {age} мин (срок {PLAN_TTL_MIN} мин)")


def _s_door_now() -> dict:
    now, px = "11:20", 300.5
    ex = {"do": "BUY", "at": "11:08", "entry": None, "take": 306.0, "inv": 297.8, "conf": 64,
          "why": "спринг 295.8 отработан: возврат в бокс на объёме ×1.9, покупатель держит 299.5 (лента 0.58)",
          "plan": "вход сейчас; стоп 297.8 под базой отката; цель 306 (+1.8%); при закреплении над 304 — держать к 309",
          "time_note": "ход к 304 вероятнее до 15:00 МСК"}
    plan = {"side": "long", "entry": None, "inv": 297.8, "take": 306.0}
    rv = [("10:30", "НОВЫЙ_АНАЛИЗ", "спринг 295.8 отработан, цена 299.6 над 299.5, лента за покупателем 0.58 — нужен "
                                    "свежий разбор")]
    check = ("ЖДАТЬ — плита 18 000 лотов на продажу у 300.6 стоит, её ещё не съели; пусть съедят или снимут")
    tb = _time_block(now, px)
    return {"id": "door_now", "node": "entry", "side": "long", "expect": "action", "time": now,
            "title": "У двери: приказ совета BUY «сейчас» 12 мин назад по 300.2, цена 300.5, стакан спокойный",
            "args": _entry_args(
                situation=situation_text(px, move=(10, 0.07, 300.2, 300.6), win30=0.2, book=(300.49, 300.51, 0.08),
                                         plan=plan, account=ACC_FLAT,
                                         gate=f"ПРОВЕРКА ВХОДА: проверок входа по этому плану: 1, последняя "
                                              f"{hm('11:09')}: {check[:160]}",
                                         prev_review=(50, "НОВЫЙ_АНАЛИЗ", rv[0][2]), council_min=_mins("11:08", now)),
                plan="\n".join([exec_text(ex), reviews_line(rv),
                                _plan_line(px, "long сейчас", plan, ex["why"], _mins("11:08", now), False)]),
                history=history_text([("11:10", 300.3), ("11:11", 300.3), ("11:12", 300.2), ("11:13", 300.3),
                                      ("11:14", 300.4), ("11:15", 300.4), ("11:16", 300.6), ("11:17", 300.5),
                                      ("11:18", 300.5), ("11:19", 300.4), ("11:20", 300.5)], px, 0.2),
                light=light_text(px, at=now, close=299.6, bid=300.49, ask=300.51, bid_vol=72100, ask_vol=61400,
                                 buy=64900, sell=51300, count=2600, big=(("buy", 18000, 300.6), ("buy", 3100, 300.5)),
                                 my=maya("вверх", (0.52, 0.31), ar=0.559, vac_up=(300.55, 300.8, 0.58, 5),
                                           wall_ask=(301.0, 2.4, 7200), wall_bid=(300.2, 2.8, 8400))),
                scan=scan_text(bid=300.49, ask=300.51, imb=0.08, pull="вверх", up=0.53, dn=0.34, word="тянут вверх",
                               streak=(11, 5), ar=0.56, agree=True, pz_up=((300.55, 300.8, 0.39, 0.58),),
                               wall_ask=(300.6, None, None, 0.52, 0.45, False), wall_bid=(300.2, 2.8, 8400, 0.61, 0.58, False),
                               tension=T_FLAT, synth=S_CALM, hawkes_n=0.49),
                checks=f"{hm('11:09')} @300.3: {check} (ждать 10 мин)",
                news=NEWS_RANGE, council=council_range(now),
                memory=MEMORY_0 + (" 30.09 09:40 совет: WAIT (ждать выхода из 296–304); 10:30 дежурный PRO позвал совет "
                                   "после спринга 295.8; 11:08 совет: BUY сейчас."),
                scout=tb["scout"], partners=tb["partners"])}


def _s_door_breakout() -> dict:
    now, px = "11:20", 304.3
    ex = {"do": "BUY", "at": "10:25", "entry": 304.0, "kind": "прорыв", "take": 310.0, "inv": 301.8, "conf": 60,
          "why": "бокс 296–304 третий день, покупатель давит к верху, откаты на сухом объёме",
          "plan": "вход на пробитии 304 (подтверждение тиками за уровнем); стоп 301.8 — возврат в бокс глубже 2 ₽; цель "
                  "310 перед 309.6–310 (верх дневного диапазона)",
          "time_note": "пробой вероятен до 13:00 МСК"}
    plan = {"side": "long", "entry": 304.0, "kind": "прорыв", "inv": 301.8, "take": 310.0, "age": _mins("10:25", now)}
    rv = [("10:55", "ЖДЁМ", "засада на пробой 304 жива, цена 303.4, объём растёт ×1.4")]
    tb = _time_block(now, px)
    return {"id": "door_breakout", "node": "entry", "side": "long", "expect": "action", "time": now,
            "title": "У двери: засада «прорыв 304» достигнута, цена 304.3, объём растёт",
            "args": _entry_args(
                situation=situation_text(px, move=(10, 0.36, 303.2, 304.4), win30=0.73, book=(304.29, 304.31, 0.26),
                                         plan=plan, account=ACC_FLAT, prev_review=(25, "ЖДЁМ", rv[0][2]),
                                         council_min=_mins("10:25", now)),
                plan="\n".join([exec_text(ex), reviews_line(rv),
                                _plan_line(px, "long прорыв @304", plan, ex["why"], plan["age"], True)]),
                history=history_text([("11:10", 303.2), ("11:11", 303.3), ("11:12", 303.3), ("11:13", 303.5),
                                      ("11:14", 303.6), ("11:15", 303.8), ("11:16", 303.9), ("11:17", 304.0),
                                      ("11:18", 304.1), ("11:19", 304.2), ("11:20", 304.3)], px, 0.73),
                light=light_text(px, at=now, close=299.6, bid=304.29, ask=304.31, bid_vol=104000, ask_vol=61200,
                                 buy=118400, sell=67600, count=4200, wall_bid=(304.0, 11800),
                                 big=(("buy", 12600, 304.05), ("buy", 8200, 304.2)),
                                 my=maya("вверх", (0.63, 0.19), ar=0.637, vac_up=(304.35, 304.7, 0.69, 4),
                                           wall_bid=(304.0, 3.9, 11800))),
                scan=scan_text(bid=304.29, ask=304.31, imb=0.26, pull="вверх", up=0.6, dn=0.27, word="тянут вверх",
                               streak=(15, 3), ar=0.63, agree=True, pf=("вверх", 304.35, 304.7, 0.46, 0.69, 120),
                               pz_up=((304.35, 304.7, 0.46, 0.69),), wall_bid=(304.0, 3.9, 11800, 0.51, 0.47, False),
                               tension=(T_UP, 0.43, 0.72), hawkes_n=0.68),
                news=news_text([N_PROFIT, N_OFZ], fresh=[N_MORTGAGE], since="10:55"), council=council_range(now),
                memory=MEMORY_0 + " 30.09 10:25 совет: BUY на пробитии 304, стоп 301.8, цель 310.",
                scout=scout_text(now, (2926.0, 0.52), (81.9, 1.1), (93100.0, -0.1)), partners=tb["partners"])}


def _s_profit_fade() -> dict:
    now, px = "11:20", 303.4
    pos = POS_LONG_298
    share = (px - pos["entry"]) / (pos["take"] - pos["entry"]) * 100
    gain = (px / pos["entry"] - 1) * 100
    fl = (px - pos["entry"]) * pos["lots"] * LOT
    thr = float(getattr(config, "PYTHIA_PROFIT_THINK_PCT", 60.0))
    reason = (f"пройдено {share:.0f} % хода от входа {pos['entry']:g} до тейка {pos['take']:g} (порог {thr:g} %): "
              f"цена {px:g}, {gain:+.2f} % от входа, плавающий {fl:+.0f} ₽")
    rv = [("10:20", "ЖДЁМ", "long +0.5% (цена 299.5), ход к 305 жив, покупатель 0.58"),
          ("10:50", "ЖДЁМ", "long +1.2% (цена 301.6), ход к 305 идёт, покупатель 0.57")]
    plan = "\n".join([exec_text(EXEC_BUY_0940), reviews_line(rv),
                      f"УРОВНИ ПОЗИЦИИ: вход {pos['entry']:g}, триггер (мягкий стоп) {pos['inv']}, {HARD} {pos['hard']}, "
                      f"тейк {pos['take']}"])
    tb = _time_block(now, px)
    return {"id": "profit_fade", "node": "profit", "side": "long", "expect": "exit_ok", "time": now,
            "title": "Прибыль: лонг с 298, цель 305, цена 303.4 (+1.8%), рывок выдыхается, дельта ленты отрицательная",
            "args": {"profit": reason,
                     "situation": situation_text(px, move=(10, -0.16, 303.2, 304.1), win30=0.6, book=(303.39, 303.41, -0.18),
                                                 pos=pos,
                                                 account="Счёт: свободно 12220 ₽, ликвидный портфель 101620 ₽, биржа "
                                                         "даёт купить 21 / продать 79 лот",
                                                 prev_review=(30, rv[-1][1], rv[-1][2]),
                                                 council_min=_mins("09:40", now)),
                     "history": history_text([("11:10", 303.9), ("11:11", 304.1), ("11:12", 304.0), ("11:13", 303.8),
                                              ("11:14", 303.7), ("11:15", 303.6), ("11:16", 303.5), ("11:17", 303.3),
                                              ("11:18", 303.2), ("11:19", 303.3), ("11:20", 303.4)], px, 0.6),
                     "light": light_text(px, at=now, close=299.6, bid=303.39, ask=303.41, bid_vol=58700, ask_vol=84600,
                                         buy=55200, sell=73400, count=2900, wall_ask=(304.0, 15300),
                                         big=(("sell", 6400, 303.9), ("sell", 4200, 303.6)),
                                         my=maya("вниз", (0.22, 0.47), ar=0.429, vac_dn=(303.35, 303.1, 0.52, 4),
                                                   wall_ask=(304.0, 5.1, 15300), wall_bid=(303.0, 2.0, 6000))),
                     "plan": plan, "news": NEWS_RANGE, "council_text": council_range(now),
                     "scan": scan_text(bid=303.39, ask=303.41, imb=-0.18, pull="вниз", up=0.51, dn=0.36,
                                       word="за всё окно тянули вверх, последние тики — вниз (перекладка?)",
                                       streak=(5, 12), ar=0.44, agree=True,
                                       wall_ask=(304.0, 5.1, 15300, 0.8, 0.76, False), tension=(T_DOWN, 0.66, 0.45),
                                       synth=S_CALM, hawkes_n=0.44),
                     "scout": tb["scout"], "partners": tb["partners"], "memory": MEMORY_LONG, "guards": "", "thoughts": ""}}


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


def _s_verdict_split() -> dict:
    now, px = "11:05", 299.2
    rv = [("10:20", "ЖДЁМ", "цена 298.4 в нижней половине коридора, лента паритет 0.49"),
          ("10:50", "НОВЫЙ_АНАЛИЗ", "спринг 295.7 и возврат над 296 на объёме ×1.9 против продавца в ленте 0.41 — "
                                    "сигналы разошлись")]
    tb = _time_block(now, px)
    ctx = {"time_msk": msk(now), "price": px, "asset_class": "share", "market": MARKET,
           "reason": "перепроверка потребовала свежий разбор: спринг 295.7 и возврат над 296 на объёме ×1.9, а лента за "
                     "продавцом 0.41 — сигналы разошлись",
           "wyckoff": wyckoff_text(D1_RANGE, wy_tf(300, "НАКОПЛЕНИЕ", "C — спринг (тест предложения)", 31, 296.0, 304.0,
                                                   2.6, VOL_PAR, H1_EVENTS[:4] + [
                                                       ("2026-09-30T10:00", "СПРИНГ", 295.7, "вынос стопов под лёд и "
                                                        "возврат — объём ×1.9; топливо для роста")]), px),
           "dossier": DOSSIER_14,
           "scan": scan_text(bid=299.19, ask=299.2, imb=-0.12, pull=None, up=0.44, dn=0.45,
                             word="выраженной тяги за окно нет", streak=(7, 10), ar=0.41, agree=None,
                             wall_ask=(300.0, 5.6, 16800, 0.84, 0.8, False), wall_bid=(296.0, 7.1, 21300, 0.9, 0.88, False),
                             tension=T_FLAT, synth=S_CALM, hawkes_n=0.47),
           "scout": tb["scout"], "partners": tb["partners"], "council": council_range(now),
           "news": news_text([N_OVERDUE, N_PROFIT, N_OFZ]), "memory": MEMORY_0,
           "prev": "\n".join([exec_text(WAIT_EXEC), reviews_line(rv)])}
    return {"id": "verdict_split", "node": "verdict", "expect": "wait_ok", "time": now,
            "title": "Вердикт при спорных сигналах: анализ за лонг (спринг 296, накопление), критика против (новость, "
                     "продавец в ленте)",
            "args": {"analysis_text": ANALYSIS_14, "critique_text": CRITIQUE_14, "ctx": ctx}}


SITUATIONS: list[dict] = [f() for f in (
    _s_range_mid_wait, _s_range_low_buyer, _s_breakout_hold, _s_false_breakout, _s_trend_no_pullback, _s_news_shock,
    _s_dead_market, _s_long_resistance, _s_long_pressure, _s_short_add, _s_door_now, _s_door_breakout, _s_profit_fade,
    _s_verdict_split)]
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
    return {"version": "5.4.2", "ticker": TICKER, "play": PLAY, "runs": runs, "mock": mock,
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


# ответы фейка на все 14 ситуаций и ожидаемые (решение, kind)
FAKE_ANSWERS: dict[str, Any] = {
    "range_mid_wait": {"choice": "ЖДЁМ", "why": "середина коридора 300.0"},
    "range_low_buyer": {"choice": "КУПИТЬ_СЕЙЧАС", "why": "плита 296", "entry": None, "entry_kind": "сейчас",
                        "invalidation": 295.6, "take": 303.5},
    "breakout_hold": {"choice": "BUY", "why": "пробой удержан", "invalidation": 303.4, "take": 310},
    "false_breakout": {"choice": "ПРОДАТЬ_СЕЙЧАС", "why": "аптраст 304.8", "invalidation": 305.1, "take": 298},
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
    # формат блоков — как у боевых сборщиков
    s1 = BY_ID["range_mid_wait"]["args"]["situation"]
    for piece in ("Цена сейчас: 300", "РЫНОК: рынок открыт: торги идут (основная сессия)", "Позиции нет, засады нет",
                  "ПРИКАЗ СОВЕТА (100 мин назад): вне рынка; совет ждал:", "Это прошлое мнение, а не запрет",
                  "КУПИТЬ_СЕЙЧАС | ЖДЁМ | ПРОДАТЬ_СЕЙЧАС | НОВЫЙ_АНАЛИЗ", "Killswitch: ок"):
        assert piece in s1, piece
    assert BY_ID["breakout_hold"]["args"]["situation"].startswith("ПРОКОЛ СКАНЕРА: сторона ВВЕРХ, стойкость 61 %")
    assert "ПОВОД ПЕРЕПРОВЕРКИ (внеплановая): прокол сканера вверх 61 %" in BY_ID["breakout_hold"]["args"]["situation"]
    lt = BY_ID["range_low_buyer"]["args"]["light"]
    assert lt.startswith("SBER: цена 296.4 (30.09.2026 11:20 МСК, среда)") and "Рентген: OBI +0.31" in lt and "Майя: тяга вверх" in lt
    assert "Лента 15 мин: 3900 сделок, объём 162200, агрессор — покупатели (63% покупок), дельта +41400" in lt
    assert "ВАЙКОФФ H1 (300 баров)" in BY_ID["range_mid_wait"]["args"]["wyckoff"]
    assert BY_ID["range_mid_wait"]["args"]["scan"].startswith("СКАНЕР СТАКАНА (онлайн, тик 3 с): 1260 тиков за 63 мин")
    pos_line = BY_ID["long_resistance"]["args"]["situation"]
    assert "ПОЗИЦИЯ: long 30 лот @298, в рынке 95 мин, плавающий P/L +1080 ₽, триггер (мягкий стоп) @296.4" in pos_line
    _, u11 = build(BY_ID["door_now"])
    assert "ПРИКАЗ И ПЛАН" in u11 and "ПЛАН ПИЛОТА (по нему готов войти сейчас, цена 300.5): long сейчас" in u11
    assert "ПРОШЛЫЕ ОТВЕТЫ У ДВЕРИ" in u11 and "ВХОЖУ: long сейчас, стоп 297.8" in u11
    assert "long прорыв @304 — уровень достигнут" in build(BY_ID["door_breakout"])[1]
    assert "ПРИБЫЛЬ: пройдено 77 % хода от входа 298 до тейка 305" in build(BY_ID["profit_fade"])[1]
    sv, uv = build(BY_ID["verdict_split"])
    assert "═══ АНАЛИЗ ═══" in uv and "═══ КРИТИКА ═══" in uv and "ДОСЬЕ БИРЖИ" in uv and "решающий голос" in sv
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
    print(f"prompt_bench self-test OK: {len(SITUATIONS)} ситуаций, промпты по закону 3 (голос, ≤ {pm.SYSTEM_MAX_LINES} "
          f"строк, без BANNED, json у JSON-узлов), kind на фейковом ИИ верны, сбой вызова — silent, отчёты и dump "
          f"(28 файлов) — {time.time() - t0:.1f} с")


if __name__ == "__main__":
    sys.exit(main())
