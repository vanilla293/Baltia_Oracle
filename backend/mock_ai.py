# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — мок ИИ и рынка для демонстрации БЕЗ сети и БЕЗ ключей.

Включается переменной PYTHIA_MOCK_AI=1 (server._startup зовёт install()).
Подменяет:
  * ai.ask / ai.ask_json / ai.stream / ai.chat / ai.health — правдоподобные
    ответы по route/system/user (JSON по схемам контракта, стрим с дельтами);
  * news.fetch_news — 30 фейковых новостей за 3 дня + свежие на каждый вызов;
  * moex.last_price / moex.candles — синтетические цены/свечи (детерминированная
    функция времени: живая цена и свечи согласованы);
  * tinkoff.enabled → False (пилот не стартует — это нормально);
    PYTHIA_MOCK_TINKOFF=1 → enabled True + фейковые resolve/last_price/orderbook/
    accounts/portfolio (депозит 100000)/candles/last_trades, futures_margin → None,
    PYTHIA_DRY=1 принудительно (брокер dry, ордера в data/trader_audit.jsonl);
  * пул ключей DeepSeek: ["sk-mock"] (только в памяти) и отдельная база
    data/pythia_v5_mock.db — настоящие данные демо не трогает.
Self-тест: python3 -m backend.mock_ai
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone

from . import ai, config, moex, news, tinkoff

log = logging.getLogger("pythia.mock_ai")

_ID_RX = re.compile(r"\[([0-9a-f]{6})\]")
_ID_Q_RX = re.compile(r'"([0-9a-f]{6})"')
_PRICE_RX = re.compile(r"(?:Цена сейчас|Цена|price|цена)\D{0,12}?(\d+(?:[.,]\d+)?)", re.I)
_TICKER_RX = re.compile(r"\b([A-Z]{2,6})\b")
_STREAM_DELAY = 0.02

_LAST_IDS: list[str] = []          # короткие id, виденные в последних промптах
_CALLS: dict[str, int] = {}        # счётчик вызовов по route (для отчёта)
_TURNS: dict[str, int] = {}        # v5.4.2: номер вызова узла (перепроверка/дверь/прибыль/приказ) — ротация ответов
_POS_RX = re.compile(r"ПОЗИЦИЯ: (long|short) ")


# ═══════════════════════════════════════════════════════════════════════════
# помощники
# ═══════════════════════════════════════════════════════════════════════════
def _h(s: str, lo: int, hi: int) -> int:
    """Детерминированное число из строки в [lo, hi]."""
    d = int(hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:8], 16)
    return lo + d % (hi - lo + 1)


def _ids(text: str) -> list[str]:
    out, seen = [], set()
    for m in _ID_RX.finditer(text or ""):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(m.group(1))
    if not out:
        for m in _ID_Q_RX.finditer(text or ""):
            if m.group(1) not in seen:
                seen.add(m.group(1))
                out.append(m.group(1))
    return out


def _lines_with_ids(text: str) -> list[tuple[str, str]]:
    out = []
    for ln in (text or "").splitlines():
        m = _ID_RX.search(ln)
        if m:
            out.append((m.group(1), ln))
    return out


_KW = {
    "SBER": ("сбер", "sber", "банк"),
    "BR": ("нефт", "brent", "опек", "баррел"),
    "GAZP": ("газпром", "газ "),
    "SI": ("доллар", "рубл", "курс", "usd"),
    "MX": ("индекс", "мосбирж", "imoex", "рынок акций"),
    "GD": ("золот",),
    "LKOH": ("лукойл",),
    "ROSN": ("роснефт",),
}
_JUNK = ("рецепт", "сериал", "футбол", "гороскоп", "погода", "кино", "звезда", "свадьб", "шоу")
_CB = ("цб", "центробанк", "ключев", "ставк", "набиуллин")


def _is_junk(line: str) -> bool:
    ln = line.lower()
    return any(k in ln for k in _JUNK)


def _is_market(line: str) -> bool:
    ln = line.lower()
    if any(k in ln for k in _CB):
        return True
    return any(any(k in ln for k in kws) for kws in _KW.values())


def _assets_of(line: str) -> list[str]:
    ln = line.lower()
    out = [t for t, kws in _KW.items() if any(k in ln for k in kws)]
    if any(k in ln for k in _CB):
        out = ["SBER", "рубль", "индекс"] + [a for a in out if a not in ("SBER",)]
    return out[:5] or ["индекс"]


def _price_from(text: str, default: float = 100.0) -> float:
    m = _PRICE_RX.search(text or "")
    if m:
        try:
            v = float(m.group(1).replace(",", "."))
            if v > 0:
                return v
        except ValueError:
            pass
    return default


def _section(text: str, head: str, next_heads: tuple[str, ...] = ()) -> str:
    """Кусок user-текста после заголовка head до следующего заголовка."""
    i = (text or "").find(head)
    if i < 0:
        return ""
    rest = text[i + len(head):]
    cut = len(rest)
    for nh in next_heads:
        j = rest.find(nh)
        if 0 <= j < cut:
            cut = j
    return rest[:cut]


def _catalog_tickers(text: str) -> list[str]:
    cat = _section(text, "КАТАЛОГ:", ("НОВОСТИ:",))
    out = []
    for ln in cat.splitlines():
        m = re.match(r"\s*([A-Z0-9]{2,8}) — ", ln)
        if m:
            out.append(m.group(1))
    return out


def _turn(key: str) -> int:
    """Номер вызова узла 1, 2, 3… — детерминированная ротация демо (v5.4.2): мок нейтрален — не «всегда ждём» и
    не «всегда входим»; по какому маршруту ни пришёл вопрос (route или схема), счёт один на узел."""
    _TURNS[key] = _TURNS.get(key, 0) + 1
    return _TURNS[key]


def _pos_side(text: str) -> str | None:
    """Сторона открытой позиции из ситуации («ПОЗИЦИЯ: long 8 лот …»); позиции нет — None."""
    m = _POS_RX.search(text or "")
    return m.group(1) if m else None


def _plan_side(text: str) -> str:
    """Сторона плана у двери: первое long/short в блоке приказа (иначе во всём тексте); по умолчанию long."""
    m = re.search(r"\b(long|short)\b", _section(text, "ПРИКАЗ И ПЛАН") or text or "")
    return m.group(1) if m else "long"


def _remember(ids: list[str]) -> None:
    global _LAST_IDS
    if ids:
        _LAST_IDS = ids[:60]


# ═══════════════════════════════════════════════════════════════════════════
# ответы по route
# ═══════════════════════════════════════════════════════════════════════════
def _triage(user: str) -> dict:
    keep = []
    for sid, ln in _lines_with_ids(user):
        if _is_junk(ln):
            continue
        if _is_market(ln) or _h(sid, 0, 1) == 0:
            keep.append(sid)
    _remember(keep)
    return {"keep": keep}


def _characterize(user: str) -> dict:
    title = _section(user, "Заголовок:", ("Текст:",)).strip() or user[:120]
    body = _section(user, "Текст:").strip()
    full = title + " " + body
    low = full.lower()
    cb = any(k in low for k in _CB)
    up = any(w in low for w in ("рост", "рекорд", "прибыл", "повыс", "дивиденд", "сниз ставк"))
    down = any(w in low for w in ("паден", "снижен", "убыт", "санкц", "штраф", "повысил ставк"))
    tone = _h(title, -60, 60) + (25 if up else 0) - (25 if down else 0)
    tone = max(-100, min(100, tone))
    eff = "вверх" if tone > 15 else "вниз" if tone < -15 else "нейтрально"
    imp = _h(title + "i", 30, 70) + (25 if cb else 0)
    kind = "прогноз" if "прогноз" in low or "ожида" in low else "мнение" if "считает" in low else "факт"
    assets = _assets_of(full)
    sectors = ["банки"] if "SBER" in assets else ["нефтегаз"] if any(a in assets for a in ("BR", "GAZP", "LKOH", "ROSN")) else ["рынок"]
    tags = (["ставка"] if cb else []) + (["дивиденды"] if "дивиденд" in low else []) + (["отчётность"] if "прибыл" in low else [])
    actors = (["ЦБ"] if cb else []) + (["Сбер"] if "SBER" in assets else []) + (["ОПЕК"] if "опек" in low else [])
    return {
        "gist": (body[:160] or title) + ". Для биржи это значит переоценку ожиданий по " + ", ".join(assets[:2]) + ".",
        "one_liner": title[:118],
        "tone": tone, "honesty": _h(title + "h", 55, 92), "hype": _h(title + "y", 10, 70),
        "importance": min(100, imp), "novelty": _h(title + "n", 30, 90),
        "kind": kind, "horizon": "часы" if cb else ("дни" if _h(title, 0, 1) else "недели"),
        "expected_effect": eff, "effect_strength": abs(tone) // 2 + (20 if cb else 5),
        "manipulation_risk": _h(title + "m", 5, 40), "actors": actors or ["рынок"],
        "assets": assets, "sectors": sectors, "tags": tags or ["рынок"],
        "why_market": ("цена денег для всего рынка" if cb else f"прямо влияет на {assets[0]}"),
    }


def _group(user: str) -> dict:
    pairs = _lines_with_ids(user)
    buckets: dict[str, list[str]] = {"ставка_цб": [], "сбер": [], "нефть": [], "рубль": []}
    orphans = []
    for sid, ln in pairs:
        low = ln.lower()
        if any(k in low for k in _CB):
            buckets["ставка_цб"].append(sid)
        elif any(k in low for k in _KW["SBER"]):
            buckets["сбер"].append(sid)
        elif any(k in low for k in _KW["BR"]):
            buckets["нефть"].append(sid)
        elif any(k in low for k in _KW["SI"]):
            buckets["рубль"].append(sid)
        else:
            orphans.append(sid)
    meta = {"ставка_цб": ("ЦБ и ставка", -30, 85, ["SBER", "рубль", "индекс"]),
            "сбер": ("Сбербанк: отчётность и дивиденды", 35, 65, ["SBER"]),
            "нефть": ("Нефть и ОПЕК", -10, 60, ["BR", "LKOH", "ROSN"]),
            "рубль": ("Курс рубля", -20, 50, ["SI", "рубль"])}
    streams = []
    for tag, ids in buckets.items():
        if not ids:
            continue
        title, tone, heat, assets = meta[tag]
        streams.append({"tag": tag, "title": title,
                        "gist": f"{title}: {len(ids)} новостей за окно. Тон {tone:+d}, накал {heat}. "
                                f"Рынок отыгрывает это в первые часы после публикации.",
                        "ids": ids, "net_tone": tone, "heat": heat, "assets": assets})
    _remember([s for s, _ in pairs])
    for extra in streams[3:]:                      # не больше трёх потоков: лишние — в одиночки
        orphans.extend(extra["ids"])
    return {"streams": streams[:3], "orphans": orphans}


