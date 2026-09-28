"""ВЕЙВЛЕТ ПО БЫСТРЫМ ТЕЛАМ — проверка директивы «линейные методы слепы».

ГИПОТЕЗА ВЛАДЕЛЬЦА. Фурье, Гильберт, Пирсон и PLV усредняют по всему ряду.
Если быстрое тело (Луна, Меркурий, Венера, Марс) звучит не всегда, а
короткими эпизодами, то средняя связь размажется в ноль, и линейный метод
честно скажет «ничего нет» — хотя эпизоды есть. Непрерывное вейвлет-
преобразование даёт карту (время × масштаб) и способно эти эпизоды
показать. Проверяем это ЧИСЛОМ, а не рассуждением.

ЧТО СЧИТАЕМ.

  1. Ψ по группам тел (механика — backend.lab.psi_split.psi_bodies, то же
     ядро, что и во всей охоте): только Луна · только Меркурий · только
     Венера · только Марс · медленные (Юпитер+Сатурн+Уран+Нептун+Плутон).
     Медленная группа здесь — КОНТРОЛЬ: линейные методы уже показали, что
     живёт только огибающая E(t) Юпитера и Сатурна.

  2. Отклик: дневные лог-доходности 8 бумаг MOEX; отдельно дневные заказы
     16 пиццерий.

  3. Локальная вейвлет-когерентность R²(s,t) (Torrence & Webster 1999,
     реализация — backend.lab.morlet.wavelet_coherence) на масштабах,
     отвечающих периодам тела. Внутри конуса влияния — NaN, эти точки не
     участвуют НИГДЕ: ни в пороге, ни в доле, ни в сериях.

  4. НУЛЬ ЛОКАЛЬНЫЙ. Суррогаты IAAFT НЕБЕСНОГО ряда (backend.lab.
     e_surrogate.iaaft): тот же спектр мощности И то же распределение
     значений, разрушена только временная привязка к отклику. Отклик не
     трогаем вообще — значит вся его структура (тренд, неделя, год) в
     нуле и в опыте одна и та же и сократится.

  5. СТАТИСТИКА — ПЕРЕМЕЖАЕМОСТЬ, А НЕ СРЕДНИЙ УРОВЕНЬ.
       порог θ = 95-й перцентиль ПУЛА нулевых значений R² на этой полосе
                 (по всем суррогатам и всем допустимым моментам);
       доля    = доля допустимого времени, где R² > θ;
       серия   = самая длинная непрерывная серия R² > θ, в сутках;
       эпизоды = доля времени внутри серий длиной ≥ период тела.
     У нуля средняя доля равна 0.05 ПО ПОСТРОЕНИЮ — это внутренняя
     проверка, она стоит в self-тесте. p = (1+#{нуль ≥ опыт})/(N+1).

  6. СУРРОГАТЫ СПАРЕНЫ ПО ОБЪЕКТАМ. У всех 8 бумаг (и всех 16 пиццерий)
     небо почти одно и то же, а отклики держит общий фактор — значит их
     доли скоррелированы. Если бы каждый объект получил СВОЙ независимый
     суррогат, медиана нуля по объектам сжалась бы как 1/√K, и любая
     ненулевая настоящая медиана дала бы крошечное p. Измерено на стенде
     без всякой связи: спаренный нуль даёт 3.3 % ложных срабатываний
     (норма 5 %), НЕспаренный — 20 %, завышение в 6 раз. Поэтому суррогат
     строится на ОБЩЕЙ суточной сетке с зерном (группа, номер) и лишь
     потом прореживается под даты объекта. Плюс к сводной таблице всегда
     печатается СОБСТВЕННЫЙ p каждого объекта — он точен независимо от
     любых предположений о связях между объектами.

🔵 МЕХАНИКА: вейвлет, конус, суррогаты, доли и серии — проверяемая
   цифровая обработка, все ответы self-теста известны аналитически.
🟡 ЯЗЫК ШКОЛЫ: сама Ψ (аспектные веса k, веда-полосы W, сродство 1.5,
   выбор натальной карты инструмента) — символическая система, не закон
   природы. Прокси, которые названы прямо:
     · торговый календарь MOEX неравномерен (выходные): шаг сетки взят
       СРЕДНИЙ, dt = (последняя дата − первая)/(бары−1) ≈ 1.4536 сут.
       Небо и отклик сэмплированы ОДНИМИ И ТЕМИ ЖЕ датами, поэтому
       искажение общее и на когерентность строки не влияет, но подпись
       периода в сутках — приближение;
     · «нота» пиццерии (band) взята как в resto/backend/sky.store_band —
       веда-полоса тела с максимальным E в момент генезиса. На группы из
       одного тела band не влияет вообще: он входит положительным
       множителем, а R² к положительному множителю нечувствительна —
       это доказано ассертом в self-тесте.
⚫ Это измерение, а не сигнал, не прогноз и не приказ. Никакой торговли.

python3 -m backend.lab.fast_planets_cwt          # только self-тесты
python3 -m backend.lab.fast_planets_cwt moex     # 8 бумаг MOEX
python3 -m backend.lab.fast_planets_cwt resto    # 16 пиццерий
python3 -m backend.lab.fast_planets_cwt all
"""
import json
import os
import sys
from datetime import date, datetime, timezone
from multiprocessing import Pool

import numpy as np

from backend import aether
from backend.lab import morlet as M
from backend.lab.e_surrogate import iaaft
from backend.lab.psi_split import psi_bodies

HIST = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/moex_hist.json"
STORES = ("/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/"
          "resto/resto/data/stores.json")

# ── группы тел ─────────────────────────────────────────────────────────
GROUPS = {
    "Луна": ["Луна"],
    "Меркурий": ["Меркурий"],
    "Венера": ["Венера"],
    "Марс": ["Марс"],
    "медленные": ["Юпитер", "Сатурн", "Уран", "Нептун", "Плутон"],
}

# ── полосы: (тело, период сут, что это) ────────────────────────────────
BANDS = [
    ("Луна", 27.32, "тропический"),
    ("Луна", 29.53, "синодический"),
    ("Меркурий", 87.97, "сидерический"),
    ("Меркурий", 115.88, "синодический"),
    ("Венера", 224.70, "сидерический"),
    ("Венера", 583.92, "синодический"),
    ("Марс", 686.98, "сидерический"),
    ("Марс", 779.94, "синодический"),
]

