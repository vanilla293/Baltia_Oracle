# -*- coding: utf-8 -*-
"""R&D: две гипотезы детекции микроструктурных аномалий (спринт Zero-Touch).

ГИПОТЕЗА 1 🟡 — «ВЕДУЩИЙ ОСЦИЛЛЯТОР» (кросс-инструментальная фазовая
синхронизация с НАПРАВЛЕНИЕМ).
  Физика: два связанных осциллятора с несимметричной связью — ведомый
  повторяет фазу ведущего с запаздыванием (master–slave). Текущий
  kuramoto.py меряет СИЛУ сцепки (PLV/r̄), но не НАПРАВЛЕНИЕ: кто кого
  ведёт. Направление — это знак наклона фазы кросс-спектра по частоте:
  постоянный лаг τ даёт фазу φ(f) = 2πfτ, растущую с f.
  Статистика: Phase-Slope Index (Нолте, 2008) — мнимая часть произведения
  соседних по частоте значений НОРМИРОВАННОЙ кросс-спектральной плотности:
      Ψ = Σ_f Im( C*(f) · C(f+δf) ),   C = S_xy / √(S_xx·S_yy).
  Ψ > 0 — x ведёт y; Ψ < 0 — y ведёт x. Устойчив к мгновенной общей
  компоненте (Im(C·C*) её зануляет — аналог volume-conduction в ЭЭГ).
  Значимость — джекнайф по сегментам: z = Ψ / σ_jackknife.
  Применение: в кислоте направление берётся у ВЕДУЩЕГО (Si/BR/GOLD против
  CR), а не у собственного шумного потока ведомого.
  Нули для проверки (обязательны до боевого веса): (а) циклический сдвиг
  ведущего на ±k минут с выбросом шва; (б) IAAFT-суррогат ведущего
  (сохраняет спектр, рвёт фазовую связь); (в) плацебо-пары из разных дней.

ГИПОТЕЗА 2 🟡 — «СКРЫТАЯ ТЕПЛОТА» (разрядка скрытого пула перед срывом).
  Физика: фазовый переход первого рода — пока лёд тает, температура стоит
  (скрытая теплота поглощает энергию); лёд кончился — температура прыгает.
  На бирже: айсберг у уровня поглощает поток (цена стоит, витрина уровня
  восстанавливается после каждого удара — «дозаправка»); резервуар
  кончился — дозаправки прекращаются, цена срывается сквозь уровень.
  Статистики:
    · скрытый объём события: h = max(0, executed − max(0, disp_before −
      disp_after)) — исполнено СВЕРХ видимой убыли витрины = кто-то
      дозаправлял уровень из скрытого пула;
    · CUSUM Пейджа S_t = max(0, S_{t−1} + h_t − k): тревога S_t > H —
      скрытый пул РАБОТАЕТ (айсберг подтверждён накоплением, не одним
      случайным принтом);
    · разрядка: пул был подтверждён, а скрытый поток упал ниже доли
      DEPLETE_FRAC от своего пика при цене всё ещё у уровня — резервуар
      пуст, ждём срыв СКВОЗЬ уровень (bid-пул иссяк → вниз).
  Нули: (а) блочная перестановка событий ленты внутри дня; (б) плацебо-
  уровни (соседние цены без айсберга); (в) контроль «просто большой
  объём» — h против сырого executed (эффект обязан жить именно в h).

Обе гипотезы — ДЕТЕКТОРЫ-КАНДИДАТЫ: до прохождения нулей и протокола
BACKTEST_PROTOCOL.md в боевой вес не идут. ⚫ не сигнал, 18+.
Self-тест: python3 -m backend.lab.rnd_micro
"""
from __future__ import annotations

import math

import numpy as np

# ── Гипотеза 1: Phase-Slope Index ───────────────────────────────────────────
SEG_LEN = 256          # длина сегмента Уэлча (степень двойки)
PSI_BAND = (0.01, 0.35)   # рабочая полоса в долях Найквиста (края выбросить)