def _summary(user: str, human: bool) -> dict:
    ids = _LAST_IDS[:6] or _ids(user)[:6]
    picks = [
        {"ticker": "SBER", "side": "long", "conviction": 72, "horizon": "день",
         "why": "Отчётность сильная, ставка без сюрпризов — банк отыгрывает вверх первым.",
         "trigger": "закрепление выше 300 на объёме", "risk": "жёсткий сигнал ЦБ вечером",
         "news_ids": ids[:3]},
        {"ticker": "BR", "side": "short", "conviction": 58, "horizon": "дни",
         "why": "ОПЕК+ наращивает добычу, спрос слабеет — давление на нефть сохраняется.",
         "trigger": "отбой от 70", "risk": "геополитика и внезапные ограничения поставок",
         "news_ids": ids[3:5]},
        {"ticker": "GAZP", "side": "long", "conviction": 51, "horizon": "дни",
         "why": "Газ в Европе дорожает, бумага отстала от рынка.",
         "trigger": "выше 130", "risk": "дивидендная неопределённость",
         "news_ids": ids[5:6]},
    ]
    out = {"regime": "смешанно",
           "summary": "Рынок живёт ставкой и отчётностью банков: деньги идут в Сбер, нефть под давлением "
                      "решения ОПЕК+, рубль стабилен. Работаем избирательно: лонг сильных бумаг, "
                      "шорт нефти от сопротивления. Ключевое время — 14:00 МСК (данные по инфляции).",
           "picks": picks,
           "avoid": [{"ticker": "SI", "why": "курс зажат интервенциями — движения нет"}],
           "watch": [{"ticker": "MX", "why": "индекс у верхней границы диапазона"}],
           "key_times": ["14:00 МСК — недельная инфляция", "19:00 МСК — вечерняя сессия, ликвидность падает"]}
    if human:
        out["human_alignment"] = [
            {"thesis": "Сбер пойдёт вверх на отчётности", "verdict": "согласен",
             "why": "цифры подтверждают, конъюнктура ставки не мешает"},
            {"thesis": "нефть развернётся вверх", "verdict": "частично",
             "why": "техника за отскок, но ОПЕК+ против — только короткие сделки"},
        ]
    return out


def _distribute(user: str) -> dict:
    picks_txt = _section(user, "ВЫБОР СОВЕТА:", ("КАТАЛОГ:",))
    catalog = set(_catalog_tickers(user))
    news_pairs = _lines_with_ids(_section(user, "НОВОСТИ:"))
    tickers = []
    for ln in picks_txt.splitlines():
        m = re.match(r"\s*ВХОД\s+([A-Z0-9]{2,8})", ln)
        if m and (not catalog or m.group(1) in catalog) and m.group(1) not in tickers:
            tickers.append(m.group(1))
    cards = []
    for t in tickers:
        kws = _KW.get(t, (t.lower(),))
        mine = [sid for sid, ln in news_pairs if any(k in ln.lower() for k in kws) or t in ln]
        if not mine:
            mine = [sid for sid, _ in news_pairs[:3]]
        cards.append({"ticker": t, "news_ids": mine[:8],
                      "note": f"{t}: новости окна {'подтверждают' if _h(t, 0, 2) else 'не мешают'} идее совета"})
    return {"cards": cards}


def _human_compress(user: str) -> dict:
    text = _section(user, "ТЕКСТ ЧЕЛОВЕКА:").strip() or user.strip()
    sents = [s.strip() for s in re.split(r"[.!?\n]+", text) if s.strip()]
    by_asset: dict[str, list[str]] = {}
    theses = []
    for s in sents[:12]:
        assets = _assets_of(s)
        a = assets[0] if assets else "рынок"
        by_asset.setdefault(a, []).append(s[:200])
        low = s.lower()
        d = "вверх" if any(w in low for w in ("рост", "вверх", "выше", "лонг", "купи")) else \
            "вниз" if any(w in low for w in ("паден", "вниз", "ниже", "шорт", "прода")) else "нейтрально"
        theses.append({"asset": a, "claim": s[:200], "direction": d,
                       "confidence": ("высокая", "средняя", "низкая")[_h(s, 0, 2)]})
    return {"clean_text": text[:4000], "by_asset": by_asset or {"рынок": [text[:200]]},
            "theses": theses or [{"asset": "рынок", "claim": text[:200], "direction": "нейтрально",
                                  "confidence": "средняя"}],
            "questions": ["Какой горизонт удержания — день или неделя?"]}


def _present(user: str) -> dict:
    tks = [t for t in dict.fromkeys(_TICKER_RX.findall(user)) if t in _KW][:4] or ["SBER"]
    mission = "миссия" in user.lower() or "приказ" in user.lower() or "exec" in user.lower()
    return {"headline": ("Приказ собран: сторона, уровни и срок заданы" if mission
                         else "Картина ясна: избирательный лонг, нефть под давлением"),
            "frame": ("Совет посмотрел на досье инструмента, свежие новости и общий фон и свёл это в приказ: "
                      "сторона или ожидание, уровни и срок; перед заявкой дежурный сверит его с живым рынком."
                      if mission else
                      "Совет разложил новости на потоки, сверил с ценами и астро-фоном. Деньги идут в сильные "
                      "бумаги, нефть слабая, рубль без движения. Ключевые часы — 14:00 и 19:00 МСК."),
            "highlights": [{"ticker": t, "side": "short" if t == "BR" else "long",
                            "line": f"{t}: {'давление продавцов' if t == 'BR' else 'покупатели держат уровень'}"}
                           for t in tks]}


def _impact(user: str) -> dict:
    new = _section(user, "НОВЫЕ НОВОСТИ:")
    serious = any(k in new for k in ("ЦБ", "Центробанк")) or "ставк" in new.lower()
    affected = sorted({a for _, ln in _lines_with_ids(new) for a in _assets_of(ln)})[:5] or ["рынок"]
    if serious:
        return {"changes": True, "severity": 80,
                "note": "ЦБ меняет ожидания по ставке: банки и рубль реагируют первыми, прежний итог "
                        "по SBER требует пересмотра. Уровни входа сдвигаются, риск-он под вопросом.",
                "affected": affected, "recommend_rerun": True}
    return {"changes": False, "severity": 30,
            "note": "Новых радикальных фактов нет: новости в русле прежнего итога совета. Уровни и "
                    "стороны сделок не меняются.",
            "affected": affected, "recommend_rerun": False}


def _exec(system: str, user: str) -> dict:
    """Приказ (v5.4.2): чаще BUY/SELL по режиму, каждый третий вызов без позиции — WAIT с wait_for и уровнями, при
    открытой позиции — HOLD (держать как есть, без добора; уровни — по стороне позиции)."""
    n = _turn("exec")
    price = _price_from(system, 0.0) or _price_from(user, 100.0)
    sell_only = "только SELL" in system
    ids = _ids(_section(user, "НОВОСТИ"))[:3]
    if n % 3 == 0 and "ОТКРЫТАЯ ПОЗИЦИЯ" not in user:          # WAIT — только без позиции
        lo, hi = round(price * 0.99, 4), round(price * 1.005, 4)
        edge = f"закрепление ниже {lo} или отскок к {hi}" if sell_only else f"закрепление выше {hi} или откат к {lo}"
        return {"do": "WAIT", "entry": None, "entry_kind": "сейчас", "take": None, "invalidation": None,
                "wait_for": edge, "why": f"мок: цена {price:g} в середине коридора {lo}–{hi} — вход у края",
                "plan": f"Вне рынка до события: {edge}.\nДежурный смотрит на край коридора на перепроверке.",
                "confidence": 50, "news_ids": ids, "levels": [lo, round(price, 4), hi],
                "time_note": "край коридора вероятнее к 14:00 МСК (данные по инфляции)"}
    if n % 3 == 0:                                              # позиция открыта: держать как есть (ревью 5.4.2)
        side = _pos_side(user) or ("short" if sell_only else "long")
        up = side == "long"
        take, inv = round(price * (1.02 if up else 0.98), 4), round(price * (0.99 if up else 1.01), 4)
        return {"do": "HOLD", "entry": None, "entry_kind": "сейчас", "take": take, "invalidation": inv, "wait_for": "",
                "why": f"мок: позиция {side} у {price:g} идёт по плану — держим как есть, без добора",
                "plan": f"Держим позицию.\nТейк {take}, стоп {inv}.\nДобора нет.", "confidence": 58, "news_ids": ids,
                "levels": [inv, round(price, 4), take], "time_note": "перепроверка по расписанию"}
    if sell_only:
        do, take, inv = "SELL", round(price * 0.98, 4), round(price * 1.01, 4)
    else:
        do, take, inv = "BUY", round(price * 1.02, 4), round(price * 0.99, 4)
    return {"do": do, "entry": None, "take": take, "invalidation": inv, "wait_for": "",
            "why": "мок: импульс совпал с новостями и лентой, стопы толпы по ту сторону сняты",
            "plan": f"Входим {do} по рынку на максимум объёма.\nТейк {take}, стоп {inv} (короткий).\n"
                    f"Добавляем только после закрепления за {round(price * (1.005 if do == 'BUY' else 0.995), 4)}.\n"
                    f"Перепроверка каждые 30 минут; при слабости ленты — выход по рынку.",
            "confidence": 64, "news_ids": ids,
            "levels": [round(price * 0.99, 4), round(price, 4), round(price * 1.02, 4)],
            "time_note": "движение ждём в первые 2 часа, к 19:00 МСК ликвидность падает"}


def _review(system: str, user: str) -> dict:
    """Перепроверка (v5.4.2): ротация по номеру вызова. Вне рынка — ЖДЁМ / КУПИТЬ_СЕЙЧАС (ПРОДАТЬ_СЕЙЧАС, если
    режим пускает только SELL; invalidation ≈ цена·0.99) / НОВЫЙ_АНАЛИЗ; в позиции — ЖДЁМ / ЗАКРЫТЬ."""
    n = _turn("review")
    price = _price_from(user, 0.0)
    px = f"{price:g}" if price else "текущей цены"
    pos = _pos_side(user)
    if pos:
        if n % 2:
            return {"choice": "ЖДЁМ", "why": f"мок: позиция {pos} у {px} идёт между триггером и тейком, лента без слома",
                    "invalidation": None, "take": None,
                    "note": "Держим позицию: триггер и тейк на месте.\nСледующая перепроверка — по расписанию."}
        return {"choice": "ЗАКРЫТЬ", "why": f"мок: у {px} импульс выдохся, лента редеет — забираю, что есть",
                "invalidation": None, "take": None, "note": "Закрываю позицию по рынку."}
    k = n % 3
    if k == 1:
        return {"choice": "ЖДЁМ", "why": f"мок: у {px} цена в середине коридора, до уровней далеко",
                "invalidation": None, "take": None, "note": "Вне рынка: смотрю на края коридора."}
    if k == 2:
        opts = _section(user, "ДОПУСТИМЫЕ choice:", ("\n",)) or system
        # v5.4.3: метки списка без «_СЕЙЧАС» (КУПИТЬ / ПРОДАТЬ) — узнаём по корню, старые метки тоже подходят
        sell = "ПРОДАТЬ" in opts and "КУПИТЬ" not in opts
        choice = "ПРОДАТЬ_СЕЙЧАС" if sell else "КУПИТЬ_СЕЙЧАС"
        inv = round(price * (1.01 if sell else 0.99), 4) if price else None
        take = round(price * (0.98 if sell else 1.02), 4) if price else None
        return {"choice": choice, "why": f"мок: у {px} {'продавцы' if sell else 'покупатели'} держат уровень, лента за сделку",
                "entry": None, "entry_kind": "сейчас", "invalidation": inv, "take": take,
                "note": f"Вход {'в шорт' if sell else 'в лонг'} сейчас, триггер {inv}, тейк {take}."}
    return {"choice": "НОВЫЙ_АНАЛИЗ", "why": "мок: картина поменялась с прошлого совета — нужен свежий разбор",
            "invalidation": None, "take": None, "note": "Зову полный совет."}


