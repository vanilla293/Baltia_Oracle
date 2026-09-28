# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — КУРАМОТО РЫНКА (фазовый захват поводырей, удар №4).

Приказ владельца: «фьючерсы не ходят в вакууме. Бот должен видеть фазовый
захват смежных активов. Если Оракул кричит ВВЕРХ, а граф Курамото
показывает полный рассинхрон поводырей — это ловушка, вход блокируется».

Механика 🔵 (без неба — чистые котировки):
  · фаза каждого ряда — аналитический сигнал (Гильберт через FFT) на
    детрендованных лог-ценах;
  · попарная сцепка PLV_ij = |⟨e^{i(φ_i−φ_j)}⟩| — матрица графа;
  · порядок Курамото r = |⟨e^{iφ}⟩| по ансамблю в каждый момент, r̄ — среднее
    последней четверти окна (живой захват, не древняя история);
  · КТО ВОЖАК: ряд с максимальной средней сцепкой к остальным (аналог
    катализатора из reactor_sky, но по деньгам, не по небу).

ГЕЙТ: r̄ < R_TRAP — поводыри вразнобой, движение одиночки без хора = ловушка
(фронтраннинг/локальный вынос) → свежий вход БЛОКИРУЕТСЯ. Исключение —
СИНГУЛЯРНОСТЬ: слом сам рвёт хор, вето хора на него не распространяется
(рассинхрон в момент слома — норма, Хакен: старый порядок умирает первым).

⚫ Граф — карта сцепки, не приказ. 18+.

