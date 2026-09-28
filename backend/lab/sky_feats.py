"""НЕБЕСНЫЕ ПРИЗНАКИ ДНЯ — один раз считаем, дальше стенды их гоняют.

Всё из локального de440.bsp, офлайн. Признаки нарочно разделены на два
класса, потому что в этом весь вопрос:

  ЗЕМНЫЕ ЧАСЫ (заведомо календарь): долгота Солнца, фаза Луны, склонение
    Солнца. Их предсказательная сила — это сезон, никакой «силы неба».
  СОБСТВЕННО НЕБЕСНЫЕ: аспекты пар планет, скорости (станции), эфирная
    плотность E_i, скалярное напряжение, объём симплекса. Вот они и есть
    предмет спора.
"""
import math, os
import numpy as np
from datetime import date, datetime, timezone

BODIES = ["Меркурий", "Венера", "Марс", "Юпитер", "Сатурн", "Уран",
          "Нептун", "Плутон"]
_EPH = None
_TS = None


def _load():
    global _EPH, _TS
    if _EPH is None:
        from skyfield.api import load, load_file
        here = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        p = os.path.join(here, "de440.bsp")
        _EPH = load_file(p)
        _TS = load.timescale()
    return _EPH, _TS


_KEY = {"Солнце": "sun", "Луна": "moon", "Меркурий": "mercury",
        "Венера": "venus", "Марс": "mars barycenter",
        "Юпитер": "jupiter barycenter", "Сатурн": "saturn barycenter",
        "Уран": "uranus barycenter", "Нептун": "neptune barycenter",
        "Плутон": "pluto barycenter"}


def longitudes(dates):
    """{тело: массив эклиптических долгот, °} по видимым положениям."""
    eph, ts = _load()
    t = ts.utc([d.year for d in dates], [d.month for d in dates],
               [d.day for d in dates], 12)
    earth = eph["earth"]
    out = {}
    for nm, key in _KEY.items():
        ast = earth.at(t).observe(eph[key]).apparent()
        lam, beta, dist = ast.frame_latlon(__import__(
            "skyfield.framelib", fromlist=["ecliptic_frame"]).ecliptic_frame)
        out[nm] = np.asarray(lam.degrees, float)
        out[nm + "_dist"] = np.asarray(dist.au, float)
        out[nm + "_beta"] = np.asarray(beta.degrees, float)
    return out


def wrap180(x):
    return (np.asarray(x) + 180.0) % 360.0 - 180.0


def build(dates):
    """Матрица признаков дня. Возвращает (имена, X)."""
    L = longitudes(dates)
    names, cols = [], []

    # ── земные часы ──
    sun = L["Солнце"]
    names += ["часы:долгота_Солнца_cos", "часы:долгота_Солнца_sin"]
    cols += [np.cos(np.radians(sun)), np.sin(np.radians(sun))]
    elong = wrap180(L["Луна"] - sun)
    names += ["часы:фаза_Луны_cos", "часы:фаза_Луны_sin"]
    cols += [np.cos(np.radians(elong)), np.sin(np.radians(elong))]
    names += ["часы:полугодовая"]
    ordv = np.array([d.toordinal() for d in dates], float)
    cols += [np.cos(2 * np.pi * ordv / 182.625)]

    # ── аспекты пар (гармоники 1,2,3,4,6) ──
    allb = ["Солнце", "Луна"] + BODIES
    for i in range(len(allb)):
        for j in range(i + 1, len(allb)):
            a, b = allb[i], allb[j]
            d = wrap180(L[a] - L[b])
            for k in (1, 2, 3, 4, 6):
                names.append(f"аспект:{a}-{b}·k{k}")
                cols.append(np.cos(np.radians(k * d)))

    # ── скорости и станции ──
    for nm in BODIES:
        v = np.gradient(np.unwrap(np.radians(L[nm])))
        v = np.degrees(v)
        names.append(f"станция:{nm}")
        cols.append(np.minimum(50.0, 1.0 / (np.abs(v) + 0.02)))

    # ── эфирная плотность (веса школы) ──
    W = {"Плутон": 108, "Сатурн": 108, "Уран": 108, "Юпитер": 54,
         "Венера": 54, "Луна": 27, "Меркурий": 27, "Марс": 72,
         "Солнце": 54, "Нептун": 81}
    R = {"Солнце": 696000.0, "Луна": 1737.4, "Меркурий": 2439.7,
         "Венера": 6051.8, "Марс": 3389.5, "Юпитер": 69911.0,
         "Сатурн": 58232.0, "Уран": 25362.0, "Нептун": 24622.0,
         "Плутон": 1188.3}
    AU = 1.495978707e8
    E = {}
    for nm in W:
        dist_km = L[nm + "_dist"] * AU
        theta = np.degrees(2 * np.arcsin(np.clip(R[nm] / dist_km, -1, 1)))
        v = np.degrees(np.gradient(np.unwrap(np.radians(L[nm]))))
        chrono = np.minimum(50.0, 1.0 / (np.abs(v) + 0.02))
        E[nm] = theta * W[nm] * chrono
        names.append(f"эфир:E_{nm}")
        cols.append(E[nm])

    # ── скалярное напряжение (оппозиции) ──
    S = np.zeros(len(dates))
    for i in range(len(allb)):
        for j in range(i + 1, len(allb)):
            a, b = allb[i], allb[j]
            sep = np.abs(wrap180(L[a] - L[b]))
            S += np.sqrt(E[a] * E[b]) * np.exp(-((180.0 - sep) ** 2) / (2 * 36.0))
    names.append("эфир:скалярное_напряжение")
    cols.append(S)

    X = np.column_stack(cols)
    # нормировка: каждый признак к нулю-единице (иначе E_i раздавит остальные)
    X = (X - X.mean(0)) / (X.std(0) + 1e-12)
    return names, X


if __name__ == "__main__":
    ds = [date(2020, 1, 1) + __import__("datetime").timedelta(i)
          for i in range(400)]
    nm, X = build(ds)
    assert X.shape == (400, len(nm)), "форма матрицы"
    assert np.isfinite(X).all(), "NaN в признаках"
    i = nm.index("часы:полугодовая")
    assert abs(np.corrcoef(X[:, i], np.cos(2 * np.pi *
               np.array([d.toordinal() for d in ds]) / 182.625))[0, 1]) > 0.99
    print(f"признаков {len(nm)}, дней {len(ds)} — self-тест OK")
    print("примеры:", nm[:3], "…", nm[-3:])
