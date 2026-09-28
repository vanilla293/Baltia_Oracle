"""ОРАКУЛ // ПИФИЯ — СТВОРЫ: будущие окна фазы (веер v3.0).

Створ — момент на форвард-сетке волны, где сходятся сразу: насыщение фазы
(|2Ψ−1|), узел Дирака рядом и проекция у уровня (плита/квантиль/край
полости). Владелец читает их как свои будущие окна — выдача остаётся
языком школы: «створ/окно фазы» — состояние поля, НЕ приказ ⚫.
Слабая сцепка (PLV) честно глушит всю шкалу створов. Всё детерминировано.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

from .aether import LOUD_THRESHOLD, PLV_LOCK

# именованные веса и пороги (по образу FIELD_W)
W_AMP, W_NODE, W_LEVEL = 0.40, 0.35, 0.25
W_TENSION = 0.15      # буст только СТАРТА при текущем версорном натяжении
GATE_MIN = 0.55
LEVEL_FRAC = 0.10     # e-складка близости уровня = 10% размаха окна 🟡
N_GATES_MAX = 4
MIN_GAP_FAST = 600.0     # 1м: разнос створов ≥ 10 мин
MIN_GAP_SLOW = 1800.0    # 5м и старше: ≥ 30 мин
MIN_GAP_FAR = 86400.0    # дальние: ≥ сутки
N_FAR_MAX = 5
FAR_SIGMA_MIN = 360.0    # мин; узел не проваливается между узлами 6ч-сетки


def gate_scores(t, psi, center, plv, nodes, levels, span,
                tension_now: bool = False, period_h: float = 24.0,
                min_gap: float = MIN_GAP_SLOW,
                n_max: int = N_GATES_MAX,
                sigma_floor_min: float = 0.0):
    """→ (индексы створов по времени, score-серия). Краевые случаи честны:
    нет center/уровней → член (в) выпадает и trust=0.5; нет узлов → S_node=0."""
    t = np.asarray(t, float)
    psi = np.asarray(psi, float)
    amp = np.abs(2.0 * psi - 1.0)
    S_amp = np.clip(amp / (2.0 * LOUD_THRESHOLD), 0.0, 1.0)
    S_node = np.zeros_like(t)
    for wnd in (nodes or []):
        sig = max(float(wnd.get("sigma_min", 0.0)), sigma_floor_min) * 60.0
        if sig <= 0:
            continue
        S_node = np.maximum(S_node, np.exp(
            -((t - float(wnd["t_exact"])) ** 2) / (2.0 * sig * sig)))
    if center is not None and levels:
        c = np.asarray(center, float)
        L = min(c.size, t.size)
        lv = np.asarray(levels, float)
        d = np.min(np.abs(c[:L, None] - lv[None, :]), axis=1)
        S_lvl = np.zeros_like(t)
        S_lvl[:L] = np.exp(-d / (LEVEL_FRAC * max(float(span), 1e-9)))
        base = W_AMP * S_amp + W_NODE * S_node + W_LEVEL * S_lvl
        trust = float(np.clip((plv or 0.0) / PLV_LOCK, 0.0, 1.0))
    else:
        base = (W_AMP * S_amp + W_NODE * S_node) / (W_AMP + W_NODE)
        trust = 0.5    # без ценового подтверждения шкала придавлена честно
    mod = 1.0 + (W_TENSION if tension_now else 0.0) * np.exp(
        -(t - t[0]) / max(period_h * 1800.0, 1.0))
    score = np.clip(trust * mod * base, 0.0, 1.0)
    peaks = [i for i in range(1, len(t) - 1)
             if score[i] >= GATE_MIN and score[i] >= score[i - 1]
             and score[i] > score[i + 1]]
    peaks.sort(key=lambda i: -score[i])
    keep = []
    for i in peaks:
        if all(abs(t[i] - t[j]) >= min_gap for j in keep):
            keep.append(i)
        if len(keep) >= n_max:
            break
    keep.sort()
    return keep, score


def _build(t, psi, score, keep, level_items, center, when_fmt: str) -> list:
    out = []
    c = np.asarray(center, float) if center is not None else None
    for i in keep:
        kind = "гребневой" if psi[i] >= 0.5 else "впадинный"
        tgt = lbl = None
        if c is not None and level_items and i < c.size:
            j = min(range(len(level_items)),
                    key=lambda j: abs(level_items[j][0] - float(c[i])))
            tgt, lbl = level_items[j]
        out.append({"t": int(t[i]),
                    "when": datetime.fromtimestamp(
                        float(t[i]), timezone.utc).strftime(when_fmt),
                    "score": round(float(score[i]), 3), "kind": kind,
                    "target": (round(float(tgt), 4) if tgt is not None else None),
                    "label": lbl,
                    "word": ("створ фазы: " + kind
                             + (f" у {lbl} {round(float(tgt), 4)}"
                                if tgt is not None else "")
                             + " — окно поля, не приказ")})
    return out


def gates_of(w_ts, psi, k_now: int, center, plv, nodes, level_items,
             span: float, tension_now: bool, period_h: float,
             interval: str, frac01=None, chaos=None) -> list:
    """Ближние створы на форвард-сетке тонкой волны.
    v3.1 (аудит №3): frac01 — фрактальный резонанс масштабов; спор масштабов
    честно придавливает шкалу (сигнал истинен при сложенной матрёшке).
    chaos — окна Чирикова: створ внутри перекрытия резонансов помечается —
    уровень в хаос-окне ненадёжен."""
    t = np.asarray(w_ts, float)[k_now:]
    p = np.asarray(psi, float)[k_now:]
    if t.size < 8:
        return []
    min_gap = MIN_GAP_FAST if interval == "1m" else MIN_GAP_SLOW
    keep, score = gate_scores(
        t, p, center, plv, nodes, [x[0] for x in (level_items or [])],
        span, tension_now, period_h, min_gap)
    if frac01 is not None:
        score = score * (0.6 + 0.4 * float(frac01))
        keep = [i for i in keep if score[i] >= GATE_MIN]
    out = _build(t, p, score, keep, level_items, center, "%d.%m %H:%M")
    for g in out:
        for cw in (chaos or []):
            if cw["t0"] - 1800 <= g["t"] <= cw["t1"] + 1800:
                g["chaos"] = True
                g["word"] += " · внутри окна хаоса (перекрытие резонансов)"
                break
    return out


def far_gates(w_ts, psi, k_now: int, center, plv, nodes,
              level_items, span: float) -> list:
    """Дальние окна фазы (длинное окно ±30 дней): top-5, разнос ≥ сутки;
    текущее натяжение на недели не тянется (mod≡1)."""
    t = np.asarray(w_ts, float)[k_now:]
    p = np.asarray(psi, float)[k_now:]
    if t.size < 8:
        return []
    keep, score = gate_scores(
        t, p, center, plv, nodes, [x[0] for x in (level_items or [])],
        span, False, 24.0, MIN_GAP_FAR, N_FAR_MAX, FAR_SIGMA_MIN)
    return _build(t, p, score, keep, level_items, center, "%d.%m")


if __name__ == "__main__":
    t0 = 1_800_000_000.0
    t = t0 + np.arange(96) * 1800.0
    psi = 0.5 + 0.45 * np.sin(2 * np.pi * (t - t0) / (24 * 3600) - np.pi / 2)
    ic = int(np.argmax(psi))
    center = 100.0 + 8.0 * (2.0 * psi - 1.0)
    lv = [(107.2, "Э⚡"), (95.0, "квантиль"), (100.0, "квантиль")]
    span = 20.0
    node = [{"t_exact": float(t[ic]), "sigma_min": 90}]

    # Т1: гребень + узел + плита у проекции → створ именно там, гребневой
    k1, s1 = gate_scores(t, psi, center, 0.9, node, [x[0] for x in lv], span)
    assert k1 and any(abs(t[i] - t[ic]) <= 1800.0 for i in k1), (k1, ic)
    assert max(k1, key=lambda i: s1[i]) == ic     # узел делает гребень главным
    g1 = _build(t, psi, s1, k1, lv, center, "%d.%m %H:%M")
    gt = next(g for g, i in zip(g1, k1) if i == ic)
    assert gt["kind"] == "гребневой" and gt["target"] == 107.2, gt

    # Т2: без узла score на гребне ниже — узел реально добавляет
    _, s2 = gate_scores(t, psi, center, 0.9, [], [x[0] for x in lv], span)
    assert s2[ic] < s1[ic] - 0.15, (s1[ic], s2[ic])
    assert GATE_MIN <= s1[ic] <= 1.0

    # Т3: слабый PLV глушит всю шкалу — створов нет
    k3, s3 = gate_scores(t, psi, center, 0.15, node, [x[0] for x in lv], span)
    assert float(s3.max()) < GATE_MIN and k3 == [], (float(s3.max()), k3)

    # Т4: два узла через 15 мин → створы с разносом ≥ 30 мин, не спам
    nn = node + [{"t_exact": float(t[ic]) + 900.0, "sigma_min": 90}]
    k4, _ = gate_scores(t, psi, center, 0.9, nn, [x[0] for x in lv], span)
    ts4 = sorted(float(t[i]) for i in k4)
    assert all(b - a >= 1800.0 for a, b in zip(ts4, ts4[1:]))
    assert len(k4) <= N_GATES_MAX

    # Т5: текущее натяжение поднимает только старт, хвост не трогает
    _, s5 = gate_scores(t, psi, center, 0.9, node, [x[0] for x in lv], span,
                        tension_now=True, period_h=24.0)
    assert s5[1] > s2[1] and abs(float(s5[-1]) - float(s2[-1])) < 0.02

    # Т6: детерминизм байт-в-байт
    k6, s6 = gate_scores(t, psi, center, 0.9, node, [x[0] for x in lv], span)
    assert k6 == k1 and np.array_equal(s6, s1)

    # Т7: дальние — разнос ≥ сутки, top-5, формат дня
    tL = t0 + np.arange(120) * 21600.0
    psiL = 0.5 + 0.45 * np.sin(2 * np.pi * (tL - t0) / (9 * 86400))
    gL = far_gates(tL, psiL, 0, 100.0 + 8.0 * (2 * psiL - 1.0), 0.85,
                   [{"t_exact": float(tL[30]), "sigma_min": 120}], lv, span)
    tsL = sorted(g["t"] for g in gL)
    assert len(gL) <= N_FAR_MAX
    assert all(b - a >= MIN_GAP_FAR for a, b in zip(tsL, tsL[1:]))
    assert all(len(g["when"]) == 5 for g in gL)   # "%d.%m"

    # Т8: NO DUMMIES — нет center/уровней: шкала придавлена (trust 0.5)
    _, s8 = gate_scores(t, psi, None, 0.9, node, [], span)
    assert float(s8.max()) <= 0.5 * 1.0 + 1e-9

    # запрет торговых слов
    blob = " ".join(g["word"] for g in g1 + gL).lower()
    for bad in ("покупай", "продавай", "лонг", "шорт", "buy", "sell"):
        assert bad not in blob, bad
    print("gates: все self-тесты пройдены ✓")
