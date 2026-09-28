# -*- coding: utf-8 -*-
"""ПИФИЯ v5.3 — СВЯЗАННЫЕ БУМАГИ (корреляции по дневным доходностям).

Воля владельца: «если у выбранной акции есть корреляция с какой-либо бумагой — даём
коррелирующие данные для полноты; IMOEX убери совсем — данные нетрезвые, отстают».
Поэтому индексов MOEX ISS здесь нет: рынок в целом — фьючерс на индекс (MX/RI) у Tinkoff.

Вселенная кандидатов для инструмента: соседи по сектору (таблица SECTORS — ликвидные бумаги
Мосбиржи: банки, нефть и газ, металлы, ритейл, телеком, IT, энергетика, транспорт,
застройщики, химия; для фьючерсов — их группа) + макро-набор фьючерсов (Si/CR — рубль,
BR — нефть, GD — золото, NG — газ, MX/RI — индексы; ближайшая серия через tinkoff.resolve).
Дневные свечи: Tinkoff, без токена — MOEX ISS для акций и сырьевых фьючерсов; индексные
фьючерсы без токена — честно «нет данных». Пирсон по лог-доходностям, сдвиг ±1 день
(lead), ход за день и за 5 дней, цена по последней дневной свече. Кэш — store_v5.kv
на PARTNERS_TTL (6 ч). Ничего не выдумывается: мало общих дней → бумага не в списке.

    SECTORS / MACRO / sector_of(code)            — таблицы
    universe(ticker, asset_class)                 — кандидаты [(code, name, asset_class, kind)]
    pearson(a, b) / returns(rows) / align(...)    — математика (чистые функции)
    compute(base_rows, part_rows)                 — {rho, lead, n, rho_lag}
    async partners(ticker, asset_class, days=60, force=False) → список до MAX_PARTNERS
        {code, name, asset_class, kind, rho, lead, n, move_1d, move_5d, price, source}
    text(items, ticker="")                        — блок «СВЯЗАННЫЕ БУМАГИ» для ИИ
    explain(ticker)                               — честная заметка последнего расчёта
    scout_requests(items)                         — quote + history 5d для разведки

Self-тест (без сети, синтетические ряды): python3 -m backend.correlate
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone

from . import config, instruments, moex, store_v5, tinkoff

log = logging.getLogger("pythia.correlate")

PARTNERS_TTL = 6 * 3600        # кэш результата
MAX_PARTNERS = 6               # бумаг в блоке
MIN_POINTS = 20                # общих дневных доходностей для честного ρ
MIN_RHO = 0.25                 # ниже — связь не показываем (кроме индексного фьючерса)
LEAD_GAIN = 0.15               # лаг заметно выше, если |ρ_lag| ≥ |ρ| + LEAD_GAIN и ≥ LEAD_MIN
LEAD_MIN = 0.30
FETCH_TIMEOUT = 45.0           # весь сбор свечей
ONE_TIMEOUT = 15.0             # одна бумага
PARALLEL = 4                   # одновременных запросов к бирже
KV_PREFIX = "partners:"

# ── таблицы ──────────────────────────────────────────────────────────────────
# сектор → ликвидные бумаги Мосбиржи (первый сектор, где найден код, — его сектор)
SECTORS: dict[str, list[str]] = {
    "банки и финансы": ["SBER", "SBERP", "VTBR", "T", "BSPB", "SVCB", "MBNK", "CBOM", "MOEX", "SFIN", "RENI"],
    "нефть и газ": ["GAZP", "LKOH", "ROSN", "NVTK", "TATN", "TATNP", "SNGS", "SNGSP", "BANE", "BANEP", "RNFT"],
    "металлы и добыча": ["GMKN", "PLZL", "CHMF", "NLMK", "MAGN", "RUAL", "ALRS", "MTLR", "MTLRP", "RASP",
                         "SELG", "UGLD", "ENPG", "VSMO", "TRMK", "POLY"],
    "ритейл": ["MGNT", "X5", "LENT", "FIXP", "MVID", "BELU", "HNFG", "OZON"],
    "телеком": ["MTSS", "RTKM", "RTKMP"],
    "IT": ["YDEX", "VKCO", "POSI", "ASTR", "HEAD", "DIAS", "SOFL", "IVAT", "DATA", "CNRU", "WUSH"],
    "энергетика": ["IRAO", "HYDR", "FEES", "UPRO", "MSNG", "LSNGP", "OGKB", "TGKA", "MRKP", "MRKC", "ELFV", "DVEC"],
    "транспорт": ["AFLT", "FLOT", "NMTP", "FESH", "TRNFP", "GLTR", "DELI"],
    "застройщики": ["PIKK", "SMLT", "LSRG", "ETLN"],
    "химия и удобрения": ["PHOR", "AKRN", "KZOS", "NKNC", "NKNCP"],
    "холдинги": ["AFKS", "SFIN"],
    # фьючерсы FORTS — их группы (код базовый, серию резолвит Tinkoff)
    "энергия (фьючерсы)": ["BR", "NG", "TTF"],
    "валюта (фьючерсы)": ["SI", "CR", "EU"],
    "металлы (фьючерсы)": ["GD", "SV"],
    "индексы (фьючерсы)": ["MX", "MM", "RI"],
}
_FUT_SECTORS = {k for k in SECTORS if "фьючерс" in k}

# макро-набор: фьючерсы Tinkoff ближайшей серии (код каталога → имя)
MACRO: list[tuple[str, str]] = [
    ("MX", "фьючерс на индекс МосБиржи (MIX)"),
    ("RI", "фьючерс на индекс РТС (RTS)"),
    ("SI", "рубль: фьючерс USD/RUB (Si)"),
    ("CR", "юань: фьючерс CNY/RUB (CR)"),
    ("BR", "нефть Brent (фьючерс BR)"),
    ("GD", "золото (фьючерс GD)"),
    ("NG", "газ Henry Hub (фьючерс NG)"),
]
INDEX_FUTURES = {"MX", "MM", "RI"}
_MACRO_NAME = dict(MACRO)

_mem: dict[str, dict] = {}     # кэш в памяти (поверх kv)
_last_note: dict[str, str] = {}


def _f(x, d=None):
    try:
        v = float(x)
        return v if v == v and v not in (math.inf, -math.inf) else d
    except (TypeError, ValueError):
        return d


def _name(code: str, ac: str) -> str:
    it = instruments.get(code) or {}
    return it.get("name") or _MACRO_NAME.get(code) or code


def sector_of(code: str) -> tuple[str, list[str]] | None:
    """(сектор, соседи без самого кода) или None — код вне таблицы."""
    c = (code or "").upper().strip()
    for name, members in SECTORS.items():
        if c in members:
            return name, [m for m in members if m != c]
    return None


def universe(ticker: str, asset_class: str) -> list[tuple[str, str, str, str]]:
    """Кандидаты: соседи по сектору + макро-набор; (code, name, asset_class, kind)."""
    t = (ticker or "").upper().strip()
    out: list[tuple[str, str, str, str]] = []
    seen = {t}
    sec = sector_of(t)
    if sec:
        name, members = sec
        ac = "futures" if name in _FUT_SECTORS else "share"
        for m in members:
            if m not in seen:
                seen.add(m)
                out.append((m, _name(m, ac), ac, f"сектор: {name}"))
    for code, name in MACRO:
        if code not in seen:
            seen.add(code)
            out.append((code, name, "futures", "макро"))
    return out


# ── математика (чистые функции) ────────────────────────────────────────────────
def _day(t) -> str | None:
    """Дата торгового дня по свече: UTC + 3 ч (Мосбиржа), чтобы 21:00Z = следующий день."""
    try:
        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt.astimezone(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
    except Exception:                                    # noqa: BLE001
        return None


def closes_by_day(rows: list[dict]) -> dict[str, float]:
    """{дата: закрытие} по дневным свечам; нули и мусор выброшены."""
    out: dict[str, float] = {}
    for r in rows or []:
        c = _f((r or {}).get("c"))
        d = _day((r or {}).get("t"))
        if c and c > 0 and d:
            out[d] = c
    return out


def returns(days: dict[str, float]) -> dict[str, float]:
    """Лог-доходности по соседним торговым дням."""
    ks = sorted(days)
    return {ks[i]: math.log(days[ks[i]] / days[ks[i - 1]]) for i in range(1, len(ks))
            if days[ks[i - 1]] > 0 and days[ks[i]] > 0}


def pearson(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 3:
        return None
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if sa == 0 or sb == 0:
        return None
    r = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)
    return max(-1.0, min(1.0, r))


def align(base: dict[str, float], part: dict[str, float]) -> tuple[list[str], list[float], list[float]]:
    """Общие дни (по датам) → (даты, доходности базы, доходности партнёра)."""
    ks = sorted(set(base) & set(part))
    return ks, [base[k] for k in ks], [part[k] for k in ks]


def compute(base_rows: list[dict], part_rows: list[dict]) -> dict | None:
    """ρ Пирсона по общим дням и сдвиг ±1 день: lead +1 — партнёр опережает на день
    (его вчера ↔ наше сегодня), −1 — наша бумага опережает; 0 — синхронно."""
    rb, rp = returns(closes_by_day(base_rows)), returns(closes_by_day(part_rows))
    days, a, b = align(rb, rp)
    if len(days) < MIN_POINTS:
        return None
    rho = pearson(a, b)
    if rho is None:
        return None
    lead, rho_lag = 0, None
    r_part_leads = pearson(a[1:], b[:-1])          # партнёр вчера → мы сегодня
    r_base_leads = pearson(a[:-1], b[1:])          # мы вчера → партнёр сегодня
    best = max(((abs(x or 0), x, s) for x, s in ((r_part_leads, 1), (r_base_leads, -1))), key=lambda z: z[0])
    if best[1] is not None and best[0] >= LEAD_MIN and best[0] >= abs(rho) + LEAD_GAIN:
        lead, rho_lag = best[2], round(best[1], 3)
    return {"rho": round(rho, 3), "lead": lead, "n": len(days), "rho_lag": rho_lag}


def strength(x: dict) -> float:
    """Сила связи: |ρ| или |ρ с лагом|, если лаг заметно сильнее (по нему и отбираем)."""
    return max(abs(_f(x.get("rho"), 0.0)), abs(_f(x.get("rho_lag"), 0.0)) if x.get("lead") else 0.0)


def moves(rows: list[dict]) -> dict:
    """Ход за день / за 5 дней и цена по последней дневной свече."""
    days = closes_by_day(rows)
    ks = sorted(days)
    if not ks:
        return {"move_1d": None, "move_5d": None, "price": None}
    c = [days[k] for k in ks]
    m1 = round((c[-1] / c[-2] - 1) * 100, 2) if len(c) >= 2 and c[-2] else None
    m5 = round((c[-1] / c[-6] - 1) * 100, 2) if len(c) >= 6 and c[-6] else None
    return {"move_1d": m1, "move_5d": m5, "price": c[-1]}


# ── свечи ─────────────────────────────────────────────────────────────────────
async def _daily(code: str, ac: str, days: int) -> tuple[list[dict], str]:
    """Дневные свечи: Tinkoff (ближайшая серия для фьючерсов) → MOEX ISS для акций и
    сырьевых/валютных фьючерсов; индексные фьючерсы без токена — пусто (честно)."""
    rows: list[dict] = []
    if tinkoff.enabled():
        try:
            inst = await tinkoff.resolve(code, ac)
            fid = (inst or {}).get("figi") or (inst or {}).get("uid")
            if fid:
                rows = await tinkoff.candles(fid, "1d", days)
        except Exception as e:                           # noqa: BLE001
            log.info("свечи %s (tinkoff): %s", code, str(e)[:80])
            rows = []
        if rows:
            return rows, "tinkoff"
    if ac == "index" or (ac == "futures" and code in INDEX_FUTURES):
        return [], ""                                    # индексы: только Tinkoff, ISS не берём
    try:
        rows = await moex.candles(code, ac, "1d", days)
    except Exception as e:                               # noqa: BLE001
        log.info("свечи %s (moex): %s", code, str(e)[:80])
        rows = []
    return (rows or []), ("moex" if rows else "")


async def _gather(cands: list[tuple[str, str, str, str]], days: int) -> dict[str, tuple[list[dict], str]]:
    sem = asyncio.Semaphore(PARALLEL)

    async def one(code, ac):
        async with sem:
            try:
                return await asyncio.wait_for(_daily(code, ac, days), ONE_TIMEOUT)
            except Exception as e:                       # noqa: BLE001
                log.info("свечи %s: %s", code, str(e)[:60])
                return [], ""

    res = await asyncio.gather(*(one(c, ac) for c, _n, ac, _k in cands), return_exceptions=True)
    return {c[0]: (r if isinstance(r, tuple) else ([], "")) for c, r in zip(cands, res)}


def _kv_key(ticker: str) -> str:
    return KV_PREFIX + (ticker or "").upper().strip()


def cached(ticker: str) -> dict | None:
    """Кэш без сети: {"ts","items","note"} или None (нет/протух)."""
    key = _kv_key(ticker)
    rec = _mem.get(key)
    if rec is None:
        try:
            rec = store_v5.kv_get(key)
        except Exception:                                # noqa: BLE001
            rec = None
        if isinstance(rec, dict):
            _mem[key] = rec
    if isinstance(rec, dict) and time.time() - _f(rec.get("ts"), 0.0) < PARTNERS_TTL:
        return rec
    return None


def _save(ticker: str, rec: dict) -> None:
    key = _kv_key(ticker)
    _mem[key] = rec
    try:
        store_v5.kv_set(key, rec)
    except Exception as e:                               # noqa: BLE001
        log.info("кэш связанных бумаг %s: %s", ticker, str(e)[:60])


def reset_cache(ticker: str | None = None) -> None:
    if ticker:
        _mem.pop(_kv_key(ticker), None)
    else:
        _mem.clear()


def explain(ticker: str) -> str:
    return _last_note.get((ticker or "").upper().strip(), "")


async def partners(ticker: str, asset_class: str, days: int = 60, *, force: bool = False) -> list[dict]:
    """До MAX_PARTNERS связанных бумаг по |ρ| (индексный фьючерс — всегда, если есть данные).
    Пусто — нет данных или связей; причина в explain(ticker). Кэш PARTNERS_TTL."""
    t = (ticker or "").upper().strip()
    if not t or not getattr(config, "PYTHIA_PARTNERS", True):
        return []
    if not force:
        rec = cached(t)
        if rec is not None:
            _last_note[t] = rec.get("note") or ""
            return list(rec.get("items") or [])
    ac = asset_class or instruments.guess_asset_class(t)
    cands = universe(t, ac)
    items: list[dict] = []
    note = ""
    try:
        base_rows, base_src = await asyncio.wait_for(_daily(t, ac, days), ONE_TIMEOUT)
        if len(closes_by_day(base_rows)) < MIN_POINTS + 1:
            note = (f"дневных свечей по {t} мало ({len(closes_by_day(base_rows))}) — связи не считаю"
                    if base_rows else f"дневных свечей по {t} нет" + ("" if tinkoff.enabled() else " (без токена Tinkoff)"))
        else:
            data = await asyncio.wait_for(_gather(cands, days), FETCH_TIMEOUT)
            skipped: list[str] = []
            for code, name, cac, kind in cands:
                rows, src = data.get(code, ([], ""))
                if not rows:
                    skipped.append(code)
                    continue
                c = compute(base_rows, rows)
                if not c:
                    skipped.append(code)
                    continue
                items.append({"code": code, "name": name, "asset_class": cac, "kind": kind, "source": src,
                              **c, **moves(rows)})
            items.sort(key=strength, reverse=True)
            strong = [x for x in items if strength(x) >= MIN_RHO]
            idx = [x for x in items if x["code"] in INDEX_FUTURES and strength(x) < MIN_RHO][:1]
            items = (strong + idx)[:MAX_PARTNERS]
            idx_missing = [c for c in skipped if c in INDEX_FUTURES]
            note = (f"кандидатов {len(cands)}, с данными {len(cands) - len(skipped)}, показано {len(items)}"
                    + (f"; без данных: {', '.join(skipped[:8])}" if skipped else "")
                    + (f"; индексных фьючерсов ({', '.join(idx_missing)}) нет — они только через Tinkoff"
                       + ("" if tinkoff.enabled() else ", токена нет") if idx_missing else "")
                    + (f"; свечи {t}: {base_src}" if base_src else ""))
            if not items:
                note = "заметных связей нет (|ρ| < %.2f) · " % MIN_RHO + note
    except asyncio.TimeoutError:
        note = "биржа не ответила вовремя — связанные бумаги не посчитаны"
        items = []
    except Exception as e:                               # noqa: BLE001
        note = f"сбой расчёта: {str(e)[:80]}"
        items = []
    _last_note[t] = note
    if items or not note.startswith("сбой") and "не ответила" not in note:
        _save(t, {"ts": time.time(), "items": items, "note": note})
    log.info("связанные бумаги %s: %s", t, note)
    return items


# ── текст для ИИ ───────────────────────────────────────────────────────────────
def _pct(v) -> str:
    v = _f(v)
    return f"{v:+.2f}%" if v is not None else "н/д"


def _px(v) -> str:
    v = _f(v)
    if v is None:
        return "н/д"
    return f"{v:,.1f}".replace(",", " ") if abs(v) >= 1000 else (f"{v:.2f}" if abs(v) >= 100 else f"{v:.4g}")


def text(items: list[dict], ticker: str = "") -> str:
    """Блок «СВЯЗАННЫЕ БУМАГИ»: факты (ρ, лаг, ход, цена) и один абзац «обычно читают так»
    без приказов; вес — выбор ИИ. Пусто → ''."""
    items = [x for x in (items or []) if isinstance(x, dict) and _f(x.get("rho")) is not None]
    if not items:
        return ""
    t = (ticker or "").upper()
    lines = [f"Связанные бумаги{(' для ' + t) if t else ''} (ρ Пирсона по дневным лог-доходностям, "
             f"общих дней n; ход за день и за 5 дней, цена по последней дневной свече):"]
    for x in items:
        lead = x.get("lead") or 0
        lag = ""
        if lead == 1:
            lag = f", опережает на день (ρ с лагом {_f(x.get('rho_lag'), 0):+.2f})"
        elif lead == -1:
            lag = f", отстаёт на день (ρ с лагом {_f(x.get('rho_lag'), 0):+.2f})"
        lines.append(f"- {x.get('code')} ({x.get('name')}; {x.get('kind')}): ρ={_f(x['rho']):+.2f} n={x.get('n')}{lag}; "
                     f"день {_pct(x.get('move_1d'))}, 5 дн. {_pct(x.get('move_5d'))}; цена {_px(x.get('price'))}"
                     + (f" · {x.get('source')}" if x.get("source") else ""))
    lines.append("Обычно читают так: при ρ выше +0.5 бумаги ходят вместе — расхождение сегодняшнего хода "
                 "(партнёр уже пошёл, наша нет) читают как догоняющий потенциал или как слабость, если "
                 "расхождение держится; при ρ ниже −0.5 партнёр — зеркало (рубль против экспортёров); "
                 "«опережает на день» — вчерашний ход партнёра часто отзывается сегодня. Это статистика "
                 "60 дней, не закон: связь рвётся на своих новостях.")
    return "\n".join(lines)


def scout_requests(items: list[dict]) -> list[dict]:
    """Запросы разведки по партнёрам: котировка сейчас + история 5 дней (свежие числа при
    каждом использовании — scout.refetch)."""
    out: list[dict] = []
    for x in items or []:
        code, ac = str(x.get("code") or "").upper(), x.get("asset_class") or "share"
        if not code:
            continue
        why = f"связанная бумага, ρ={_f(x.get('rho'), 0):+.2f}"
        out.append({"kind": "quote", "code": code, "asset_class": ac, "name": x.get("name") or code,
                    "days": 5, "interval": "1d", "why": why})
        out.append({"kind": "history", "code": code, "asset_class": ac, "name": x.get("name") or code,
                    "days": 5, "interval": "1d", "why": why})
    return out


# ── self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import random

    rnd = random.Random(7)                               # только для синтетики теста, не для расчётов

    def series(n: int, start: float, rets: list[float], t0: str = "2026-06-01") -> list[dict]:
        d0 = datetime.fromisoformat(t0).replace(tzinfo=timezone.utc)
        out, px = [], start
        for i in range(n):
            if i:
                px *= math.exp(rets[i - 1])
            out.append({"t": (d0 + timedelta(days=i)).strftime("%Y-%m-%dT21:00:00Z"), "c": round(px, 4)})
        return out

    N = 60
    r_base = [rnd.gauss(0, 0.015) for _ in range(N - 1)]
    base = series(N, 100.0, r_base)
    same = series(N, 50.0, [x * 2 for x in r_base])                   # ρ ≈ +1
    anti = series(N, 80.0, [-x for x in r_base])                      # ρ ≈ −1
    lagged = series(N, 30.0, [0.0] + r_base[:-1])                     # партнёр повторяет нас на день позже → мы опережаем (−1)
    leader = series(N, 30.0, r_base[1:] + [0.0])                      # партнёр ходит на день раньше → опережает (+1)
    noise = series(N, 10.0, [rnd.gauss(0, 0.02) for _ in range(N - 1)])

    # чистая математика
    assert abs(pearson([1, 2, 3, 4], [2, 4, 6, 8]) - 1) < 1e-9 and pearson([1, 1, 1], [1, 2, 3]) is None
    assert pearson([1, 2], [1, 2]) is None
    c = compute(base, same)
    assert c and c["rho"] > 0.99 and c["lead"] == 0 and c["n"] == N - 1, c
    c = compute(base, anti)
    assert c and c["rho"] < -0.99 and c["lead"] == 0, c
    c = compute(base, lagged)
    assert c and c["lead"] == -1 and c["rho_lag"] > 0.95 and abs(c["rho"]) < 0.4, c
    c = compute(base, leader)
    assert c and c["lead"] == 1 and c["rho_lag"] > 0.95, c
    assert compute(base[:10], same[:10]) is None, "мало общих дней → None"
    assert _day("2026-09-01T21:00:00Z") == "2026-09-02" and _day("2026-09-02T07:00:00Z") == "2026-09-02"
    mv = moves(base)
    assert mv["price"] == base[-1]["c"] and abs(mv["move_1d"] - (base[-1]["c"] / base[-2]["c"] - 1) * 100) < 0.02
    assert abs(mv["move_5d"] - (base[-1]["c"] / base[-6]["c"] - 1) * 100) < 0.02
    assert moves([]) == {"move_1d": None, "move_5d": None, "price": None}

    # вселенная
    u = universe("SBER", "share")
    codes = [x[0] for x in u]
    assert "VTBR" in codes and "T" in codes and "SBER" not in codes and "MX" in codes and "BR" in codes, codes
    assert u[0][3] == "сектор: банки и финансы" and [x for x in u if x[0] == "MX"][0][3] == "макро"
    assert sector_of("LKOH")[0] == "нефть и газ" and sector_of("ZZZZ") is None
    ub = universe("BR", "futures")
    assert [x[0] for x in ub][:2] == ["NG", "TTF"] and "BR" not in [x[0] for x in ub] and all(x[2] == "futures" for x in ub)
    assert [x[0] for x in universe("ZZZZ", "share")] == [m for m, _ in MACRO], "вне таблицы — только макро"

    # partners() с фейковой биржей: кэш в памяти, kv подменён
    kv: dict = {}
    store_v5.kv_get = lambda k, d=None: kv.get(k, d)     # noqa: E731
    store_v5.kv_set = lambda k, v: kv.__setitem__(k, v)  # noqa: E731
    ROWS = {"SBER": base, "VTBR": same, "SI": anti, "T": lagged, "MX": noise, "BR": leader}
    calls: list = []

    class FakeTinkoff:
        on = True

        @classmethod
        def enabled(cls):
            return cls.on

        @staticmethod
        async def resolve(code, ac):
            calls.append(("resolve", code, ac))
            return {"figi": "F-" + code} if code in ROWS else None

        @staticmethod
        async def candles(figi, interval, days):
            assert interval == "1d"
            return list(ROWS.get(figi[2:], []))

    class FakeMoex:
        @staticmethod
        async def candles(code, ac, interval, days):
            calls.append(("moex", code, ac))
            return list(ROWS.get(code, [])) if code != "MX" else noise

    tinkoff, moex = FakeTinkoff, FakeMoex     # noqa: F811

    async def main():
        items = await partners("SBER", "share")
        got = {x["code"]: x for x in items}
        assert list(got)[0] == "VTBR" and got["VTBR"]["rho"] > 0.99 and got["SI"]["rho"] < -0.99, got
        assert got["T"]["lead"] == -1 and got["BR"]["lead"] == 1, (got["T"], got["BR"])
        assert "MX" in got and strength(got["MX"]) < MIN_RHO, "индексный фьючерс — всегда, если данные есть"
        assert list(got) == sorted(got, key=lambda k: strength(got[k]), reverse=True)
        assert all(x["source"] == "tinkoff" for x in items) and got["VTBR"]["kind"].startswith("сектор")
        assert got["VTBR"]["price"] == same[-1]["c"] and got["VTBR"]["n"] == N - 1
        assert "кандидатов" in explain("SBER") and "показано" in explain("SBER"), explain("SBER")
        assert len(items) <= MAX_PARTNERS
        n_calls = len(calls)
        assert await partners("SBER", "share") == items and len(calls) == n_calls, "кэш: сети нет"
        assert cached("SBER")["items"] == items and kv[KV_PREFIX + "SBER"]["items"] == items
        # текст блока
        tx = text(items, "SBER")
        assert tx.startswith("Связанные бумаги для SBER") and "VTBR (ВТБ; сектор: банки и финансы): ρ=+1.00" in tx, tx
        assert "SI (рубль: фьючерс USD/RUB (Si); макро): ρ=-1.00" in tx and "опережает на день" in tx and "отстаёт на день" in tx
        assert "Обычно читают так" in tx and "входи" not in tx.lower() and text([]) == ""
        # запросы разведки по партнёрам
        rq = scout_requests(items)
        assert len(rq) == 2 * len(items) and rq[0]["kind"] == "quote" and rq[1]["kind"] == "history" and rq[1]["days"] == 5
        si = [r for r in rq if r["code"] == "SI"]
        assert rq[0]["asset_class"] == "share" and len(si) == 2 and si[0]["asset_class"] == "futures" and "ρ=" in rq[0]["why"]
        # протух → пересчёт; force
        _mem[_kv_key("SBER")]["ts"] = time.time() - PARTNERS_TTL - 1
        kv[KV_PREFIX + "SBER"]["ts"] = _mem[_kv_key("SBER")]["ts"]
        assert cached("SBER") is None
        assert [x["code"] for x in await partners("SBER", "share", force=True)] == [x["code"] for x in items]
        # без токена: акции через MOEX, индексный фьючерс честно без данных
        FakeTinkoff.on = False
        reset_cache()
        kv.clear()
        items2 = await partners("SBER", "share")
        c2 = {x["code"] for x in items2}
        assert "VTBR" in c2 and all(x["source"] == "moex" for x in items2) and "MX" not in c2, items2
        assert ("moex", "MX", "futures") not in calls, "индексные фьючерсы с ISS не берём"
        assert "без данных: " in explain("SBER") and "индексных фьючерсов (MX, RI) нет" in explain("SBER") and "токена нет" in explain("SBER"), explain("SBER")
        # базы нет → пусто и честная заметка
        FakeTinkoff.on = True
        assert await partners("ZZZZ", "share") == [] and "нет" in explain("ZZZZ"), explain("ZZZZ")
        assert await partners("", "share") == []
        # выключено конфигом
        config.PYTHIA_PARTNERS = False
        assert await partners("SBER", "share") == []
        config.PYTHIA_PARTNERS = True

    asyncio.run(main())
    print("correlate self-test OK: ρ≈+1/−1, лаг ±1 день, вселенная (сектор + макро), partners с фейковой "
          "биржей, кэш kv 6 ч, без токена — MOEX для акций и «нет данных» для индексных фьючерсов, текст блока")
