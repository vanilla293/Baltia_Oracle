# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — ОРАКУЛ-СВОД v2: ИЕРАРХИЧЕСКАЯ МАШИНА СОСТОЯНИЙ.

Приказ владельца (спринт «Хищник», прокол №1): «нельзя усреднять
сингулярность. Логистическая труба гладкая, а катастрофы Тома и бифуркации
Пригожина — разрывы фазы. Бифуркационные модели должны работать как
Абсолютный Вектор (Override), а не как голос в демократии лог-оддсов».

Исполнено. Оракул — машина СОСТОЯНИЙ с жёсткой иерархией перехвата:

  1. ХАОС        — инфаркт спреда: вето всему, наблюдение. Разорванный стакан
                   не торгуется ни при каком сигнале.
  2. СИНГУЛЯРНОСТЬ (OVERRIDE) — детекторы слома совпали И сторона слома
                   известна (LPPLS Сорнетта qualified+near, окно двигателя
                   бифуркаций, критичность Хоукса как микро-подтверждение):
                   направление НАЗНАЧАЕТСЯ вектором слома, парламент голосов
                   ИГНОРИРУЕТСЯ (записывается для журнала, но не решает).
                   Лёгкий шум против сингулярности не размывает её.
  3. У ПОРОГА    — давление слома высокое, но СТОРОНА не названа: свежие
                   входы только при сильном согласии парламента; иначе ждём,
                   когда среда объявит сторону.
  4. ПАРЛАМЕНТ   — линейный мир: лог-оддсы голосов (стакан, майя, бифуркации,
                   тренд/дрейф/отскок, Вайкофф, ИИ, небо 🟡 с урезанным по
                   нулям лаборатории весом). Гладкая логика уместна ТОЛЬКО
                   здесь — вдали от разрывов.

ВОЛНА Ψ (прокол №3): понижена с «диктатора направления» до КАРТЫ
ЛИКВИДНОСТИ. Режим «край−середина» (единственный доказанный эффект, 8/8)
остаётся маршрутизатором тактики, но возврат-к-среднему требует
ПОДТВЕРЖДЕНИЯ стаканОМ: односторонний токсичный поток без абсорбции
(фронтраннинг ММ — тащит цену без откатов) отменяет фейд и переключает в
моментум. Плюс голос «айсберг»: волна+плита+абсорбция = место, где ММ
выгодно ставить стену; торгуем ОТ стены, не сквозь неё.

Калибровка по журналу сделок сохранена (голоса взвешиваются реальными
деньгами). ⚫ Машина выдаёт состояние среды и вероятность, не судьбу; в
сингулярности она агрессивна по ПРАВУ, данному владельцем, но рамка та же:
НЕ приказ, НЕ обещание прибыли, курок за человеком, 18+.