def _segments(x: np.ndarray, seg: int) -> np.ndarray:
    n = (len(x) // seg) * seg
    if n < seg * 2:                     # меньше двух сегментов — не судим
        return np.empty((0, seg))
    return x[:n].reshape(-1, seg)


def phase_slope_index(x, y, seg: int = SEG_LEN) -> dict:
    """Ψ и его z-оценка (джекнайф): Ψ>0 и |z|≥2 — x ВЕДЁТ y.
    Вход: два ряда одинаковой длины (лог-цены или доходности, детренд —
    забота вызывающего). Мало данных → {"psi": None} (NO DUMMIES)."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    if len(x) != len(y):
        return {"psi": None, "note": "ряды разной длины"}
    xs, ys = _segments(x - x.mean(), seg), _segments(y - y.mean(), seg)
    if len(xs) < 4:
        return {"psi": None, "note": f"нужно ≥4 сегментов по {seg} точек"}
    win = np.hanning(seg)
    fx = np.fft.rfft(xs * win, axis=1)
    fy = np.fft.rfft(ys * win, axis=1)
    lo = max(1, int(PSI_BAND[0] * (seg // 2)))
    hi = max(lo + 2, int(PSI_BAND[1] * (seg // 2)))

    def _psi(mask: np.ndarray) -> float:
        sxx = (np.abs(fx[mask]) ** 2).mean(axis=0)
        syy = (np.abs(fy[mask]) ** 2).mean(axis=0)
        sxy = (fx[mask] * np.conj(fy[mask])).mean(axis=0)
        c = sxy / np.sqrt(sxx * syy + 1e-30)
        band = c[lo:hi]
        return float(np.imag(np.conj(band[:-1]) * band[1:]).sum())

    k = len(xs)
    full = _psi(np.ones(k, bool))
    jack = []
    for i in range(k):
        m = np.ones(k, bool)
        m[i] = False
        jack.append(_psi(m))
    jack = np.asarray(jack)
    var = (k - 1) / k * ((jack - jack.mean()) ** 2).sum()
    sd = math.sqrt(var) if var > 0 else 0.0
    # ВЫРОЖДЕНИЕ: джекнайф-СКО пренебрежимо мало против масштаба самой
    # оценки (одинаковые сегменты, нулевая мощность) — деление на такой
    # «ноль» давало бы z порядка 1e15 и фиктивный «значимый лидер».
    # Честный отказ от вердикта вместо выдуманной значимости (NO DUMMIES).
    scale = max(abs(full), float(np.abs(jack).max() if k else 0.0))
    if sd <= 1e-12 * max(scale, 1.0):
        return {"psi": round(full, 6), "z": None, "segments": k,
                "leader": "не значимо",
                "note": "джекнайф-дисперсия вырождена (сегменты неразличимы) "
                        "— значимость не оценивается"}
    z = full / sd
    lead = "x→y" if full > 0 else ("y→x" if full < 0 else "нет")
    return {"psi": round(full, 6), "z": round(z, 2), "segments": k,
            "leader": (lead if abs(z) >= 2.0 else "не значимо"),
            "note": "🟡 кандидат: до нулей (сдвиг/IAAFT/плацебо-пары) "
                    "в боевой вес не идёт"}


# ── Гипотеза 2: скрытая теплота (айсберг → разрядка) ────────────────────────
CUSUM_K_FRAC = 0.25    # снос k = доля медианного executed (шум не копится)
CUSUM_H_FRAC = 3.0     # порог H = кратное k (правило трёх сносов)
DEPLETE_FRAC = 0.2     # скрытый поток упал ниже доли пика → пул иссяк


def hidden_volume(executed, disp_before, disp_after) -> float:
    """Скрытый объём одного события у уровня: исполнено СВЕРХ видимой убыли
    витрины (дозаправка из скрытого пула). Все аргументы ≥ 0."""
    e = max(0.0, float(executed or 0.0))
    drop = max(0.0, float(disp_before or 0.0) - float(disp_after or 0.0))
    return max(0.0, e - drop)


def page_cusum(xs, k: float, h: float) -> dict:
    """Односторонний CUSUM Пейджа: S=max(0, S+x−k), тревога S>h (сброс после).
    Возврат: {alarms: [индексы], s_last}. Детерминирован."""
    s = 0.0
    alarms = []
    for i, x in enumerate(xs):
        s = max(0.0, s + float(x) - k)
        if s > h:
            alarms.append(i)
            s = 0.0
    return {"alarms": alarms, "s_last": round(s, 4)}


def latent_heat_scan(events: list, price_pinned: bool = True) -> dict:
    """Скан «скрытой теплоты» по событиям ленты у best-уровней.
    events: [{side: 'bid'|'ask', executed, disp_before, disp_after}, ...]
    в хронологическом порядке (окно наблюдения одного уровня).

    Возврат: iceberg_side — где подтверждён скрытый пул (CUSUM), depleted —
    пул иссяк (разрядка), break_dir — ожидаемое направление срыва
    ('down' = bid-пул иссяк, 'up' = ask-пул иссяк, None — нет сигнала).
    price_pinned=False (цена уже ушла от уровня) гасит сигнал разрядки —
    срыв уже случился, ловить нечего."""
    hid = {"bid": [], "ask": []}
    for ev in events or []:
        side = ev.get("side")
        if side in hid:
            hid[side].append(hidden_volume(ev.get("executed"),
                                           ev.get("disp_before"),
                                           ev.get("disp_after")))
    ex_all = [max(0.0, float(e.get("executed") or 0.0)) for e in events or []]
    med = sorted(ex_all)[len(ex_all) // 2] if ex_all else 0.0
    if med <= 0:
        return {"iceberg_side": None, "depleted": False, "break_dir": None,
                "note": "нет исполнений — не судим"}
    k = CUSUM_K_FRAC * med
    h = CUSUM_H_FRAC * k
    out = {"iceberg_side": None, "depleted": False, "break_dir": None}
    for side in ("bid", "ask"):
        xs = hid[side]
        if len(xs) < 6:
            continue
        cus = page_cusum(xs, k, h)
        if not cus["alarms"]:
            continue
        out["iceberg_side"] = side
        out["cusum_alarms"] = len(cus["alarms"])
        # разрядка: пик скрытого потока в прошлом, свежий поток ≈ ноль
        peak = max(xs)
        tail = xs[-3:]
        recent = sum(tail) / len(tail)
        if peak > 0 and recent <= DEPLETE_FRAC * peak and price_pinned:
            out["depleted"] = True
            out["break_dir"] = "down" if side == "bid" else "up"
            out["note"] = (f"пул {side} иссяк: поток {recent:.0f} ≤ "
                           f"{DEPLETE_FRAC:.0%} пика {peak:.0f} — ждём срыв "
                           f"{'вниз' if side == 'bid' else 'вверх'} 🟡")
        else:
            # вердикт направления обязан соответствовать ПОСЛЕДНЕЙ записанной
            # стороне: при айсбергах с ОБЕИХ сторон разрядка первой не должна
            # оставаться висеть вместе с iceberg_side второй
            out["depleted"] = False
            out["break_dir"] = None
            out["note"] = f"пул {side} работает (CUSUM подтвердил) — уровень держат"
    if out.get("iceberg_side") and hid["bid"] and hid["ask"]:
        # обе стороны держат скрытые пулы — сторона срыва не определена
        both = all(len(hid[s]) >= 6 and page_cusum(
            hid[s], k, h)["alarms"] for s in ("bid", "ask"))
        if both:
            out["two_sided"] = True
            out["break_dir"] = None
            out["depleted"] = False
            out["note"] = ("скрытые пулы С ОБЕИХ сторон — сторона срыва не "
                           "определена, сигнала нет (NO DUMMIES)")
    return out


# ── self-test: детерминированная синтетика, реальные assert ─────────────────
if __name__ == "__main__":
    # Г1: y = x с запаздыванием 5 тактов → x ведёт (Ψ>0, |z|≥2), зеркально <0
    n, lag = 4096, 5
    t = np.arange(n + lag)
    base = (np.sin(2 * np.pi * t / 37.0) + 0.6 * np.sin(2 * np.pi * t / 11.0)
            + 0.3 * np.sin(2 * np.pi * t / 101.0))
    x = base[lag:]
    y = base[:-lag]                       # y отстаёт от x на lag
    r1 = phase_slope_index(x, y)
    assert r1["psi"] is not None and r1["psi"] > 0 and abs(r1["z"]) >= 2.0, r1
    assert r1["leader"] == "x→y", r1
    r2 = phase_slope_index(y, x)
    assert r2["psi"] < 0 and r2["leader"] == "y→x", r2
    # несвязанные ряды (разные несоизмеримые частоты) — лидер не назначается
    z1 = np.sin(2 * np.pi * np.arange(n) / 13.0)
    z2 = np.sin(2 * np.pi * np.arange(n) / 29.0)
    r3 = phase_slope_index(z1, z2)
    assert r3["leader"] == "не значимо", r3
    # мало данных → честный None (NO DUMMIES)
    assert phase_slope_index([1, 2, 3], [1, 2, 3])["psi"] is None
    # ВЫРОЖДЕНИЕ: одинаковые сегменты (нулевая джекнайф-дисперсия) не дают
    # фиктивной значимости — раньше пол 1e-30 рождал z порядка 1e15
    seg_rep = np.tile(np.sin(2 * np.pi * np.arange(SEG_LEN) / 16.0), 8)
    r_deg = phase_slope_index(seg_rep, np.roll(seg_rep, 3))
    assert r_deg["leader"] == "не значимо" or (r_deg["z"] is not None
                                               and abs(r_deg["z"]) < 1e6), r_deg
    r_zero = phase_slope_index(np.zeros(2048), np.zeros(2048))
    assert r_zero["leader"] == "не значимо", r_zero
    # детерминизм
    assert phase_slope_index(x, y) == phase_slope_index(x, y)

    # Г2: скрытый объём — исполнение сверх видимой убыли витрины
    assert hidden_volume(100, 50, 50) == 100      # витрина цела — всё скрытое
    assert hidden_volume(100, 150, 50) == 0       # чистая витрина, пула нет
    assert hidden_volume(100, 80, 60) == 80       # 20 видимых + 80 скрытых
    assert hidden_volume(0, 10, 5) == 0.0
    # CUSUM: копится на серии, не на одиночном принте
    c = page_cusum([10, 10, 10, 10], k=2.0, h=20.0)
    assert c["alarms"], c
    assert not page_cusum([10, 0, 0, 0, 0], k=2.0, h=20.0)["alarms"]
    assert page_cusum([], 1, 5) == {"alarms": [], "s_last": 0.0}

    # сценарий: bid-айсберг дозаправляется 10 раз, потом пул иссякает
    ev = ([{"side": "bid", "executed": 100, "disp_before": 40,
            "disp_after": 35} for _ in range(10)]        # скрытых ~95/удар
          + [{"side": "bid", "executed": 8, "disp_before": 40,
              "disp_after": 33} for _ in range(3)])      # поток иссяк
    r = latent_heat_scan(ev, price_pinned=True)
    assert r["iceberg_side"] == "bid" and r["depleted"] is True, r
    assert r["break_dir"] == "down", r                   # bid иссяк → вниз
    # пока пул работает — разрядки нет, уровень держат
    r_hold = latent_heat_scan(ev[:10], price_pinned=True)
    assert r_hold["iceberg_side"] == "bid" and r_hold["depleted"] is False
    # цена уже ушла от уровня → сигнал не выдаётся (поздно)
    r_gone = latent_heat_scan(ev, price_pinned=False)
    assert r_gone["break_dir"] is None
    # ask-зеркало
    ev_a = ([{"side": "ask", "executed": 90, "disp_before": 30,
              "disp_after": 28} for _ in range(8)]
            + [{"side": "ask", "executed": 5, "disp_before": 30,
                "disp_after": 26} for _ in range(3)])
    ra = latent_heat_scan(ev_a)
    assert ra["break_dir"] == "up", ra
    # чистая витрина без скрытого пула — тишина
    ev_c = [{"side": "bid", "executed": 30, "disp_before": 100,
             "disp_after": 70} for _ in range(12)]
    assert latent_heat_scan(ev_c)["iceberg_side"] is None
    # ДВУСТОРОННИЕ айсберги: направление срыва не определено — вердикт
    # обязан быть пустым, а не унаследованным от первой стороны
    ev_two = (ev[:10]                     # bid-пул работает
              + [{"side": "ask", "executed": 90, "disp_before": 30,
                  "disp_after": 28} for _ in range(8)]
              + [{"side": "bid", "executed": 6, "disp_before": 40,
                  "disp_after": 34} for _ in range(3)])   # bid иссяк
    r_two = latent_heat_scan(ev_two, price_pinned=True)
    assert r_two.get("two_sided") is True, r_two
    assert r_two["break_dir"] is None and r_two["depleted"] is False, r_two
    # пустота → не судим
    assert latent_heat_scan([])["break_dir"] is None

    print("rnd_micro self-test OK: PSI находит ведущего (x→y / y→x / "
          "не значимо), джекнайф-z работает; скрытая теплота — айсберг "
          "подтверждается CUSUM, разрядка даёт направление срыва, чистая "
          "витрина и ушедшая цена молчат; всё детерминировано")