DJ = 1.0 / 16.0          # шаг сетки масштабов: 4.4 % по периоду
P_MIN = 12.0             # нижний край сетки, сут
P_MAX = 1200.0           # верхний край сетки, сут
BAND_HALF = 2.0 ** 0.125  # полуширина полосы: ±9 % по периоду
Q_THR = 0.95             # уровень порога по пулу нуля
NSUR = 300               # суррогатов на (объект × группа)
SEED = 20260801          # зерно фиксировано: прогон воспроизводим побитово
MIN_DAYS = 150           # короче — объект не берём, а не «дотягиваем» нулями


# ══════════════════════════════════════════════════════════ небо

def store_band(genesis):
    """Веда-полоса заведения 🟡: тело с максимальным E в момент генезиса.

    Тот же критерий, что в resto/backend/sky.store_band. На группы из
    одного тела не влияет (положительный множитель), влияет только на
    смесь медленных.
    """
    t0 = datetime(*[int(v) for v in genesis], tzinfo=timezone.utc)
    tr = aether.transit_series(t0, 24.0, 24.0)
    if tr is None:
        return None
    best = max(aether._BODY_ORDER, key=lambda nm: float(tr["E"][nm][0]))
    return int(aether.W_VEDIC[best])


def sky_groups(dates, ticker=None, genesis=None, band=None):
    """Ψ по группам тел на датах отклика. Нет эфемерид → None, без заглушек."""
    got = psi_bodies(dates, ticker=ticker, genesis=genesis, band=band)
    if got is None:
        return None
    ts, parts = got
    pmap = {datetime.fromtimestamp(t, timezone.utc).date(): i
            for i, t in enumerate(ts)}
    out = {}
    for g, bodies in GROUPS.items():
        x = sum(parts[b] for b in bodies)
        v = np.array([x[pmap[d]] if d in pmap else np.nan for d in dates])
        if not np.all(np.isfinite(v)):
            return None
        out[g] = v
    return out


# ══════════════════════════════════════════════════════════ полосы и серии

def band_rows(periods, T, half=BAND_HALF):
    """Строки сетки масштабов внутри полосы T/half … T·half.

    Если сетка грубее полосы — берём одну ближайшую строку (полоса не
    может оказаться пустой).
    """
    periods = np.asarray(periods, float)
    rows = np.flatnonzero((periods >= T / half) & (periods <= T * half))
    if rows.size == 0:
        rows = np.array([int(np.argmin(np.abs(periods - T)))])
    return rows


def band_series(R2, periods, T, half=BAND_HALF):
    """R²(t), усреднённая по строкам полосы. Точка живая, только если
    ВСЕ строки полосы вне конуса — иначе NaN (конус не подмешиваем)."""
    rows = band_rows(periods, T, half)
    sub = R2[rows]
    ok = np.all(np.isfinite(sub), axis=0)
    out = np.full(R2.shape[1], np.nan)
    if np.any(ok):
        out[ok] = sub[:, ok].mean(axis=0)
    return out


def runs_of_true(mask):
    """Длины непрерывных серий True, в отсчётах. Пустой массив, если серий нет."""
    m = np.asarray(mask, bool).astype(np.int8)
    if m.size == 0:
        return np.zeros(0, int)
    d = np.diff(np.concatenate(([0], m, [0])))
    beg = np.flatnonzero(d == 1)
    end = np.flatnonzero(d == -1)
    return end - beg


def episode_stats(vals, thr, dt, T):
    """Три числа перемежаемости для одного ряда R² выше порога.

      доля      — доля времени выше порога;
      серия     — самая длинная непрерывная серия, сут;
      эпизоды   — доля времени внутри серий длиной ≥ T (один период тела).
    """
    m = np.asarray(vals, float) > thr
    n = m.size
    if n == 0:
        return None
    rl = runs_of_true(m)
    mx = float(rl.max() * dt) if rl.size else 0.0
    long = rl[rl * dt >= T]
    return {"frac": float(m.mean()), "run": mx,
            "epi": float(long.sum() / n) if n else np.nan}


def p_ge(null, real):
    """p = (1 + #{нуль ≥ опыт}) / (N + 1) — односторонний, без нуля в знаменателе."""
    null = np.asarray(null, float)
    return float((1.0 + np.sum(null >= real)) / (null.size + 1.0))


# ══════════════════════════════════════════════════════════ движок объекта

def _grid():
    """Сетка масштабов, одна и та же для всех рядов: (s0, j_tot)."""
    s0 = M.period_to_scale(P_MIN)
    jt = int(np.ceil(np.log2(M.period_to_scale(P_MAX) / s0) / DJ))
    return s0, jt


def feasible(n, dt, T, half=BAND_HALF):
    """Есть ли хоть одна точка вне конуса для полосы T? (√2·s ≤ расстояние
    до края, самый жёсткий — верхний край полосы)."""
    s = M.period_to_scale(T * half)
    return (np.sqrt(2.0) * s) < ((n - 1) / 2.0) * dt