def _stream_text(route: str, user: str) -> tuple[str, str]:
    price = _price_from(user, 0.0)
    tks = [t for t in dict.fromkeys(_TICKER_RX.findall(user)) if t in _KW][:3] or ["SBER", "BR"]
    t0 = tks[0]
    px = f"{price:g}" if price else "уровня открытия"
    think = (f"Смотрю данные по {', '.join(tks)}. Цена {px}. Проверяю новости, стакан и тайминги. "
             f"Ищу, где тезисы расходятся с ценой.")
    if route.endswith("analysis"):
        text = (f"АНАЛИЗ. {t0}: цена {px}, покупатели держат уровень, объём выше среднего за 5 дней. "
                f"Новости: ставка без сюрпризов, отчётность сильная — фон для {t0} положительный. "
                f"Нефть под давлением ОПЕК+, шорт BR от сопротивления оправдан. Рубль зажат — SI без движения. "
                f"Ключевое время — 14:00 МСК (инфляция): до него объёмы тонкие, после — импульс. "
                f"Уровни {t0}: поддержка {px}×0.99, сопротивление {px}×1.02.")
    elif route == "chat":
        m = re.search(r"ВОПРОС ВЛАДЕЛЬЦА:\s*(.+)", user)
        q = (m.group(1).strip() if m else "вопрос")[:120]
        text = (f"По вопросу «{q}»: {t0} у {px}, позиция и уровни — как в данных выше: держим по плану, триггер "
                f"мягкого стопа ниже входа, аварийный трос ещё дальше. Из чата приказов бирже нет: закрыть — кнопка "
                f"ПАНИКА на экране миссии, пересмотреть — ПЕРЕСМОТР. (мок-ответ без сети)")
    elif route.endswith("critique"):
        text = (f"КРИТИКА. Слабое место — ставка: жёсткий сигнал ЦБ вечером ломает лонг {t0}. "
                f"Объём «выше среднего» может быть закрытием шортов, а не притоком. По нефти шорт против "
                f"геополитики — риск гэпа. Стопы обязаны быть короткими (≤1%), размер — с учётом 19:00 МСК, "
                f"когда ликвидность уходит. Данных по ленте мало — не выдумываем.")
    else:
        text = (f"ВЕРДИКТ. {t0} — long от {px}: крупный игрок набирает, стопы толпы ниже уже сняты; "
                f"стоп {px}×0.99 (там идея мертва), тейк {px}×1.02, уверенность 70%. BR — short от "
                f"сопротивления, уверенность 58%. SI — вне рынка до выхода из коридора. "
                f"MX смотрим у верхней границы. Пересмотр после 14:00 МСК по данным инфляции.")
    return think, text


def _guard(user: str) -> dict:
    """Мягкий стоп (v5.2): мок чередует ЖДАТЬ / СЛИТЬ, чтобы демо показывало оба пути."""
    n = _CALLS.get("mission_guard", 1)
    if n % 2:
        return {"decision": "ЖДАТЬ", "why": "мок: прокол без объёма, стакан держит — ждём и передаём задачу Совету",
                "hold_until_price": None, "hold_minutes": 5, "note": "мок-ответ у троса"}
    return {"decision": "СЛИТЬ", "why": "мок: поток продавцов, лента против — сливаем", "hold_until_price": None,
            "hold_minutes": None, "note": "мок-ответ у троса"}


def _take(user: str) -> dict:
    """Мягкий тейк (v5.3 W2): мок чередует ПОДЕРЖАТЬ / ЗАФИКСИРОВАТЬ, lock_price и tp_next — от цены в промпте."""
    n = _CALLS.get("mission_take", 1)
    price = _price_from(user, 0.0)
    if n % 2:
        return {"decision": "ПОДЕРЖАТЬ", "why": "мок: импульс не выдохся, стакан толкает дальше — держим и передаём Совету",
                "lock_price": round(price * 0.995, 4) if price else None, "tp_next": round(price * 1.01, 4) if price else None,
                "hold_minutes": 10, "note": "мок-ответ у тейка"}
    return {"decision": "ЗАФИКСИРОВАТЬ", "why": "мок: цель взята, лента редеет — фиксируем", "lock_price": None,
            "tp_next": None, "hold_minutes": None, "note": "мок-ответ у тейка"}


def _event_triage(user: str) -> dict:
    """Триаж события (v5.3 W2): мок по кругу — ПЛАНОВО / САМ (подтянуть трос) / СЕЙЧАС."""
    n = _CALLS.get("event_triage", 1)
    price = _price_from(user, 0.0)
    if n % 3 == 1:
        return {"urgency": "ПЛАНОВО", "action": None, "why": "мок: ход в русле плана, PRO подождёт плановой"}
    if n % 3 == 2:
        return {"urgency": "САМ", "action": "подтянуть_трос", "trigger": round(price * 0.992, 4) if price else None,
                "why": "мок: прибыль есть — подтянем триггер, PRO не нужен"}
    return {"urgency": "СЕЙЧАС", "action": None, "why": "мок: картина могла сломаться — будим PRO"}


def _entry(user: str) -> dict:
    """Проверка входа у двери (v5.4.2): ротация ВОЙТИ / ЖДАТЬ (откат: entry ≈ цена·0.997 для long, ·1.003 для
    short) / ВОЙТИ. Только PYTHIA_MOCK_AI=1; в бой не течёт."""
    n = _turn("entry")
    price = _price_from(user, 0.0)
    px = f"{price:g}" if price else "текущей цене"
    if n % 3 == 2:
        short = _plan_side(user) == "short"
        entry = round(price * (1.003 if short else 0.997), 4) if price else None
        return {"decision": "ЖДАТЬ", "why": f"мок: у {px} лента вялая, точка лучше рядом — откат к {entry}",
                "entry": entry, "entry_kind": "откат", "wait_minutes": None, "invalidation": None, "take": None,
                "council": False, "note": "мок-ответ у двери"}
    return {"decision": "ВОЙТИ", "why": f"мок: при {px} лента и стакан не отменили идею приказа — вхожу",
            "entry": None, "entry_kind": None, "wait_minutes": None, "invalidation": None, "take": None,
            "council": False, "note": "мок-ответ у двери"}


def _profit(user: str) -> dict:
    """Мысль о прибыли (v5.4.2): ротация ДЕРЖАТЬ / ВЫЙТИ / ДЕРЖАТЬ; триггер и цель не двигает."""
    n = _turn("profit")
    if n % 3 == 2:
        return {"decision": "ВЫЙТИ", "why": "мок: рывок выдохся, лента редеет — забираю прибыль",
                "lock_price": None, "take": None, "reentry": None, "reentry_kind": None, "note": "мок-ответ о прибыли"}
    return {"decision": "ДЕРЖАТЬ", "why": "мок: ход жив, лента не редеет — держу, цель та же",
            "lock_price": None, "take": None, "reentry": None, "reentry_kind": None, "note": "мок-ответ о прибыли"}


def _explain(user: str) -> str:
    """Толмач (v5.3): живой текст по данным владельца — события, цена, позиция, приказ."""
    price = _price_from(user, 0.0)
    ev = [l.strip() for l in _section(user, "ЧТО ПРОИЗОШЛО", ("СИТУАЦИЯ", "ПРИКАЗ", "ПОСЛЕДНИЕ", "ПАМЯТЬ", "СВЕЖИЕ",
                                                             "СВЯЗАННЫЕ", "ТВОИ")).splitlines() if l.strip()]
    ev = [l.split(" · ", 1)[1] if " · " in l else l for l in ev if not l.startswith("═══")]
    what = ev[0] if ev else "узел миссии"
    tail = f" и ещё: {'; '.join(e.split(':')[0] for e in ev[1:3])}" if len(ev) > 1 else ""
    sit = _section(user, "СИТУАЦИЯ ПИЛОТА", ("ПРИКАЗ", "ПОСЛЕДНИЕ", "ПАМЯТЬ", "СВЕЖИЕ", "СВЯЗАННЫЕ", "ТВОИ"))
    pos = re.search(r"ПОЗИЦИЯ: (\w+) (\d+) лот @([\d.]+)", sit)
    trig = re.search(r"триггер \(мягкий стоп\) @([\d.]+)", sit)
    take = re.search(r"тейк ([\d.]+)", sit)
    px = f"{price:g}" if price else "текущей цене"
    if pos:
        second = (f"Позиция {('лонг' if pos.group(1) == 'long' else 'шорт')} {pos.group(2)} лот от {pos.group(3)}, цена сейчас {px}"
                  + (f", триггер мягкого стопа {trig.group(1)}" if trig else "") + (f", тейк {take.group(1)}" if take else "")
                  + ". Пилот держит план: у триггера не бьёт стоп вслепую, а спрашивает FLASH, за аварийным тросом закрывает без вопросов.")
    else:
        second = (f"Позиции сейчас нет, цена {px}. Пилот ждёт своего момента по приказу: вход по рынку или у уровня, "
                  "размер даст биржа.")
    return (f"Только что: {what}{tail}. {second} Дальше смотри на цену у ближайших уровней и на свежие новости — "
            f"если картина сломается, дежурный PRO перепроверит и решит, держать или выходить.")


