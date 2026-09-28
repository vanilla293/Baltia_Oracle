# -*- coding: utf-8 -*-
"""ЛАБ · ПРОВЕРКА СПОСОБА ВХОДА (гипотеза владельца: «направление верно, а вход — нет»).

Правило направления фиксировано — АНТИМОМЕНТУМ (против хода последнего окна):
    d = -1, если ret_win > 0, иначе +1.
Меняется ТОЛЬКО механика входа:
  (а) сразу по p0 — база;
  (б) лимитка на откате 2/5/10 бп с ожиданием до 60с (если не дошла — сделки нет);
  (в) задержка 15/30/60с, вход по текущей цене;
  (г) лучшая цена за первые 30с — ЗАГЛЯДЫВАНИЕ В БУДУЩЕЕ, только как потолок.

Причинность: признаки и направление — из прошлого (features_at/run_instrument).
Механики (б) и (в) исполнимы в реальном времени; (г) — нет, помечена.
Комиссия 5 бп за круг вычитается из среднего хода ВСЕГДА.
⚫ Измерение прошлого на конкретной выборке. Не сигнал. 18+.
"""
from __future__ import annotations

import math
import pickle
import sys

import numpy as np

sys.path.insert(0, "/home/user/Real_Sky-/pythia")
from backend.lab import forward_test as ft   # noqa: E402

COMMISSION_BPS = 5.0
DEAD = ft.DEAD_FRAC          # 5e-5 — «стоял на месте», как в базе
MIN_CELL = 30                # требование задачи: при n<30 не судим
CACHE = "/tmp/claude-0/-home-user-Real-Sky-/c14b381a-069b-5b6f-8c22-60cde6cdcb90/scratchpad/obs.pkl"


# ── сбор наблюдений + сырых рядов ──────────────────────────────────────────
def build(db: str) -> tuple:
    obs, series = [], {}
    for sec in ft.instruments(db):
        d = ft.load_series(db, sec)
        o = ft.run_instrument(sec, d)
        if o:
            obs.extend(o)
            series[sec] = {"ts": np.asarray(d["ts"], dtype=np.int64),
                           "px": np.asarray(d["px"], dtype=float)}
        print(f"  {sec}: тиков={len(d['ts'])}, точек решения={len(o)}", flush=True)
    return obs, series


def load_cached(db: str) -> tuple:
    try:
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    except Exception:
        r = build(db)
        with open(CACHE, "wb") as f:
            pickle.dump(r, f)
        return r


# ── механики входа: возвращают (цена_входа, время_входа) либо None ─────────
def _px_at(ts, px, t):
    """Первый тик в момент t или позже (та же конвенция, что у базы для факта)."""
    j = int(np.searchsorted(ts, t, side="left"))
    if j >= ts.size:
        return None, None
    return float(px[j]), int(ts[j])


def entry_now(ts, px, i, t0, p0, d, **_):
    return p0, t0


def entry_delay(ts, px, i, t0, p0, d, sec_delay=30, **_):
    p, t = _px_at(ts, px, t0 + sec_delay * 1000)
    return (p, t) if p and p > 0 else (None, None)


def entry_limit(ts, px, i, t0, p0, d, pull_bps=5.0, wait_s=60, **_):
    """Лимитка на откате: ждём ход ПРОТИВ нашего направления на pull_bps.

    d=+1 (покупаем) → ждём цену ниже p0*(1-x); d=-1 → выше p0*(1+x).
    Заполнение по цене лимита (консервативно: гэп сквозь уровень не даём в плюс).
    Не дошла за wait_s — сделки нет.
    """
    x = pull_bps / 1e4
    target = p0 * (1.0 - d * x)
    lo = int(np.searchsorted(ts, t0, side="right"))
    hi = int(np.searchsorted(ts, t0 + wait_s * 1000, side="right"))
    if hi <= lo:
        return None, None
    seg = px[lo:hi]
    hit = np.nonzero(seg <= target)[0] if d > 0 else np.nonzero(seg >= target)[0]
    if hit.size == 0:
        return None, None
    return float(target), int(ts[lo + int(hit[0])])


def entry_best(ts, px, i, t0, p0, d, win_s=30, **_):
    """ЛУКАХЕД: лучшая цена за первые win_s. Не исполнимо, только потолок."""
    lo = int(np.searchsorted(ts, t0, side="right"))
    hi = int(np.searchsorted(ts, t0 + win_s * 1000, side="right"))
    if hi <= lo:
        return None, None
    seg = px[lo:hi]
    k = int(np.argmin(seg)) if d > 0 else int(np.argmax(seg))
    return float(seg[k]), int(ts[lo + k])