def analyse_object(name, dt, resp, psi_master, idx, nsur=NSUR, seed=SEED):
    """Полный прогон одного объекта: CWT отклика, CWT каждой небесной
    группы, локальная когерентность, IAAFT-нуль и статистики полос.

    psi_master — Ψ на ОБЩЕЙ суточной сетке всех объектов, idx — позиции
    дат отклика в этой сетке. Суррогат строится на общей сетке с зерном,
    зависящим только от (группа, номер суррогата), и лишь потом
    прореживается под объект. Это КРИТИЧНО: у всех объектов небо почти
    одно и то же, поэтому их настоящие доли скоррелированы между собой;
    независимый суррогат на каждый объект дал бы нулю искусственно узкое
    распределение медианы и завысил бы значимость. Спаренный суррогат
    воспроизводит ту же зависимость, что и в опыте.
    """
    s0, jt = _grid()
    res_r = M.cwt(resp, dt=dt, dj=DJ, s0=s0, j_tot=jt)
    per = res_r["periods"]
    live = [b for b in BANDS if feasible(resp.size, dt, b[1])]
    out = {"n": int(resp.size), "dt": float(dt), "bands_live": [b[1] for b in live]}
    power = {}
    for gi, (g, xm) in enumerate(sorted(psi_master.items())):
        x = xm[idx]
        res_s = M.cwt(x, dt=dt, dj=DJ, s0=s0, j_tot=jt)
        gp = M.global_power(res_s)
        tot = float(np.nansum(gp))
        co = M.wavelet_coherence(res_s, res_r)
        R2 = co["R2"]
        real = {b[1]: band_series(R2, per, b[1]) for b in live}
        power[g] = {b[1]: (float(np.nansum(gp[band_rows(per, b[1])])) / tot
                           if tot > 0 else np.nan) for b in live}
        null = {b[1]: [] for b in live}
        for i in range(nsur):
            rng = np.random.default_rng([seed, gi, i])   # СПАРЕНО по объектам
            rs = M.cwt(iaaft(xm, rng)[idx], dt=dt, dj=DJ, s0=s0, j_tot=jt)
            cs = M.wavelet_coherence(rs, res_r)["R2"]
            for b in live:
                null[b[1]].append(band_series(cs, per, b[1]).astype(np.float32))
        for b in live:
            T = b[1]
            v = real[T]
            ok = np.isfinite(v)
            if not np.any(ok):
                out[(g, T)] = None
                continue
            NM = np.asarray(null[T])[:, ok]          # (nsur, n_ok)
            thr = float(np.quantile(NM, Q_THR))
            st = episode_stats(v[ok], thr, dt, T)
            ns = [episode_stats(NM[i], thr, dt, T) for i in range(nsur)]
            out[(g, T)] = {
                "thr": thr, "n_ok": int(ok.sum()),
                # сколько НЕЗАВИСИМЫХ эпизодов длины T вообще помещается
                # в допустимое время — предел разрешающей силы клетки
                "indep": float(ok.sum()) * dt / T,
                "share_power": power[g][T],
                "frac": st["frac"], "run": st["run"], "epi": st["epi"],
                "frac_null": np.array([s["frac"] for s in ns]),
                "run_null": np.array([s["run"] for s in ns]),
                "epi_null": np.array([s["epi"] for s in ns]),
                # контрольная «линейная» статистика: СРЕДНИЙ уровень
                # когерентности. Директива утверждает, что главное —
                # перемежаемость; проверяем это, считая оба числа.
                "mean": float(np.mean(v[ok])),
                "mean_null": np.mean(NM, axis=1).astype(float),
            }
    out["name"] = name
    return out


def _job(payload):
    name, dt, resp, psi_master, idx, nsur = payload
    return analyse_object(name, dt, resp, psi_master, idx, nsur=nsur)


def master_grid(all_dates):
    """Общая сплошная суточная сетка, покрывающая все объекты."""
    lo = min(d[0] for d in all_dates)
    hi = max(d[-1] for d in all_dates)
    n = (hi - lo).days + 1
    return [date.fromordinal(lo.toordinal() + k) for k in range(n)], lo


def master_idx(dates, lo):
    """Позиции дат объекта в общей сетке."""
    return np.array([(d - lo).days for d in dates], int)


# ══════════════════════════════════════════════════════════ отчёт

def _pool_report(res, title, nsur):
    """Сводная таблица: по каждой клетке (группа × полоса) медиана по
    объектам против медианы нуля, спаренного по индексу суррогата."""
    names = [r["name"] for r in res]
    print()
    print("=" * 108)
    print(title)
    print("=" * 108)
    print(f"объектов {len(names)}: {', '.join(names)}")
    print(f"суррогатов IAAFT на объект×группу: {nsur}; "
          f"порог θ = {Q_THR:.2f}-квантиль пула нуля; "
          f"внутри конуса влияния не считаем")
    print()

    hdr = ("тело/группа       полоса, сут  объект  живых  эпиз  "
           "мощн.Ψ%   доля>θ   нуль    p(доля)   серия,сут  нуль   p(сер)  p(эпиз)"
           "   ср.R²   нуль   p(ср)  объектов p<.05")
    print(hdr)
    print("-" * len(hdr))
    table = {}
    for g in list(GROUPS):
        for (body, T, kind) in BANDS:
            cells = [r.get((g, T)) for r in res if r.get((g, T))]
            if not cells:
                continue
            n_ok = int(np.median([c["n_ok"] for c in cells]))
            ind = float(np.median([c["indep"] for c in cells]))
            shp = float(np.median([c["share_power"] for c in cells]))
            fr = float(np.median([c["frac"] for c in cells]))
            frn = np.median(np.array([c["frac_null"] for c in cells]), axis=0)
            rn_ = float(np.median([c["run"] for c in cells]))
            rnn = np.median(np.array([c["run_null"] for c in cells]), axis=0)
            ep = float(np.median([c["epi"] for c in cells]))
            epn = np.median(np.array([c["epi_null"] for c in cells]), axis=0)
            mn = float(np.median([c["mean"] for c in cells]))
            mnn = np.median(np.array([c["mean_null"] for c in cells]), axis=0)
            pf, pr = p_ge(frn, fr), p_ge(rnn, rn_)
            pe, pm = p_ge(epn, ep), p_ge(mnn, mn)
            # собственный p КАЖДОГО объекта — точный тест без всяких
            # предположений о независимости объектов между собой
            p_own = [p_ge(c["frac_null"], c["frac"]) for c in cells]
            n_own = int(np.sum(np.asarray(p_own) < 0.05))
            table[(g, T)] = (fr, float(np.median(frn)), pf, rn_,
                             float(np.median(rnn)), pr, pe, mn,
                             float(np.median(mnn)), pm, n_own, len(cells),
                             float(np.min(p_own)))
            star = "  ←" if (body == g and pf < 0.05) else ""
            print(f"{g:16s}  {T:8.2f} {kind[:4]}  {len(cells):4d}  "
                  f"{n_ok:6d} {ind:5.1f}  {shp*100:7.3f}  "
                  f"{fr:7.4f} {float(np.median(frn)):7.4f} "
                  f"{pf:8.4f}  {rn_:9.1f} {float(np.median(rnn)):6.1f} "
                  f"{pr:7.4f} {pe:7.4f}  {mn:6.4f} "
                  f"{float(np.median(mnn)):6.4f} {pm:6.4f}"
                  f"   {n_own:2d}/{len(cells):2d} (ждём {0.05*len(cells):.1f})"
                  f"{star}")
    print()
    print("СВОЙ ПРОТИВ ЧУЖОГО (диагональ = группа звучит на своей полосе):")
    for (body, T, kind) in BANDS:
        row = []
        for g in list(GROUPS):
            t = table.get((g, T))
            row.append(f"{g[:4]}={t[2]:.3f}" if t else f"{g[:4]}=—")
        own = table.get((body, T))
        print(f"  {T:7.2f} ({body} {kind}): " + "  ".join(row)
              + ("   СВОЙ p=%.4f" % own[2] if own else ""))
    return table


