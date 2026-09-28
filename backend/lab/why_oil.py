"""ПОЧЕМУ ЭФИР «РИСУЕТСЯ» ТОЛЬКО У НЕФТИ — диагностика тракта сцепки.

Стенд не проверяет гипотезы школы. Он считает БУХГАЛТЕРИЮ ТОЧЕК: сколько
баров пришло на вход, сколько дожило до PLV, где именно они потерялись, как
соотносятся сетка волны и сетка цены, и насколько устойчив перцентиль нуля.

Всё офлайн: свечи из candles_cache.json, эфемериды из локального de440.bsp.

Запуск:  cd .../pythia/pythia && python3 -m backend.lab.why_oil
"""
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from backend import aether as A                                    # noqa: E402

SCR = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad"
CACHE = SCR + "/candles_cache.json"
TICKERS = ("BRU6", "SBER", "GAZP", "LKOH")

# «сейчас» стенда — момент, на который снят кэш контекстов (ds/pythia_ctx.json).
# Фиксировано, чтобы числа воспроизводились байт-в-байт, без random и без часов.
NOW = datetime(2026, 7, 31, 17, 39, 15, tzinfo=timezone.utc)


def set_now(t: datetime) -> None:
    """Стенд считает один и тот же тракт на двух «сейчас»: момент снятия кэша
    (данные устарели) и момент последнего бара (данные свежие). Так видно, что
    структурная потеря, а что — несвежесть фида."""
    global NOW
    NOW = t

_IV_SEC = {"1m": 60, "5m": 300, "10m": 600, "15m": 900, "30m": 1800,
           "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800,
           "1M": 2592000}


# ──────────────────────────────────────────────────────────────────────
# вспомогательное
# ──────────────────────────────────────────────────────────────────────

