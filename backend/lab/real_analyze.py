# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ · ЛАБ — РАЗБОР РЕАЛЬНЫХ ДАННЫХ MOEX (обе машины Пифии).

Берёт базу, собранную moex_farm (реальные котировки и лента MOEX ISS), и
прогоняет по ней:
  · ФИЗИК (tagger)          — карта плотности среды во времени;
  · ПАТОЛОГОАНАТОМ (patterns) — ультра-пробои и сигнатуры ловушек;
  · ХОУКС по СЫРЫМ таймстампам реальных сделок — ветвление потока;
  · МАШИНА «НОВАЯ» (oracle)  — иерархическая машина состояний v4.x;
  · МАШИНА «ОРИГИНАЛ» (directive) — свод голосов ДО редизайна.
Обе машины на ОДНИХ И ТЕХ ЖЕ реальных данных — видно, где они расходятся.

Ограничение источника (честно): публичный ISS не отдаёт стакан, поэтому
OBI отсутствует. Голоса, требующие стакана, у обеих машин не голосуют —
это не баг, а отсутствие данных (NO DUMMIES). Что реально есть: mid,
спред (Казимир), сырая лента со стороной агрессора (CVD, VPIN, Хоукс).

Запуск: python3 -m backend.lab.real_analyze --db data/moex.db --out отчёт.txt
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone

try:
    from .. import bifurcation, directive, hawkes, oracle, patterns, tagger
except ImportError:                                        # запуск как скрипт
    sys.path.insert(0, __file__.rsplit("/backend/", 1)[0])
    from backend import bifurcation, directive, hawkes, oracle, patterns, tagger

MIN_BOOK = 64          # меньше точек ряда — разбирать нечего


def load(db_path: str) -> dict:
    """Все инструменты базы → {тикер: сырьё в формате patterns}."""
    db = sqlite3.connect(db_path)
    try:
        secs = [r[0] for r in db.execute(
            "SELECT DISTINCT figi FROM book ORDER BY figi").fetchall()]
        out = {}
        for s in secs:
            book = db.execute(
                "SELECT ts_ms,best_bid,best_ask,obi,spread_bps FROM book "
                "WHERE figi=? ORDER BY ts_ms", (s,)).fetchall()
            tr = db.execute(
                "SELECT ts_ms,price,qty,side FROM trades WHERE figi=? "
                "ORDER BY ts_ms", (s,)).fetchall()
            out[s] = {
                "book_ts": [r[0] for r in book],
                "mid": [((r[1] + r[2]) / 2.0 if r[1] and r[2] else 0.0)
                        for r in book],
                "obi": [r[3] for r in book],
                "spread_bps": [r[4] for r in book],
                "trade_ts": [r[0] for r in tr],
                "trade_side": [r[3] for r in tr],
                "trade_qty": [r[2] for r in tr],
                "trade_px": [r[1] for r in tr],
            }
        return out
    finally:
        db.close()


def _series(d: dict) -> tuple:
    """Ценовой ряд для разбора. ГЛАВНЫЙ источник — цены реальных СДЕЛОК
    (тиковый ряд, тысячи точек); котировочный mid идёт в дело, только если
    сделок мало. Спред всегда из котировок (для Казимира)."""
    px = [p for p in (d.get("trade_px") or []) if p and p > 0]
    ts = [t for p, t in zip(d.get("trade_px") or [], d["trade_ts"]) if p and p > 0]
    if len(px) >= MIN_BOOK:
        return ts, px, "сделки (тиковый ряд)"
    mids = [m for m in d["mid"] if m > 0]
    mts = [t for m, t in zip(d["mid"], d["book_ts"]) if m > 0]
    return mts, mids, "котировки (mid)"


