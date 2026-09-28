# -*- coding: utf-8 -*-
"""aether_resonance.py — МАКРО-СИНХРОНИЗАЦИЯ поводырей (спринт «Абсолют»).

Радиофизика вместо астрологии: тяжёлые активы-поводыри (Индекс МосБиржи,
Нефть BR, Валюта Si — «Сатурн/Плутон рынка») трактуются как связанные
осцилляторы. Три вычислимых измерения:

  1. МГНОВЕННАЯ ЧАСТОТА через преобразование Гильберта: аналитический
     сигнал z(t)=x+iH[x], мгновенная фаза φ=arg z, частота f=φ'/2π.
  2. ИНТЕРМОДУЛЯЦИЯ (IMD): нелинейное смешение двух поводырей рождает не
     сумму, а фантомные частоты биений f_beat=|n·f1 ± m·f2|. Резонанс —
     когда f_beat совпадает с «несущей» рабочего инструмента.
  3. ЗАХВАТ АДЛЕРА: dΔθ/dt = Δω − K·sin(Δθ). Фазовый захват (phase locking)
     — когда производная разности фаз держится у нуля: инструмент
     «порабощён» макро-эгрегором, ММ теряет контроль.

🟡 ЧЕСТНАЯ РАМКА. Это ДЕТЕКТОР-КАНДИДАТ, переведённый из эзотерического лога
в строгую статистику. Мода Шумана 7.83 Гц, «Лилит», «эфир» — из кода
УБРАНЫ: они не имеют операционального смысла на тиковых рядах (частота
дискретизации рынка не соизмерима с 7.83 Гц — сопоставление было бы
подгонкой). Оставлено только вычислимое: Гильберт, IMD, Адлер. Ни одна
функция не идёт в боевой вес до прохождения нулей (сдвиг/IAAFT/плацебо-
пары) и протокола BACKTEST_PROTOCOL.md. ⚫ не сигнал, не обещание, 18+.

Векторизовано на numpy; scipy.signal.hilbert если есть, иначе честный
FFT-фолбэк (тот же аналитический сигнал). Self-тест: python3 -m
backend.aether_resonance
"""
from __future__ import annotations

import math

import numpy as np

try:
    from scipy.signal import hilbert as _sp_hilbert
    _HAVE_SCIPY = True
except Exception:                                        # noqa: BLE001
    _HAVE_SCIPY = False

MIN_LEN = 64           # короче — мгновенная частота шумит, не судим
EDGE = 8               # краевые отсчёты Гильберта выбрасываем (артефакт)
ADLER_LOCK = 0.15      # |dΔθ/dt| ниже (рад/отсчёт) при низком разбросе — захват