def _memory(user: str) -> str:
    """Память миссии (v5.3): один абзац «что было и чем кончилось» по накопившемуся."""
    acc = _section(user, "НАКОПИЛОСЬ С ТЕХ ПОР", ("ТЕКУЩИЙ ПРИКАЗ",))
    old = _section(user, "ПРОШЛАЯ ПАМЯТЬ", ("НАКОПИЛОСЬ",)).strip()
    n_rev = len(re.findall(r"\d\d:\d\d (?:ЖДЁМ|ЗАКРЫТЬ|КУПИТЬ_СЕЙЧАС|ПРОДАТЬ_СЕЙЧАС|ДОБРАТЬ|ПЕРЕВЕРНУТЬ|НОВЫЙ_АНАЛИЗ)", acc))
    n_tr = len(re.findall(r"P/L [+-]?\d+ ₽ —", acc))
    n_news = len(_ids(acc))
    tm = re.search(r"ВРЕМЯ: (.+? МСК)", user)
    when = tm.group(1) if tm else "сейчас"
    head = (old[:400] + " Далее: ") if old else ""
    return (f"{head}К {when}: перепроверок {n_rev}, сделок закрыто {n_tr}, новостей до совета {n_news}. Идея совета "
            f"держалась по плану, стопы и тейки не менялись без повода; что не влияет на текущую картину — отпущено. "
            f"Дальше пилот идёт по свежему приказу.")[: 1500]


def _scout(user: str) -> dict:
    """Разведка (v5.2): мок просит фьючерс на индекс (MX) сейчас и за неделю и курс рубля;
    индексов MOEX ISS нет (v5.3), связанные бумаги разведка добавляет сама."""
    return {"requests": [{"kind": "quote", "code": "MX", "why": "фон рынка"},
                         {"kind": "history", "code": "MX", "days": 5, "interval": "1d", "why": "неделя индекса"},
                         {"kind": "quote", "code": "SI", "why": "рубль"}],
            "note": "мок: фьючерс на индекс сейчас и за неделю, рубль"}


def _answer(route: str, system: str, user: str, json_mode: bool):
    """Ответ по route. Возвращает dict (JSON-стадии) или str (текст)."""
    _CALLS[route] = _CALLS.get(route, 0) + 1
    r = route or ""
    if r == "triage":
        return _triage(user)
    if r == "characterize":
        return _characterize(user)
    if r in ("group", "group_merge"):
        return _group(user)
    if r == "summary":
        return _summary(user, human='"human_alignment"' in system)
    if r == "distribute":
        return _distribute(user)
    if r == "human_compress":
        return _human_compress(user)
    if r == "present":
        return _present(user)
    if r == "impact":
        return _impact(user)
    if r == "mission_exec":
        return _exec(system, user)
    if r in ("mission_review", "aip_review"):
        return _review(system, user)
    if r == "shrink":
        m = re.search(r"ЛИМИТ:\s*(\d+)", user)
        lim = int(m.group(1)) if m else 4000
        body = _section(user, "ТЕКСТ:").strip() or user
        return body[: max(200, lim - 20)]
    if r == "health":
        return "готов"
    if r == "mission_guard":                     # v5.2: мягкий стоп — FLASH у троса
        return _guard(user)
    if r == "mission_take":                      # v5.3 W2: мягкий тейк — FLASH у тейка
        return _take(user)
    if r == "event_triage":                      # v5.3 W2: триаж события
        return _event_triage(user)
    if r == "mission_entry":                     # v5.4.1: проверка входа у двери (5.4.2: ротация ответов)
        return _entry(user)
    if r == "mission_profit":                    # v5.4.1: мысль о прибыли (5.4.2: ротация ответов)
        return _profit(user)
    if r == "scout":                             # v5.2: разведка данных перед советом/миссией/чатом
        return _scout(user)
    if r == "chat":                              # v5.2: чат панели
        return _stream_text("chat", user)[1]
    if r == "explain":                           # v5.3: толмач — объяснение владельцу
        return _explain(user)
    if r == "memory":                            # v5.3: память миссии одним абзацем
        return _memory(user)
    if json_mode:
        # неизвестная JSON-стадия — угадываем по схеме в system
        if '"keep"' in system:
            return _triage(user)
        if '"cards"' in system:
            return _distribute(user)
        if '"choice"' in system:
            return _review(system, user)
        if '"do"' in system:
            return _exec(system, user)
        if '"severity"' in system:
            return _impact(user)
        if '"urgency"' in system:
            return _event_triage(user)
        if '"wait_minutes"' in system:           # v5.4.1: схема проверки входа
            return _entry(user)
        if '"reentry"' in system:                # v5.4.1: схема мысли о прибыли (lock_price есть и у тейка — проверка раньше)
            return _profit(user)
        if '"lock_price"' in system:
            return _take(user)
        if '"picks"' in system:
            return _summary(user, '"human_alignment"' in system)
        if '"streams"' in system:
            return _group(user)
        if '"headline"' in system:
            return _present(user)
        if '"gist"' in system:
            return _characterize(user)
        return {"ok": True, "note": "мок: схема не распознана"}
    return _stream_text(r, user)[1]


# ═══════════════════════════════════════════════════════════════════════════
# подмены ai.*
# ═══════════════════════════════════════════════════════════════════════════
async def _m_ask(system: str, user: str, *, model=None, json_mode=False, thinking=None, effort=None,
                 temperature=1.0, max_tokens=None, route="ask", api_key=None) -> str:
    await asyncio.sleep(0.03)
    out = _answer(route, system, user, json_mode)
    if isinstance(out, (dict, list)):
        return json.dumps(out, ensure_ascii=False)
    return str(out)


async def _m_ask_json(system: str, user: str, *, model=None, thinking=None, effort=None,
                      max_tokens=None, route="ask_json", api_key=None):
    await asyncio.sleep(0.03)
    out = _answer(route, system, user, True)
    if isinstance(out, str):
        return ai._extract_json(out)
    return out


def _chunks(text: str, n_min: int = 20, n_max: int = 40) -> list[str]:
    words = text.split(" ")
    n = max(1, min(len(words), random.randint(n_min, n_max)))
    per = max(1, math.ceil(len(words) / n))
    return [" ".join(words[i:i + per]) + (" " if i + per < len(words) else "")
            for i in range(0, len(words), per)]


async def _m_stream(system: str, user: str, *, on_think=None, on_text=None, on_retry=None, model=None,
                    thinking=None, effort=None, temperature=1.0, max_tokens=None, route="stream",
                    api_key=None) -> str:
    _CALLS[route] = _CALLS.get(route, 0) + 1
    think, text = _stream_text(route, user)
    full_t = ""
    if on_think:
        for d in _chunks(think, 4, 8):
            full_t += d
            await on_think(d, full_t)
            await asyncio.sleep(_STREAM_DELAY)
    full = ""
    for d in _chunks(text):
        full += d
        if on_text:
            await on_text(d, full)
        await asyncio.sleep(_STREAM_DELAY)
    return full


async def _m_chat(messages: list[dict], *, model=None, thinking=None, effort=None, temperature=1.0,
                  max_tokens=None, on_think=None, on_text=None, route="chat", api_key=None) -> str:
    user = ""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            user = str(m.get("content") or "")
            break
    return await _m_stream("", user, on_think=on_think, on_text=on_text, route=route)


async def _m_health() -> dict:
    return {"ok": True, "reply": "готов (мок)"}


# ═══════════════════════════════════════════════════════════════════════════
# фейковые новости
# ═══════════════════════════════════════════════════════════════════════════
_BASE_NEWS = [
    ("rbc", "ЦБ сохранил ключевую ставку и дал умеренно жёсткий сигнал",
     "Банк России оставил ключевую ставку без изменений. Регулятор отметил, что инфляционные ожидания остаются повышенными, а пространство для снижения появится не раньше конца года."),
    ("interfax", "Сбербанк отчитался о рекордной прибыли за 8 месяцев",
     "Чистая прибыль Сбера по РПБУ выросла на 7% год к году, рентабельность капитала выше 22%. Банк подтверждает планы по дивидендам."),
    ("kommersant", "ОПЕК+ увеличит добычу нефти в октябре",
     "Страны ОПЕК+ договорились о повышении квот. Brent отреагировала снижением на 2%, аналитики ждут давления на котировки в ближайшие недели."),
    ("tass", "Курс рубля стабилен: доллар около 90",
     "Рубль торгуется без выраженной динамики, экспортёры продают валюту в рамках налогового периода."),
    ("rbc", "Газпром: экспорт газа в Европу вырос на 12%",
     "Поставки по «Турецкому потоку» обновили максимум, цены на TTF растут на фоне низких запасов."),
    ("interfax", "Индекс Мосбиржи закрылся у верхней границы диапазона",
     "IMOEX прибавил 0,8%, лидеры роста — банки и металлурги. Объёмы выше среднего."),
    ("kommersant", "Лукойл рекомендовал промежуточные дивиденды",
     "Совет директоров рекомендовал выплатить дивиденды за первое полугодие, доходность около 5%."),
    ("tass", "Минфин увеличит покупки золота и юаней",
     "Объём покупок валюты и золота в рамках бюджетного правила вырастет, что поддержит рубль."),
    ("rbc", "Роснефть увеличила добычу на новых месторождениях",
     "Компания сообщила о росте добычи на «Восток Ойл», капзатраты остаются высокими."),
    ("interfax", "Инфляция за неделю замедлилась до 0,05%",
     "Недельная инфляция замедлилась, годовая — около 8%. Рынок ОФЗ отреагировал ростом."),
    ("kommersant", "Аналитики ждут снижения ставки ЦБ в декабре",
     "Консенсус-прогноз: первое снижение ключевой ставки в декабре, банки выиграют первыми."),
    ("tass", "Brent упала ниже 70 долларов",
     "Нефть Brent опустилась ниже 70 долларов за баррель впервые за три месяца на фоне слабого спроса в Китае."),
    ("rbc", "Сбер запустил новую программу лояльности",
     "Банк объявил о запуске обновлённой программы «Спасибо», ожидает роста комиссионных доходов."),
    ("interfax", "Золото обновило исторический максимум",
     "Котировки золота выросли до нового рекорда на ожиданиях снижения ставки ФРС."),
    ("kommersant", "Мосбиржа расширит вечернюю сессию",
     "Биржа планирует расширить вечернюю сессию для акций, ликвидность вечером вырастет."),
    ("tass", "Курс доллара: прогноз аналитиков на осень",
     "Большинство аналитиков ждут курс в диапазоне 88–95 рублей за доллар до конца года."),
    ("rbc", "Правительство обсуждает налог на сверхприбыль банков",
     "В правительстве обсуждают разовый налог на банки, Сбер может потерять до 3% прибыли."),
    ("interfax", "Газпром обсуждает дивиденды за 2026 год",
     "Менеджмент не исключает возврата к выплатам, решение — весной."),
    ("kommersant", "Санкции: ЕС обсуждает новый пакет против нефтяного сектора",
     "Обсуждаются ограничения на танкерный флот, эффект для Роснефти и Лукойла оценивается как умеренный."),
    ("tass", "ЦБ: инфляционные ожидания населения снизились",
     "Опрос показал снижение ожиданий до 12,5%, что расширяет пространство для смягчения политики."),
    # мусор
    ("lenta", "Рецепт идеального осеннего супа", "Пошаговый рецепт тыквенного супа с имбирём."),
    ("lenta", "Погода на выходные: дожди и похолодание", "Синоптики обещают дожди и до +8 в Москве."),
    ("lenta", "Футбол: «Спартак» обыграл «Зенит»", "Матч завершился со счётом 2:1."),
    ("lenta", "Сериал недели: что посмотреть", "Обзор новых сериалов сентября."),
    ("lenta", "Гороскоп на неделю", "Овнам стоит быть внимательнее к деньгам."),
    ("lenta", "Звезда шоу-бизнеса сыграла свадьбу", "Церемония прошла в Подмосковье."),
    ("lenta", "Кино: премьеры сентября", "Пять фильмов, которые стоит увидеть."),
    ("rbc", "Ритейлер X5 открыл тысячный магазин «Чижик»", "Сеть дискаунтеров растёт быстрее рынка."),
    ("interfax", "Аэрофлот увеличил перевозки на 15%", "Пассажиропоток вырос, загрузка кресел 90%."),
    ("kommersant", "Металлурги просят господдержку", "Северсталь и НЛМК жалуются на падение спроса."),
]
_FRESH = [
    ("rbc", "ЦБ внезапно повысил ключевую ставку на 100 б.п.",
     "Банк России повысил ключевую ставку на внеочередном заседании, сославшись на ускорение инфляции. Банки и рубль реагируют первыми."),
    ("interfax", "Сбербанк: наблюдательный совет утвердил дивиденды",
     "Наблюдательный совет Сбера рекомендовал дивиденды, доходность около 10%."),
    ("tass", "Brent отскочила на 3% на данных о запасах",
     "Запасы нефти в США упали сильнее ожиданий, Brent вернулась выше 70."),
    ("kommersant", "Индекс Мосбиржи обновил максимум месяца",
     "Рост ведут банки, объёмы выше среднего на 30%."),
]
_fetch_calls = 0