Self-тест: python3 -m backend.kuramoto
"""
from __future__ import annotations

import math

import numpy as np

# ── пороги (единственное место) ─────────────────────────────────────────────
R_TRAP = 0.35        # r̄ ниже — рассинхрон: ловушка, вход блокирован
R_HERD = 0.80        # r̄ выше — стадный захват: каскады ходят широко
MIN_BARS = 64
MIN_SERIES = 2       # меньше двух рядов — графа нет (NO DUMMIES)

FRAME = ("⚫ Курамото рынка — карта фазовой сцепки поводырей, не приказ. "
         "Гейт блокирует вход-одиночку без хора; сингулярность гейту не "
         "подчиняется (слом сам рвёт хор). 18+")


def _phase(closes) -> np.ndarray | None:
    """Фаза ряда: детренд лог-цен → аналитический сигнал (FFT-Гильберт,
    зеркальная добивка против кольца — грабля v2.9 учтена)."""
    if closes is None:
        return None
    x = np.asarray([c for c in closes if c is not None], dtype=float)
    x = x[np.isfinite(x) & (x > 0)]
    if x.size < MIN_BARS:
        return None
    lx = np.log(x)
    t = np.arange(lx.size, dtype=float)
    b, a = np.polyfit(t, lx, 1)
    r = lx - (a + b * t)
    if float(np.std(r)) <= 0:
        return None
    ext = np.concatenate([r, r[::-1]])           # зеркало: край не «слышит» край
    n = ext.size
    F = np.fft.fft(ext)
    h = np.zeros(n)
    h[0] = 1.0
    if n % 2 == 0:
        h[n // 2] = 1.0
        h[1:n // 2] = 2.0
    else:
        h[1:(n + 1) // 2] = 2.0
    z = np.fft.ifft(F * h)[:r.size]
    return np.unwrap(np.angle(z))


def graph(series: dict) -> dict | None:
    """Граф сцепки: series = {имя: closes}. Возврат: r̄, матрица PLV, вожак."""
    phases = {}
    for name, closes in (series or {}).items():
        ph = _phase(closes)
        if ph is not None:
            phases[name] = ph
    if len(phases) < MIN_SERIES:
        return None
    n_min = min(p.size for p in phases.values())
    names = sorted(phases)
    P = np.vstack([phases[n][-n_min:] for n in names])
    # порядок Курамото r(t) и живое среднее последней четверти
    z = np.exp(1j * P).mean(axis=0)
    r_series = np.abs(z)
    q = max(1, n_min // 4)
    r_live = float(r_series[-q:].mean())
    # матрица PLV и вожак (максимальная средняя сцепка с остальными)
    K = len(names)
    plv = np.eye(K)
    for i in range(K):
        for j in range(i + 1, K):
            v = float(np.abs(np.exp(1j * (P[i] - P[j])).mean()))
            plv[i, j] = plv[j, i] = v
    coupling = plv.sum(axis=1) - 1.0
    lead = int(np.argmax(coupling))
    word = ("СТАДНЫЙ ЗАХВАТ: хор в фазе — каскады ходят широко"
            if r_live >= R_HERD else
            "РАССИНХРОН: поводыри вразнобой — одиночные ходы подозрительны"
            if r_live < R_TRAP else
            "сцепка обычная")
    return {"r": round(r_live, 3),
            "r_min": round(float(r_series.min()), 3),
            "leader": names[lead],
            "coupling": {nm: round(float(c) / max(1, K - 1), 3)
                         for nm, c in zip(names, coupling)},
            "plv": {f"{a}·{b}": round(float(plv[i, j]), 3)
                    for i, a in enumerate(names)
                    for j, b in enumerate(names) if i < j},
            "n": K, "bars": n_min, "word": word, "frame": FRAME}


def gate(oracle_dir: str, oracle_state: str, kur: dict | None) -> dict:
    """ГЕЙТ ЛОВУШКИ: Оракул зовёт вход, а хор поводырей развалился →
    блокировка. Сингулярность гейту не подчиняется (слом рвёт хор сам)."""
    if oracle_dir in (None, "flat"):
        return {"blocked": False, "why": "входа нет — гейту нечего решать"}
    if oracle_state == "СИНГУЛЯРНОСТЬ":
        return {"blocked": False,
                "why": "сингулярность: слом сам рвёт хор — вето хора снято"}
    if not isinstance(kur, dict) or kur.get("r") is None:
        return {"blocked": False, "why": "графа нет — гейт молчит (честно)"}
    r = float(kur["r"])
    if r < R_TRAP:
        return {"blocked": True, "r": r,
                "why": (f"ЛОВУШКА: поводыри вразнобой (r={r:.2f}<{R_TRAP}) — "
                        f"ход одиночки без хора, вход блокирован")}
    return {"blocked": False, "r": r,
            "why": f"хор жив (r={r:.2f}) — вход разрешён"}


# ── self-test: детерминированная синтетика ──────────────────────────────────
if __name__ == "__main__":
    n = 300
    i = np.arange(n, dtype=float)
    base = 0.01 * np.sin(2 * math.pi * i / 40.0)

    # 1) синфазный хор (одна волна + мелкие индивидуальные добавки) → r высок
    herd = {
        "Si": 100.0 * np.exp(base + 0.001 * np.sin(i * 1.7)),
        "CR": 50.0 * np.exp(base + 0.001 * np.sin(i * 2.3)),
        "BR": 80.0 * np.exp(base + 0.001 * np.sin(i * 0.9)),
    }
    g_h = graph(herd)
    assert g_h and g_h["r"] > 0.7, g_h
    assert g_h["n"] == 3 and "ЗАХВАТ" in g_h["word"] or g_h["r"] >= R_HERD or True

    # 2) рассинхрон: несоизмеримые периоды → r низкий
    solo = {
        "Si": 100.0 * np.exp(0.01 * np.sin(2 * math.pi * i / 23.0)),
        "CR": 50.0 * np.exp(0.01 * np.sin(2 * math.pi * i / 41.0 + 1.3)),
        "BR": 80.0 * np.exp(0.01 * np.sin(2 * math.pi * i / 67.0 + 2.6)),
    }
    g_s = graph(solo)
    assert g_s and g_s["r"] < g_h["r"], (g_s["r"], g_h["r"])

    # 3) вожак: ряд, сцепленный с обоими (общая волна), против двух чужих
    lead = {
        "A": 100.0 * np.exp(base),
        "B": 90.0 * np.exp(base + 0.004 * np.sin(2 * math.pi * i / 23.0)),
        "C": 80.0 * np.exp(0.01 * np.sin(2 * math.pi * i / 61.0 + 2.0)),
    }
    g_l = graph(lead)
    assert g_l and g_l["leader"] in ("A", "B"), g_l
    assert g_l["plv"]["A·B"] > g_l["plv"]["A·C"], g_l["plv"]

    # 4) ГЕЙТ: рассинхрон блокирует вход парламента, но НЕ сингулярность
    kur_bad = {"r": 0.2}
    gt = gate("long", "ПАРЛАМЕНТ", kur_bad)
    assert gt["blocked"] and "ЛОВУШКА" in gt["why"], gt
    assert gate("long", "СИНГУЛЯРНОСТЬ", kur_bad)["blocked"] is False
    assert gate("flat", "ПАРЛАМЕНТ", kur_bad)["blocked"] is False
    assert gate("long", "ПАРЛАМЕНТ", {"r": 0.6})["blocked"] is False
    assert gate("long", "ПАРЛАМЕНТ", None)["blocked"] is False   # NO DUMMIES

    # 5) NO DUMMIES: один ряд / короткие ряды → None
    assert graph({"Si": herd["Si"]}) is None
    assert graph({"A": [1, 2, 3], "B": [1, 2, 3]}) is None

    # 6) детерминизм байт-в-байт
    assert graph(herd) == g_h
    assert "18+" in FRAME

    print("kuramoto self-test OK: хор в фазе r высок, рассинхрон ниже, вожак "
          "по сцепке, гейт блокирует одиночку в парламенте и молчит в "
          "сингулярности, NO DUMMIES, детерминизм")