# ── оценка одной механики на одном горизонте ───────────────────────────────
def evaluate(obs, series, H, entry_fn, kw=None, exit_from_entry=False,
             only=None, dead_filter=True):
    """only — множество ключей (sec,t) для сопоставимой подвыборки (matched)."""
    kw = kw or {}
    hits = miss = ties = 0
    rets, taken, eligible = [], 0, 0
    filled_keys = []
    for r in obs:
        sec, t0, p0 = r["sec"], r["t"], r["p0"]
        base_fwd = r.get(f"fwd{H}")
        if base_fwd is None:
            continue
        if dead_filter and abs(base_fwd) < DEAD:
            continue                       # тот же фильтр «стоял на месте», что у базы
        d = -1 if r["ret_win"] > 0 else 1  # АНТИМОМЕНТУМ
        key = (sec, t0)
        if only is not None and key not in only:
            continue
        eligible += 1
        S = series[sec]
        ts, px = S["ts"], S["px"]
        pe, te = entry_fn(ts, px, None, t0, p0, d, **kw)
        if pe is None or pe <= 0:
            continue
        t_exit = (te if exit_from_entry else t0) + H * 1000
        pex, _ = _px_at(ts, px, t_exit)
        if pex is None or t_exit <= te:
            continue
        taken += 1
        filled_keys.append(key)
        ret = d * (pex - pe) / pe
        if ret > 0:
            hits += 1
        elif ret < 0:
            miss += 1
        else:
            ties += 1
            continue
        rets.append(ret)
    n = hits + miss
    if n == 0:
        return None
    mean_bps = float(np.mean(rets)) * 1e4
    sigma = (hits - n / 2.0) / math.sqrt(n / 4.0)
    return {"n": n, "taken": taken, "eligible": eligible, "ties": ties,
            "fill_rate": taken / eligible if eligible else 0.0,
            "hit_rate": hits / n, "sigma": sigma, "mean_bps": mean_bps,
            "net_bps": mean_bps - COMMISSION_BPS,
            "keys": filled_keys}


METHODS = [
    ("а) сразу по p0 (БАЗА)",          entry_now,   {}),
    ("б) лимит откат 2бп / 60с",       entry_limit, {"pull_bps": 2.0}),
    ("б) лимит откат 5бп / 60с",       entry_limit, {"pull_bps": 5.0}),
    ("б) лимит откат 10бп / 60с",      entry_limit, {"pull_bps": 10.0}),
    ("в) задержка 15с",                entry_delay, {"sec_delay": 15}),
    ("в) задержка 30с",                entry_delay, {"sec_delay": 30}),
    ("в) задержка 60с",                entry_delay, {"sec_delay": 60}),
    ("г) лучшая цена 30с (ЛУКАХЕД)",   entry_best,  {"win_s": 30}),
]


def fmt(name, m):
    if m is None:
        return f"  {name:32s}  —  нет данных"
    flag = "" if m["n"] >= MIN_CELL else "  (n<30: НЕ СУДИМ)"
    return (f"  {name:32s} N={m['n']:5d} вход={m['fill_rate']*100:5.1f}% "
            f"попад={m['hit_rate']*100:5.1f}% σ={m['sigma']:+6.2f} "
            f"ход={m['mean_bps']:+7.2f}бп нетто={m['net_bps']:+7.2f}бп{flag}")


def selftest(obs, series):
    """Реальные assert'ы: воспроизводимость базы, причинность, арифметика лимитки."""
    b = evaluate(obs, series, 180, entry_now)
    assert b and b["n"] > 900, f"база: слишком мало наблюдений {b}"
    assert 0.54 < b["hit_rate"] < 0.56, f"база 180с должна давать ~55%: {b['hit_rate']}"
    assert abs(b["net_bps"] - (b["mean_bps"] - 5.0)) < 1e-9, "комиссия не вычтена"
    assert b["net_bps"] < 0, "база обязана быть в минусе после комиссии"
    # лимитка обязана давать скидку РОВНО в размер отката (иначе ошибка исполнения)
    for pull in (2.0, 5.0, 10.0):
        m = evaluate(obs, series, 180, entry_limit, {"pull_bps": pull})
        assert m, f"лимитка {pull}бп: пусто"
        b2 = evaluate(obs, series, 180, entry_now, only=set(m["keys"]))
        disc = m["mean_bps"] - b2["mean_bps"]
        assert abs(disc - pull) < 0.05, f"скидка {disc:.3f} != откат {pull}"
        assert m["fill_rate"] < 1.0, "лимитка не может исполняться всегда"
    # причинность: цена входа/выхода строго ПОСЛЕ точки решения
    S = series[obs[0]["sec"]]
    p, t = entry_delay(S["ts"], S["px"], None, obs[0]["t"], obs[0]["p0"], 1, sec_delay=30)
    assert t is None or t >= obs[0]["t"] + 30_000, "задержка смотрит в прошлое"
    print("self-тест entry_probe: OK")


def main():
    db = "/home/user/Real_Sky-/pythia/data/moex_real.db"
    obs, series = load_cached(db)
    print(f"\nвсего точек решения: {len(obs)}\n")
    selftest(obs, series)
    print()

    for exit_mode, tag in ((False, "выход в t0+H (единый конец горизонта, как в базе)"),
                           (True,  "выход в t_входа+H (полный горизонт от входа)")):
        for H in (180, 600):
            print("=" * 96)
            print(f"ГОРИЗОНТ {H}с · {tag}")
            print("=" * 96)
            base = evaluate(obs, series, H, entry_now, exit_from_entry=exit_mode)
            for name, fn, kw in METHODS:
                m = evaluate(obs, series, H, fn, kw, exit_from_entry=exit_mode)
                print(fmt(name, m))
                # сопоставимая подвыборка: та же база на тех же точках, где вход состоялся
                if m and m["fill_rate"] < 0.999:
                    bm = evaluate(obs, series, H, entry_now, exit_from_entry=exit_mode,
                                  only=set(m["keys"]))
                    print(fmt("      ↳ база на той же подвыборке", bm))
            print()


if __name__ == "__main__":
    main()