def _fmt_pub(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")


async def _m_fetch_news(*, force: bool = False, days: int | None = None) -> list:
    global _fetch_calls
    _fetch_calls += 1
    now = time.time()
    step = 3 * 86400 / len(_BASE_NEWS)
    out = []
    for i, (src, title, summary) in enumerate(_BASE_NEWS):
        ts = now - 1800 - i * step
        out.append(news.News(source=src, title=title, summary=summary,
                             link=f"https://mock.news/{i}", published=_fmt_pub(ts), ts=ts))
    # свежие: на каждый вызов пара новых (одна — про ЦБ), чтобы дозор/пересчёт
    # видели «новое», а не одно и то же окно
    k = _fetch_calls
    for j in range(2):                              # нечётный вызов — пара с ЦБ (серьёзно), чётный — обычная
        src, title, summary = _FRESH[(0 if k % 2 else 2) + j]
        ts = now - 60 - j * 30
        out.append(news.News(source=src, title=f"{title} ({k})", summary=summary,
                             link=f"https://mock.news/fresh/{k}/{j}", published=_fmt_pub(ts), ts=ts))
    await asyncio.sleep(0.01)
    return out


_TK_NEWS = [
    ("interfax", "{name}: отчётность за квартал лучше ожиданий",
     "Компания {name} ({t}) показала рост прибыли; аналитики повышают целевые цены."),
    ("rbc", "{name}: крупный пакет прошёл через биржу",
     "В стакане {t} прошли объёмы втрое выше средних — на рынке говорят о перекладке фонда."),
]


async def _m_fetch_for_ticker(ticker: str, name: str, *, days: int = 14, fallback_days: int = 90,
                              limit: int = 22, asset_class: str = "share") -> tuple[list, str]:
    """Фейк Google News по инструменту (news.fetch_for_ticker): две новости в окне,
    детерминированные по тикеру — в сеть не ходим."""
    t = (ticker or "").upper()
    now = time.time()
    out = []
    for i, (src, title, summary) in enumerate(_TK_NEWS):
        ts = now - 3600 * (2 + i * 5)
        out.append(news.News(source=src, title=title.format(name=name or t, t=t),
                             summary=summary.format(name=name or t, t=t),
                             link=f"https://mock.news/tk/{t.lower()}/{i}", published=_fmt_pub(ts), ts=ts))
    await asyncio.sleep(0.01)
    return out, f"окно {days} дн (мок)"


async def _m_fetch_weather(regions: list) -> list:
    """Фейк open-meteo (weather.fetch_weather): детерминированная погода по региону."""
    out = []
    for r in regions or []:
        try:
            nm = str((r or {}).get("name") or (r or {}).get("region") or "регион")
            base = 5 + _h(nm, 0, 20)
            out.append({"region": nm, "temp_now": base, "wind_now": 3 + _h(nm + "w", 0, 6),
                        "avg_7d": base - 1, "min_7d": base - 5, "max_7d": base + 4,
                        "heating_degree_days_7d": max(0, (18 - base + 1) * 7),
                        "signal": "нейтрально (мок)"})
        except Exception:                            # noqa: BLE001
            continue
    await asyncio.sleep(0.01)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# синтетический рынок
# ═══════════════════════════════════════════════════════════════════════════
_BASE_PX = {"SBER": 300.0, "SBERP": 298.0, "GAZP": 130.0, "LKOH": 6800.0, "ROSN": 520.0, "BR": 70.0,
            "SI": 90000.0, "MX": 300000.0, "GD": 2600.0, "NG": 2.8, "TTF": 35.0, "VTBR": 95.0}


def _base_px(ticker: str) -> float:
    t = (ticker or "").upper().replace("MOCK-", "")
    if t in _BASE_PX:
        return _BASE_PX[t]
    return float(_h(t, 50, 5000))


def px_smooth(ticker: str, t: float) -> float:
    """Медленная волна цены без шума (W3: сетка глубокого стакана в демо стоит на ней)."""
    b = _base_px(ticker)
    seed = _h(ticker, 0, 1000)
    return b * (1 + 0.015 * math.sin((t + seed) / 5400.0) + 0.008 * math.sin((t + seed) / 900.0))


def px_at(ticker: str, t: float) -> float:
    """Цена как детерминированная функция времени: волна + шум на 3-секундной сетке."""
    b = _base_px(ticker)
    noise = (_h(f"{ticker}:{int(t // 3)}", -100, 100) / 100.0) * 0.0015
    return round(px_smooth(ticker, t) * (1 + noise), 4 if b < 100 else 2)


_IV_S = {"1m": 60, "5m": 300, "10m": 600, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400,
         "1d": 86400, "1w": 7 * 86400, "1M": 30 * 86400}


def candles_at(ticker: str, interval: str, days: float) -> list[dict]:
    step = _IV_S.get(interval, 900)
    now = time.time()
    n = int(min(3000, max(2, days * 86400 / step)))
    start = int((now - n * step) // step * step)
    out = []
    for i in range(n):
        t0 = start + i * step
        o = px_at(ticker, t0)
        c = px_at(ticker, t0 + step - 1)
        mid = px_at(ticker, t0 + step / 2)
        h = max(o, c, mid) * (1 + 0.002 * _h(f"{ticker}h{t0}", 0, 3))
        lo = min(o, c, mid) * (1 - 0.002 * _h(f"{ticker}l{t0}", 0, 3))
        out.append({"t": datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "o": round(o, 4), "h": round(h, 4), "l": round(lo, 4), "c": round(c, 4),
                    "v": _h(f"{ticker}v{t0}", 1000, 90000), "complete": t0 + step <= now})
    return out


async def _m_moex_last_price(ticker: str, asset_class: str, _resolved: bool = False) -> dict | None:
    now = time.time()
    p = px_at(ticker, now)
    prev = px_at(ticker, now - 86400)
    return {"price": p, "stale": None, "change_pct": round((p / prev - 1) * 100, 2),
            "value_today": 1_000_000_000, "volume_today": 12_000_000,
            "bid": round(p * 0.9995, 4), "offer": round(p * 1.0005, 4),
            "ts": now, "quote_time": datetime.now(timezone(timedelta(hours=3))).strftime("%H:%M:%S"),
            "source": "moex"}


async def _m_moex_candles(ticker: str, asset_class: str, interval: str, days_back: int) -> list[dict]:
    await asyncio.sleep(0)
    return candles_at(ticker, interval, days_back)


async def _m_iss(path: str, params: dict) -> dict | None:
    return None


async def _m_tk_post(path: str, body: dict, timeout: float = 15.0) -> dict:
    raise RuntimeError("мок: сети нет")


# ── фейковый Tinkoff (PYTHIA_MOCK_TINKOFF=1) ────────────────────────────────
def _figi_ticker(figi: str) -> str:
    return (figi or "").replace("MOCK-", "").upper() or "SBER"


async def _tk_resolve(ticker: str, asset_class: str) -> dict | None:
    t = (ticker or "").upper().strip()
    if not t:
        return None
    return {"figi": f"MOCK-{t}", "uid": f"mock-uid-{t}", "ticker": t, "name": t, "lot": 1,
            "minPriceIncrement": 0.01 if _base_px(t) < 1000 else 1.0, "currency": "rub",
            "dlong": 0.5, "dshort": 0.6, "classCode": "TQBR", "expirationDate": None}


async def _tk_last_price(figi: str) -> dict | None:
    return {"price": px_at(_figi_ticker(figi), time.time()), "ts": time.time()}


async def _tk_close_price(figi: str) -> float | None:
    return px_at(_figi_ticker(figi), time.time() - 86400)


_SCAN_GRID_PCT = 0.005          # W3: глубокий стакан демо стоит на сетке 0.5 % цены (без шума)
_SCAN_VAC_ASK = range(8, 13)    # полоса вакуума над ценой (уровни 9–13, глубокая — тяга вверх)
_SCAN_VAC_BID = range(12, 17)   # полоса под ценой (мельче)


async def _tk_orderbook(figi: str, depth: int = 50) -> dict | None:
    t = _figi_ticker(figi)
    b = _base_px(t)
    p = px_at(t, time.time())
    tick = 0.01 if b < 1000 else 1.0
    deep = depth >= 50
    if deep:
        # W3: стакан 50 уровней (сканер, рентген) в демо стоит на сетке 0.5 % медленной цены и держит две полосы
        # вакуума на фиксированных уровнях — иначе шум ±0.15 % каждые 3 с размазывал бы полосы по бинам сканера
        # и «прокол» в демо не появлялся бы никогда; топ-10 (пилот) живёт на живой цене
        g = max(tick, round(b * _SCAN_GRID_PCT / tick) * tick)
        p = round(round(px_smooth(t, time.time()) / g) * g, 4 if b < 100 else 2)
    bids = [(round(p - tick * (i + 1), 4), 100 + _h(f"{t}b{i}", 0, 400)) for i in range(depth)]
    asks = [(round(p + tick * (i + 1), 4), 100 + _h(f"{t}a{i}", 0, 400)) for i in range(depth)]
    if deep:
        for i in _SCAN_VAC_ASK:
            asks[i] = (asks[i][0], 3)
        for i in _SCAN_VAC_BID:
            bids[i] = (bids[i][0], 40)
    bv, av = sum(q for _, q in bids), sum(q for _, q in asks)
    return {"best_bid": bids[0][0], "best_ask": asks[0][0], "spread": round(2 * tick, 6),
            "spread_bps": round(2 * tick / p * 1e4, 2), "bid_vol": bv, "ask_vol": av,
            "imbalance": round((bv - av) / (bv + av), 3),
            "top_bids": [{"p": a, "q": b} for a, b in bids[:10]],
            "top_asks": [{"p": a, "q": b} for a, b in asks[:10]],
            "levels_bid": [{"p": a, "q": b} for a, b in bids],
            "levels_ask": [{"p": a, "q": b} for a, b in asks],
            "wall_bid": {"p": bids[3][0], "q": 900}, "wall_ask": {"p": asks[4][0], "q": 800},
            "limit_up": round(p * 1.2, 2), "limit_down": round(p * 0.8, 2),
            "last_price": p, "close_price": px_at(t, time.time() - 86400)}


async def _tk_last_trades(figi: str, minutes: int = 60) -> dict | None:
    t = _figi_ticker(figi)
    p = px_at(t, time.time())
    return {"count": 40, "window_min": minutes, "buy_vol": 2200, "sell_vol": 1800, "delta": 400,
            "aggressor_ratio": 0.55, "buy_trades": 22, "sell_trades": 18, "avg_trade_size": 100.0,
            "largest_prints": [{"q": 500, "p": p, "side": "BUY"}, {"q": 300, "p": p, "side": "SELL"}]}


async def _tk_candles(figi: str, interval: str, days_back: int) -> list[dict]:
    return candles_at(_figi_ticker(figi), interval, days_back)


async def _tk_accounts() -> list[dict] | None:
    return [{"id": "mock-acc", "name": "Мок-счёт", "type": "ACCOUNT_TYPE_TINKOFF", "status": "OPEN"}]


async def _tk_portfolio(account_id: str) -> dict | None:
    return {"total_rub": 100000.0, "free_rub": 100000.0, "expected_yield_pct": 0.0, "positions": [],
            "note": "мок-портфель: 100000 ₽ свободных"}


_MOCK_FEE_PCT = 0.05     # комиссия демо-брокера, % от оборота за сторону
_MOCK_SLIP = 0.0002      # исполнение чуть хуже цены тика (как на бирже)
_MOCK_HAND_GAP = 400.0   # круг «руками владельца» ставится в зазор ≥ 400 с после закрытия сделки журнала


def _mock_op(oid: str, ts: float, kind: str, figi: str, uid: str, qty: int, price: float) -> dict:
    notional = price * qty
    return {"id": oid, "ts": ts, "kind": kind, "type": "OPERATION_TYPE_BUY" if kind == "buy" else "OPERATION_TYPE_SELL",
            "state": "OPERATION_STATE_EXECUTED", "figi": figi, "uid": uid, "qty": qty, "price": price,
            "payment": round(-notional if kind == "buy" else notional, 2),
            "fee": round(notional * _MOCK_FEE_PCT / 100.0, 2), "trades": [{"ts": ts, "qty": qty, "price": price}],
            "name": "мок-операция", "parent": None}


def _mock_ops_for_trades() -> list[dict]:
    """Фейковые операции демо-брокера по сделкам мок-базы (для ledger: VWAP, комиссии, нетто): вход/выход через
    секунду после записи пилота, цена на 0,02 % хуже тика, комиссия 0,05 % за сторону. Плюс один круг «руками
    владельца» (buy 5 → sell 5, +0,4 %) в первом зазоре ≥ 400 с после закрытия сделки журнала — сделка, которой в
    журнале нет: ledger добавит её «по операциям брокера»."""
    try:
        from . import store_v5
        trades = [t for t in store_v5.trades_full(None, limit=50) if t.get("closed_ts")]
    except Exception:                                # noqa: BLE001
        return []
    out: list[dict] = []
    for t in trades:
        if t.get("why") == "по операциям брокера":
            continue
        tk = t["ticker"]
        figi, uid = t.get("figi") or f"MOCK-{tk}", f"mock-uid-{tk}"
        lot = max(1, int(_f((t.get("data") or {}).get("lot"), 1) or 1))
        qty = max(1, int(t.get("lots") or 1)) * lot
        long = t.get("side") == "long"
        e, x = float(t.get("entry") or 0), float(t.get("exit_px") or 0)
        if e <= 0 or x <= 0:
            continue
        e_px = round(e * (1 + _MOCK_SLIP if long else 1 - _MOCK_SLIP), 4)
        x_px = round(x * (1 - _MOCK_SLIP if long else 1 + _MOCK_SLIP), 4)
        opened = float(t.get("opened_ts") or float(t["closed_ts"]) - 60.0) + 1.0
        closed = float(t["closed_ts"]) + 1.0
        out.append(_mock_op(f"mock-op-{t['id']}-in", opened, "buy" if long else "sell", figi, uid, qty, e_px))
        out.append(_mock_op(f"mock-op-{t['id']}-out", closed, "sell" if long else "buy", figi, uid, qty, x_px))
    now = time.time()
    for i, t in enumerate(trades):                    # круг руками владельца — в первом достаточном зазоре
        if t.get("why") == "по операциям брокера":
            continue
        start = float(t["closed_ts"]) + 130.0
        nxt = min((float(u.get("opened_ts") or u["closed_ts"]) for u in trades[i + 1:]), default=now)
        if min(nxt - 70.0, now) - start >= _MOCK_HAND_GAP - 130.0:
            tk = t["ticker"]
            px = round(px_at(tk, start), 2)
            out.append(_mock_op("mock-op-hand-in", start, "buy", f"MOCK-{tk}", f"mock-uid-{tk}", 5, px))
            out.append(_mock_op("mock-op-hand-out", start + 60.0, "sell", f"MOCK-{tk}", f"mock-uid-{tk}", 5,
                                round(px * 1.004, 2)))
            break
    return sorted(out, key=lambda o: (o["ts"], o["id"]))


def _f(x, d=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


async def _tk_operations(account_id: str, from_ts: float, to_ts: float, instrument_id: str | None = None) -> list[dict]:
    """GetOperationsByCursor демо: операции по сделкам мок-базы в окне (и по инструменту, если задан)."""
    return [o for o in _mock_ops_for_trades()
            if from_ts <= o["ts"] <= to_ts and (not instrument_id or instrument_id in (o["figi"], o["uid"]))]


async def _tk_none(*a, **k):
    return None


async def _tk_trading_status(instrument_id: str) -> dict | None:
    """Рыночные часы в демо: биржа всегда «открыта», чтобы пилот на моке жил в любое время суток."""
    return {"status": "SECURITY_TRADING_STATUS_NORMAL_TRADING", "limit_order": True,
            "market_order": True, "api_available": True}


def _tk_server_date():
    return (time.time(), time.time())                # сдвига часов в демо нет


async def _tk_noop(*a, **k):
    return None


# ═══════════════════════════════════════════════════════════════════════════
# установка
# ═══════════════════════════════════════════════════════════════════════════
_installed = False


def install() -> None:
    """Подменить ИИ и рынок фейками (идемпотентно)."""
    global _installed
    if _installed:
        return
    _installed = True
    # ключи: пул из одного мок-ключа — ТОЛЬКО в памяти (data/config_user.json не трогаем)
    keys = ["sk-mock"]

    def _set_keys(ks):
        keys[:] = [str(k) for k in (ks or []) if str(k).strip()] or ["sk-mock"]

    config.deepseek_keys = lambda: list(keys)
    config.set_deepseek_keys = _set_keys
    config.pick_pool_key = lambda exclude=None: next((k for k in keys if k != exclude), keys[0])
    # своя база: демо не смешивается с настоящими новостями/советами/сделками
    try:
        from . import store_v5
        store_v5.DB_PATH = config.DATA_DIR / "pythia_v5_mock.db"
        store_v5._schema()
    except Exception as e:                           # noqa: BLE001
        log.warning("мок: отдельная база не поднялась: %s", str(e)[:80])
    # ИИ
    ai.ask, ai.ask_json, ai.stream, ai.chat, ai.health = _m_ask, _m_ask_json, _m_stream, _m_chat, _m_health
    ai.reset_client = lambda key=None: None
    # новости: RSS и Google News по инструменту; погода open-meteo
    news.fetch_news = _m_fetch_news
    news.fetch_for_ticker = _m_fetch_for_ticker
    try:
        from . import weather
        weather.fetch_weather = _m_fetch_weather
    except Exception:                                # noqa: BLE001
        pass
    # MOEX
    moex.last_price, moex.candles, moex._iss = _m_moex_last_price, _m_moex_candles, _m_iss
    try:
        from . import instruments
        async def _uni(force: bool = False):
            return list(getattr(instruments, "_universe", []) or [])
        instruments.fetch_universe = _uni
    except Exception:                                # noqa: BLE001
        pass
    # связанные бумаги (v5.3): в моке считаются по синтетическим рядам, кэш — в памяти процесса
    try:
        from . import correlate
        correlate.reset_cache()
    except Exception:                                # noqa: BLE001
        pass
    # Tinkoff: без токена / фейковый
    tinkoff._post = _m_tk_post
    tinkoff.prewarm = _tk_noop
    if os.getenv("PYTHIA_MOCK_TINKOFF") == "1":
        os.environ["PYTHIA_DRY"] = "1"               # брокер только dry — в сеть ничего не уходит
        tinkoff.enabled = lambda: True
        tinkoff.resolve = _tk_resolve
        tinkoff.last_price = _tk_last_price
        tinkoff.close_price = _tk_close_price
        tinkoff.orderbook = _tk_orderbook
        tinkoff.last_trades = _tk_last_trades
        try:                                          # W3: сканер в демо тикает раз в секунду — агрегаты и прокол за полминуты
            from . import maya_scan
            maya_scan.SCAN_INTERVAL_S = 1.0
        except Exception:                             # noqa: BLE001
            pass
        tinkoff.candles = _tk_candles
        tinkoff.accounts = _tk_accounts
        tinkoff.portfolio = _tk_portfolio
        tinkoff.operations = _tk_operations          # фаза 4 W1: журнал по операциям — нетто и в демо
        tinkoff.futures_margin = _tk_none
        for fn in ("consensus", "next_dividend", "tech", "dividends", "fundamentals", "trading_schedules"):
            if hasattr(tinkoff, fn):
                setattr(tinkoff, fn, _tk_none)
        tinkoff.trading_status = _tk_trading_status  # рыночные часы: в демо биржа открыта
        tinkoff.server_date = _tk_server_date
        log.warning("МОК: ИИ, новости, MOEX и Tinkoff подменены фейками (PYTHIA_MOCK_TINKOFF=1, брокер dry)")
    else:
        tinkoff.enabled = lambda: False
        log.warning("МОК: ИИ, новости и MOEX подменены фейками; Tinkoff выключен (пилот не стартует)")


def calls() -> dict:
    return dict(_CALLS)


# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    os.environ["PYTHIA_MOCK_TINKOFF"] = "1"
    install()
    assert config.pick_pool_key() == "sk-mock" and config.deepseek_keys() == ["sk-mock"]
    from . import store_v5 as _st
    assert _st.DB_PATH.name == "pythia_v5_mock.db"

    async def main():
        # новости: 30 базовых + 2 свежих, id стабильны между вызовами
        a = await news.fetch_news(force=True, days=3)
        b = await news.fetch_news(force=True, days=3)
        assert len(a) == len(_BASE_NEWS) + 2 and len(b) == len(a)
        assert {x.link for x in a if "fresh" not in x.link} == {x.link for x in b if "fresh" not in x.link}
        assert any("ЦБ" in x.title for x in a if "fresh" in x.link)
        # Google News по инструменту и погода — фейки, в сеть не ходят
        tk, note = await news.fetch_for_ticker("SBER", "Сбербанк", days=3, asset_class="share")
        assert len(tk) == 2 and all("SBER" in x.summary for x in tk) and "мок" in note
        assert tk[0].link != tk[1].link and (await news.fetch_for_ticker("SBER", "Сбербанк"))[0][0].link == tk[0].link
        from . import weather as _w
        rows = await _w.fetch_weather([{"name": "Роттердам"}, {"name": "Хьюстон"}])
        assert len(rows) == 2 and "Роттердам" in _w.render_for_ai(rows) and rows[0]["temp_now"] == rows[0]["temp_now"]
        # triage: мусор выкинут, биржевое оставлено
        lines = "\n".join(f"[{'%06x' % i}] 18.09 · {x.source} · {x.title}" for i, x in enumerate(a))
        keep = (await ai.ask_json("s", lines, route="triage"))["keep"]
        assert 5 <= len(keep) < len(a) and "%06x" % 0 in keep and "%06x" % 20 not in keep
        # разметка §2.3
        c = await ai.ask_json("s", "Заголовок: ЦБ повысил ставку\n\nТекст: банки под давлением", route="characterize")
        for k in ("gist", "one_liner", "tone", "honesty", "hype", "importance", "novelty", "kind", "horizon",
                  "expected_effect", "effect_strength", "manipulation_risk", "actors", "assets", "sectors",
                  "tags", "why_market"):
            assert k in c, k
        assert isinstance(c["tone"], int) and c["kind"] in ("факт", "мнение", "слух", "прогноз", "реклама")
        assert "ЦБ" in c["actors"] and "SBER" in c["assets"]
        # группировка: каждый id ровно один раз
        g = await ai.ask_json("s", lines, route="group")
        ids_all = [i for s in g["streams"] for i in s["ids"]] + g["orphans"]
        assert len(ids_all) == len(set(ids_all)) == len(a) and 2 <= len(g["streams"]) <= 3
        # итог
        s = await ai.ask_json('… "human_alignment" …', "ВЕРДИКТ: …", route="summary")
        assert len(s["picks"]) == 3 and s["picks"][0]["ticker"] == "SBER" and s["human_alignment"]
        assert "human_alignment" not in await ai.ask_json("s", "ВЕРДИКТ", route="summary")
        # раскладка
        d = await ai.ask_json("s", "ВЫБОР СОВЕТА:\nВХОД SBER long 72%\nВХОД BR short 58%\n\nКАТАЛОГ:\n"
                                   "SBER — Сбербанк\nBR — Brent\n\nНОВОСТИ:\n" + lines, route="distribute")
        assert [c["ticker"] for c in d["cards"]] == ["SBER", "BR"] and all(c["news_ids"] for c in d["cards"])
        # impact
        i1 = await ai.ask_json("s", "ИТОГ СОВЕТА:\n…\n\nНОВЫЕ НОВОСТИ:\n[aaaaaa] ЦБ повысил ставку", route="impact")
        i2 = await ai.ask_json("s", "ИТОГ СОВЕТА:\n…\n\nНОВЫЕ НОВОСТИ:\n[aaaaaa] Лукойл дивиденды", route="impact")
        assert i1["severity"] == 80 and i1["recommend_rerun"] and i2["severity"] == 30
        # exec (v5.4.2): BUY/SELL по режиму, иногда WAIT с wait_for и уровнями; стороны уровней — только не у WAIT
        def check_exec(e_, px_, sell_):
            assert e_["do"] in ({"SELL", "WAIT"} if sell_ else {"BUY", "WAIT"}), e_
            assert "перевес есть" not in e_["why"] and "вход сейчас" not in e_["why"], e_["why"]
            if e_["do"] == "WAIT":
                assert e_["wait_for"] and e_["levels"] and e_["invalidation"] is None and e_["entry"] is None \
                    and e_["take"] is None, e_
            elif sell_:
                assert e_["take"] < px_ < e_["invalidation"] and e_["entry"] is None, e_
            else:
                assert e_["entry"] is None and abs(e_["take"] - px_ * 1.02) < 0.01 \
                    and abs(e_["invalidation"] - px_ * 0.99) < 0.01 and e_["wait_for"] == "", e_
        ex_do = []
        for _ in range(3):
            e = await ai.ask_json("… Цена 285.4.\n…", "ОБЪЕКТ: SBER", route="mission_exec")
            check_exec(e, 285.4, False)
            ex_do.append(e["do"])
        assert "BUY" in ex_do and "WAIT" in ex_do and ex_do.count("BUY") >= 2, ex_do
        for _ in range(3):
            check_exec(await ai.ask_json("Цена 100. Режим: только SELL", "x", route="mission_exec"), 100.0, True)
        pos_do = []
        for _ in range(3):                           # открытая позиция — WAIT нельзя (только без позиции); HOLD — можно
            e = await ai.ask_json("… Цена 100.\n…", "═══ ОТКРЫТАЯ ПОЗИЦИЯ ═══\nlong 8 лот", route="mission_exec")
            assert e["do"] in ("BUY", "HOLD"), e
            if e["do"] == "HOLD":
                assert e["entry"] is None and e["invalidation"] < 100 < e["take"], e
            pos_do.append(e["do"])
        assert "HOLD" in pos_do and "BUY" in pos_do, pos_do
        # перепроверка (v5.4.2): ротация, вне рынка ЖДЁМ / КУПИТЬ_СЕЙЧАС (invalidation ≈ цена·0.99) / НОВЫЙ_АНАЛИЗ
        rv_u = "ОБЪЕКТ: SBER. ДОПУСТИМЫЕ choice: КУПИТЬ_СЕЙЧАС | ЖДЁМ | ПРОДАТЬ_СЕЙЧАС | НОВЫЙ_АНАЛИЗ\n\n" \
               "═══ СИТУАЦИЯ ПИЛОТА ═══\nЦена сейчас: 285.4\nПозиции нет, засады нет — полностью вне рынка"
        rv = [await ai.ask_json("s", rv_u, route="mission_review") for _ in range(3)]
        rv_c = [x["choice"] for x in rv]
        assert set(rv_c) <= {"ЖДЁМ", "КУПИТЬ_СЕЙЧАС", "НОВЫЙ_АНАЛИЗ"} and len(set(rv_c)) >= 2, rv_c
        buy = [x for x in rv if x["choice"] == "КУПИТЬ_СЕЙЧАС"]
        assert buy and abs(buy[0]["invalidation"] - 285.4 * 0.99) < 0.01 and buy[0]["take"] > 285.4, buy
        assert all("перевес есть" not in x["why"] and "Держим позицию" not in x["note"] for x in rv), rv
        rv_s = [await ai.ask_json("s", rv_u.replace("КУПИТЬ_СЕЙЧАС | ", ""), route="mission_review") for _ in range(3)]
        sell_ = [x for x in rv_s if x["choice"] == "ПРОДАТЬ_СЕЙЧАС"]
        assert sell_ and "КУПИТЬ_СЕЙЧАС" not in [x["choice"] for x in rv_s] and sell_[0]["invalidation"] > 285.4, rv_s
        rv_p = [(await ai.ask_json("s", "Цена сейчас: 290\nПОЗИЦИЯ: long 8 лот @285.4, в рынке 5 мин",
                                   route="mission_review"))["choice"] for _ in range(2)]
        assert sorted(rv_p) == ["ЖДЁМ", "ЗАКРЫТЬ"], rv_p
        assert (await ai.ask_json('… {"choice":"из списка"} …', rv_u, route="z"))["choice"] in (
            "ЖДЁМ", "КУПИТЬ_СЕЙЧАС", "НОВЫЙ_АНАЛИЗ"), "по схеме — тот же обработчик"
        # v5.3 W2: мягкий тейк чередует ПОДЕРЖАТЬ / ЗАФИКСИРОВАТЬ (lock_price/tp_next от цены), триаж — по кругу
        t1 = await ai.ask_json("s", "Цена сейчас: 100", route="mission_take")
        t2 = await ai.ask_json("s", "Цена сейчас: 100", route="mission_take")
        assert t1["decision"] == "ПОДЕРЖАТЬ" and abs(t1["lock_price"] - 99.5) < 1e-6 and t1["tp_next"] > 100
        assert t2["decision"] == "ЗАФИКСИРОВАТЬ" and t2["lock_price"] is None
        tr = [(await ai.ask_json("s", "Цена сейчас: 100", route="event_triage"))["urgency"] for _ in range(3)]
        assert tr == ["ПЛАНОВО", "САМ", "СЕЙЧАС"], tr
        assert (await ai.ask_json('… {"urgency":"…"} …', "Цена сейчас: 100", route="x"))["urgency"] in ("ПЛАНОВО", "САМ", "СЕЙЧАС")
        # v5.4.2: у двери ротация ВОЙТИ / ЖДАТЬ (откат ≈ цена·0.997) / ВОЙТИ, прибыль ДЕРЖАТЬ / ВЫЙТИ / ДЕРЖАТЬ; по схеме тоже
        ens = [await ai.ask_json("s", "Цена сейчас: 285.4\n\n═══ ПРИКАЗ И ПЛАН ═══\nBUY long сейчас", route="mission_entry")
               for _ in range(3)]
        assert [x["decision"] for x in ens] == ["ВОЙТИ", "ЖДАТЬ", "ВОЙТИ"], ens
        assert ens[0]["entry"] is None and ens[0]["council"] is False and "285.4" in ens[0]["why"], ens[0]
        assert ens[1]["entry_kind"] == "откат" and abs(ens[1]["entry"] - 285.4 * 0.997) < 0.01, ens[1]
        assert all("перевес" not in x["why"] for x in ens), ens
        en_s = [await ai.ask_json("s", "Цена сейчас: 100\n\n═══ ПРИКАЗ И ПЛАН ═══\nSELL short сейчас", route="mission_entry")
                for _ in range(3)]
        assert en_s[1]["decision"] == "ЖДАТЬ" and en_s[1]["entry"] > 100, en_s[1]
        pfs = [await ai.ask_json("s", "Цена сейчас: 289", route="mission_profit") for _ in range(3)]
        assert [x["decision"] for x in pfs] == ["ДЕРЖАТЬ", "ВЫЙТИ", "ДЕРЖАТЬ"], pfs
        assert all(x["lock_price"] is None and x["take"] is None and x["reentry"] is None for x in pfs), pfs
        assert (await ai.ask_json('… {"decision":"ВОЙТИ|ЖДАТЬ|ОТМЕНИТЬ","wait_minutes":число|null,"council":true|false} …', "u", route="y"))["decision"] in ("ВОЙТИ", "ЖДАТЬ")
        assert (await ai.ask_json('… {"decision":"ДЕРЖАТЬ|…","lock_price":число|null,"reentry":число|null} …', "u", route="y"))["decision"] in ("ДЕРЖАТЬ", "ВЫЙТИ")
        assert (await ai.ask_json('… {"decision":"ЗАФИКСИРОВАТЬ|ПОДЕРЖАТЬ","lock_price":число|null,"tp_next":число|null} …', "Цена сейчас: 100", route="y"))["decision"] in ("ПОДЕРЖАТЬ", "ЗАФИКСИРОВАТЬ")
        assert calls()["mission_entry"] == 6 and calls()["mission_profit"] == 3
        ex_ = await ai.ask_json("… Цена 100.\n…", "ОБЪЕКТ: SBER", route="mission_exec")
        assert "главное войти" not in ex_["why"] and "оттягивать" not in ex_["plan"] and "перевес есть" not in ex_["why"]
        # ключи маршрутизации по настоящим промптам (audit mock-prompt-text-keys): неизвестный route → обработчик по схеме
        from . import prompts_mission as _pm
        _ctx = {"time_msk": "28.09.2026 12:00 МСК", "price": 285.4, "asset_class": "share"}
        _se, _ue = _pm.exec_order("SBER", "Сбербанк", "auto", 285.4, "вердикт", _ctx)
        assert _price_from(_se) == 285.4 and "только SELL" in _pm.exec_order("SBER", "Сбербанк", "short", 285.4, "в", _ctx)[0]
        assert _answer("??", _se, _ue, True)["do"] in ("BUY", "SELL", "WAIT")
        _kw = dict(situation="Цена сейчас: 285.4", history="", light="", plan="", news="", council_text="")
        _sr, _ur = _pm.review("SBER", "Сбербанк", "auto", situation="Цена сейчас: 285.4", light="", council_text="",
                              prev_exec="", news="", watch="", astro_line="", in_pos=False, time_msk="28.09 12:00 МСК")
        assert _answer("??", _sr, _ur, True)["choice"] in ("ЖДЁМ", "КУПИТЬ_СЕЙЧАС", "НОВЫЙ_АНАЛИЗ")
        _sn, _un = _pm.entry_check("SBER", "Сбербанк", "auto", **_kw)
        assert _answer("??", _sn, _un, True)["decision"] in ("ВОЙТИ", "ЖДАТЬ")
        _sp, _up = _pm.profit_think("SBER", "Сбербанк", "auto", profit="+1.2 %", **_kw)
        assert _answer("??", _sp, _up, True)["decision"] in ("ДЕРЖАТЬ", "ВЫЙТИ")
        _st_, _ut_ = _pm.take_guard("SBER", "Сбербанк", "auto", take="290", **_kw)
        assert _answer("??", _st_, _ut_, True)["decision"] in ("ПОДЕРЖАТЬ", "ЗАФИКСИРОВАТЬ")
        # толмач и память (v5.3): живой текст по данным, без «мок» в каждом слове
        ex_u = ("ОБЪЕКТ: SBER — Сбербанк. ВРЕМЯ: 23.09.2026 14:35 МСК. Цена: 285.4.\n\n═══ ЧТО ПРОИЗОШЛО ═══\n"
                "23.09 14:35 · Вход исполнен: long 8 лот @285.4\n23.09 14:35 · FLASH у троса: ЖДАТЬ: прокол\n\n"
                "═══ СИТУАЦИЯ ПИЛОТА ═══\nЦена сейчас: 285.4\nПОЗИЦИЯ: long 8 лот @285.4, в рынке 1 мин, плавающий P/L +0 ₽, "
                "триггер (мягкий стоп) @282.5, тейк 291\n\nОбъясни владельцу, что произошло и что дальше.")
        ex_t = await ai.ask("s", ex_u, route="explain")
        assert "Вход исполнен" in ex_t and "лонг 8 лот" in ex_t and "282.5" in ex_t and "291" in ex_t and "мок" not in ex_t.lower(), ex_t
        assert "FLASH у троса" in ex_t, ex_t
        mem_t = await ai.ask("s", "ОБЪЕКТ: SBER. ВРЕМЯ: 23.09.2026 14:35 МСК. Цена: 285.\n\n═══ НАКОПИЛОСЬ С ТЕХ ПОР ═══\n"
                                  "Перепроверки: 23.09 12:00 ЖДЁМ — план жив; 23.09 12:30 ЗАКРЫТЬ — слом\nСделки:\n"
                                  "23.09 12:31 long 8 лот 285 → 287, P/L +160 ₽ — решение\n\nСведи в один абзац ≤ 1500 символов.",
                             route="memory")
        assert "перепроверок 2" in mem_t and "сделок закрыто 1" in mem_t and len(mem_t) <= 1500 and "мок" not in mem_t.lower(), mem_t
        # стрим: 20–40 дельт текста + think
        th, tx = [], []
        async def ot(d, f): tx.append(d)
        async def oth(d, f): th.append(d)
        full = await ai.stream("s", "SBER цена 300", on_think=oth, on_text=ot, route="analysis")
        assert 15 <= len(tx) <= 40 and th and "".join(tx) == full and "SBER" in full
        assert (await ai.health())["ok"]
        # рынок
        lp = await moex.last_price("SBER", "share")
        cs = await moex.candles("SBER", "share", "15m", 2)
        assert 250 < lp["price"] < 350 and len(cs) > 100 and all(c["l"] <= min(c["o"], c["c"]) for c in cs)
        assert abs(cs[-1]["c"] - lp["price"]) / lp["price"] < 0.03   # волна мока за 15-мин свечу уходит до ~2 %
        # tinkoff-мок
        assert tinkoff.enabled() and os.environ.get("PYTHIA_DRY") == "1"
        r = await tinkoff.resolve("SBER", "share")
        ob = await tinkoff.orderbook(r["figi"], 10)
        assert r["figi"] == "MOCK-SBER" and ob["best_bid"] < ob["best_ask"] and (await tinkoff.futures_margin("x")) is None
        assert (await tinkoff.portfolio("mock-acc"))["free_rub"] == 100000.0
        # фаза 4 W1: операции демо по сделкам (временная база, не data/) → ledger: VWAP, комиссии, нетто, круг «руками»
        import tempfile
        from pathlib import Path
        from . import ledger
        _st.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
        _st._schema()
        assert await tinkoff.operations("mock-acc", 0, time.time()) == []
        now_ = time.time()
        tid = _st.trade_add({"ticker": "SBER", "side": "long", "lots": 3, "entry": 300.0, "exit_px": 302.0, "pnl": 6.0,
                             "opened_ts": now_ - 3000, "closed_ts": now_ - 2400, "why": "тейк", "mode": "dry",
                             "figi": "MOCK-SBER", "lot": 1, "point_value": 1.0})
        ops_ = await tinkoff.operations("mock-acc", 0, now_)
        assert [o["id"] for o in ops_] == [f"mock-op-{tid}-in", f"mock-op-{tid}-out", "mock-op-hand-in", "mock-op-hand-out"], ops_
        assert ops_[0]["kind"] == "buy" and ops_[0]["qty"] == 3 and ops_[0]["price"] == 300.06 and ops_[1]["price"] == 301.9396
        assert ops_[0]["fee"] == round(300.06 * 3 * 0.0005, 2) and ops_[0]["payment"] < 0 < ops_[1]["payment"]
        assert (await tinkoff.operations("mock-acc", 0, now_, "mock-uid-SBER")) and not (await tinkoff.operations("mock-acc", 0, now_, "MOCK-GAZP"))
        assert ledger.mode() == "mock"
        r_ = await ledger.sync_and_reconcile("SBER")
        assert r_["ok"] and r_["sync"]["added"] == 4 and r_["reconcile"]["matched"] == 1 and r_["reconcile"]["added"] == 1, r_
        rows_ = _st.trades_full("SBER")
        a_ = rows_[0]
        assert a_["source"] == "mock" and not a_["est"] and a_["entry_real"] == 300.06 and a_["exit_real"] == 301.9396
        assert abs(a_["pnl_gross"] - round((301.9396 - 300.06) * 3, 2)) < 1e-6 and a_["fee"] > 0 and a_["pnl_net"] < a_["pnl_gross"], a_
        h_ = rows_[1]
        assert h_["why"] == "по операциям брокера" and h_["lots"] == 5 and h_["source"] == "mock" and h_["mode"] == "dry"
        assert h_["pnl_net"] < h_["pnl_gross"] and sorted(h_["ops"]) == ["mock-op-hand-in", "mock-op-hand-out"]
        r2_ = await ledger.sync_and_reconcile("SBER")
        assert r2_["sync"]["added"] == 0 and r2_["reconcile"]["added"] == 0 and len(_st.trades_full("SBER")) == 2
        sm_ = ledger.trades_summary("SBER")
        assert sm_["count"] == 2 and sm_["confirmed"] == 2 and not sm_["est"] and sm_["mode"] == "mock" and sm_["net"] < sm_["gross"]
        ds_ = await ledger.day_summary()
        assert ds_["ok"] and ds_["account"]["total"] == 100000.0 and "мок" in ds_["account"]["note"]
        ts_ = await tinkoff.trading_status("MOCK-SBER")
        assert ts_["status"] == "SECURITY_TRADING_STATUS_NORMAL_TRADING" and ts_["api_available"] and tinkoff.server_date()
        from . import market_clock
        market_clock.reset_cache()
        mst = await market_clock.status("SBER", "share", "MOCK-SBER")
        assert mst["open"] and mst["source"] == "tinkoff", mst
        try:
            await tinkoff._post("x", {})
            raise AssertionError("сеть не должна работать")
        except RuntimeError:
            pass

    asyncio.run(main())
    print("mock_ai self-test OK:", calls())