def _verdict(table, n_cells_primary):
    """Честный вывод: пережил ли хоть один быстрый диагональный эффект нуль."""
    print()
    bonf = 0.05 / max(n_cells_primary, 1)
    fast = [(b, T, table[(b, T)]) for (b, T, _k) in BANDS
            if b != "медленные" and (b, T) in table]
    win = [(b, T, t) for (b, T, t) in fast if t[2] < bonf]
    print(f"порог Бонферрони на {n_cells_primary} диагональных клеток: "
          f"p < {bonf:.4f} (минимально достижимое p = {1.0/(NSUR+1):.4f})")
    if win:
        print("ДИАГОНАЛЬНЫЕ КЛЕТКИ, ПЕРЕЖИВШИЕ НУЛЬ:")
        for b, T, t in win:
            print(f"  {b} на {T:.2f} сут: доля {t[0]:.4f} против нуля "
                  f"{t[1]:.4f}, p={t[2]:.4f}")
    else:
        print("НИ ОДНА диагональная клетка быстрого тела не пережила локальный "
              "нуль на уровне Бонферрони.")
    p_all = sorted((t[2], b, T) for (b, T, t) in fast)
    print("лучшие три клетки быстрых тел по p(доля): " + ", ".join(
        f"{b}@{T:.0f}:{p:.4f}" for p, b, T in p_all[:3]))
    all_cells = list(table.values())
    n5 = {k: sum(1 for t in all_cells if t[i] < 0.05)
          for k, i in (("доля", 2), ("серия", 5), ("эпизоды", 6),
                       ("ср.R²", 9))}
    print(f"клеток с p<0.05 из {len(all_cells)} (ожидание чистого шума "
          f"{0.05*len(all_cells):.1f}): " + ", ".join(
              f"{k} {v}" for k, v in n5.items()))
    print("перемежаемость против среднего уровня: если доля/серия не бьют "
          "ср.R², отдельного «квантового резонанса» в данных нет")
    tot_obj = sum(t[11] for t in all_cells)
    hit_obj = sum(t[10] for t in all_cells)
    print(f"собственные p объектов: {hit_obj} значимых из {tot_obj} "
          f"пар (объект × клетка), ожидание чистого шума "
          f"{0.05*tot_obj:.1f}; минимальный собственный p по всем клеткам "
          f"{min(t[12] for t in all_cells):.4f} при пороге Бонферрони "
          f"{0.05/max(tot_obj,1):.5f}")


# ══════════════════════════════════════════════════════════ MOEX

def load_moex():
    """8 бумаг: даты, лог-доходности, средний шаг сетки в сутках."""
    raw = json.load(open(HIST, encoding="utf-8"))
    out = []
    for tk in sorted(raw):
        rows = raw[tk]
        ds = [date.fromisoformat(r[0]) for r in rows]
        cl = np.array([float(r[4]) for r in rows], float)
        if not np.all(cl > 0):
            continue
        ret = np.diff(np.log(cl))
        dd = ds[1:]
        dt = (dd[-1] - dd[0]).days / (len(dd) - 1.0)
        out.append((tk, dd, ret, dt))
    return out


def run_moex(nsur=NSUR, procs=4):
    print("=" * 108)
    print("MOEX · дневные лог-доходности 8 бумаг против Ψ по группам тел")
    print("=" * 108)
    if not os.environ.get("TINKOFF_TOKEN"):
        print("TINKOFF_TOKEN не задан — живые свечи не запрашивались; "
              "работаем на локальной истории moex_hist.json (2013–2026). 🔵")
    rows = load_moex()
    mdates, lo = master_grid([r[1] for r in rows])
    print(f"общая суточная сетка неба: {mdates[0]}…{mdates[-1]}, "
          f"{len(mdates)} суток (суррогаты спарены по ней)")
    jobs = []
    for (tk, dd, ret, dt) in rows:
        psi = sky_groups(mdates, ticker=tk)
        if psi is None:
            print(f"  {tk}: нет эфемерид → пропуск (None, без заглушки)")
            continue
        print(f"  {tk}: баров {len(ret)}, {dd[0]}…{dd[-1]}, "
              f"средний шаг {dt:.4f} сут")
        jobs.append((tk, dt, ret, psi, master_idx(dd, lo), nsur))
    res = _run_jobs(jobs, procs)
    tb = _pool_report(res, "MOEX · перемежаемость вейвлет-когерентности", nsur)
    _verdict(tb, sum(1 for (b, T, _k) in BANDS if (b, T) in tb and b != "медленные"))
    return res, tb


# ══════════════════════════════════════════════════════════ пиццерии

def load_stores():
    """16 пиццерий: самый длинный СПЛОШНОЙ отрезок суток с валидным
    числом заказов. Дырки не заклеиваем — берём непрерывный кусок."""
    raw = json.load(open(STORES, encoding="utf-8"))
    out = []
    for code in sorted(raw):
        st = raw[code]
        days = st["days"]
        ks = sorted(days)
        best, cur, prev = [], [], None
        for k in ks:
            v = days[k].get("orders")
            d = date.fromisoformat(k)
            if v is None or not np.isfinite(float(v)):
                cur, prev = [], None
                continue
            if prev is not None and (d - prev).days != 1:
                cur = []
            cur.append((d, float(v)))
            prev = d
            if len(cur) > len(best):
                best = list(cur)
        if len(best) < MIN_DAYS:
            print(f"  {code}: самый длинный сплошной отрезок {len(best)} сут "
                  f"< {MIN_DAYS} → объект честно исключён")
            continue
        gen = tuple(int(x) for x in st["genesis"].split("-")) + (9,)
        out.append((code, [b[0] for b in best],
                    np.array([b[1] for b in best], float), 1.0, gen,
                    len(ks)))
    return out