Self-тест: python3 -m backend.oracle
"""
from __future__ import annotations

import json
import math

# единые константы школы — из первоисточников, литерал-копия была бы багом
from .bifurcation import PHI_CRITICAL      # φ Пригожина «у слома»
from .hawkes import N_CRITICAL             # ветвление Хоукса — лавина
from .microstructure import CASIMIR_CRIT   # предельное сжатие спреда

# ── состояния машины ────────────────────────────────────────────────────────
ST_CHAOS = "ХАОС"
ST_SINGULARITY = "СИНГУЛЯРНОСТЬ"
ST_CRITICAL = "У ПОРОГА"
ST_PARLIAMENT = "ПАРЛАМЕНТ"

# ── веса доверия голосам парламента (единственное место) ────────────────────
W_XRAY = 1.00      # рентген стакана — главный живой голос
W_MAYA = 0.75      # тяга вакуума стакана
W_BIF = 0.90       # двигатель бифуркаций ВНЕ окна (в окне он не голос — вектор)
W_TREND = 0.55
W_DRIFT = 0.60
W_BOUNCE = 0.50
W_WYCKOFF = 0.50
W_ICEBERG = 0.70   # волна×плита×абсорбция: стена ММ (прокол №3)
W_AI = 0.45
W_REACTOR = 0.25   # 🟡 небо: вес урезан по нулям лаборатории
W_WAVE = 0.20      # 🟡 небо

K_SIGMOID = 2.2
# ── КАЛИБРОВКА ВЕРОЯТНОСТЕЙ (измерена на реальных данных MOEX 07.08.2026) ──
# Прямой вопрос владельца: «когда машина пишет 70% или 100% — какой реальный
# шанс попадания?» Замер (lab/calibration.py, 995 наблюдений, горизонты 180
# и 600с) дал жёсткий ответ: заявленные проценты СИСТЕМАТИЧЕСКИ завышены —
#     заявлено 72% → фактически 55%/46%
#     заявлено 84% → фактически 44%/41%
#     заявлено 94% → фактически 50%/54%
# Brier 0.42 против 0.25 у монеты, разрешающая способность ≈0.001 (модель
# почти не различает ситуации, но всегда «уверена»).
#
# Корень дефекта виден в формуле ниже: p_vote = σ(K·score·√den), где den —
# сумма весов голосов. Чем БОЛЬШЕ голосов, тем сильнее насыщается сигмоида —
# уверенность росла от КОЛИЧЕСТВА голосов, а не от их качества. Пять слабых
# согласных голосов давали «94%».
#
# Лечение (два слоя, оба консервативные):
#  1) √den заменён на нормировку числом голосов — количество больше не
#     раздувает уверенность само по себе;
#  2) CAL_TABLE — измеренная поправка «заявлено → реально», монотонная;
#     применяется к итоговой вероятности. Пока не набрана новая статистика,
#     таблица сжимает завышенные проценты к измеренной реальности.
# ⚠ Таблица измерена на ОДНОЙ сессии (1ч44м, 6 инструментов). Это не закон
# рынка, а честная поправка на то, что уже видели. Пересчитывать на новых
# данных: python3 -m backend.lab.calibration --db <база>.
CAL_TABLE = ((0.50, 0.50), (0.72, 0.52), (0.84, 0.53), (0.94, 0.55), (1.0, 0.58))
CAL_ON = True      # выключать только для диагностики сырых чисел
# Пороги ПОСЛЕ калибровки живут в шкале РЕАЛЬНЫХ вероятностей, а не
# заявленных. Максимум, который даёт CAL_TABLE, — 0.58 (conf 0.16), поэтому
# старые пороги 0.20/0.45 стали недостижимы и заблокировали бы всё.
# Новые пороги — в терминах измеренного края:
CONF_ACT = 0.04    # реальная вероятность ≥52% — минимальный край для действия
CONF_CRIT = 0.08   # ≥54% — согласие парламента в состоянии «У ПОРОГА»
# ⚠ Следствие, которое надо понимать: потолок лестницы плеча (leverage,
# CONF_FULL=0.75) в этой шкале НЕДОСТИЖИМ — и это верно. Максимальное плечо
# оправдано только при реальной вероятности ~87%, которой на измеренной
# выборке не наблюдалось ни разу. Пока край 52-58% — размер должен быть
# скромным, иначе толстый хвост съест депозит.
PSI_MID_LO, PSI_MID_HI = 0.35, 0.65
MOM_DAMP, MOM_BOOST = 0.45, 1.25
EXP_MOVE_CAP = 0.05
WINDOW_EXP_BOOST = 1.6
CAL_MIN, CAL_MAX = 0.6, 1.4
CAL_MIN_TRADES = 8
# триггеры сингулярности
SING_PRESSURE = 0.5    # давление слома, ниже которого окно не признаётся
SING_DIR_MIN = 0.25    # |направление слома| ниже — сторона «не названа»
FLOW_TOXIC_VPIN = 0.4  # односторонность потока, отменяющая фейд без абсорбции
PLATE_NEAR_FRAC = 0.002    # плита ближе 0.2% от цены — «у стены»
# ── ДУАЛЬНАЯ МЕХАНИКА СРЕД (спринт PREDATOR: щёлочь/кислота) ───────────────
# «Щёлочь» = мир mean-reversion: хвосты умеренные (Хилл α≥3), распределение
# близко к гауссову — линейный парламент лог-оддсов законен. «Кислота» = мир
# Мандельброта: α<2, дисперсия БЕСКОНЕЧНА — среднее взвешенных голосов не
# сходится, линейное усреднение статистически недействительно. В кислоте
# парламент ОТСТРАНЯЕТСЯ (записывается для журнала, но не решает), право
# голоса остаётся только у векторных детекторов разрыва (LPPLS Сорнетта,
# окно двигателя бифуркаций, критичность Хоукса) — каждый из них несёт
# СОБСТВЕННУЮ квалификацию, поэтому в кислоте вектор признаётся и без
# внешнего давления bif.pressure. Между мирами — переходная зона 2≤α<3:
# парламент говорит, но шёпотом (вероятность сжимается к 0.5 линейно по α).
# 🔵 α — оценка Хилла по ценам (bifurcation.hill_tail); пороги 2/3 —
# конвенция EVT 🟡. Нет α → сред не судим, права полные (NO DUMMIES).
ACID_ALPHA = 2.0       # α ниже — «кислота»: парламент отстранён
ALKALINE_ALPHA = 3.0   # α выше — «щёлочь»: парламент в полных правах
PHI_ACID = 0.985       # φ Пригожина выше в переходной зоне — реакция уже
                       # началась (критическое замедление), добиваем до кислоты
# квантовый триггер 🟡: совпадение трёх независимых предвестников слома —
# пороги берутся из модулей-первоисточников (CASIMIR_CRIT / N_CRITICAL /
# PHI_CRITICAL, импорт выше), совместное срабатывание — конвенция лога

FRAME = ("⚫ машина состояний выдаёт состояние среды и вероятность, не судьбу. "
         "В сингулярности агрессия — право владельца, не рекомендация. Небо 🟡 "
         "несёт урезанный вес по нулям лаборатории. Курок за человеком. 18+")

_MOMENTUM_VOICES = {"trend", "drift"}
_REVERT_VOICES = {"bounce", "iceberg"}


def _f(x, d=0.0):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def _sig(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def calibrate_prob(p: float) -> float:
    """Заявленная вероятность → измеренная реальность (CAL_TABLE, линейно
    между узлами, симметрично относительно 0.5). Без калибровки размер
    позиции считается от вранья: leverage выходит на потолок плеча там, где
    фактическая точность — монета."""
    if not CAL_ON:
        return p
    p = max(0.0, min(1.0, _f(p, 0.5)))
    side = 1.0 if p >= 0.5 else -1.0
    q = 0.5 + abs(p - 0.5)                       # зеркалим в верхнюю половину
    out = 0.5
    for (x0, y0), (x1, y1) in zip(CAL_TABLE, CAL_TABLE[1:]):
        if x0 <= q <= x1:
            t = (q - x0) / (x1 - x0) if x1 > x0 else 0.0
            out = y0 + t * (y1 - y0)
            break
    else:
        out = CAL_TABLE[-1][1] if q > CAL_TABLE[-1][0] else CAL_TABLE[0][1]
    return 0.5 + side * (out - 0.5)


# ── голоса парламента ───────────────────────────────────────────────────────

def _voice_xray(xray: dict | None):
    if not (isinstance(xray, dict) and xray.get("available")):
        return None
    m = xray.get("metrics") or {}
    cvd, obi = _f(m.get("cvd")), _f(m.get("obi"))
    s = 0.0
    if cvd:
        s += 0.6 * (1.0 if cvd > 0 else -1.0)
    if obi:
        s += 0.4 * max(-1.0, min(1.0, obi * 2.0))
    if s == 0.0:
        return None
    if "ИНФАРКТ" in (xray.get("actor") or ""):
        return None
    return (max(-1.0, min(1.0, s)), _f(xray.get("confidence"), 0.3))


def _voice_maya(m: dict | None):
    if not (isinstance(m, dict) and m.get("available")):
        return None
    side = (m.get("pull") or {}).get("side")
    if side not in ("вверх", "вниз"):
        return None
    s = 1.0 if side == "вверх" else -1.0
    ag = m.get("aggressor") or {}
    conf = 0.8 if ag.get("with_pull") is True else \
        0.4 if ag.get("with_pull") is False else 0.6
    return (s, conf)


def _voice_bif(bif: dict | None):
    s = ((bif or {}).get("summary") or {})
    d = s.get("direction")
    if d is None or not d:
        return None
    press = _f(s.get("pressure"), 0.0)
    return (max(-1.0, min(1.0, _f(d))), 0.35 + 0.65 * press)


def _voice_simple(sign: str | None, conf: float):
    if sign in ("long", "up"):
        return (1.0, conf)
    if sign in ("short", "down"):
        return (-1.0, conf)
    return None


def _voice_reactor(rc: dict | None):
    if not (isinstance(rc, dict) and rc.get("mode") == "precise"):
        return None
    bodies = rc.get("bodies") or []
    ins = sum(1 for b in bodies if str((b or {}).get("doppler", "")).startswith("атака"))
    outs = sum(1 for b in bodies if str((b or {}).get("doppler", "")).startswith("всас"))
    if not ins + outs:
        return None
    s = (ins - outs) / (ins + outs)
    crossing = bool(((rc.get("prigogine") or {}).get("crossing")))
    return (max(-1.0, min(1.0, s)), 0.6 if crossing else 0.35)


def _voice_wave(wave: dict | None):
    if not isinstance(wave, dict):
        return None
    slope = wave.get("slope")
    if slope is None:
        return None
    s = max(-1.0, min(1.0, _f(slope) * 3.0))
    if s == 0.0:
        return None
    return (s, max(0.1, min(1.0, _f(wave.get("plv"), 0.3))))


def _voice_iceberg(price, plates, absorbing: bool | None):
    """Прокол №3: волна/плиты как карта ЛИКВИДНОСТИ. Плита рядом + абсорбция
    = ММ ставит стену: торгуем ОТ стены (плита снизу держит → вверх)."""
    if not (price and plates and absorbing):
        return None
    p = _f(price)
    best = None
    for pl in plates:
        v = _f(pl)
        if v <= 0:
            continue
        d = abs(v - p) / p
        if d <= PLATE_NEAR_FRAC and (best is None or d < best[1]):
            best = (v, d)
    if best is None:
        return None
    plate, _ = best
    return (1.0 if plate < p else -1.0, 0.6)


# ── дуальная механика: маршрутизатор сред ──────────────────────────────────

def dual_env(alpha, phi=None) -> dict:
    """Маршрутизатор сред по индексу Хилла α и автокорреляции Пригожина φ.

    Возврат: {env, parliament, why}, где parliament ∈ [0,1] — множитель прав
    линейного парламента: 1.0 — полные права (щёлочь), 0.0 — отстранён
    (кислота), между — переходная зона, вероятность голосования сжимается
    к 0.5 этим множителем.

      α < 2                      → кислота  (дисперсия бесконечна)
      2 ≤ α < 3 и φ ≥ PHI_ACID   → кислота  (хвосты тяжелеют И критическое
                                   замедление: фазовый переход уже идёт)
      2 ≤ α < 3                  → переход  (права = α−2, линейно 0→1)
      α ≥ 3                      → щёлочь   (полные права)
      α неизвестна               → не судим (полные права, NO DUMMIES)
    """
    a = _f(alpha, float("nan"))
    if a != a or a <= 0.0:            # NaN/мусор (α физически > 0) — не судим:
        return {"env": "неизвестно", "parliament": 1.0,      # мусор на входе
                "why": "α Хилла не измерена (или ≤0 — мусор) — среду не судим"}
    if a <= ACID_ALPHA:               # граница ВКЛЮЧИТЕЛЬНО: права и так 0,
        return {"env": "кислота", "parliament": 0.0,   # разрыва поведения нет
                "why": (f"α={a:.2f}≤{ACID_ALPHA:.0f}: дисперсия бесконечна, "
                        "линейное усреднение голосов недействительно")}
    if a < ALKALINE_ALPHA:
        p = _f(phi, float("nan"))
        if p == p and p >= PHI_ACID:
            return {"env": "кислота", "parliament": 0.0,
                    "why": (f"α={a:.2f} тяжёлая И φ={p:.3f}≥{PHI_ACID} — "
                            "критическое замедление: переход уже идёт")}
        t = (a - ACID_ALPHA) / (ALKALINE_ALPHA - ACID_ALPHA)
        return {"env": "переход", "parliament": round(t, 3),
                "why": (f"α={a:.2f} в переходной зоне ({ACID_ALPHA:.0f};"
                        f"{ALKALINE_ALPHA:.0f}) — права парламента {t:.2f}")}
    return {"env": "щёлочь", "parliament": 1.0,
            "why": f"α={a:.2f}≥{ALKALINE_ALPHA:.0f}: хвосты умеренные, парламент в правах"}


# ── режим тактики: «край−середина» ПОД ПОДТВЕРЖДЕНИЕМ стакана ──────────────

def accel_gate(ret_win, accel) -> dict:
    """ВОРОТА УСКОРЕНИЯ (замер на реальных данных MOEX 07.08.2026).

    Найдено веером на живом рынке: разворот (фейд) стоит торговать ТОЛЬКО на
    РАЗГОНЯЮЩЕМСЯ ходе — когда последняя треть окна прошла больше первой.
    На тормозящем ходе фейд превращается в честную монету:

        180с: разгон 61.8% (σ+4.71) · торможение 50.6% (σ+0.30), z=+3.41
        600с: разгон 59.9% (σ+3.91) · торможение 47.5% (σ−1.18), z=+3.12

    Держится на 6 инструментах из 6 (180с) и монотонно по порогу силы —
    это структура, а не одна красивая ячейка. Экономический смысл прозрачен:
    разворачивается ПЕРЕГРЕТЫЙ импульс, а выдохшийся просто затухает.

    ⚠ ЧЕСТНО О СТАТУСЕ: с поправкой на перекрытие окон σ падает до +1.92, а
    семейная p-value с учётом перебора гипотез = 0.395. Экономически ни одна
    конфигурация не окупила комиссию 5 бп. Поэтому это ГИПОТЕЗА О РЕЖИМЕ 🟡,
    а не торговое правило: она гасит фейд там, где он статистически пуст, и
    не даёт разрешения на агрессию.

    Возврат: {open: bool, why: str}. Нет данных → open=True (не мешаем)."""
    r, a = _f(ret_win), _f(accel)
    if r == 0.0 or a == 0.0:
        return {"open": True, "why": "нет данных об ускорении — ворота не судят"}
    same = (r > 0) == (a > 0)
    return {"open": bool(same),
            "why": ("ход РАЗГОНЯЕТСЯ — перегретый импульс, фейд осмыслен"
                    if same else
                    "ход ТОРМОЗИТ — фейд статистически пуст (50.6%), гасим")}


def regime_of(psi_now, hurst, *, vpin=None, absorbing=None,
              ret_win=None, accel=None) -> dict:
    """Маршрутизатор тактики. Первичен доказанный эффект «край−середина»
    (8/8, IAAFT), но фейд БЕЗ подтверждения стаканом запрещён (прокол №3):
    односторонний токсичный поток (VPIN≥0.4) без абсорбции = фронтраннинг —
    фейдить его самоубийственно, переключаемся в моментум за потоком."""
    base = None
    if psi_now is not None:
        p = _f(psi_now)
        if PSI_MID_LO <= p <= PSI_MID_HI:
            base = {"mode": "mean_revert", "src": "Ψ-середина (эффект 8/8)",
                    "psi": round(p, 3)}
        else:
            base = {"mode": "momentum", "src": "Ψ-край (эффект 8/8)",
                    "psi": round(p, 3)}
    elif hurst is not None:
        h = _f(hurst, 0.5)
        if h > 0.55:
            base = {"mode": "momentum", "src": f"Хёрст {h:.2f}>0.55"}
        elif h < 0.45:
            base = {"mode": "mean_revert", "src": f"Хёрст {h:.2f}<0.45"}
    if base is None:
        return {"mode": "mixed", "src": "нет Ψ и Хёрст нейтрален"}
    # ВОРОТА УСКОРЕНИЯ: фейд без разгона статистически пуст (замер на MOEX)
    if base["mode"] == "mean_revert" and ret_win is not None and accel is not None:
        g = accel_gate(ret_win, accel)
        if not g["open"]:
            return {"mode": "mixed", "src": base["src"] + " ГАШЕН: " + g["why"],
                    "accel_gated": True}
    if (base["mode"] == "mean_revert" and vpin is not None
            and _f(vpin) >= FLOW_TOXIC_VPIN and not absorbing):
        return {"mode": "momentum",
                "src": (base["src"] + f" ОТМЕНЁН: поток односторонний "
                        f"(VPIN {_f(vpin):.2f}) без абсорбции — фронтраннинг, "
                        "не фейдим"), "demoted": True}
    return base


# ── калибровка по журналу ───────────────────────────────────────────────────

def calibrate(journal_rows: list) -> dict:
    acc: dict[str, list] = {}
    for r in journal_rows or []:
        voices = (r or {}).get("voices") or {}
        pnl = _f((r or {}).get("pnl"))
        if not voices or pnl == 0:
            continue
        for name, s in voices.items():
            sv = _f(s)
            if sv == 0:
                continue
            hit = 1.0 if (sv > 0) == (pnl > 0) else 0.0
            acc.setdefault(name, []).append((hit, abs(sv)))
    out = {}
    for name, obs in acc.items():
        if len(obs) < CAL_MIN_TRADES:
            out[name] = 1.0
            continue
        wsum = sum(w for _, w in obs)
        hit = (sum(h * w for h, w in obs) / wsum) if wsum > 0 else 0.5
        out[name] = max(CAL_MIN, min(CAL_MAX, 0.5 + hit))
    return out


def load_journal(path) -> list:
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


# ── МАШИНА СОСТОЯНИЙ ────────────────────────────────────────────────────────

def _singularity_vector(bs: dict, lp: dict, hawkes_n, cvd,
                        phi=None, casimir_a=None, alpha=None) -> tuple | None:
    """Есть ли у среды НАЗВАННАЯ сторона слома? → (направление ±1, давление,
    источник). Иначе None. Это и есть Абсолютный Вектор (прокол №1)."""
    press = _f(bs.get("pressure"), 0.0)
    # LPPLS: сингулярность близко и сторона названа — самый прямой вектор
    if lp and lp.get("qualified") and lp.get("near"):
        side = -1.0 if _f(lp.get("B")) < 0 else 1.0
        return (side, max(press, 0.6), f"LPPLS Сорнетта: tc≈{lp.get('tc_bars_ahead')} баров")
    # окно двигателя бифуркаций с направлением
    if bs.get("window_open") and abs(_f(bs.get("direction"))) >= SING_DIR_MIN:
        return (1.0 if _f(bs.get("direction")) > 0 else -1.0, press,
                "окно двигателя бифуркаций")
    # критичность Хоукса + давление: сторона — куда бьёт поток
    if (hawkes_n is not None and _f(hawkes_n) >= N_CRITICAL
            and press >= SING_PRESSURE
            and cvd is not None and _f(cvd) != 0):
        return (1.0 if _f(cvd) > 0 else -1.0, press,
                f"критичность Хоукса n={_f(hawkes_n):.2f} + поток")
    # T95 — АБСОЛЮТНЫЙ ТРИГГЕР СИНГУЛЯРНОСТИ 🟡 (спринт «Абсолют»): ЧЕТЫРЕ
    # НЕЗАВИСИМЫХ эфирных состояния схлопнулись разом —
    #   α ≤ ACID_ALPHA        кислота: дисперсия бесконечна (Хилл),
    #   0 < a ≤ CASIMIR_CRIT  спред сжат до предела (Казимир-пружина),
    #   n ≥ N_CRITICAL        лавина потока (Хоукс),
    #   φ ≥ PHI_CRITICAL      критическое замедление (Пригожин).
    # Сторона — куда бьёт поток (CVD). Каждый компонент измерен кодом 🔵,
    # совместный порог — конвенция лога, нулями НЕ проверен — потому 🟡.
    # «95%» в имени — риторика источника, НЕ измеренная вероятность: замер
    # ВАЛИДАЦИЯ_ЧЕСТНАЯ давал по-компонентно 44–49%; совместное срабатывание
    # на доступных данных редкость — статистики нет. a строго > 0: нулевой/
    # скрещённый спред — сломанный стакан, а не «сжатие»; α обязательна:
    # без кислоты это не T95 (спецификация владельца).
    if (alpha is not None and 0.0 < _f(alpha, -1.0) <= ACID_ALPHA
            and casimir_a is not None and 0.0 < _f(casimir_a, -1.0) <= CASIMIR_CRIT
            and hawkes_n is not None and _f(hawkes_n) >= N_CRITICAL
            and phi is not None and _f(phi) >= PHI_CRITICAL
            and cvd is not None and _f(cvd) != 0):
        return (1.0 if _f(cvd) > 0 else -1.0, max(press, 0.6),
                (f"T95 🟡: α={_f(alpha):.2f} (кислота) + Казимир "
                 f"a={_f(casimir_a):.2f} + Хоукс n={_f(hawkes_n):.2f} + "
                 f"Пригожин φ={_f(phi):.3f}"))
    return None


def verdict(inputs: dict, weight_mults: dict | None = None) -> dict:
    """Вердикт машины состояний. inputs (всё опционально — NO DUMMIES):
      xray, maya, bif, reactor, wave {slope, plv, psi_now}, ai_bias,
      wyckoff_bias, trend, drift, bounce, hurst, atr_frac, corridor_frac,
      hawkes {n,...}, plates [уровни цены], price, mathieu_unstable bool."""
    d = inputs or {}
    mults = weight_mults or {}
    xray = d.get("xray") or {}
    xm = xray.get("metrics") or {}
    wave = d.get("wave") or {}
    bs = ((d.get("bif") or {}).get("summary") or {})
    lp = ((d.get("bif") or {}).get("lppls") or {})
    if not isinstance(lp, dict) or "error" in lp:
        lp = {}
    hw = d.get("hawkes") or {}
    absorbing = None
    if xray.get("available"):
        # абсорбция видна классификатору как актор «КИТ» либо передана майей
        absorbing = ("КИТ" in (xray.get("actor") or "")) or bool(d.get("absorbing"))
    # ── дуальная механика: среда решает, кому дано право голоса ──
    alpha = bs.get("tail_alpha")
    if alpha is None:
        alpha = d.get("hill_alpha")                # прямой вход (HFT-фид/стенд)
    phi = None                                     # только честное число, не
    phi_pr = (d.get("bif") or {}).get("prigogine")  # фабрикуем 0.0 из мусора
    if isinstance(phi_pr, dict) and "error" not in phi_pr:
        _p = phi_pr.get("phi")
        if isinstance(_p, (int, float)) and not isinstance(_p, bool) and _p == _p:
            phi = float(_p)
    env = dual_env(alpha, phi)
    acid = env["env"] == "кислота"
    regime = regime_of(wave.get("psi_now"), d.get("hurst"),
                       vpin=xm.get("vpin"), absorbing=absorbing,
                       ret_win=d.get("ret_win"), accel=d.get("accel"))
    if acid and regime["mode"] == "mean_revert":
        # в кислоте фейдить нечего: импульс с бесконечной дисперсией не
        # «возвращается к среднему» — гасим возврат, идём за потоком
        regime = {"mode": "momentum",
                  "src": regime["src"] + " ОТМЕНЁН средой: " + env["why"],
                  "acid_forced": True}
    mode = regime["mode"]

    # ── парламент: лог-оддсы (всегда считается — журнал и фолбэк) ──
    voices = {}
    raw = {
        "xray": (_voice_xray(xray), W_XRAY),
        "maya": (_voice_maya(d.get("maya")), W_MAYA),
        "bif": (_voice_bif(d.get("bif")), W_BIF),
        "trend": (_voice_simple(d.get("trend"), 0.6), W_TREND),
        "drift": (_voice_simple(d.get("drift"), 0.7), W_DRIFT),
        "bounce": ((1.0, 0.7) if d.get("bounce") else None, W_BOUNCE),
        "iceberg": (_voice_iceberg(d.get("price"), d.get("plates"), absorbing),
                    W_ICEBERG),
        "wyckoff": ((max(-1.0, min(1.0, _f(d.get("wyckoff_bias")))), 0.6)
                    if d.get("wyckoff_bias") is not None else None, W_WYCKOFF),
        "ai": (_voice_simple(d.get("ai_bias"), 0.5), W_AI),
        "reactor": (_voice_reactor(d.get("reactor")), W_REACTOR),
        "wave": (_voice_wave(wave), W_WAVE),
    }
    num = den = 0.0
    for name, (v, w) in raw.items():
        if v is None:
            continue
        s, c = v
        m = _f(mults.get(name), 1.0)
        if mode == "mean_revert" and name in _MOMENTUM_VOICES:
            m *= MOM_DAMP
        elif mode == "momentum" and name in _MOMENTUM_VOICES:
            m *= MOM_BOOST
        elif mode == "mean_revert" and name in _REVERT_VOICES:
            m *= MOM_BOOST
        elif mode == "momentum" and name in _REVERT_VOICES:
            m *= MOM_DAMP
        voices[name] = {"s": round(s, 3), "c": round(c, 3), "w": round(w * m, 3)}
        num += w * m * c * s
        den += w * m * c
    score = (num / den) if den > 0 else 0.0
    # УВЕРЕННОСТЬ ОТ КАЧЕСТВА, НЕ ОТ КОЛИЧЕСТВА (замер калибровки показал:
    # √den раздувал проценты числом голосов). Теперь den нормируется на сумму
    # весов — согласие пяти слабых голосов больше не даёт «94%».
    w_sum = sum(v["w"] for v in voices.values()) or 1e-9
    strength = min(1.0, den / w_sum)             # средняя уверенность голосов
    p_raw = _sig(K_SIGMOID * score * strength)
    p_vote = calibrate_prob(p_raw)               # → измеренная реальность
    conf_vote = abs(2.0 * p_vote - 1.0)

    # ── иерархия состояний ──
    # фазовый модулятор макро-циклов («Сырая дата», удар №2): синхронный хор
    # циклов ДЕЛИТ пороги — сингулярность и согласие признаются раньше 🟡
    sens = max(1.0, _f(d.get("sens_mult"), 1.0))
    sing_thr = SING_PRESSURE / sens
    crit_thr = CONF_CRIT / sens
    press = _f(bs.get("pressure"), 0.0)
    sing = _singularity_vector(bs, lp, hw.get("n"), xm.get("cvd"),
                               phi=phi, casimir_a=xm.get("casimir_a"),
                               alpha=alpha)
    infarct = "ИНФАРКТ" in (xray.get("actor") or "")
    # права парламента по среде: вероятность голосования сжимается к 0.5
    # множителем прав (кислота → 0.5 ровно: линейный мир не судит разрывы)
    p_dec = 0.5 + (p_vote - 0.5) * env["parliament"]
    conf_dec = abs(2.0 * p_dec - 1.0)

    if infarct:
        state, override = ST_CHAOS, False
        direction, p_long, conf = "flat", 0.5, 0.0
        why = "инфаркт спреда — вето всему, наблюдаем"
    elif sing is not None and (press >= sing_thr or acid):
        # в кислоте вектор признаётся без внешнего давления: каждый источник
        # вектора (_singularity_vector) несёт собственную квалификацию, а
        # парламент всё равно отстранён — либо вектор, либо наблюдение
        state, override = ST_SINGULARITY, True
        sdir, spress, ssrc = sing
        direction = "long" if sdir > 0 else "short"
        # даже вектор сингулярности проходит калибровку: замер показал, что
        # высокие проценты не подтверждаются фактом (см. CAL_TABLE)
        p_long = calibrate_prob(0.5 + 0.5 * min(1.0, 0.55 + 0.45 * spress) * sdir)
        conf = abs(2.0 * p_long - 1.0)
        mode = "momentum"                      # слом торгуется ЗА вектором
        why = (f"OVERRIDE: {ssrc} — вектор {direction}, парламент записан, не решает"
               + (f" [{env['env']}: {env['why']}]" if acid else ""))
    elif press >= sing_thr:
        state, override = ST_CRITICAL, False
        if acid:
            direction, p_long, conf = "flat", 0.5, 0.0
            why = (f"у порога, но среда — кислота ({env['why']}): вход только "
                   "по вектору сингулярности, ждём")
        elif conf_dec >= crit_thr and voices:
            direction = "long" if p_dec > 0.5 else "short"
            p_long, conf = p_dec, conf_dec
            why = ("у порога, сторона слома не названа — вход только по "
                   f"сильному согласию парламента ({conf_dec:.2f})")
        else:
            direction, p_long, conf = "flat", p_dec, conf_dec
            why = "у порога: давление есть, сторона не названа, согласия нет — ждём"
    else:
        state, override = ST_PARLIAMENT, False
        p_long, conf = p_dec, conf_dec
        if acid:
            direction = "flat"
            why = f"парламент ОТСТРАНЁН ({env['why']}) — ждём вектор сингулярности"
        else:
            direction = ("flat" if (conf < CONF_ACT or not voices)
                         else ("long" if p_long > 0.5 else "short"))
            why = f"парламент: P(вверх)={p_long:.2f} по {len(voices)} голосам"

    # окно бифуркации (для лестницы): сингулярность ИЛИ совпадение детекторов
    pr = ((d.get("reactor") or {}).get("prigogine") or {})
    win_votes = [state == ST_SINGULARITY,
                 bool(bs.get("window_open")),
                 bool(pr.get("crossing")) and press >= sing_thr,
                 bool(d.get("mathieu_unstable")) and press >= sing_thr]
    window_open = any(win_votes) and direction != "flat"

    parts = []
    if bs.get("predictability") is not None:
        parts.append(_f(bs.get("predictability")))
    if wave.get("plv") is not None:
        parts.append(_f(wave.get("plv")))
    predictability = round(sum(parts) / len(parts), 3) if parts else 0.35

    atr_f = max(0.0, _f(d.get("atr_frac")))
    cor_f = max(0.0, _f(d.get("corridor_frac")))
    exp_move = max(atr_f, cor_f)
    if window_open:
        exp_move *= WINDOW_EXP_BOOST
    if mode == "mean_revert" and cor_f > 0:
        exp_move = min(exp_move, cor_f)
    exp_move = min(exp_move, EXP_MOVE_CAP)

    a_out = _f(alpha, float("nan"))
    return {
        "state": state, "override": bool(override),
        "dir": direction, "p_long": round(p_long, 4),
        "p_vote": round(p_vote, 4), "score": round(score, 4),
        "confidence": round(conf, 3), "mode": mode,
        "env": env["env"], "parliament_rights": env["parliament"],
        "hill_alpha": (round(a_out, 2) if a_out == a_out else None),
        "prigogine_phi": (round(phi, 4) if phi is not None else None),
        "regime_src": regime["src"],
        "window_open": bool(window_open),
        "predictability": predictability,
        "expected_move_frac": round(exp_move, 5),
        "voices": voices, "n_voices": len(voices),
        "bif_regime": bs.get("regime"),
        "hawkes_n": hw.get("n"),
        "reason": f"[{state}] {why}",
        "frame": FRAME,
    }


# ── self-test ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    up_xray = {"available": True, "confidence": 0.7,
               "metrics": {"cvd": 300, "obi": 0.4}}
    up_maya = {"available": True, "pull": {"side": "вверх"},
               "aggressor": {"with_pull": True}}
    hot_bif = {"summary": {"direction": 0.8, "pressure": 0.75,
                           "predictability": 0.6, "window_open": True,
                           "regime": "БИФУРКАЦИЯ ОТКРЫТА"}}

    # 1) все голоса вверх + окно с направлением → СИНГУЛЯРНОСТЬ, long, окно
    v = verdict({"xray": up_xray, "maya": up_maya, "bif": hot_bif,
                 "trend": "up", "ai_bias": "long", "hurst": 0.62,
                 "atr_frac": 0.004, "corridor_frac": 0.006})
    assert v["state"] == ST_SINGULARITY and v["override"], v["state"]
    assert v["dir"] == "long" and v["p_long"] > 0.5, v
    #  калибровка: заявленная вероятность больше не раздувается (было >0.9)
    assert v["p_long"] < 0.65, ("после калибровки проценты честные", v)
    assert v["window_open"] is True and v["mode"] == "momentum"
    assert v["expected_move_frac"] > 0.006

    # 1b) ПРОКОЛ №1 ЗАКРЫТ: сингулярность ВНИЗ не размывается шумом вверх —
    #     парламент единогласно long, но вектор слома short ПЕРЕХВАТЫВАЕТ
    down_bif = {"summary": {"direction": -0.7, "pressure": 0.8,
                            "predictability": 0.6, "window_open": True,
                            "regime": "БИФУРКАЦИЯ ОТКРЫТА"}}
    v_ov = verdict({"xray": up_xray, "maya": up_maya, "trend": "up",
                    "ai_bias": "long", "bif": down_bif})
    assert v_ov["state"] == ST_SINGULARITY and v_ov["dir"] == "short", v_ov
    assert v_ov["override"] is True and v_ov["p_vote"] > 0.5   # парламент был за long
    #     LPPLS-вектор тоже перехватывает (Король-дракон)
    v_lp = verdict({"xray": up_xray, "trend": "up",
                    "bif": {"summary": {"direction": 0.0, "pressure": 0.6,
                                        "predictability": 0.5,
                                        "window_open": False, "regime": "У ПОРОГА"},
                            "lppls": {"qualified": True, "near": True, "B": -0.004,
                                      "tc_bars_ahead": 12}}})
    assert v_lp["state"] == ST_SINGULARITY and v_lp["dir"] == "short", v_lp

    # 1c) КАЛИБРОВКА: «заявлено N%» → измеренная реальность (CAL_TABLE)
    assert abs(calibrate_prob(0.5) - 0.5) < 1e-9          # нейтраль не двигается
    assert calibrate_prob(0.94) < 0.60, calibrate_prob(0.94)   # 94% → ~55%
    assert calibrate_prob(0.06) > 0.40                    # симметрия вниз
    assert abs((calibrate_prob(0.94) - 0.5) + (calibrate_prob(0.06) - 0.5)) < 1e-9
    #    монотонность: больше заявлено — не меньше реально
    assert calibrate_prob(0.94) >= calibrate_prob(0.84) >= calibrate_prob(0.72)
    #    уверенность больше НЕ растёт от количества слабых голосов
    many = verdict({"trend": "up", "drift": "long", "ai_bias": "long",
                    "wyckoff_bias": 0.5})
    one = verdict({"trend": "up"})
    assert many["p_long"] <= one["p_long"] + 0.10, (many["p_long"], one["p_long"])

    # 2) нет голосов → парламент, flat
    v0 = verdict({})
    assert v0["state"] == ST_PARLIAMENT and v0["dir"] == "flat"
    assert abs(v0["p_long"] - 0.5) < 1e-9 and not v0["window_open"]

    # 3) У ПОРОГА: давление есть, стороны нет → вход только по согласию
    crit_bif = {"summary": {"direction": 0.05, "pressure": 0.6,
                            "predictability": 0.5, "window_open": False,
                            "regime": "У ПОРОГА"}}
    v_cr = verdict({"bif": crit_bif})                        # согласия нет
    assert v_cr["state"] == ST_CRITICAL and v_cr["dir"] == "flat", v_cr
    v_cr2 = verdict({"bif": crit_bif, "xray": up_xray, "maya": up_maya,
                     "trend": "up"})                          # парламент един
    #  после калибровки «единый парламент» = реальный край ~52%, а не «94%».
    #  Проверяем механику: согласие ПОДНИМАЕТ уверенность против разнобоя,
    #  и вход открывается ровно тогда, когда край перевалил CONF_CRIT.
    assert v_cr2["state"] == ST_CRITICAL, v_cr2
    assert v_cr2["confidence"] > v_cr["confidence"], (v_cr2, v_cr)
    assert (v_cr2["dir"] == "long") == (v_cr2["confidence"] >= CONF_CRIT), v_cr2

    # 4) ХАОС: инфаркт спреда — вето даже при открытом окне
    v_ch = verdict({"xray": {"available": True, "actor": "ИНФАРКТ СПРЕДА (хаос)",
                             "metrics": {"cvd": 500}}, "bif": hot_bif})
    assert v_ch["state"] == ST_CHAOS and v_ch["dir"] == "flat", v_ch

    # 5) ПРОКОЛ №3 ЗАКРЫТ: Ψ-середина зовёт фейд, но односторонний токсичный
    #    поток без абсорбции (фронтраннинг ММ) отменяет — моментум за потоком
    v_fr = verdict({"wave": {"psi_now": 0.5},
                    "xray": {"available": True, "confidence": 0.6,
                             "metrics": {"cvd": 900, "obi": 0.3, "vpin": 0.65}},
                    "trend": "up"})
    assert v_fr["mode"] == "momentum" and "не фейдим" in v_fr["regime_src"], v_fr
    #    а при абсорбции (Кит впитывает) фейд разрешён
    v_ab = verdict({"wave": {"psi_now": 0.5},
                    "xray": {"available": True, "confidence": 0.6,
                             "actor": "КИТ грузит айсберг",
                             "metrics": {"cvd": 900, "obi": 0.3, "vpin": 0.65}}})
    assert v_ab["mode"] == "mean_revert", v_ab

    # 5b) голос «айсберг»: плита под ценой + абсорбция → тяга вверх от стены
    v_ic = verdict({"price": 100.0, "plates": [99.85, 108.0],
                    "xray": {"available": True, "confidence": 0.6,
                             "actor": "КИТ грузит айсберг",
                             "metrics": {"cvd": 100, "obi": 0.1}}})
    assert "iceberg" in v_ic["voices"] and v_ic["voices"]["iceberg"]["s"] > 0, v_ic

    # 6) Хоукс-критичность + давление + поток вниз → сингулярность short
    v_hw = verdict({"bif": {"summary": {"direction": 0.0, "pressure": 0.55,
                                        "predictability": 0.4,
                                        "window_open": False, "regime": "У ПОРОГА"}},
                    "hawkes": {"n": 0.97},
                    "xray": {"available": True, "confidence": 0.6,
                             "metrics": {"cvd": -400, "obi": -0.2}}})
    assert v_hw["state"] == ST_SINGULARITY and v_hw["dir"] == "short", v_hw
    assert "Хоукса" in v_hw["reason"]

    # 7) небо 🟡 не перекрикивает стакан в парламенте
    dn_xray = {"available": True, "confidence": 0.7,
               "metrics": {"cvd": -300, "obi": -0.4}}
    rc = {"mode": "precise", "bodies": [{"doppler": "атака"}] * 5,
          "prigogine": {"crossing": True}}
    v7 = verdict({"xray": dn_xray, "reactor": rc})
    assert v7["state"] == ST_PARLIAMENT and v7["p_long"] < 0.5, v7

    # 8) калибровка работает как раньше
    rows = ([{"voices": {"bif": 1.0}, "pnl": 50}] * 10
            + [{"voices": {"bif": 1.0}, "pnl": -30}] * 2)
    cal = calibrate(rows)
    assert cal["bif"] > 1.2, cal
    v_cal = verdict({"bif": hot_bif}, weight_mults={"bif": CAL_MIN})
    v_raw = verdict({"bif": hot_bif})
    assert v_cal["voices"]["bif"]["w"] < v_raw["voices"]["bif"]["w"]

    # 8b) ВОРОТА УСКОРЕНИЯ (замер на реальных данных MOEX): фейд разрешён
    #     только на разгоняющемся ходе, на тормозящем — гасится
    assert accel_gate(0.01, 0.003)["open"] is True       # растёт и разгоняется
    assert accel_gate(0.01, -0.003)["open"] is False     # растёт, но тормозит
    assert accel_gate(-0.01, -0.003)["open"] is True     # падает и ускоряется
    assert accel_gate(0.0, 0.0)["open"] is True          # нет данных — не мешаем
    v_gate = verdict({"wave": {"psi_now": 0.5}, "trend": "up",
                      "ret_win": 0.01, "accel": -0.003})
    assert v_gate["mode"] == "mixed" and "ГАШЕН" in v_gate["regime_src"], v_gate
    v_open = verdict({"wave": {"psi_now": 0.5}, "trend": "up",
                      "ret_win": 0.01, "accel": 0.003})
    assert v_open["mode"] == "mean_revert", v_open

    # 9) фазовый модулятор («Сырая дата», удар №2): синхронный хор циклов
    #    ДЕЛИТ порог — слом с давлением 0.35 признаётся сингулярностью
    low_bif = {"summary": {"direction": -0.7, "pressure": 0.35,
                           "predictability": 0.5, "window_open": True,
                           "regime": "напряжение копится"}}
    v_nm = verdict({"bif": low_bif})
    v_m = verdict({"bif": low_bif, "sens_mult": 1.6})
    assert v_nm["state"] != ST_SINGULARITY, v_nm["state"]
    assert v_m["state"] == ST_SINGULARITY and v_m["dir"] == "short", v_m
    #    модулятор не выдумывает вектор: без направления слома он бессилен
    v_m0 = verdict({"bif": {"summary": {"direction": 0.0, "pressure": 0.35,
                                        "predictability": 0.5,
                                        "window_open": False,
                                        "regime": "напряжение"}},
                    "sens_mult": 1.6})
    assert v_m0["state"] != ST_SINGULARITY

    # 10) ДУАЛЬНАЯ МЕХАНИКА (PREDATOR): щёлочь/переход/кислота
    #    (а) маршрутизатор сред: границы и монотонность прав
    assert dual_env(None)["parliament"] == 1.0          # α нет — не судим
    assert dual_env(3.5)["env"] == "щёлочь" and dual_env(3.5)["parliament"] == 1.0
    assert dual_env(1.8)["env"] == "кислота" and dual_env(1.8)["parliament"] == 0.0
    assert dual_env(2.5)["env"] == "переход"
    assert 0.0 < dual_env(2.5)["parliament"] < 1.0
    assert dual_env(2.2)["parliament"] < dual_env(2.8)["parliament"]
    #    φ Пригожина добивает переходную зону до кислоты (но не щёлочь)
    assert dual_env(2.5, phi=0.99)["env"] == "кислота"
    assert dual_env(3.5, phi=0.99)["env"] == "щёлочь"
    #    граница α=2.0 — кислота ВКЛЮЧИТЕЛЬНО (округление hill_tail до 2
    #    знаков затягивает 1.995..2.0 ровно сюда — разрыва поведения нет)
    assert dual_env(2.0)["env"] == "кислота"
    #    мусор на входе не делает машину агрессивнее (NO DUMMIES):
    #    α≤0 физически невозможна — среду не судим, права полные
    assert dual_env(0.0)["env"] == "неизвестно"
    assert dual_env(-1.0)["parliament"] == 1.0
    assert dual_env("мусор")["env"] == "неизвестно"

    #    (б) КИСЛОТА: единогласный парламент вверх ОТСТРАНЁН — flat
    acid_bif = {"summary": {"direction": 0.0, "pressure": 0.1,
                            "predictability": 0.4, "window_open": False,
                            "regime": "линейный", "tail_alpha": 1.8,
                            "fat_tails": True}}
    v_ac = verdict({"xray": up_xray, "maya": up_maya, "trend": "up",
                    "ai_bias": "long", "bif": acid_bif})
    assert v_ac["env"] == "кислота" and v_ac["dir"] == "flat", v_ac
    assert v_ac["state"] == ST_PARLIAMENT and "ОТСТРАНЁН" in v_ac["reason"]
    assert abs(v_ac["p_long"] - 0.5) < 1e-9              # сжато к монете
    assert v_ac["p_vote"] > 0.5                          # журнал помнит голоса

    #    (в) КИСЛОТА + вектор LPPLS: сингулярность стреляет БЕЗ давления bif
    v_al = verdict({"xray": up_xray, "trend": "up",
                    "bif": {"summary": {"direction": 0.0, "pressure": 0.1,
                                        "predictability": 0.4,
                                        "window_open": False,
                                        "regime": "линейный",
                                        "tail_alpha": 1.6},
                            "lppls": {"qualified": True, "near": True,
                                      "B": -0.004, "tc_bars_ahead": 9}}})
    assert v_al["state"] == ST_SINGULARITY and v_al["dir"] == "short", v_al
    assert v_al["env"] == "кислота" and v_al["override"] is True

    #    (г) ПЕРЕХОД: та же картина голосов, но уверенность сжата линейно по α
    def _with_alpha(a):
        b = {"summary": dict(acid_bif["summary"], tail_alpha=a)}
        return verdict({"xray": up_xray, "maya": up_maya, "trend": "up",
                        "ai_bias": "long", "bif": b})
    v35, v25 = _with_alpha(3.5), _with_alpha(2.5)
    assert v35["confidence"] > v25["confidence"] > v_ac["confidence"], \
        (v35["confidence"], v25["confidence"], v_ac["confidence"])
    assert v35["dir"] == "long"                          # щёлочь торгует

    #    (д) кислота гасит фейд: Ψ-середина звала mean_revert → momentum
    v_am = verdict({"wave": {"psi_now": 0.5}, "trend": "up", "bif": acid_bif})
    assert v_am["mode"] == "momentum" and "ОТМЕНЁН средой" in v_am["regime_src"], v_am

    #    (е) У ПОРОГА в кислоте: давление есть, парламент всё равно не пускают
    v_acr = verdict({"xray": up_xray, "maya": up_maya, "trend": "up",
                     "bif": {"summary": {"direction": 0.05, "pressure": 0.6,
                                         "predictability": 0.5,
                                         "window_open": False,
                                         "regime": "У ПОРОГА",
                                         "tail_alpha": 1.7}}})
    assert v_acr["state"] == ST_CRITICAL and v_acr["dir"] == "flat", v_acr
    assert "кислота" in v_acr["reason"]

    #    (ж) прямой вход hill_alpha (HFT-фид) работает как tail_alpha из bif
    v_direct = verdict({"xray": up_xray, "trend": "up", "hill_alpha": 1.8})
    assert v_direct["env"] == "кислота" and v_direct["dir"] == "flat"

    #    (з) КВАНТОВЫЙ ТРИГГЕР 🟡: Казимир a≤0.40 + Хоукс n≥0.95 + Пригожин
    #        φ≥0.97 + поток вниз → сингулярность short БЕЗ давления bif
    qt_bif = {"summary": {"direction": 0.0, "pressure": 0.1,
                          "predictability": 0.4, "window_open": False,
                          "regime": "линейный"},
              "prigogine": {"phi": 0.985}}
    v_qt = verdict({"bif": qt_bif, "hawkes": {"n": 0.96},
                    "xray": {"available": True, "confidence": 0.6,
                             "metrics": {"cvd": -700, "obi": -0.2,
                                         "casimir_a": 0.3}},
                    "hill_alpha": 1.8})
    assert v_qt["state"] == ST_SINGULARITY and v_qt["dir"] == "short", v_qt
    assert "T95" in v_qt["reason"]
    #        T95 БЕЗ кислоты не существует: та же физика, но α=3.5 → щёлочь,
    #        триггер молчит (спецификация: (α<2)∧(a≤0.40)∧(n≥0.95)∧(φ≥0.97))
    v_qt_alk = verdict({"bif": qt_bif, "hawkes": {"n": 0.96},
                        "xray": {"available": True, "confidence": 0.6,
                                 "metrics": {"cvd": -700, "obi": -0.2,
                                             "casimir_a": 0.3}},
                        "hill_alpha": 3.5})
    assert v_qt_alk["state"] != ST_SINGULARITY, v_qt_alk
    #        α не измерена → триггер тоже молчит (NO DUMMIES, не выдумываем)
    v_qt_na = verdict({"bif": qt_bif, "hawkes": {"n": 0.96},
                       "xray": {"available": True, "confidence": 0.6,
                                "metrics": {"cvd": -700, "obi": -0.2,
                                            "casimir_a": 0.3}}})
    assert v_qt_na["state"] != ST_SINGULARITY, v_qt_na
    #        любой из трёх предвестников выпал → триггера нет (парламент/кислота)
    v_qt0 = verdict({"bif": qt_bif, "hawkes": {"n": 0.90},
                     "xray": {"available": True, "confidence": 0.6,
                              "metrics": {"cvd": -700, "obi": -0.2,
                                          "casimir_a": 0.3}}})
    assert v_qt0["state"] != ST_SINGULARITY, v_qt0
    v_qt1 = verdict({"bif": qt_bif, "hawkes": {"n": 0.96},
                     "xray": {"available": True, "confidence": 0.6,
                              "metrics": {"cvd": -700, "obi": -0.2,
                                          "casimir_a": 0.8}}})
    assert v_qt1["state"] != ST_SINGULARITY, v_qt1
    #        a=0.0 (скрещённый/нулевой спред — сломанный стакан, не сжатие)
    #        триггер НЕ признаёт: гигиена данных важнее красивого совпадения
    v_qt_z = verdict({"bif": qt_bif, "hawkes": {"n": 0.96},
                      "xray": {"available": True, "confidence": 0.6,
                               "metrics": {"cvd": -700, "obi": -0.2,
                                           "casimir_a": 0.0}}})
    assert v_qt_z["state"] != ST_SINGULARITY, v_qt_z
    #        мусорный φ (строка) не фабрикует 0.0 в журнале
    v_phi = verdict({"bif": {"summary": {"pressure": 0.1},
                             "prigogine": {"phi": "мусор"}}})
    assert v_phi["prigogine_phi"] is None, v_phi

    # 11) детерминизм и рамка
    assert verdict({"xray": up_xray, "trend": "up"}) == \
        verdict({"xray": up_xray, "trend": "up"})
    assert verdict({"xray": up_xray, "bif": acid_bif}) == \
        verdict({"xray": up_xray, "bif": acid_bif})
    assert "18+" in v["frame"] and "не судьбу" in v["frame"]

    print("oracle self-test OK: машина состояний — сингулярность ПЕРЕХВАТЫВАЕТ "
          "парламент (шум не размывает Короля-дракона), у порога вход только по "
          "согласию, хаос — вето, фронтраннинг отменяет фейд, айсберг-голос от "
          "плиты, Хоукс даёт вектор, калибровка жива; ДУАЛЬНАЯ МЕХАНИКА — "
          "кислота (α<2) отстраняет парламент и пускает только вектор, переход "
          "сжимает права линейно, φ Пригожина добивает переход до кислоты")
