"""ЧЕСТНЫЙ НУЛЬ ДЛЯ СЦЕПКИ PLV — самый острый вопрос ко всему эфиру Пифии.

Продукт показывает PLV эфир↔цена до 0.965 и говорит «фазы сцеплены».
Возражение, которое надо закрыть или принять: ДВА ГЛАДКИХ УЗКОПОЛОСНЫХ
сигнала дают высокий PLV почти всегда. Тогда 0.965 — не связь, а свойство
гладкости, и вся сцепка декоративна.

Здесь строятся ЧЕТЫРЕ нуля, каждый убивает свою лазейку:

  N1 ЦИКЛИЧЕСКИЙ СДВИГ Ψ. Спектр Ψ сохранён полностью, временная привязка
     к цене разрушена. Если PLV не падает — привязки не было.
  N2 ФАЗОВЫЙ СУРРОГАТ Ψ (рандомизация фаз Фурье). Сохранён автоспектр,
     разрушена форма. Отделяет «связь» от «одинаковой полосы».
  N3 ЧУЖОЙ ЭФИР. Ψ другого инструмента (другой генезис, та же гладкость).
     Если своя волна не лучше чужой — генезис ничего не значит.
  N4 ГЛАДКИЙ ШУМ. Случайный ряд, отфильтрованный до полосы Ψ. Проверяет
     прямое утверждение «с любым гладким сигналом выйдет ~0.9».

Приговор — перцентиль настоящего PLV в каждом нуле. Ниже 95-го хотя бы в
одном → величина не заслуживает слова «сцепка».

python3 -m backend.lab.plv_null
"""
import json, math, os, sys
import numpy as np

from backend import aether

RNG = np.random.default_rng(20260731)
NSUR = 400


def plv_of(psi_ts, psi_vals, price_ts, price_close):
    """PLV ровно тем конвейером, что в продукте."""
    r = aether.couple_wave_price(psi_ts, psi_vals, price_ts, price_close)
    return None if not r else float(r["plv"])


def phase_surrogate(x, rng):
    """Рандомизация фаз Фурье: автоспектр сохранён, форма разрушена."""
    n = len(x)
    X = np.fft.rfft(x - x.mean())
    ph = rng.uniform(0, 2 * np.pi, len(X))
    ph[0] = 0.0
    if n % 2 == 0:
        ph[-1] = 0.0
    Y = np.abs(X) * np.exp(1j * ph)
    return np.fft.irfft(Y, n) + x.mean()


def smooth_noise(n, like, rng):
    """Случайный ряд, приведённый к полосе образца (тот же спектр по модулю)."""
    z = rng.standard_normal(n)
    Z = np.fft.rfft(z)
    L = np.abs(np.fft.rfft(like - like.mean()))
    Y = (Z / (np.abs(Z) + 1e-12)) * L
    return np.fft.irfft(Y, n)


def run_one(tk, ctx, others):
    w = ctx.get("wave") or {}
    sc = (ctx.get("scales") or {}).get("day") or {}
    psi_ts = np.asarray(w.get("ts") or [], float)
    psi_v = np.asarray(w.get("psi") or w.get("vals") or [], float)
    if psi_ts.size < 32 or psi_v.size != psi_ts.size:
        return None
    pt = np.asarray(ctx["_price_ts"], float)
    pc = np.asarray(ctx["_price_close"], float)
    real = plv_of(psi_ts, psi_v, pt, pc)
    if real is None:
        return None
    out = {"ticker": tk, "plv_real": real}

    # N1 циклический сдвиг
    v = []
    n = psi_v.size
    for k in range(1, min(NSUR, n - 1)):
        sh = int(k * n / min(NSUR, n - 1))
        p = plv_of(psi_ts, np.roll(psi_v, sh), pt, pc)
        if p is not None:
            v.append(p)
    out["N1_shift"] = v

    # N2 фазовый суррогат
    v = []
    for _ in range(NSUR // 2):
        p = plv_of(psi_ts, phase_surrogate(psi_v, RNG), pt, pc)
        if p is not None:
            v.append(p)
    out["N2_phase"] = v

    # N3 чужой эфир
    v = []
    for tk2, c2 in others.items():
        if tk2 == tk:
            continue
        w2 = c2.get("wave") or {}
        t2 = np.asarray(w2.get("ts") or [], float)
        v2 = np.asarray(w2.get("psi") or w2.get("vals") or [], float)
        if t2.size < 32 or v2.size != t2.size:
            continue
        vi = np.interp(psi_ts, t2, v2)
        p = plv_of(psi_ts, vi, pt, pc)
        if p is not None:
            v.append(p)
    out["N3_alien"] = v

    # N4 гладкий шум в полосе Ψ
    v = []
    for _ in range(NSUR // 2):
        p = plv_of(psi_ts, smooth_noise(psi_v.size, psi_v, RNG), pt, pc)
        if p is not None:
            v.append(p)
    out["N4_smooth"] = v
    return out


def pct(real, null):
    null = np.asarray(null, float)
    return float((null < real).mean() * 100.0) if null.size else float("nan")


def run(ctxs):
    print("=" * 92)
    print("ЧЕСТНЫЙ НУЛЬ ДЛЯ PLV ЭФИР↔ЦЕНА")
    print("=" * 92)
    res = []
    for tk, ctx in ctxs.items():
        r = run_one(tk, ctx, ctxs)
        if not r:
            print(f"{tk}: данных мало"); continue
        res.append(r)
        print(f"\n{tk}: PLV настоящий = {r['plv_real']:.4f}")
        for key, name in (("N1_shift", "цикл. сдвиг Ψ"),
                          ("N2_phase", "фазовый суррогат"),
                          ("N3_alien", "чужой эфир"),
                          ("N4_smooth", "гладкий шум")):
            v = np.asarray(r[key], float)
            if not v.size:
                print(f"   {name:20s}: нет"); continue
            print(f"   {name:20s}: n={v.size:4d}  медиана {np.median(v):.4f}  "
                  f"p95 {np.quantile(v,0.95):.4f}  макс {v.max():.4f}  → "
                  f"настоящий выше {pct(r['plv_real'], v):5.1f}% нуля")
    return res