def deweek(dates, y):
    """Убрать НЕДЕЛЬНЫЙ цикл: вычесть среднее по дню недели.

    Зачем контроль. У заказов недельный цикл — самая громкая структура
    ряда, а его 4-я субгармоника 4×7 = 28 сут лежит ровно в лунной полосе
    27.3…29.5. Отклик у опыта и у нуля один и тот же, поэтому сам по себе
    цикл ложного срабатывания не даёт; но он делает отклик почти строго
    периодическим, и тогда когерентность с ним меряет не связь, а
    фазовую устойчивость небесной полосы. Снимаем цикл и смотрим, что
    из находок выживет. 🔵 механика, не подгонка: вычитается ровно семь
    средних, ни одного подобранного параметра.
    """
    y = np.asarray(y, float)
    w = np.array([d.weekday() for d in dates])
    out = y.copy()
    for k in range(7):
        m = w == k
        if np.any(m):
            out[m] = y[m] - y[m].mean()
    return out


def run_resto(nsur=NSUR, procs=4, deseason=False):
    print("=" * 108)
    print("ПИЦЦЕРИИ · дневные заказы 16 точек против Ψ по группам тел"
          + (" · КОНТРОЛЬ: недельный цикл снят" if deseason else ""))
    print("=" * 108)
    rows = load_stores()
    mdates, lo = master_grid([r[1] for r in rows])
    print(f"общая суточная сетка неба: {mdates[0]}…{mdates[-1]}, "
          f"{len(mdates)} суток (суррогаты спарены по ней)")
    jobs = []
    for (code, dd, orders, dt, gen, n_all) in rows:
        band = store_band(gen)
        psi = sky_groups(mdates, genesis=gen, band=band)
        if psi is None:
            print(f"  {code}: нет эфемерид → пропуск (None, без заглушки)")
            continue
        live = [b[1] for b in BANDS if feasible(len(orders), dt, b[1])]
        print(f"  {code}: сплошных суток {len(orders)} из {n_all} "
              f"({dd[0]}…{dd[-1]}), band {band}, "
              f"полос вне конуса {len(live)}/{len(BANDS)}")
        jobs.append((code, dt, deweek(dd, orders) if deseason else orders,
                     psi, master_idx(dd, lo), nsur))
    res = _run_jobs(jobs, procs)
    tb = _pool_report(res, "ПИЦЦЕРИИ · перемежаемость вейвлет-когерентности"
                      + (" (недельный цикл снят)" if deseason else ""), nsur)
    _verdict(tb, sum(1 for (b, T, _k) in BANDS if (b, T) in tb and b != "медленные"))
    return res, tb


def _inject(resp, res_sky, per, T, frac_win, amp):
    """Подмешать в отклик СИНТЕТИЧЕСКИЙ захват фазы неба на доле времени.

    В окно длиной frac_win·n добавляем amp·σ(resp)·cos(φ_неба(t)), где
    φ — фаза вейвлет-коэффициента неба на полосе T. Ровно то, что метод
    и должен ловить: связь есть только внутри окна.
    """
    n = resp.size
    j = int(np.argmin(np.abs(per - T)))
    ph = np.angle(res_sky["W_full"][j])
    k = int(round(frac_win * n))
    lo = (n - k) // 2
    w = np.zeros(n)
    w[lo:lo + k] = 1.0
    return resp + amp * float(np.std(resp)) * w * np.cos(ph)


def power_probe(ticker="SBER", group="Луна", T=27.32, nsur=200,
                wins=(0.10, 0.25, 0.50), amps=(0.10, 0.25, 0.50)):
    """ЧУВСТВИТЕЛЬНОСТЬ: какой захват метод БЫ поймал на этих данных.

    Отрицательный результат без этого числа ничего не стоит: «не нашли»
    может значить и «нечего искать», и «нечем искать». Вкладываем в
    реальный отклик известный захват и смотрим, с какой доли времени и
    какой амплитуды нуль перестаёт его объяснять.
    """
    rows = [r for r in load_moex() if r[0] == ticker]
    if not rows:
        return None
    tk, dd, ret, dt = rows[0]
    psi = sky_groups(dd, ticker=tk)
    if psi is None:
        return None
    s0, jt = _grid()
    res_s = M.cwt(psi[group], dt=dt, dj=DJ, s0=s0, j_tot=jt)
    per = res_s["periods"]
    print()
    print("=" * 96)
    print(f"ЧУВСТВИТЕЛЬНОСТЬ МЕТОДА · {ticker}, Ψ «{group}», полоса {T:.2f} сут, "
          f"{nsur} суррогатов")
    print("=" * 96)
    print("вклад: amp·σ(доходности)·cos(фаза неба) внутри окна; вне окна — "
          "чистые данные")
    print("окно\\амплитуда  " + "".join(f"{a:>12.2f}" for a in amps))
    grid = {}
    for w in wins:
        line = f"{w*100:5.0f} %        "
        for a in amps:
            resp = _inject(ret, res_s, per, T, w, a)
            r = analyse_object(f"{tk}~{w}~{a}", dt, resp, {group: psi[group]},
                               np.arange(ret.size), nsur=nsur)
            c = r.get((group, T))
            if not c:
                line += f"{'—':>12s}"
                continue
            p = p_ge(c["frac_null"], c["frac"])
            grid[(w, a)] = (c["frac"], p)
            line += f"{c['frac']:6.3f}/{p:5.3f}"
        print(line, flush=True)
    print("читается: доля времени выше порога / p. Клетки с p ≤ 0.05 — то, "
          "что метод на этих данных РЕАЛЬНО видит.")
    lo_c, hi_c = grid.get((wins[0], amps[0])), grid.get((wins[-1], amps[-1]))
    if lo_c and hi_c:                    # больше окно и громче — не хуже
        assert hi_c[1] <= lo_c[1] + 1e-12, (lo_c, hi_c)
        assert hi_c[0] >= lo_c[0], (lo_c, hi_c)
    return grid


def _run_jobs(jobs, procs):
    if not jobs:
        return []
    if procs > 1 and len(jobs) > 1:
        with Pool(min(procs, len(jobs))) as p:
            res = []
            for r in p.imap_unordered(_job, jobs):
                print(f"    … готов {r['name']}", flush=True)
                res.append(r)
    else:
        res = [_job(j) for j in jobs]
    res.sort(key=lambda r: r["name"])
    return res


# ══════════════════════════════════════════════════════════ self-тесты