def analyze_one(sec: str, d: dict) -> dict:
    """Полный разбор одного инструмента обеими машинами."""
    res = {"sec": sec, "n_book": len(d["mid"]), "n_trades": len(d["trade_ts"])}
    ser_ts, mids, src = _series(d)
    res["series_src"] = src
    res["n_series"] = len(mids)
    if len(mids) < MIN_BOOK:
        res["skip"] = f"мало точек ряда ({len(mids)})"
        return res
    # ряд для Патологоанатома: пробои ищем по тому же ряду, что и всё
    d = dict(d)
    d["book_ts"], d["mid"] = ser_ts, mids
    if len(d["spread_bps"]) < len(mids):          # спред растянуть на ряд
        sp = d["spread_bps"] or [0.0]
        d["spread_bps"] = [sp[min(i * len(sp) // max(1, len(mids)), len(sp) - 1)]
                           for i in range(len(mids))]
        d["obi"] = [0.0] * len(mids)
    # диапазон и ход цены за окно
    res["px_first"], res["px_last"] = mids[0], mids[-1]
    res["move_pct"] = (mids[-1] - mids[0]) / mids[0] * 100.0
    res["range_pct"] = (max(mids) - min(mids)) / min(mids) * 100.0
    res["spread_bps_med"] = sorted(d["spread_bps"])[len(d["spread_bps"]) // 2]

    # ── ДВИГАТЕЛЬ БИФУРКАЦИЙ по реальному ряду ──
    try:
        bc = bifurcation.bifurcation_context(mids)
        res["bif"] = bc["summary"]
        lp = bc.get("lppls")
        res["lppls"] = (lp if isinstance(lp, dict) and "error" not in lp else None)
    except Exception as e:                                   # noqa: BLE001
        res["bif_error"] = str(e)[:100]
        bc = {"summary": {}}

    # ── ФИЗИК: карта плотности среды ──
    try:
        tags = tagger.tag_series(list(zip(d["book_ts"], d["mid"])),
                                 win=90, step=30,
                                 spread_series=d["spread_bps"])
        res["tags"] = tags
        res["hot"] = sum(1 for t in tags if t["density"] == "наэлектризованная")
        res["visc"] = sum(1 for t in tags if t["density"] == "вязкая")
    except Exception as e:                                   # noqa: BLE001
        res["tagger_error"] = str(e)[:100]
        res["tags"] = []

    # ── ХОУКС по СЫРЫМ таймстампам реальных сделок ──
    try:
        hk = hawkes.branching_times(d["trade_ts"]) if len(d["trade_ts"]) >= 60 else None
        res["hawkes"] = hk
    except Exception as e:                                   # noqa: BLE001
        res["hawkes_error"] = str(e)[:100]
        hk = None

    # ── ПАТОЛОГОАНАТОМ: пробои и сигнатуры ──
    try:
        mined = patterns.mine(d, gap_s=120.0)
        res["breakouts"] = mined["events"]
        res["sig_up"] = mined["signature_up"]
        res["sig_down"] = mined["signature_down"]
    except Exception as e:                                   # noqa: BLE001
        res["patterns_error"] = str(e)[:100]
        res["breakouts"] = []

    # ── ПОТОК: CVD и VPIN по реальной ленте ──
    sides, qtys = d["trade_side"], d["trade_qty"]
    cvd = sum(q * s for q, s in zip(qtys, sides))
    buy = sum(q for q, s in zip(qtys, sides) if s > 0)
    sell = sum(q for q, s in zip(qtys, sides) if s < 0)
    tot = buy + sell
    res["cvd"] = cvd
    res["vpin"] = (abs(buy - sell) / tot) if tot > 0 else None

    # ── МАШИНА «НОВАЯ»: Оракул (машина состояний) ──
    # стакана нет → xray/maya не голосуют честно; работают: бифуркации,
    # тренд по ряду, Хоукс, Хёрст
    hu = (bc.get("hurst") or {}) if isinstance(bc.get("hurst"), dict) else {}
    trend = ("up" if mids[-1] > mids[max(0, len(mids) - 40)] else
             "down" if mids[-1] < mids[max(0, len(mids) - 40)] else "flat")
    atr_frac = (max(mids[-60:]) - min(mids[-60:])) / mids[-1] if len(mids) >= 60 else None
    try:
        v_new = oracle.verdict({
            "bif": bc, "hawkes": hk, "trend": trend,
            "hurst": hu.get("H"), "atr_frac": atr_frac,
        })
        res["oracle_new"] = {k: v_new.get(k) for k in
                             ("state", "override", "dir", "p_long", "confidence",
                              "mode", "window_open", "predictability",
                              "n_voices", "reason")}
    except Exception as e:                                   # noqa: BLE001
        res["oracle_error"] = str(e)[:120]

    # ── МАШИНА «ОРИГИНАЛ»: directive (свод голосов до редизайна) ──
    # ему нужен живой стакан (maya) — его нет; подаём что есть, смотрим,
    # как он себя ведёт на неполных данных
    try:
        wave_slope = ((mids[-1] - mids[max(0, len(mids) - 20)])
                      / mids[max(0, len(mids) - 20)]) if len(mids) >= 20 else None
        v_old = directive.directive(None, None, wave_slope)
        res["directive_orig"] = {k: v_old.get(k) for k in
                                 ("dir", "dir_word", "score", "confidence",
                                  "regime", "sources", "reason")}
    except Exception as e:                                   # noqa: BLE001
        res["directive_error"] = str(e)[:120]
    return res


def render(results: list, header: str) -> list:
    """Человекочитаемый отчёт."""
    L = []
    L.append(header)
    L.append("")
    tot_br = sum(len(r.get("breakouts") or []) for r in results)
    tot_tr = sum(r.get("n_trades", 0) for r in results)
    tot_bk = sum(r.get("n_book", 0) for r in results)
    L.append(f"ИНСТРУМЕНТОВ: {len(results)} | точек ряда: {tot_bk} | "
             f"реальных сделок: {tot_tr} | ультра-пробоев: {tot_br}")
    L.append("")
    for r in sorted(results, key=lambda x: -(x.get("n_trades") or 0)):
        L.append("=" * 72)
        L.append(f"{r['sec']}   ряд={r.get('n_series', r['n_book'])} точек [{r.get('series_src','?')}], сделок={r['n_trades']}")
        L.append("=" * 72)
        if r.get("skip"):
            L.append(f"  пропуск: {r['skip']}")
            continue
        L.append(f"  цена: {r['px_first']:.4f} → {r['px_last']:.4f} "
                 f"({r['move_pct']:+.3f}%), размах {r['range_pct']:.3f}%, "
                 f"спред медиана {r['spread_bps_med']:.2f} бпс")
        if r.get("cvd") is not None:
            vp = r.get("vpin")
            L.append(f"  поток: CVD={r['cvd']:+.0f}"
                     + (f", VPIN={vp:.3f}" if vp is not None else ""))
        hk = r.get("hawkes")
        if hk:
            L.append(f"  ХОУКС (сырые таймстампы): ветвление n={hk['n']}, "
                     f"ядро {hk['half_life_ms']} мс, событий {hk['events']} — "
                     f"{hk['word']}")
        b = r.get("bif") or {}
        if b:
            L.append(f"  БИФУРКАЦИИ: {b.get('regime')}; давление="
                     f"{b.get('pressure')}, предсказуемость={b.get('predictability')}, "
                     f"хвост α={b.get('tail_alpha')}, горизонт Ляпунова="
                     f"{b.get('lyap_horizon')}, окно={b.get('window_open')}")
        lp = r.get("lppls")
        if lp and lp.get("qualified"):
            L.append(f"  LPPLS СОРНЕТТА: сингулярность через ~{lp['tc_bars_ahead']} "
                     f"баров, {lp['side']}, R²={lp['r2']}")
        L.append(f"  ФИЗИК: наэлектризованных окон={r.get('hot')}, "
                 f"вязких={r.get('visc')}, всего={len(r.get('tags') or [])}")
        evs = r.get("breakouts") or []
        if evs:
            L.append(f"  ⚡ УЛЬТРА-ПРОБОИ: {len(evs)}")
            for e in evs[:5]:
                hh = datetime.fromtimestamp(e["t0_ms"] / 1000.0,
                                            timezone.utc).strftime("%H:%M:%S")
                L.append(f"      {hh}Z  {e['side'].upper()}  {e['move_frac']*100:.2f}%")
        for key, ttl in (("sig_up", "вверх"), ("sig_down", "вниз")):
            s = r.get(key)
            if s:
                L.append(f"  🎯 СИГНАТУРА ({ttl}, n={s['n_events']}): {s['portrait']}")
                L.append(f"      медианы: сжатие_спреда={s['spread_squeeze_med']}, "
                         f"CVD={s['cvd_med']}, Хоукс={s['hawkes_n_med']}")
        on = r.get("oracle_new")
        if on:
            L.append(f"  🤖 МАШИНА НОВАЯ (Оракул): [{on['state']}] "
                     f"{on['dir']}, P={on['p_long']}, увер.={on['confidence']}, "
                     f"режим={on['mode']}, голосов={on['n_voices']}, "
                     f"окно={on['window_open']}")
        od = r.get("directive_orig")
        if od:
            L.append(f"  🕰 МАШИНА ОРИГИНАЛ (directive): {od['dir_word']}, "
                     f"счёт={od['score']}, увер.={od['confidence']}, "
                     f"источники={od['sources']}")
        L.append("")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description="Разбор реальных данных MOEX")
    ap.add_argument("--db", default="data/moex.db")
    ap.add_argument("--out", default="")
    ap.add_argument("--title", default="РАЗБОР РЕАЛЬНЫХ ДАННЫХ MOEX")
    a = ap.parse_args()
    data = load(a.db)
    results = [analyze_one(s, d) for s, d in data.items()]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    lines = render(results, f"{a.title}\nисточник: MOEX ISS (публичный, "
                            f"реальные торги) · сформировано {now}")
    txt = "\n".join(lines)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(txt + "\n")
        print(f"отчёт: {a.out} ({len(lines)} строк)")
    else:
        print(txt)


if __name__ == "__main__":
    main()