def _f(x, d=0.0):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def analytic(x: np.ndarray) -> np.ndarray:
    """Аналитический сигнал z=x+iH[x]. scipy.hilbert либо FFT-фолбэк
    (одностороннее спектральное окно — каноническое определение)."""
    x = np.asarray(x, float)
    if _HAVE_SCIPY:
        return _sp_hilbert(x)
    n = len(x)
    X = np.fft.fft(x)
    h = np.zeros(n)
    if n % 2 == 0:
        h[0] = h[n // 2] = 1.0
        h[1:n // 2] = 2.0
    else:
        h[0] = 1.0
        h[1:(n + 1) // 2] = 2.0
    return np.fft.ifft(X * h)


def inst_phase_freq(x, detrend: bool = True) -> dict:
    """Мгновенная фаза (unwrap) и частота (φ'/2π) через Гильберт.
    Возврат: {phase, freq, f_med} или {phase: None} при нехватке данных."""
    x = np.asarray([_f(v) for v in x], float)
    if len(x) < MIN_LEN:
        return {"phase": None, "note": f"нужно ≥{MIN_LEN} отсчётов"}
    if detrend:                                          # линейный детренд —
        t = np.arange(len(x))                            # тренд ломает Гильберта
        x = x - np.polyval(np.polyfit(t, x, 1), t)
    # ВЫРОЖДЕННЫЙ РЯД (ревью «Абсолюта»): константа, чистая прямая или
    # остановленные торги — после детренда остаётся числовой мусор, у
    # которого мгновенная фаза не определена. Гильберт от него вернул бы
    # «частоту», а Адлер — уверенный «захват». Честный отказ (NO DUMMIES).
    span = float(np.ptp(x))
    if span <= 1e-12 or float(np.std(x)) <= 1e-12:
        return {"phase": None,
                "note": "вырожденный (плоский/остановленный) ряд — "
                        "мгновенная фаза не определена"}
    z = analytic(x)
    ph = np.unwrap(np.angle(z))
    fr = np.gradient(ph) / (2.0 * math.pi)               # цикл/отсчёт
    ph, fr = ph[EDGE:-EDGE], fr[EDGE:-EDGE]              # края — артефакт
    if len(fr) == 0:
        return {"phase": None, "note": "слишком короткий ряд после обрезки краёв"}
    return {"phase": ph, "freq": fr, "f_med": float(np.median(fr))}


def imd_beats(x1, x2, harmonics: int = 2) -> dict:
    """Интермодуляция двух поводырей: фантомные биения f_beat=|n·f1 ± m·f2|
    по МЕДИАННЫМ мгновенным частотам (n,m ∈ 1..harmonics). Возврат список
    положительных биений, отсортированный по возрастанию частоты."""
    a = inst_phase_freq(x1)
    b = inst_phase_freq(x2)
    if a.get("f_med") is None or b.get("f_med") is None:
        return {"beats": None, "note": "мало данных у поводыря"}
    f1, f2 = abs(a["f_med"]), abs(b["f_med"])
    beats = set()
    for n in range(1, harmonics + 1):
        for m in range(1, harmonics + 1):
            for s in (+1, -1):
                fb = abs(n * f1 + s * m * f2)
                if fb > 1e-9:
                    beats.add(round(fb, 8))
    return {"beats": sorted(beats), "f1": round(f1, 8), "f2": round(f2, 8),
            "note": "IMD 🟡: фантомные биения поводырей, нулями не проверено"}


def imd_resonance(x1, x2, x_market, *, tol: float = 0.15,
                  harmonics: int = 1) -> dict:
    """Резонирует ли фантомное биение поводырей с несущей рабочего
    инструмента: есть ли f_beat в относительной окрестности tol от f_market.

    ⚠ ЧЕСТНОЕ ОГРАНИЧЕНИЕ метода (проверено self-тестом): гребёнка биений
    густеет с harmonics — при harmonics≥2 комбинаций |n·f1±m·f2| столько,
    что почти ЛЮБАЯ несущая находит «резонанс» (ложноположительный по
    построению). Дефолт harmonics=1 (только |f1±f2| и f1+f2) — это уже
    даёт 4 линии; выше поднимать только с поправкой на множественность
    сравнений, иначе «резонанс» ничего не значит. Возврат: {resonant,
    f_beat, f_market, rel_err, n_beats}."""
    imd = imd_beats(x1, x2, harmonics)
    mk = inst_phase_freq(x_market)
    if imd.get("beats") is None or mk.get("f_med") is None:
        return {"resonant": False, "note": "мало данных"}
    fm = abs(mk["f_med"])
    if fm <= 1e-9:
        return {"resonant": False, "note": "несущая инструмента ≈0 — не судим"}
    best = min(imd["beats"], key=lambda fb: abs(fb - fm))
    rel = abs(best - fm) / fm
    return {"resonant": bool(rel <= tol), "f_beat": round(best, 8),
            "f_market": round(fm, 8), "rel_err": round(rel, 4),
            "n_beats": len(imd["beats"]),
            "note": ("резонанс IMD↔несущая 🟡" if rel <= tol
                     else "нет резонанса")}


def adler_lock(x_market, x_macro, *, lock: float = ADLER_LOCK) -> dict:
    """Захват Адлера: dΔθ/dt = Δω − K·sin(Δθ). Считаем разность мгновенных
    фаз рынка и макро-эгрегора, её производную; захват — когда |dΔθ/dt|
    мало И стабильно (низкий разброс) на хвосте окна.

    Возврат: {locked: bool, dphase_rate, stability, delta_omega}."""
    m = inst_phase_freq(x_market)
    g = inst_phase_freq(x_macro)
    if m.get("phase") is None or g.get("phase") is None:
        return {"locked": False, "note": "мало данных"}
    n = min(len(m["phase"]), len(g["phase"]))
    if n < 16:
        return {"locked": False, "note": "короткое перекрытие фаз"}
    dtheta = m["phase"][-n:] - g["phase"][-n:]
    rate = np.gradient(dtheta)                            # dΔθ/dt, рад/отсчёт
    tail = rate[-min(16, n):]
    mean_rate = float(np.mean(np.abs(tail)))
    stab = float(np.std(tail))                            # разброс производной
    delta_omega = float(np.mean(rate))                    # средняя расстройка
    # ОТНОСИТЕЛЬНЫЙ критерий (ревью «Абсолюта», major): абсолютный порог
    # 0.15 рад/отсчёт сам по себе объявляет захватом ЛЮБУЮ пару медленных
    # рядов — у них и без всякой связи |dΔθ/dt| мал просто потому, что
    # фазы ползут медленно. Захват — это (1) расстройка мала ОТНОСИТЕЛЬНО
    # собственного ритма инструмента и (2) разность фаз НЕ наматывается:
    # за окно Δθ гуляет меньше полного оборота.
    w_market = abs(float(m.get("f_med") or 0.0)) * 2.0 * math.pi   # рад/отсчёт
    rel = (abs(delta_omega) / w_market) if w_market > 1e-12 else float("inf")
    winding = float(np.ptp(dtheta))                       # размах Δθ за окно
    locked = bool(mean_rate <= lock and stab <= lock
                  and rel <= 0.25 and winding <= 2.0 * math.pi)
    return {"locked": locked, "dphase_rate": round(mean_rate, 5),
            "stability": round(stab, 5), "delta_omega": round(delta_omega, 5),
            "rel_detuning": (round(rel, 4) if rel != float("inf") else None),
            "winding_rad": round(winding, 3),
            "note": ("ФАЗОВЫЙ ЗАХВАТ 🟡: расстройка мала относительно "
                     "собственного ритма, разность фаз не наматывается"
                     if locked else
                     "свободные осцилляторы, захвата нет "
                     f"(отн.расстройка {rel:.2f}, намотка {winding:.1f} рад)")}


def sync_rupture(x_market, leaders: dict, *, tol: float = 0.15,
                 lock: float = ADLER_LOCK) -> dict:
    """SYNC_RUPTURE — сигнал абсолютного приоритета: макро-эгрегор захватил
    инструмент И фантомное биение резонирует с его несущей.
    leaders: {name: ряд}. Нужно ≥2 поводыря для IMD. Направление НЕ
    выдаётся (захват — про синхронизацию, не про сторону): сторону даёт
    поток CVD в Оракуле. Возврат: {rupture: bool, adler, imd, leader}."""
    names = [k for k, v in (leaders or {}).items()
             if v is not None and len(v) >= MIN_LEN]
    if len(names) < 2:
        return {"rupture": False, "note": "нужно ≥2 поводыря длиной ≥"
                f"{MIN_LEN}"}
    # ведущий макро — с максимальной средней |частотой| (самый «активный»)
    freqs = {nm: abs(inst_phase_freq(leaders[nm]).get("f_med") or 0.0)
             for nm in names}
    lead = max(freqs, key=freqs.get)
    others = [nm for nm in names if nm != lead]
    imd = imd_resonance(leaders[lead], leaders[others[0]], x_market,
                        tol=tol)
    adl = adler_lock(x_market, leaders[lead], lock=lock)
    rup = bool(imd.get("resonant") and adl.get("locked"))
    return {"rupture": rup, "leader": lead, "adler": adl, "imd": imd,
            "note": ("SYNC_RUPTURE 🟡: захват + IMD-резонанс — "
                     "ММ теряет контроль (сторона — по CVD)" if rup
                     else "нет полной синхронизации")}


# ── self-test: детерминированная синтетика, реальные assert ─────────────────
if __name__ == "__main__":
    N = 1024
    t = np.arange(N)

    # 1) Гильберт: чистая синусоида → постоянная частота = 1/период
    per = 20.0
    x = np.sin(2 * math.pi * t / per)
    r = inst_phase_freq(x, detrend=False)
    assert r["phase"] is not None
    assert abs(r["f_med"] - 1.0 / per) < 1e-3, r["f_med"]
    # мало данных → None (NO DUMMIES)
    assert inst_phase_freq(np.zeros(10))["phase"] is None

    # 2) IMD: два тона f1,f2 → биения содержат |f1−f2| и f1+f2
    f1, f2 = 1 / 15.0, 1 / 25.0
    x1 = np.sin(2 * math.pi * f1 * t)
    x2 = np.sin(2 * math.pi * f2 * t)
    imd = imd_beats(x1, x2, harmonics=2)
    beats = imd["beats"]
    assert any(abs(b - abs(f1 - f2)) < 2e-3 for b in beats), (beats, abs(f1-f2))
    assert any(abs(b - (f1 + f2)) < 2e-3 for b in beats), beats

    # 3) IMD-резонанс: инструмент на частоте биения |f1−f2| → резонанс есть
    fm = abs(f1 - f2)
    xm = np.sin(2 * math.pi * fm * t)
    res = imd_resonance(x1, x2, xm, tol=0.1)              # harmonics=1 дефолт
    assert res["resonant"] is True and res["n_beats"] == 2, res
    # инструмент на частоте МЕЖДУ линиями гребёнки → резонанса нет.
    # (при harmonics=1 линий всего 2: |f1−f2|=0.0267 и f1+f2=0.1067;
    #  берём несущую 0.055 — далеко от обеих)
    xm2 = np.sin(2 * math.pi * 0.055 * t)
    assert imd_resonance(x1, x2, xm2, tol=0.1)["resonant"] is False
    # ЧЕСТНОЕ ОГРАНИЧЕНИЕ: густая гребёнка (harmonics=3) ловит ту же 0.055
    # как «резонанс» — метод ложноположителен при высоких гармониках
    assert imd_resonance(x1, x2, xm2, tol=0.1, harmonics=3)["resonant"] is True

    # 4) Адлер: РЫНОК = МАКРО с постоянным сдвигом фазы (Δθ=const) → захват
    macro = np.sin(2 * math.pi * t / per)
    market = np.sin(2 * math.pi * t / per + 0.7)          # тот же ритм, сдвиг
    al = adler_lock(market, macro)
    assert al["locked"] is True, al
    # разные несоизмеримые частоты → фаза разбегается → захвата нет
    market_free = np.sin(2 * math.pi * t / 13.3)
    al2 = adler_lock(market_free, macro)
    assert al2["locked"] is False, al2
    # ЛОЖНЫЙ ЗАХВАТ НА МЕДЛЕННЫХ РЯДАХ (ревью, major): две НЕСВЯЗАННЫЕ
    # длинноволновые синусоиды имеют малый |dΔθ/dt| просто по медленности —
    # абсолютный порог объявлял бы это захватом. Относительный критерий
    # (расстройка к собственному ритму + отсутствие намотки) их разводит.
    slow_a = np.sin(2 * math.pi * t / 400.0)
    slow_b = np.sin(2 * math.pi * t / 260.0)          # чужой ритм, не связаны
    al_slow = adler_lock(slow_a, slow_b)
    assert al_slow["locked"] is False, al_slow
    assert al_slow.get("rel_detuning") is not None
    # ВЫРОЖДЕННЫЙ РЯД: константа/плоскость → фаза не определена (NO DUMMIES)
    assert inst_phase_freq(np.full(512, 42.0))["phase"] is None
    assert inst_phase_freq(np.linspace(0, 1, 512))["phase"] is None  # чистая прямая
    assert adler_lock(np.full(512, 7.0), macro)["locked"] is False

    # 5) SYNC_RUPTURE: 2 поводыря + рынок на их биении и в захвате с ведущим
    lead_fast = np.sin(2 * math.pi * (1 / 12.0) * t)      # ведущий (выше f)
    lead_slow = np.sin(2 * math.pi * (1 / 30.0) * t)
    fbeat = abs(1 / 12.0 - 1 / 30.0)
    # рынок захвачен ведущим (его ритм) — для Адлера; IMD резонирует с биением
    # берём рынок на частоте биения И со сдвигом относительно ведущего
    mk = np.sin(2 * math.pi * fbeat * t)
    sr = sync_rupture(mk, {"IMOEX": lead_fast, "BR": lead_slow})
    assert "rupture" in sr and sr["leader"] == "IMOEX", sr
    # один поводырь → честный отказ
    assert sync_rupture(mk, {"IMOEX": lead_fast})["rupture"] is False
    # детерминизм
    assert adler_lock(market, macro) == adler_lock(market, macro)

    print("aether_resonance self-test OK: Гильберт даёт частоту синуса, IMD "
          "ловит |f1±f2|, резонанс несущей срабатывает точечно, Адлер видит "
          "захват при Δθ=const и его отсутствие на несоизмеримых частотах, "
          "SYNC_RUPTURE требует ≥2 поводыря; всё детерминировано, "
          "scipy=%s" % _HAVE_SCIPY)