def _t_bands_and_runs():
    """Полосы и серии — ответы считаются на бумаге."""
    per = M.scale_to_period(M.make_scales(4096, 1.0, DJ, M.period_to_scale(P_MIN),
                                          int(np.ceil(np.log2(P_MAX / P_MIN) / DJ))))
    rows = band_rows(per, 100.0)
    assert rows.size >= 3, rows.size
    assert np.all(per[rows] >= 100.0 / BAND_HALF - 1e-9)
    assert np.all(per[rows] <= 100.0 * BAND_HALF + 1e-9)
    # ближайшая строка обязана попасть в полосу
    assert int(np.argmin(np.abs(per - 100.0))) in rows
    # число строк: полоса шириной 0.25 октавы на сетке dj=1/16 → 4 или 5
    assert rows.size in (4, 5), rows.size

    m = np.array([0, 1, 1, 0, 1, 1, 1, 1, 0], bool)
    rl = runs_of_true(m)
    assert list(rl) == [2, 4], rl
    st = episode_stats(np.where(m, 1.0, 0.0), 0.5, 2.0, 6.0)
    assert abs(st["frac"] - 6.0 / 9.0) < 1e-12
    assert abs(st["run"] - 8.0) < 1e-12          # 4 отсчёта × dt=2 сут
    assert abs(st["epi"] - 4.0 / 9.0) < 1e-12    # только серия 8 сут ≥ 6
    assert runs_of_true(np.zeros(5, bool)).size == 0
    assert list(runs_of_true(np.ones(5, bool))) == [5]

    # вклад: нулевая амплитуда ничего не меняет, окно ровно нужной длины
    nn = 1000
    z = np.cos(2 * np.pi * np.arange(nn) / 40.0)
    rs = M.cwt(z, dj=DJ, s0=M.period_to_scale(P_MIN),
               j_tot=int(np.ceil(np.log2(P_MAX / P_MIN) / DJ)))
    base = np.linspace(-1.0, 1.0, nn)
    assert np.array_equal(_inject(base, rs, rs["periods"], 40.0, 0.3, 0.0), base)
    d = _inject(base, rs, rs["periods"], 40.0, 0.3, 1.0) - base
    assert int(np.sum(d != 0.0)) <= 300 and int(np.sum(d != 0.0)) >= 295, \
        int(np.sum(d != 0.0))
    print("  1. полосы и серии: строк в полосе 100 сут = %d, серии [2,4], "
          "доля 0.6667, серия 8.0 сут, эпизоды 0.4444 — совпало" % rows.size)


def _t_scale_invariance():
    """R² не меняется от положительного множителя — значит band (нота) на
    группы из одного тела не влияет вообще."""
    n = 800
    t = np.arange(n, dtype=float)
    x = np.sin(2 * np.pi * t / 60.0) + 0.4 * np.sin(2 * np.pi * t / 17.0)
    y = np.sin(2 * np.pi * t / 60.0 + 0.7) + 0.4 * np.cos(2 * np.pi * t / 23.0)
    s0, jt = _grid()
    a = M.wavelet_coherence(M.cwt(x, dj=DJ, s0=s0, j_tot=jt),
                            M.cwt(y, dj=DJ, s0=s0, j_tot=jt))["R2"]
    b = M.wavelet_coherence(M.cwt(1.5 * x, dj=DJ, s0=s0, j_tot=jt),
                            M.cwt(y, dj=DJ, s0=s0, j_tot=jt))["R2"]
    d = np.nanmax(np.abs(a - b))
    assert d < 1e-9, d
    print(f"  2. масштабная инвариантность R²: max|Δ| = {d:.2e} < 1e-9 "
          "(нота-множитель не может ничего изменить)")


def _t_coi():
    """Учёт конуса — против аналитической формулы √2·s ≤ расстояние до края."""
    n, dt, T = 1024, 1.0, 120.0
    x = np.cos(2 * np.pi * np.arange(n) * dt / T)
    s0, jt = _grid()
    r = M.cwt(x, dt=dt, dj=DJ, s0=s0, j_tot=jt)
    v = band_series(np.abs(r["W"]) ** 2 * 0 + np.where(np.isfinite(r["W"]), 0.5, np.nan),
                    r["periods"], T)
    rows = band_rows(r["periods"], T)
    s_max = r["scales"][rows].max()
    idx = np.arange(n)
    d = np.minimum(idx, n - 1 - idx) * dt
    expect = int(np.sum(np.sqrt(2.0) * s_max <= d))
    got = int(np.sum(np.isfinite(v)))
    assert got == expect, (got, expect)
    assert feasible(n, dt, T) and not feasible(200, dt, T)
    print(f"  3. конус влияния: живых точек {got}, аналитика {expect} — "
          f"совпало; полоса {T:.0f} сут на 200 отсчётах честно недоступна")


def _t_locality():
    """Главный тест директивы: связь ЕСТЬ только в куске ряда.

    Локальный вейвлет обязан её увидеть, глобальный (Гильберт+PLV,
    Пирсон) — размазать. Ответы известны по построению.
    """
    from scipy.signal import hilbert
    n, T = 3000, 60.0
    t = np.arange(n, dtype=float)
    sky = np.sin(2 * np.pi * t / T)
    lo, hi = 1000, 2000                      # окно захвата фазы
    ph = np.where((t >= lo) & (t < hi), 0.6,
                  0.6 + 2 * np.pi * (t - lo) / 250.0)
    resp = np.sin(2 * np.pi * t / T + ph)
    s0, jt = _grid()
    co = M.wavelet_coherence(M.cwt(sky, dj=DJ, s0=s0, j_tot=jt),
                             M.cwt(resp, dj=DJ, s0=s0, j_tot=jt))
    v = band_series(co["R2"], co["periods"], T)
    inn = np.nanmean(v[lo + 150:hi - 150])
    out = np.nanmean(np.concatenate([v[300:lo - 150], v[hi + 150:n - 300]]))
    assert inn > 0.90, inn
    assert out < 0.35, out
    assert inn - out > 0.55
    pl = float(np.abs(np.mean(np.exp(1j * (np.angle(hilbert(sky))
                                           - np.angle(hilbert(resp)))))))
    pe = float(np.corrcoef(sky, resp)[0, 1])
    assert pl < 0.55, pl
    st = episode_stats(v[np.isfinite(v)], 0.8, 1.0, T)
    share = (hi - lo) / float(np.sum(np.isfinite(v)))
    assert abs(st["frac"] - share) < 0.12, (st["frac"], share)
    assert st["run"] > 0.8 * (hi - lo)
    print(f"  4. локальность: R² в окне {inn:.3f}, вне окна {out:.3f}; "
          f"глобальный PLV по Гильберту {pl:.3f}, Пирсон {pe:+.3f}")
    print(f"     доля времени выше 0.8 = {st['frac']:.3f} при истинной доле "
          f"окна {share:.3f}; длиннейшая серия {st['run']:.0f} сут из "
          f"{hi-lo} истинных")