def _agg(rows: list, bucket_s: int) -> list:
    """Склейка баров в бакеты по bucket_s секунд (детерминированно, по краю
    бакета). Нужна, чтобы собрать «дневной» слот продукта на любом ТФ из
    того, что реально лежит в кэше."""
    out, cur, edge = [], None, None
    for r in rows:
        t = datetime.fromisoformat(r["t"].replace("Z", "+00:00")).timestamp()
        e = math.floor(t / bucket_s) * bucket_s
        if edge is None or e != edge:
            if cur is not None:
                out.append(cur)
            edge = e
            cur = {"t": datetime.fromtimestamp(e, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"), "o": r["o"], "h": r["h"], "l": r["l"],
                "c": r["c"], "v": r.get("v", 0)}
        else:
            cur["h"] = max(cur["h"], r["h"])
            cur["l"] = min(cur["l"], r["l"])
            cur["c"] = r["c"]
            cur["v"] = cur.get("v", 0) + r.get("v", 0)
    if cur is not None:
        out.append(cur)
    return out


def day_slot(cache_tk: dict, interval: str, dd: int) -> list:
    """«Дневной» слот в раскладке продукта: server.api_ether зовёт
    _candles(code, interval, dd), где dd = tf_params(interval)[2].
    Собираем такой слот из того, что есть в кэше (5м / 1ч / 1д)."""
    sec = _IV_SEC.get(interval, 3600)
    if sec <= 1800:
        base, base_sec = cache_tk["day"], 300
    elif sec <= 14400:
        base, base_sec = cache_tk["week"], 3600
    else:
        base, base_sec = cache_tk["month"], 86400
    rows = base if sec <= base_sec else _agg(base, sec)
    lo = (NOW - timedelta(days=dd)).timestamp()
    return [r for r in rows
            if datetime.fromisoformat(r["t"].replace("Z", "+00:00")
                                      ).timestamp() >= lo]


def wave_windows(interval: str):
    """Два окна волны ровно как в compute_context: тонкое (±horizon, шаг
    step_min) для слота day и длинное (±30 сут, шаг 6 ч) для week/month."""
    h, s, dd = A.tf_params(interval)
    step_s = float(s) * 60.0
    t0 = datetime.fromtimestamp(
        math.floor((NOW - timedelta(hours=h)).timestamp() / step_s) * step_s,
        timezone.utc)
    trans = A.transit_series(t0, h * 2.0, s / 60.0)
    t0l = (NOW - timedelta(days=30)).replace(hour=0, minute=0, second=0,
                                             microsecond=0)
    trans_long = A.transit_series(t0l, 60 * 24.0, 6.0)
    return h, s, dd, trans, trans_long


def _iso(t) -> str:
    return datetime.fromtimestamp(float(t), timezone.utc).strftime("%d.%m %H:%M")


def ledger(w_ts, w_psi, price_rows) -> dict:
    """Бухгалтерия точек одной сцепки: где именно теряются бары."""
    ts, cl = A._rows_ts_close(price_rows)
    d = {"in": len(price_rows), "parsed": len(ts)}
    if not ts:
        d.update(fin=0, n=0, lost_left=0, lost_right=0, reason="нет баров")
        return d
    pt = np.asarray(ts, float)
    pc = np.asarray(cl, float)
    ok = np.isfinite(pc)
    pt, pc = pt[ok], pc[ok]
    d["fin"] = int(pt.size)
    lo = max(float(w_ts[0]), float(pt[0]))
    hi = min(float(w_ts[-1]), float(pt[-1]))
    m = (pt >= lo) & (pt <= hi)
    d["lost_left"] = int((pt < lo).sum())
    d["lost_right"] = int((pt > hi).sum())
    d["n"] = int(m.sum())
    d["lo"], d["hi"] = lo, hi
    d["overlap_h"] = (hi - lo) / 3600.0
    d["knots"] = int(((np.asarray(w_ts) >= lo) & (np.asarray(w_ts) <= hi)).sum())
    d["w_span_h"] = (float(w_ts[-1]) - float(w_ts[0])) / 3600.0
    d["w_step_min"] = (float(w_ts[1]) - float(w_ts[0])) / 60.0
    d["left_cut"] = "волна" if float(w_ts[0]) > float(pt[0]) else "цена"
    d["right_cut"] = "волна" if float(w_ts[-1]) < float(pt[-1]) else "цена"
    d["stale_h"] = (NOW.timestamp() - float(pt[-1])) / 3600.0
    if d["n"] >= 16:
        w = np.interp(pt[m], np.asarray(w_ts, float), np.asarray(w_psi, float))
        ph = np.unwrap(np.angle(A._analytic(w)))
        d["cycles"] = float((ph[-1] - ph[0]) / (2.0 * np.pi))
        dd_ = np.abs(np.diff(w))
        med = float(np.median(dd_[dd_ > 0])) if (dd_ > 0).any() else 1e-12
        d["seam"] = abs(float(w[0] - w[-1])) / med       # разрыв, вносимый np.roll
    else:
        d["cycles"] = d["seam"] = float("nan")
    return d


# ──────────────────────────────────────────────────────────────────────
# блоки отчёта
# ──────────────────────────────────────────────────────────────────────

def block1(cache, tag: str = "") -> dict:
    """Учёт баров по продуктовой раскладке слотов, все разрешённые ТФ."""
    print("\n" + "=" * 100)
    print(f"БЛОК 1{tag}. УЧЁТ БАРОВ: сколько пришло → сколько дожило до PLV")
    print(f"«сейчас» = {NOW.isoformat()}")
    print("Раскладка продукта (server.api_ether): day = свечи ТЕКУЩЕГО ТФ за")
    print("dd суток, week = часовые за 7 суток, month = дневные за 60 суток.")
    print("=" * 100)
    hdr = (f"{'тикер':6s}{'ТФ':5s}{'слот':6s}{'баров':>7s}{'n→PLV':>7s}"
           f"{'потеряно слева':>16s}{'справа':>8s}{'перекр.,ч':>11s}"
           f"{'окно волны,ч':>14s}{'шаг,мин':>9s}{'узлов':>7s}{'растяж.':>9s}"
           f"{'циклов Ψ':>10s}  рисуется")
    print(hdr)
    print("-" * len(hdr))
    res = {}
    for tk in TICKERS:
        for iv in ("5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"):
            h, s, dd, trans, trans_long = wave_windows(iv)
            g = A.genesis_for(tk)
            nat = A.natal_lons(g["genesis"])
            psi = A.psi_of(trans, nat, g["band"])
            psi_l = A.psi_of(trans_long, nat, g["band"])
            slots = {"day": (day_slot(cache[tk], iv, dd), trans["ts"], psi),
                     "week": (cache[tk]["week"], trans_long["ts"], psi_l),
                     "month": (cache[tk]["month"], trans_long["ts"], psi_l)}
            for sc, (rows, wts, wp) in slots.items():
                if sc != "day" and iv != "1h":
                    continue        # week/month не зависят от ТФ — печатаем раз
                L = ledger(wts, wp, rows)
                cp = A.couple_wave_price(wts, wp, *A._rows_ts_close(rows)) \
                    if L["n"] >= 16 else None
                up = (L["n"] / L["knots"]) if L.get("knots") else float("nan")
                draw = "ДА" if cp else "нет (couple=None)"
                if cp:
                    draw += f"  PLV {cp['plv']:.3f} pct {cp['plv_pct']}"
                print(f"{tk:6s}{iv:5s}{sc:6s}{L['in']:7d}{L['n']:7d}"
                      f"{L['lost_left']:16d}{L['lost_right']:8d}"
                      f"{L['overlap_h']:11.2f}{L['w_span_h']:14.1f}"
                      f"{L['w_step_min']:9.1f}{L.get('knots', 0):7d}"
                      f"{up:9.1f}{L['cycles']:10.2f}  {draw}")
                res[(tk, iv, sc)] = (L, cp)
    return res


def block2(cache):
    """Сетка цены: скважность торговых часов и дыры (MOEX против круглых суток)."""
    print("\n" + "=" * 100)
    print("БЛОК 2. СЕТКИ ЦЕНЫ: волна живёт 24/7 равномерно, цена — только в")
    print("торговые часы. Гильберт считает сетку РАВНОМЕРНОЙ — ночь сжата в шаг.")
    print("=" * 100)
    print(f"{'тикер':6s}{'слот':7s}{'баров':>7s}{'шаг медиана,мин':>17s}"
          f"{'скважность':>12s}{'дыр>2шага':>11s}{'макс дыра,ч':>13s}"
          f"{'часы UTC':>22s}")
    for tk in TICKERS:
        for sc in ("day", "week", "month"):
            ts, _ = A._rows_ts_close(cache[tk][sc])
            pt = np.asarray(ts, float)
            df = np.diff(pt)
            med = float(np.median(df))
            duty = float(df[df <= 1.5 * med].sum()) / float(pt[-1] - pt[0])
            hrs = sorted({datetime.fromtimestamp(t, timezone.utc).hour
                          for t in pt})
            span = f"{hrs[0]:02d}-{hrs[-1]:02d} ({len(hrs)} ч)"
            print(f"{tk:6s}{sc:7s}{pt.size:7d}{med / 60:17.1f}"
                  f"{duty * 100:11.1f}%{int((df > 2 * med).sum()):11d}"
                  f"{df.max() / 3600:13.2f}{span:>22s}")


def block3(cache, long_days: int = 1100, n_slide: int = 120):
    """Проверка нуля: np.roll против честного скользящего окна по реальному Ψ."""
    print("\n" + "=" * 100)
    print("БЛОК 3. НУЛЬ PLV: np.roll (в продукте) против ЧЕСТНОГО нуля —")
    print(f"то же реальное Ψ, но окно взято на 5,10,…,{5 * n_slide} суток раньше")
    print(f"(ряд Ψ на {long_days} суток, шаг 6 ч; разрыва в сигнале не вносится).")
    print("=" * 100)
    t0l = (NOW - timedelta(days=30)).replace(hour=0, minute=0, second=0,
                                             microsecond=0)
    trl = A.transit_series(t0l, 60 * 24.0, 6.0)
    t0v = (NOW - timedelta(days=long_days)).replace(hour=0, minute=0, second=0,
                                                    microsecond=0)
    trv = A.transit_series(t0v, long_days * 24.0, 6.0)
    print(f"{'тикер':6s}{'слот':7s}{'n':>6s}{'PLV':>7s}{'pct np.roll':>12s}"
          f"{'pct честный':>12s}{'|разница|':>10s}{'шов/шаг':>9s}")
    diffs = []
    for tk in TICKERS:
        g = A.genesis_for(tk)
        nat = A.natal_lons(g["genesis"])
        psi = A.psi_of(trl, nat, g["band"])
        psiv = A.psi_of(trv, nat, g["band"])
        for sc in ("day", "week", "month"):
            ts, cl = A._rows_ts_close(cache[tk][sc])
            L = ledger(trl["ts"], psi, cache[tk][sc])
            if L["n"] < 16:
                continue
            pt = np.asarray(ts, float)
            pc = np.asarray(cl, float)
            m = (pt >= L["lo"]) & (pt <= L["hi"])
            ptm, pcm = pt[m], pc[m]
            w = np.interp(ptm, trl["ts"], psi)
            edge = max(3, ptm.size // A.EDGE_FRAC)
            ph_p = np.unwrap(np.angle(A._analytic(A._bandpass_like(pcm, w))))
            plv, _ = A._plv(np.unwrap(np.angle(A._analytic(w))), ph_p, edge)
            p_roll = A._plv_percentile(w, ph_p, edge, plv)
            vals = []
            for k in range(1, n_slide + 1):
                tt = ptm - k * 5 * 86400.0
                if tt[0] < trv["ts"][0]:
                    break
                v, _ = A._plv(np.unwrap(np.angle(A._analytic(
                    np.interp(tt, trv["ts"], psiv)))), ph_p, edge)
                if v == v:
                    vals.append(float(v))
            p_sl = round(float((np.asarray(vals) < plv).mean() * 100.0), 1)
            dl = abs((p_roll if p_roll is not None else 0.0) - p_sl)
            diffs.append(dl)
            print(f"{tk:6s}{sc:7s}{ptm.size:6d}{plv:7.3f}"
                  f"{str(p_roll):>12s}{p_sl:12.1f}{dl:10.1f}{L['seam']:9.1f}")
    print(f"\nсреднее расхождение двух нулей: {np.mean(diffs):.1f} перц. пункта,"
          f" максимум {np.max(diffs):.1f}")
    return diffs


def block4(cache):
    """Устойчивость перцентиля: двигаем правый край окна на 1..24 бара назад."""
    print("\n" + "=" * 100)
    print("БЛОК 4. УСТОЙЧИВОСТЬ pct: правый край окна отодвигаем на 0..24 бара")
    print("=" * 100)
    t0l = (NOW - timedelta(days=30)).replace(hour=0, minute=0, second=0,
                                             microsecond=0)
    trl = A.transit_series(t0l, 60 * 24.0, 6.0)
    print(f"{'тикер':6s}{'слот':7s}{'PLV мин..макс':>16s}{'pct мин..макс':>16s}"
          f"{'медиана':>9s}{'доля pct>=95':>14s}")
    out = {}
    for tk in TICKERS:
        g = A.genesis_for(tk)
        psi = A.psi_of(trl, A.natal_lons(g["genesis"]), g["band"])
        for sc in ("day", "week"):
            ts, cl = A._rows_ts_close(cache[tk][sc])
            ps, pp = [], []
            for k in range(0, 25):
                cp = A.couple_wave_price(trl["ts"], psi, ts[:len(ts) - k],
                                         cl[:len(cl) - k])
                if cp and cp["plv_pct"] is not None:
                    ps.append(cp["plv_pct"])
                    pp.append(cp["plv"])
            if not ps:
                continue
            ps, pp = np.asarray(ps), np.asarray(pp)
            out[(tk, sc)] = float((ps >= 95).mean())
            print(f"{tk:6s}{sc:7s}{pp.min():7.3f}..{pp.max():<7.3f}"
                  f"{ps.min():7.1f}..{ps.max():<8.1f}{np.median(ps):9.1f}"
                  f"{100 * (ps >= 95).mean():13.1f}%")
    return out


def block5(cache):
    """Кросс-матрица «волна X × цена Y»: именная ли сцепка."""
    print("\n" + "=" * 100)
    print("БЛОК 5. КРОСС-МАТРИЦА волна×цена (слот day, окно ±30 сут):")
    print("если высокий pct — свойство инструмента, диагональ обязана выигрывать.")
    print("=" * 100)
    t0l = (NOW - timedelta(days=30)).replace(hour=0, minute=0, second=0,
                                             microsecond=0)
    trl = A.transit_series(t0l, 60 * 24.0, 6.0)
    waves = {}
    for tk in TICKERS:
        g = A.genesis_for(tk)
        waves[tk] = (A.psi_of(trl, A.natal_lons(g["genesis"]), g["band"]),
                     g["band"], g["key"])
    print("волна / цена  " + "".join(f"{t:>18s}" for t in TICKERS))
    diag_best = 0
    for wt in TICKERS:
        psi, band, key = waves[wt]
        cells = []
        row = []
        for pt_ in TICKERS:
            ts, cl = A._rows_ts_close(cache[pt_]["day"])
            cp = A.couple_wave_price(trl["ts"], psi, ts, cl)
            cells.append(f"{cp['plv']:.3f}/{cp['plv_pct']}")
            row.append(cp["plv_pct"] if cp["plv_pct"] is not None else -1)
        if row[TICKERS.index(wt)] == max(row):
            diag_best += 1
        print(f"{wt + ' b' + str(band):14s}" + "".join(f"{c:>18s}" for c in cells))
    print(f"\nдиагональ выиграла свою строку: {diag_best} из {len(TICKERS)}")
    print("GAZP и LKOH вне реестра GENESIS → обе падают в MOEX_MUNDANE (band 72)")
    print("→ волна у них ОДНА И ТА ЖЕ, строки матрицы совпадают байт-в-байт.")
    return diag_best


def block6():
    """Статическая сверка карт интервалов: где ТФ молча превращается в дневки."""
    print("\n" + "=" * 100)
    print("БЛОК 6. КАРТЫ ИНТЕРВАЛОВ: молчаливая подмена ТФ")
    print("=" * 100)
    from backend import tinkoff, moex
    ok = {"1m", "5m", "10m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"}
    print(f"{'ТФ':5s}{'server ok':>10s}{'tinkoff':>28s}{'moex':>8s}"
          f"{'aether._TF_WAVE':>18s}")
    bad = []
    for iv in sorted(ok, key=lambda x: _IV_SEC.get(x, 0)):
        t = tinkoff._INTERVAL.get(iv)
        m = moex._IV.get(iv)
        w = "есть" if iv in A._TF_WAVE else "НЕТ→профиль 1h"
        if t is None or iv not in A._TF_WAVE:
            bad.append(iv)
        print(f"{iv:5s}{'да':>10s}{(t or 'НЕТ→CANDLE_INTERVAL_DAY'):>28s}"
              f"{str(m):>8s}{w:>18s}")
    print(f"\nТФ с молчаливой подменой: {bad}")
    print("server._candles пробует Тинькофф ПЕРВЫМ; получив непустой список")
    print("дневок, до moex (где 10m/1M разобраны верно) он не доходит.")
    return bad


def block7():
    """Часовой 400 у Тинькофф: лимит API или наш вызов."""
    print("\n" + "=" * 100)
    print("БЛОК 7. ЧАСОВОЙ 400 У SBER")
    print("=" * 100)
    live = SCR + "/ds/live_SBER.json"
    if os.path.exists(live):
        d = json.load(open(live, encoding="utf-8"))
        c = d.get("_candles") or {}
        for k, v in c.items():
            n = len(v) if isinstance(v, list) else 0
            if n:
                t0, t1 = v[0]["t"], v[-1]["t"]
                ts = [datetime.fromisoformat(r["t"].replace("Z", "+00:00"))
                      for r in v]
                st = np.median(np.diff([t.timestamp() for t in ts])) / 60
                print(f"  слот {k:6s}: {n:5d} баров, {t0} .. {t1}, "
                      f"медианный шаг {st:.0f} мин")
            else:
                print(f"  слот {k:6s}: {n:5d} баров  ← запрос упал")
    print("\n  ds/live_raw.py звал: ('day','5min',400) ('week','1h',400) "
          "('month','1d',400)")
    print("  · '5min' НЕ ключ tinkoff._INTERVAL → .get(..., "
          "'CANDLE_INTERVAL_DAY')")
    print("    → в слот 5-минуток легли ДНЕВНЫЕ бары (шаг 1440 мин, см. выше).")
    print("  · '1h' ключ есть, но days_back=400: from=now−400сут для "
          "CANDLE_INTERVAL_HOUR")
    print("    превышает допустимый диапазон запроса Тинькофф на часовом ТФ →")
    print("    HTTP 400. tinkoff.candles НЕ клампит days_back по интервалу,")
    print("    ловит исключение и молча возвращает [] (слот week = 0 баров).")
    print("  · продуктовый путь (server.api_ether) просит ('1h', 7) — "
          "укладывается.")
    print("  ВЫВОД: не ограничение API как таковое, а НАШ незажатый запрос +")
    print("  молчаливый пустой список вместо честной ошибки.")


def block8():
    """Проверка правки _TF_WAVE числами ДО/ПОСЛЕ на длинной истории MOEX."""
    print("\n" + "=" * 100)
    print("БЛОК 8. ПРАВКА _TF_WAVE: числа до и после (ds/moex_hist.json,")
    print("дневные 2013-2026, склеены в недельные и месячные бары)")
    print("=" * 100)
    hist = json.load(open(SCR + "/ds/moex_hist.json", encoding="utf-8"))
    now = datetime(2026, 7, 30, 21, 0, tzinfo=timezone.utc)

    def _agg_hist(rows, bs):
        out, cur, e0 = [], None, None
        for r in rows:
            t = datetime.fromisoformat(r[0]).replace(
                tzinfo=timezone.utc).timestamp()
            e = math.floor(t / bs) * bs
            if e != e0:
                if cur:
                    out.append(cur)
                e0 = e
                cur = {"t": datetime.fromtimestamp(e, timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"), "c": r[4]}
            else:
                cur["c"] = r[4]
        if cur:
            out.append(cur)
        return out

    # профили ДО правки: «1w» держал 90 суток, «1M» вообще падал на профиль 1h
    OLD = {"1w": (2160.0, 720.0, 180), "1M": (24.0, 30.0, 7)}
    print(f"{'ТФ':4s}{'тикер':7s}{'профиль':22s}{'горизонт,сут':>13s}"
          f"{'свечей(dd)':>11s}{'n→PLV':>7s}{'couple':>26s}")
    got = {}
    for iv, bs in (("1w", 604800), ("1M", 2592000)):
        for tk in ("SBER", "GAZP", "LKOH", "ROSN"):
            rows = _agg_hist(hist[tk], bs)
            for lbl, prof in (("было", OLD[iv]), ("стало", A.tf_params(iv))):
                h, st, dd = prof
                if lbl == "было" and iv == "1M":
                    lbl = "было (тихо профиль 1h)"
                step_s = st * 60.0
                t0 = datetime.fromtimestamp(
                    math.floor((now - timedelta(hours=h)).timestamp() / step_s)
                    * step_s, timezone.utc)
                tr = A.transit_series(t0, h * 2.0, st / 60.0)
                g = A.genesis_for(tk)
                psi = A.psi_of(tr, A.natal_lons(g["genesis"]), g["band"])
                lo_c = (now - timedelta(days=dd)).timestamp()
                rr = [r for r in rows
                      if datetime.fromisoformat(r["t"].replace("Z", "+00:00")
                                                ).timestamp() >= lo_c]
                ts, cl = A._rows_ts_close(rr)
                if not ts:
                    print(f"{iv:4s}{tk:7s}{lbl:22s}{h / 24:13.0f}{0:11d}"
                          f"{0:7d}{'None (нет баров)':>26s}")
                    got[(iv, tk, lbl[:5])] = 0
                    continue
                pt = np.asarray(ts, float)
                lo = max(tr["ts"][0], pt[0])
                hi = min(tr["ts"][-1], pt[-1])
                n = int(((pt >= lo) & (pt <= hi)).sum())
                cp = A.couple_wave_price(tr["ts"], psi, ts, cl)
                w = (f"ДА PLV {cp['plv']:.3f} pct {cp['plv_pct']}"
                     if cp else "None")
                print(f"{iv:4s}{tk:7s}{lbl:22s}{h / 24:13.0f}{len(rr):11d}"
                      f"{n:7d}{w:>26s}")
                got[(iv, tk, lbl[:5])] = n
    return got


def main():
    cache = json.load(open(CACHE, encoding="utf-8"))
    print("ПОЧЕМУ ЭФИР РИСУЕТСЯ ТОЛЬКО У НЕФТИ — диагностика тракта")
    print(f"«сейчас» стенда: {NOW.isoformat()}  (момент снятия ds/pythia_ctx.json)")
    r1 = block1(cache, "-А (данные как в кэше, фид отстал)")
    # второй прогон: «сейчас» = последний бар, фид идеально свежий.
    # Разница между А и Б — цена несвежести; то, что осталось в Б, — структура.
    last = max(datetime.fromisoformat(cache[t]["day"][-1]["t"].replace(
        "Z", "+00:00")) for t in TICKERS)
    set_now(last)
    r2 = block1(cache, "-Б (данные свежие: «сейчас» = последний бар)")
    set_now(datetime(2026, 7, 31, 17, 39, 15, tzinfo=timezone.utc))
    block2(cache)
    block3(cache)
    block4(cache)
    block5(cache)
    block6()
    block7()
    block8()
    print("\n" + "=" * 100)
    print("ГЛАВНОЕ ЧИСЛОМ")
    print("=" * 100)
    n_a = sum(1 for k, (L, cp) in r1.items() if k[2] == "day" and cp)
    n_b = sum(1 for k, (L, cp) in r2.items() if k[2] == "day" and cp)
    tot = sum(1 for k in r1 if k[2] == "day")
    print(f"· слот day (ближний эфир, им рисуется лента на графике) считается:")
    print(f"  на отставшем фиде {n_a} из {tot} комбинаций тикер×ТФ, "
          f"на свежем — {n_b} из {tot}.")
    d = r1[("BRU6", "1h", "day")][0]
    print(f"· окно сцепки = ПОЛОВИНА окна волны: t0=now−H, span=2H, будущая")
    print(f"  половина цены не встречает. Плюс правый край режет последний бар.")
    print(f"  BRU6 1h/day: окно волны {d['w_span_h']:.0f} ч, данные "
          f"устарели на {d['stale_h']:.2f} ч →")
    print(f"  перекрытие {d['overlap_h']:.2f} ч → n={d['n']} из {d['in']} баров "
          f"({100 * d['lost_left'] / d['in']:.1f}% выброшено как «левее окна»).")
    print("· сетка волны и сетка цены не совпадают в обе стороны (колонка")
    print("  «растяж.» = ценовых точек на один узел волны): в продуктовой")
    print("  раскладке волна ПРОРЕЖЕНА ×0.1…0.5 на 1h/4h/1d/1w — 48-180 узлов")
    print("  схлопываются в 13-30 баров; а если в слот day подать 5-минутки на")
    print("  ТФ 1d (так снят ds/pythia_ctx.json), волна РАСТЯНУТА ×47…56 —")
    print("  22 узла размазаны np.interp на 1030-1297 точек, и отчётное n=1030")
    print("  в 47 раз больше числа реальных степеней свободы волны.")
    cyc = [L["cycles"] for k, (L, cp) in r2.items()
           if k[2] == "day" and cp and L["cycles"] == L["cycles"]]
    print(f"· циклов Ψ в окне сцепки (свежий фид, слот day): "
          f"{min(cyc):.2f}…{max(cyc):.2f}, меньше одного у "
          f"{sum(1 for c in cyc if c < 1.0)} из {len(cyc)} —")
    print("  на неполном обороте «фаза» Гильберта это монотонная дуга, а PLV")
    print("  между двумя дугами высок почти всегда, независимо от инструмента.")


if __name__ == "__main__":
    cache = json.load(open(CACHE, encoding="utf-8"))
    t0l = (NOW - timedelta(days=30)).replace(hour=0, minute=0, second=0,
                                             microsecond=0)
    trl = A.transit_series(t0l, 60 * 24.0, 6.0)
    assert trl is not None, "нет эфемерид — стенд бессмыслен"

    # ── Т-1: бухгалтерия точек сходится: n + слева + справа = финитных баров
    for tk in TICKERS:
        for sc in ("day", "week", "month"):
            g = A.genesis_for(tk)
            psi = A.psi_of(trl, A.natal_lons(g["genesis"]), g["band"])
            L = ledger(trl["ts"], psi, cache[tk][sc])
            assert L["n"] + L["lost_left"] + L["lost_right"] == L["fin"], (tk, sc, L)

    # ── Т-2: на слоте month волна ПРОРЕЖЕНА (узлов больше, чем баров) —
    #        это не гипотеза, а арифметика сеток
    g = A.genesis_for("SBER")
    psi = A.psi_of(trl, A.natal_lons(g["genesis"]), g["band"])
    Lm = ledger(trl["ts"], psi, cache["SBER"]["month"])
    assert Lm["knots"] > 3 * Lm["n"], Lm            # 117 узлов против 30 баров
    Ld = ledger(trl["ts"], psi, cache["SBER"]["day"])
    assert Ld["n"] > 40 * Ld["knots"], Ld           # 1297 точек на 23 узла

    # ── Т-3: тонкое окно 1h физически не может взять больше horizon_h цены:
    #        сколько бы баров ни лежало, перекрытие не превосходит H
    h, s, dd, tr, _ = wave_windows("1h")
    L1 = ledger(tr["ts"], A.psi_of(tr, A.natal_lons(g["genesis"]), g["band"]),
                cache["SBER"]["day"])
    assert L1["overlap_h"] <= h + 1e-6, (L1["overlap_h"], h)
    assert L1["in"] > 1000 and L1["n"] < 60, L1     # 1297 баров → 40 точек

    # ── Т-4: GAZP и LKOH делят один генезис → волна побайтно одна
    ga = A.genesis_for("GAZP")
    lk = A.genesis_for("LKOH")
    assert ga["genesis"] == lk["genesis"] and ga["band"] == lk["band"], (ga, lk)
    assert np.array_equal(A.psi_of(trl, A.natal_lons(ga["genesis"]), ga["band"]),
                          A.psi_of(trl, A.natal_lons(lk["genesis"]), lk["band"]))

    # ── Т-5: np.roll вносит в суррогат разрыв в сотни типичных шагов —
    #        нуль считается по СЛОМАННОМУ сигналу
    assert Ld["seam"] > 100.0, Ld["seam"]

    # ── Т-6: карты интервалов рассинхронены (реальный баг тракта)
    from backend import tinkoff as _tk
    assert "10m" not in _tk._INTERVAL and "1M" not in _tk._INTERVAL
    # ── Т-6б: правка _TF_WAVE на месте — в горизонт каждого ТФ влезает >=16
    #        баров своего размера (окно сцепки = половина окна волны)
    for _iv, _sec in A._IV_SEC.items():
        assert _iv in A._TF_WAVE, _iv
        assert A.tf_params(_iv)[0] * 3600.0 / _sec >= 16.0, _iv
    assert A.tf_params("1w")[0] == 4320.0 and A.tf_params("1M")[0] == 17280.0

    # ── Т-7: сцепка не именная — чужая волна даёт не хуже своей
    ts_l, cl_l = A._rows_ts_close(cache["LKOH"]["day"])
    psi_br = A.psi_of(trl, A.natal_lons(A.genesis_for("BRU6")["genesis"]),
                      A.genesis_for("BRU6")["band"])
    cp_x = A.couple_wave_price(trl["ts"], psi_br, ts_l, cl_l)
    ts_b, cl_b = A._rows_ts_close(cache["BRU6"]["day"])
    cp_o = A.couple_wave_price(trl["ts"], psi_br, ts_b, cl_b)
    assert cp_x["plv"] > 0.9 and cp_o["plv"] > 0.9, (cp_x["plv"], cp_o["plv"])

    print("self-тесты why_oil: 7/7 пройдены\n")
    main()