def _t_null_calibration():
    """Калибровка нуля. Два аналитических факта:

      · IAAFT сохраняет НАБОР значений ряда точно (перестановка) и
        спектр — приближённо;
      · при пороге θ = 95-й перцентиль ПУЛА нуля средняя доля времени
        выше порога у суррогатов равна ровно 0.05 — это тождество, оно и
        проверяется. Плюс разделение: захваченная пара против независимой.
    """
    rng = np.random.default_rng(7)
    n, T, NSU = 1800, 60.0, 60
    t = np.arange(n, dtype=float)
    x = np.sin(2 * np.pi * t / T) + 0.3 * np.sin(2 * np.pi * t / 137.0)
    su = iaaft(x, rng)
    assert np.allclose(np.sort(su), np.sort(x), atol=1e-12)
    ax, au = np.abs(np.fft.rfft(x)), np.abs(np.fft.rfft(su))
    rel = float(np.linalg.norm(ax - au) / np.linalg.norm(ax))
    assert rel < 0.05, rel

    s0, jt = _grid()

    def cell(sky, resp):
        co = M.wavelet_coherence(M.cwt(sky, dj=DJ, s0=s0, j_tot=jt),
                                 M.cwt(resp, dj=DJ, s0=s0, j_tot=jt))
        per = co["periods"]
        v = band_series(co["R2"], per, T)
        ok = np.isfinite(v)
        NM = np.asarray([band_series(M.wavelet_coherence(
            M.cwt(iaaft(sky, np.random.default_rng([11, i])), dj=DJ,
                  s0=s0, j_tot=jt),
            M.cwt(resp, dj=DJ, s0=s0, j_tot=jt))["R2"], per, T)[ok]
            for i in range(NSU)])
        thr = float(np.quantile(NM, Q_THR))
        st = episode_stats(v[ok], thr, 1.0, T)
        fn = np.array([episode_stats(NM[i], thr, 1.0, T)["frac"]
                       for i in range(NSU)])
        assert abs(fn.mean() - 0.05) < 0.005, fn.mean()   # тождество порога
        return st, p_ge(fn, st["frac"]), int(ok.sum())

    # небо с БЛУЖДАЮЩЕЙ фазой (широкая полоса — как настоящая Ψ)
    wan = 3.0 * np.sin(2 * np.pi * t / 300.0) + 1.2 * np.sin(2 * np.pi * t / 173.0 + 0.7)
    sky = np.sin(2 * np.pi * t / T + wan)
    def band_noise(seed):
        """Шум в той же полосе, независимый от неба. Зерно своё — не зависит
        от того, сколько случайности потратили выше."""
        g = np.random.default_rng(seed)
        F = np.fft.rfft(g.standard_normal(n)) * np.exp(
            -0.5 * ((np.fft.rfftfreq(n) - 1 / T) / (0.25 / T)) ** 2)
        z = np.fft.irfft(F, n)
        return z / z.std()

    lo, hi = 700, 1300
    win = (t >= lo) & (t < hi)
    noise = band_noise(0)
    resp = np.where(win, np.sin(2 * np.pi * t / T + wan + 0.6), noise)

    st_l, p_l, n_ok = cell(sky, resp)
    share = (hi - lo) / float(n_ok)
    # сглаживание Torrence-Webster (σ = s ≈ T) съедает по периоду с каждого
    # края окна: систематическая недооценка доли ≈ 2T/n_ok = 0.074. Отсюда
    # допуск 0.15 и требование «не меньше половины истинной доли».
    assert abs(st_l["frac"] - share) < 0.15, (st_l["frac"], share)
    assert st_l["frac"] > 0.5 * share, (st_l["frac"], share)
    assert st_l["run"] > 0.5 * (hi - lo) and st_l["run"] > 5.0 * T, st_l["run"]
    assert p_l <= 1.0 / (NSU + 1) + 1e-12, p_l

    # связи нет вообще — три независимых розыгрыша шума
    ctl = [cell(sky, band_noise(sd))[:2] for sd in (0, 1, 2)]
    f_n = float(np.median([c[0]["frac"] for c in ctl]))
    p_n = float(np.median([c[1] for c in ctl]))
    assert f_n < 0.10, f_n
    assert p_n > 0.10, p_n

    # МОНОХРОМНОЕ небо: суррогат такой же синусоиды остаётся синусоидой,
    # а R² к постоянному сдвигу фазы нечувствительна — нуль съедает захват.
    mono = np.sin(2 * np.pi * t / T)
    st_m, p_m, _ = cell(mono, np.where(win, np.sin(2 * np.pi * t / T + 0.6), noise))
    assert st_m["frac"] < 0.5 * st_l["frac"], (st_m["frac"], st_l["frac"])

    print(f"  5. нуль: IAAFT сохранил набор значений точно, спектр с "
          f"невязкой {rel*100:.2f} %; средняя доля нуля = 0.0500 "
          f"(тождество порога выполнено во всех трёх стендах)")
    print(f"     захват в окне: доля {st_l['frac']:.4f} при истинной доле окна "
          f"{share:.4f}, серия {st_l['run']:.0f} сут из {hi-lo}, "
          f"p={p_l:.4f} (минимум сетки)")
    print(f"     связи нет (медиана 3 розыгрышей): доля {f_n:.4f}, p={p_n:.4f}; "
          f"монохромное небо при том же захвате: доля {st_m['frac']:.4f}, "
          f"p={p_m:.4f} — узкополосную Ψ нуль съедает, это предел метода")


def _t_sky():
    """Небо: главный период Ψ каждой группы известен ЗАРАНЕЕ, из механики.

      · Луна не бывает ретроградной, хроно-член E(t) у неё не взрывается,
        и Ψ задаётся возвратом угла к НЕПОДВИЖНОЙ натальной долготе —
        это ТРОПИЧЕСКИЙ месяц 27.32 сут, а не синодический 29.53;
      · у планет хроно-член min(cap, 1+1/|dλ/dt|) взрывается в стояниях,
        а стояния повторяются с СИНОДИЧЕСКИМ периодом: Меркурий 115.88,
        Венера 583.92, Марс 779.94;
      · у смеси медленных стояния приходят раз в земной год (их
        синодические периоды 367…378 сут) → пик у 365…385.

    Если бы Ψ считалась неверно, эти четыре числа не сошлись бы.
    """
    ds = [date.fromordinal(date(2013, 1, 1).toordinal() + k)
          for k in range(0, 4700, 1)]
    g = sky_groups(ds, ticker="SBER")
    assert g is not None, "нет эфемерид — небесная часть не проверена"
    s0, jt = _grid()
    want = {"Луна": (27.32, 0.03), "Меркурий": (115.88, 0.05),
            "Венера": (583.92, 0.05), "Марс": (779.94, 0.05),
            "медленные": (372.0, 0.06)}
    got = {}
    for nm, (T0, tol) in want.items():
        r = M.cwt(g[nm], dt=1.0, dj=DJ, s0=s0, j_tot=jt)
        gp = M.global_power(r)
        j = int(np.nanargmax(np.where(r["periods"] <= 1000.0, gp, np.nan)))
        Tp = float(r["periods"][j])
        got[nm] = Tp
        assert abs(Tp - T0) / T0 < tol, (nm, Tp, T0)
    print("  7. небо: главные периоды Ψ — " + ", ".join(
        f"{k} {v:.2f}" for k, v in got.items()))
    print("     аналитика: тропический месяц 27.32 и синодические 115.88 / "
          "583.92 / 779.94, у медленных стояния раз в год — совпало")


def _t_pairing():
    """ЗАЧЕМ СУРРОГАТЫ СПАРЕНЫ. У 8 бумаг MOEX (и у 16 пиццерий) небо почти
    одно и то же, а отклики держит общий рыночный фактор. Их настоящие
    доли поэтому скоррелированы. Если каждому объекту дать СВОЙ независимый
    суррогат, медиана нуля по объектам сожмётся в точку (дисперсия падает
    как 1/K), и любая ненулевая настоящая медиана получит крошечное p.
    Стенд: связи со небом нет вообще, K объектов почти одинаковы. Спаренный
    нуль обязан оставить p незначимым, неспаренный — обязан соврать.
    """
    s0, jt = _grid()
    n, T, K, S = 700, 60.0, 5, 60
    t = np.arange(n, dtype=float)
    rng = np.random.default_rng(5)
    sky = np.sin(2 * np.pi * t / T + 2.5 * np.sin(2 * np.pi * t / 210.0))
    common = rng.standard_normal(n)                # общий фактор откликов
    rs = [M.cwt(common + 0.25 * rng.standard_normal(n), dj=DJ, s0=s0, j_tot=jt)
          for _ in range(K)]
    per = rs[0]["periods"]
    ok = np.isfinite(band_series(M.wavelet_coherence(
        M.cwt(sky, dj=DJ, s0=s0, j_tot=jt), rs[0])["R2"], per, T))
    V = np.empty((S, K, int(ok.sum())))            # S суррогатов × K объектов
    for i in range(S):
        rsur = M.cwt(iaaft(sky, np.random.default_rng([3, i])), dj=DJ,
                     s0=s0, j_tot=jt)
        for k in range(K):
            V[i, k] = band_series(M.wavelet_coherence(rsur, rs[k])["R2"],
                                  per, T)[ok]
    thr = float(np.quantile(V, Q_THR))
    F = np.array([[episode_stats(V[i, k], thr, 1.0, T)["frac"]
                   for k in range(K)] for i in range(S)])
    # калибровка: каждый суррогат по очереди играет роль «настоящего неба».
    # Настоящей связи нет НИ В ОДНОМ случае, значит доля ложных срабатываний
    # обязана быть 5 %. Считаем её для обоих способов построения нуля.
    fp = {"спаренный": 0, "неспаренный": 0}
    for m in range(S):
        real = float(np.median(F[m]))
        oth = [i for i in range(S) if i != m]
        pair = np.array([np.median(F[i]) for i in oth])
        unpair = np.array([np.median([F[(i + 13 * k) % S, k]
                                      for k in range(K)]) for i in oth])
        fp["спаренный"] += p_ge(pair, real) < 0.05
        fp["неспаренный"] += p_ge(unpair, real) < 0.05
    r_p, r_u = fp["спаренный"] / S, fp["неспаренный"] / S
    assert r_p <= 0.15, r_p
    assert r_u >= 0.15 and r_u > 3.0 * max(r_p, 1e-9), (r_u, r_p)
    print(f"  6. спаривание нуля: связи со небом нет ни в одном из {S} "
          f"прогонов, K={K} почти одинаковых объектов")
    print(f"     доля ложных срабатываний: спаренный нуль {r_p*100:.1f} % "
          f"(норма 5 %), НЕспаренный {r_u*100:.1f} % — "
          f"завышение в {r_u/max(r_p,1e-9):.1f}× ; поэтому суррогаты спарены")


def _selftest():
    print("=" * 96)
    print("ВЕЙВЛЕТ ПО БЫСТРЫМ ТЕЛАМ · SELF-ТЕСТЫ (ответы известны заранее)")
    print("=" * 96)
    _t_bands_and_runs()
    _t_scale_invariance()
    _t_coi()
    _t_locality()
    _t_null_calibration()
    _t_pairing()
    _t_sky()
    print("\nвсе семь тестов пройдены")
    print("⚫ Это измерение, а не сигнал и не приказ.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    mode = args[0] if args else ""
    ns = int(args[1]) if len(args) > 1 else NSUR
    if mode in ("", "test", "all"):
        _selftest()
    if mode in ("moex", "all"):
        print()
        run_moex(nsur=ns)
    if mode in ("resto", "all"):
        print()
        run_resto(nsur=ns)
    if mode in ("restodw", "all"):
        print()
        run_resto(nsur=ns, deseason=True)
    if mode in ("power", "all"):
        power_probe(nsur=min(ns, 200))
