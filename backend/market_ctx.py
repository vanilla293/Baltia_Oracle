# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — контекст рынка для миссии (§4 контракта).

build()  — полное досье биржи (pipeline.collect_dossier → render_dossier) +
           астро + эфир + Курамото (поводыри MX/BR/SI + тикер, 1h/14д) +
           бифуркации (часовые закрытия, иначе дневные) + ВСЕ математические
           методы старой Пифии 4.x, каждый готовым текстом для ИИ:
             wyckoff_text          — Вайкофф D1/H1: бокс (лёд/крик), «куда
                                     ближе», расстояния до событий;
             xray / xray_text      — рентген стакана (microstructure.xray_live);
             reactor / reactor_text — реактор неба (reactor_sky);
             calendar / calendar_text — календарь Матьё + тег плотности среды;
             sync / sync_text      — синхронизация поводырей (Гильберт/IMD/Адлер);
             weather / weather_text — погода регионов спроса (газ/нефть);
             oracle / directive / oracle_text — свод голосов 4.x (машина
                                     состояний oracle + директива);
             scan / scan_text      — онлайн-сканер стакана (maya_scan), если идёт.
           Каждый слой в своём try/except с таймаутом: сбой одного не роняет
           миссию — текст слоя честно говорит «слой недоступен: причина», запись
           уходит в errors[]. Независимые слои считаются параллельно (две группы
           gather), тяжёлая синхронная математика — в потоках (to_thread).
           Толкования методов даются в форме «школа читает это так», без
           приказов; числа и факты — как есть, нули не выдумываются.
light()  — лёгкий снимок для перепроверки: цена, стакан, лента (Tinkoff) +
           рентген и Майя из свежего стакана; без токена — MOEX.
Self-тест (без сети): python3 -m backend.market_ctx
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import time

from . import (aether, ai_v5, astro, bifurcation, compress, instruments,
               kuramoto, moex, pipeline, tinkoff)

log = logging.getLogger("pythia.market_ctx")


def _opt(name: str):
    """Модуль 4.x в защите: нет модуля/зависимости → None, слой честно молчит."""
    try:
        return importlib.import_module(f".{name}", __package__)
    except Exception as e:                           # noqa: BLE001
        log.warning("модуль %s недоступен: %s", name, str(e)[:100])
        return None


wyckoff = _opt("wyckoff")
microstructure = _opt("microstructure")
maya = _opt("maya")
reactor_sky = _opt("reactor_sky")
bif_calendar = _opt("bif_calendar")
tagger = _opt("tagger")
aether_resonance = _opt("aether_resonance")
weather = _opt("weather")
oracle = _opt("oracle")
directive = _opt("directive")
maya_scan = _opt("maya_scan")

DOSSIER_LIMIT = 150_000        # аварийный потолок досье (обычное досье 3–15 тыс. симв.); разумные
                               # пределы держит mission.py: выше PYTHIA_CTX_LIMIT досье ужимает FLASH
LEADERS = ("MX", "BR", "SI")   # поводыри Курамото (коды каталога)
KUR_DAYS = 14
BIF_MIN_HOURLY = 128           # меньше часовых точек — берём дневные
DOSSIER_TIMEOUT = 90.0
LAYER_TIMEOUT = 40.0
NORM_SPREAD_MIN_BPS = 3.0      # норма спреда (Казимир/инфаркт) не ниже 3 bps…
NORM_SPREAD_STEPS = 2.5        # …и не уже 2.5 шага цены
TAPE_DAY_MIN = 840.0           # торговый день ≈ 14 ч — прокси нормы объёма ленты
LIGHT_DEPTH = 50               # стакан перепроверки: 50 уровней (как в досье)
_NORM_CACHE: dict[str, float] = {}   # тикер → норма спреда (build → light)


def _closes(candles) -> list[float]:
    out = []
    for c in candles or []:
        try:
            v = float(c.get("c") if isinstance(c, dict) else c)
            if v > 0:
                out.append(v)
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def _f(x, d=None):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def _fp(x) -> str:
    """Цена/разность без хвоста float: 285.1, 110500.5, −0.35."""
    v = _f(x)
    return "н/д" if v is None else f"{v:.10g}"


def _norm_spread_bps(step, price) -> float:
    """Норма спреда: max(3 bps, 2.5 шага цены в bps от текущей цены)."""
    s, p = _f(step), _f(price)
    if s and p and s > 0 and p > 0:
        return max(NORM_SPREAD_MIN_BPS, NORM_SPREAD_STEPS * s / p * 1e4)
    return NORM_SPREAD_MIN_BPS


def _why(errors: list[str], prefix: str) -> str:
    """Причина последнего сбоя слоя из errors[] — для честной строки-заглушки."""
    for e in reversed(errors):
        if e.startswith(prefix + ":"):
            return e[len(prefix) + 1:].strip() or "нет данных"
    return "нет данных"


async def _guard(name: str, aw, errors: list[str], *, timeout: float = LAYER_TIMEOUT,
                 default=None):
    """Слой в защите: таймаут + любой сбой → default и запись в errors[]."""
    try:
        return await asyncio.wait_for(aw, timeout)
    except asyncio.TimeoutError:
        errors.append(f"{name}: таймаут {timeout:g} с")
        log.info("слой %s: таймаут %g с", name, timeout)
    except Exception as e:                           # noqa: BLE001
        errors.append(f"{name}: {str(e)[:100] or type(e).__name__}")
        log.info("слой %s: %s", name, str(e)[:120] or type(e).__name__)
    return default


# ── рендеры слоёв ──────────────────────────────────────────────────────────────
def render_kuramoto(kur) -> str:
    if not isinstance(kur, dict) or kur.get("r") is None:
        return "Курамото: графа нет (мало рядов/баров) — сцепка поводырей неизвестна."
    r = float(kur["r"])
    coup = kur.get("coupling") or {}
    coup_s = ", ".join(f"{k} {v:.2f}" for k, v in sorted(coup.items(), key=lambda kv: -kv[1]))
    if r < kuramoto.R_TRAP:
        sense = (f"r̄<{kuramoto.R_TRAP}: хор поводырей вразнобой — одиночный ход тикера без хора "
                 "= ловушка, вход по такому ходу опасен")
    elif r >= kuramoto.R_HERD:
        sense = f"r̄≥{kuramoto.R_HERD}: стадный захват — хор в фазе, каскады ходят широко, ходы доверять"
    else:
        sense = "сцепка обычная: хор жив, вход по ходу разрешён, но без стадного ускорения"
    return (f"КУРАМОТО (1h, {KUR_DAYS}д, {kur.get('n')} рядов × {kur.get('bars')} баров): "
            f"r̄={r:.2f} (мин {kur.get('r_min')}) — {kur.get('word')}. Вожак: {kur.get('leader')}. "
            f"Сцепка: {coup_s}.\nГейт-смысл: {sense}.")


def render_bifurcation(bif) -> str:
    if not isinstance(bif, dict):
        return "Бифуркации: расчёта нет (мало данных)."

    def ok(k):
        v = bif.get(k)
        return v if isinstance(v, dict) and "error" not in v else None

    s = bif.get("summary") or {}
    L = [f"БИФУРКАЦИИ: режим — {s.get('regime', '?')}; давление слома {s.get('pressure')}; "
         f"направление {s.get('direction')} (−1 вниз … +1 вверх); предсказуемость {s.get('predictability')}"
         + ("; ОКНО БИФУРКАЦИИ ОТКРЫТО" if s.get("window_open") else "")]
    pr = ok("prigogine")
    if pr:
        L.append(f"Пригожин φ={pr.get('phi')} (до слома {pr.get('dist_to_bif')}) — {pr.get('word')}")
    ew = ok("early_warning")
    if ew:
        L.append(f"Предвестники (EWS) счёт {ew.get('score')} — {ew.get('word')}")
    lp = ok("lppls")
    if lp and lp.get("qualified"):
        L.append(f"LPPLS: tc через ~{lp.get('tc_bars_ahead')} баров, сторона {lp.get('side')}"
                 + (" (РЯДОМ)" if lp.get("near") else ""))
    hu = ok("hurst")
    if hu:
        L.append(f"Хёрст H={hu.get('H')} — {hu.get('word')}")
    ly = ok("lyapunov")
    if ly:
        L.append(f"Ляпунов λ={ly.get('lambda_max')}, горизонт "
                 f"{ly.get('horizon_bars') if ly.get('horizon_bars') is not None else '∞'} баров"
                 + (" — хаос" if ly.get("chaotic") else ""))
    cu = ok("cusp")
    if cu and cu.get("bistable"):
        L.append(f"Cusp: {cu.get('word')}")
    if len(L) == 1:
        L.append("детекторы молчат: ряд короткий")
    return "\n".join(L)


# ── Вайкофф: бокс, «куда ближе», события с расстояниями ───────────────────────
WYCKOFF_SCHOOL = ("Как читают (школа Вайкоффа): спринг — вынос стопов под лёд и возврат, "
                  "топливо для роста; аптраст — ловушка над криком; SOS/LPS — выход вверх и "
                  "опора; SOW/LPSY — выход вниз и предложение; причина→следствие: чем шире "
                  "бокс, тем длиннее ход из него; усилие без результата — поглощение.")


def _wyckoff_tf(w: dict, tf: str, price) -> list[str]:
    lo, hi = _f(w.get("box_low")), _f(w.get("box_high"))
    p = _f(price) or _f(w.get("last_close"))
    bias = int(_f(w.get("bias"), 0.0) or 0)
    L = [f"ВАЙКОФФ {tf} ({w.get('bars')} баров): {w.get('read')} · фаза {w.get('phase')} · "
         f"bias {bias:+d} (−100…+100)"]
    box_atr = _f(w.get("box_atr"), 0.0) or 0.0
    box_ok = lo is not None and hi is not None and hi > lo
    atr = (hi - lo) / box_atr if (box_ok and box_atr > 0) else None   # ATR этого TF в цене
    if box_ok and p:
        pos = (p - lo) / (hi - lo) * 100.0
        line = (f"бокс: лёд {_fp(lo)} … крик {_fp(hi)} (ширина {(hi - lo) / p * 100:.2f}%"
                + (f", ≈ {box_atr:g} ATR" if box_atr > 0 else "")
                + f"); цена {_fp(p)} на {pos:.0f}% высоты бокса")
        if pos > 100:
            line += " — НАД криком, вне бокса"
        elif pos < 0:
            line += " — ПОД льдом, вне бокса"
        L.append(line)
        d_ice, d_creek = lo - p, hi - p

        def _d(dv: float) -> str:
            s = f"{dv / p * 100:+.2f}% ({dv:+.10g}"
            if atr:
                s += f", ~{abs(dv) / atr:.1f} ATR"
            return s + ")"
        near = ("льду" if abs(d_ice) < abs(d_creek) else
                "крику" if abs(d_creek) < abs(d_ice) else "середине (равноудалена)")
        L.append(f"до льда {_d(d_ice)}, до крика {_d(d_creek)} → ближе к {near}")
    else:
        L.append("бокс: границы не определены")
    if w.get("vol_character"):
        L.append(f"усилие/результат: {w['vol_character']}")
    evs = w.get("events_all") or w.get("events") or []
    if evs:
        L.append(f"события (свежие в конце, {len(evs)}):")
        for e in evs:
            t = str(e.get("t") or "")
            t = t[:16].replace("T", " ") if (tf == "H1" and "T" in t) else t[:10]
            ep = _f(e.get("price"))
            dist = f" ({(ep / p - 1) * 100:+.2f}% от цены)" if (ep and p) else ""
            L.append(f"  {t} · {e.get('name')} @ {_fp(ep)}{dist} — {e.get('note') or ''}")
    else:
        L.append("события: не найдены (бокс без кульминаций и проколов)")
    return L


def render_wyckoff(wy, price=None) -> str:
    """Плотный блок Вайкоффа для ИИ: D1 и H1, бокс, расстояния, все события."""
    if not isinstance(wy, dict) or not any(isinstance(wy.get(k), dict) and wy.get(k)
                                           for k in ("daily", "hourly")):
        return "Вайкофф: расчёта нет (мало свечей)"
    L: list[str] = []
    for key, tf in (("daily", "D1"), ("hourly", "H1")):
        w = wy.get(key)
        if isinstance(w, dict) and w:
            L.extend(_wyckoff_tf(w, tf, price))
    L.append(WYCKOFF_SCHOOL)
    return "\n".join(L)


# ── рентген стакана (микроструктура) ───────────────────────────────────────────
XRAY_SCHOOL = ("как читают: OBI — куда давят стенами; CVD — реальные деньги ленты; "
               "VPIN — односторонность потока; Хёрст >0.5 тренд, <0.5 возврат.")


def _poke_against(prices) -> bool | None:
    """Прокол недавнего экстремума с возвратом (ветка Stop-Hunt рентгена):
    последние 6 баров 5-мин против предыдущих ≥42. Мало баров → None."""
    p = [x for x in (prices or []) if x]
    if len(p) < 48:
        return None
    recent, prior = p[-6:], p[-48:-6]
    lo_r, hi_r = min(recent), max(recent)
    lo_p, hi_p = min(prior), max(prior)
    last = p[-1]
    return bool((lo_r < lo_p and last > lo_p) or (hi_r > hi_p and last < hi_p))


def _xray_of(d: dict, price, norm: float) -> tuple[dict, float | None, int]:
    """Рентген из досье: стакан + лента + 5-мин закрытия + VWAP + прокси нормы объёма.
    Возврат: (xray, vol_norm, число 5-мин точек)."""
    if microstructure is None:
        raise RuntimeError("модуль microstructure недоступен")
    ob = d.get("orderbook") if isinstance(d.get("orderbook"), dict) else None
    tape = d.get("tape") if isinstance(d.get("tape"), dict) else {}
    prices = _closes(d.get("candles_intraday_5m"))
    im = d.get("intraday_metrics") or {}
    dm = d.get("daily_metrics") or {}
    vol_now = _f(tape.get("buy_vol"), 0.0) + _f(tape.get("sell_vol"), 0.0)
    win = _f(tape.get("window_min"), 0.0) or 0.0
    avg20 = _f(dm.get("avg_volume_20d"))
    vol_norm = (avg20 * win / TAPE_DAY_MIN) if (avg20 and win) else None
    xr = microstructure.xray_live(
        ob, tape or None, norm_spread_bps=norm, prices=prices or None,
        vwap_now=_f(im.get("vwap")), vol_now=(vol_now if vol_now > 0 else None),
        vol_norm=vol_norm, poke_against=_poke_against(prices))
    return xr, vol_norm, len(prices)


def render_xray(xr, ob=None, tape=None, *, norm=None, vol_norm=None, n_prices: int = 0) -> str:
    """1–3 строки рентгена: метрики, классификатор, «как читают»."""
    if not isinstance(xr, dict) or not xr.get("available"):
        why = xr.get("note") if isinstance(xr, dict) else None
        return ("микроструктура: стакана нет (нет токена/биржа закрыта)"
                + (f" — {why}" if why else ""))
    m = xr.get("metrics") or {}
    ob = ob if isinstance(ob, dict) else {}
    tape = tape if isinstance(tape, dict) else {}
    win = tape.get("window_min")
    parts = [f"OBI {_f(m.get('obi'), 0.0):+.2f}",
             f"CVD {_f(m.get('cvd'), 0.0):+.0f} лотов" + (f" за {win} мин" if win else ""),
             f"VPIN {_f(m.get('vpin'), 0.0):.2f}"]
    hu = _f(m.get("hurst"))
    if n_prices >= 8 and hu is not None:
        parts.append(f"Хёрст {hu:.2f} по 5-мин "
                     f"({'тренд' if hu > 0.55 else 'возврат' if hu < 0.45 else 'нейтрален'})")
    else:
        parts.append("Хёрст: ряда 5-мин нет")
    sp = _f(ob.get("spread_bps"))
    if sp is not None:
        parts.append(f"спред {sp:g} bps при норме {norm:.1f}" if norm else f"спред {sp:g} bps")
    ca = _f(m.get("casimir_a"))
    if ca is not None:
        crit = microstructure is not None and ca <= microstructure.CASIMIR_CRIT
        parts.append(f"Казимир a={ca:g} ({'сжат критично — пружина' if crit else 'не сжат'})")
    if m.get("vwap_dev") is not None:
        parts.append(f"к VWAP {_f(m.get('vwap_dev'), 0.0) * 100:+.2f}%")
    vol_now = _f(tape.get("buy_vol"), 0.0) + _f(tape.get("sell_vol"), 0.0)
    if vol_now and vol_norm:
        parts.append(f"объём ленты {vol_now:g} = {vol_now / vol_norm:.2f}× прокси-нормы "
                     f"{vol_norm:.0f} (avg_volume_20d × окно ленты / {TAPE_DAY_MIN:g} мин)")
    sig = xr.get("signals") or []
    return "\n".join([
        "РЕНТГЕН СТАКАНА (микроструктура 4.x, снимок досье): " + ", ".join(parts) + ".",
        f"классификатор: {xr.get('actor')} (уверенность {_f(xr.get('confidence'), 0.0):.2f}); "
        f"сигналы: {', '.join(sig) if sig else 'нет'}; {_posture_note(xr)}.",
        XRAY_SCHOOL])


def _posture_note(xr: dict) -> str:
    """v5.4.3: «поза» классификатора 4.x — толкование школы, а не приказ (закон 3). Без сигналов классификатор
    по умолчанию писал «ждать чистый сетап» — это стояло в user-части каждого узла у денег как скрытый толчок
    к ожиданию; теперь без сигналов позы нет, с сигналами — цитатой «обычно читают так»."""
    if not (xr.get("signals") or []):
        return "явного давления крупного игрока классификатор не видит"
    return f"школа микроструктуры обычно читает так (толкование, не приказ): «{xr.get('posture')}»"


# ── календарь среды: Матьё + тег плотности ─────────────────────────────────────
CALENDAR_SCHOOL = ("как читают: язык Матьё — окно восприимчивости среды (раскачка возможна), "
                   "не событие; наэлектризованная среда — пробой зреет, ловушки живут; "
                   "вязкая — ходы вязнут, инерция держит.")


def render_calendar(cal, tag=None) -> str:
    if not isinstance(cal, dict):
        return "КАЛЕНДАРЬ СРЕДЫ: слой недоступен: расчёта нет"
    parts = []
    pb = cal.get("dominant_period_bars")
    parts.append(f"доминантный цикл цены {pb:g} баров (D1)" if pb
                 else "доминантный цикл цены не выделен (ряд короткий/плоский)")
    tongues = cal.get("tongues") or []
    if tongues:
        parts.append("языки Матьё в допуске: " + ", ".join(
            f"{t.get('body')} n={t.get('n')} (γ к 2ω₀/n = {t.get('ratio')}, "
            f"{'тугой' if t.get('tight') else 'в допуске'})" for t in tongues))
    elif not cal.get("periods_loaded"):
        parts.append("языки Матьё: периоды неба не загружены (data/mathieu_periods.json)")
    else:
        parts.append("языки Матьё в допуске: нет")
    parts.append("флаг Матьё-неустойчивости: " + ("да" if cal.get("mathieu_unstable") else "нет"))
    wins = cal.get("windows") or []
    parts.append("датированные окна: " + ("; ".join(
        f"{w.get('kind')} — {w.get('when')} ({w.get('note')})" for w in wins) if wins else "нет"))
    mod = cal.get("modulator") or {}
    if mod.get("R") is not None:
        parts.append(f"модулятор фаз R={mod.get('R')}, чувствительность ×{mod.get('sens_mult')} "
                     f"({mod.get('word')})")
    else:
        parts.append("модулятор фаз: " + str(mod.get("note") or mod.get("error") or "нет"))
    if isinstance(tag, dict):
        pr = tag.get("parts") or {}
        parts.append(f"плотность среды: {tag.get('density')} (энергия {tag.get('energy')}: "
                     f"слом {pr.get('bif')}, непредсказуемость {pr.get('lyap')}, хвосты "
                     f"{pr.get('tail')}, Казимир {pr.get('casimir')}, модулятор {pr.get('mod')}) "
                     f"— {tag.get('word')}")
    else:
        parts.append("плотность среды: расчёта нет (мало баров)")
    return "КАЛЕНДАРЬ СРЕДЫ: " + "; ".join(parts) + ".\n" + CALENDAR_SCHOOL


# ── синхронизация поводырей ────────────────────────────────────────────────────
SYNC_SCHOOL = ("как читают: захват Адлера — инструмент порабощён макро-ритмом поводыря, ММ "
               "теряет контроль; IMD-резонанс — фантомное биение поводырей совпало с несущей "
               "инструмента; сторону синхронизация не задаёт — её даёт поток (CVD).")


def render_sync(sr, n_leaders: int = 0, n_own: int = 0) -> str:
    head = f"СИНХРОНИЗАЦИЯ ПОВОДЫРЕЙ (Гильберт/IMD/Адлер, 1h, {KUR_DAYS} дн): "
    if not isinstance(sr, dict) or not sr.get("leader"):
        note = sr.get("note") if isinstance(sr, dict) else None
        return (head + f"слой недоступен: {note or 'мало рядов'} "
                f"(поводырей {n_leaders}, точек тикера {n_own})")
    adl = sr.get("adler") or {}
    imd = sr.get("imd") or {}

    def yn(b) -> str:
        return "да" if b else "нет"
    a_det = (f"dΔθ/dt {adl.get('dphase_rate')}, разброс {adl.get('stability')}, отн. расстройка "
             f"{adl.get('rel_detuning')}, намотка {adl.get('winding_rad')} рад"
             if adl.get("dphase_rate") is not None else str(adl.get("note") or "нет данных"))
    i_det = (f"биение {imd.get('f_beat')} vs несущая {imd.get('f_market')} цикл/бар, отн. ошибка "
             f"{imd.get('rel_err')}" if imd.get("f_beat") is not None
             else str(imd.get("note") or "нет данных"))
    return (head + f"ведущий {sr.get('leader')}; захват Адлера: {yn(adl.get('locked'))} ({a_det}); "
            f"IMD-резонанс: {yn(imd.get('resonant'))} ({i_det}); "
            f"SYNC_RUPTURE: {yn(sr.get('rupture'))} — {sr.get('note')}.\n" + SYNC_SCHOOL)


# ── свод голосов 4.x: оракул + директива ───────────────────────────────────────
ORACLE_CALIBRATION = ("Калибровка: заявленные проценты приведены к измеренной точности на "
                      "истории (около монеты), выше ~60% не бывает.")


def render_oracle(v, dv) -> str:
    """v — verdict оракула (dict) или строка-причина сбоя; dv — директива или причина."""
    L = ["СВОД ГОЛОСОВ СТАРОЙ ПИФИИ (детерминированные машины 4.x, не приказ):"]
    if isinstance(v, dict) and v.get("state"):
        env_d = []
        if v.get("hill_alpha") is not None:
            env_d.append(f"α Хилла {v['hill_alpha']}")
        if v.get("prigogine_phi") is not None:
            env_d.append(f"φ Пригожина {v['prigogine_phi']}")
        L.append(f"оракул — состояние {v.get('state')}"
                 f"{' (OVERRIDE: вектор слома)' if v.get('override') else ''}, направление "
                 f"{v.get('dir')}, P(вверх)={_f(v.get('p_long'), 0.5):.2f}, уверенность "
                 f"{_f(v.get('confidence'), 0.0):.2f}, режим {v.get('mode')} ({v.get('regime_src')}), "
                 f"среда {v.get('env')}" + (f" ({', '.join(env_d)})" if env_d else "")
                 + f", права парламента {v.get('parliament_rights')}; окно бифуркации "
                 f"{'открыто' if v.get('window_open') else 'закрыто'}; ожидаемый ход "
                 f"{_f(v.get('expected_move_frac'), 0.0) * 100:.2f}%; предсказуемость "
                 f"{v.get('predictability')}; причина: {v.get('reason')}")
        voices = v.get("voices") or {}
        if voices:
            vs = sorted(voices.items(), key=lambda kv: -_f((kv[1] or {}).get("w"), 0.0))
            L.append(f"голоса ({len(vs)}; s — сторона −1…+1, c — уверенность голоса, w — вес): "
                     + "; ".join(f"{k} {_f(x.get('s'), 0.0):+.2f} (c {_f(x.get('c'), 0.0):.2f}, "
                                 f"w {_f(x.get('w'), 0.0):.2f})" for k, x in vs))
        else:
            L.append("голоса: ни один источник не проголосовал (нет стакана/ленты/рядов)")
    else:
        L.append("оракул: слой недоступен: " + (v if isinstance(v, str) and v else "расчёта нет"))
    if isinstance(dv, dict) and dv.get("dir"):
        vt = dv.get("votes") or {}
        # v5.4.3: машина 4.x при флете писала «только чистые сетапы, мелочь игнорируем» и «нет перевеса — вне
        # рынка, ждём чистый сетап» — это совет школы, а не факт (закон 3): в промпт — только режим и зоны-числа
        regime = str(dv.get("regime") or "").split(" — ")[0]
        zones = [str(z) for z in (dv.get("entry"), dv.get("stop"), dv.get("invalidation"))
                 if z and z != "—" and "ждём" not in str(z)]
        L.append(f"директива — {dv.get('dir_word')}, уверенность "
                 f"{_f(dv.get('confidence'), 0.0) * 100:.0f}% по своей шкале"
                 f"{' (сильная)' if dv.get('strong') else ''}, "
                 f"режим {regime}, голоса стакан {_f(vt.get('стакан'), 0.0):+.2f} / "
                 f"волна {_f(vt.get('волна'), 0.0):+.2f} / небо {_f(vt.get('небо'), 0.0):+.2f} "
                 f"(источники: {', '.join(dv.get('sources') or []) or 'нет'}); зоны: "
                 + ("; ".join(zones) if zones else "своего перекоса по голосам нет") + ".")
    else:
        L.append("директива: слой недоступен: " + (dv if isinstance(dv, str) and dv else "расчёта нет"))
    L.append(ORACLE_CALIBRATION)
    return "\n".join(L)


def _wave_of(d: dict) -> tuple[dict, list[float]]:
    """Волна Ψ эфира из досье: {slope, plv, psi_now} + дневные плиты [floor, mid, ceil]."""
    ae = d.get("aether") if isinstance(d.get("aether"), dict) else {}
    w = ae.get("wave") or {}
    psi = w.get("psi") or []
    wave: dict = {}
    if len(psi) >= 4 and _f(psi[-1]) is not None and _f(psi[-4]) is not None:
        wave["slope"] = float(psi[-1]) - float(psi[-4])
    if w.get("now_val") is not None:
        wave["psi_now"] = w["now_val"]
    day = ((ae.get("scales") or {}).get("day") or {})
    plv = (day.get("couple") or {}).get("plv")
    if plv is not None:
        wave["plv"] = plv
    pl = day.get("plates") or {}
    plates = [x for x in (_f(pl.get(k)) for k in ("floor", "mid", "ceil")) if x]
    return wave, plates


def _oracle_inputs(d: dict, xr, bif, rc, cal, scan, price, n_prices: int) -> dict:
    dm = d.get("daily_metrics") or {}
    wy = ((d.get("wyckoff") or {}).get("daily") or {}) if isinstance(d.get("wyckoff"), dict) else {}
    wave, plates = _wave_of(d)
    a50, a200 = dm.get("above_ema50"), dm.get("above_ema200")
    trend = ("up" if (a50 is True and a200 is True) else
             "down" if (a50 is False and a200 is False) else None)
    atr, hi, lo = _f(dm.get("atr14")), _f(dm.get("high_20d")), _f(dm.get("low_20d"))
    p = _f(price)
    hurst = ((xr or {}).get("metrics") or {}).get("hurst") if n_prices >= 8 else None
    return {"xray": xr, "maya": d.get("maya"), "bif": bif, "reactor": rc,
            "wave": wave or None,
            "wyckoff_bias": (_f(wy.get("bias")) / 100.0) if _f(wy.get("bias")) is not None else None,
            "trend": trend, "hurst": hurst,
            "atr_frac": (atr / p) if (atr and p) else None,
            "corridor_frac": ((hi - lo) / p) if (hi and lo and p and hi > lo) else None,
            "hawkes": (scan or {}).get("hawkes") if isinstance(scan, dict) else None,
            "plates": plates or None, "price": p,
            "mathieu_unstable": bool((cal or {}).get("mathieu_unstable")),
            "sens_mult": ((cal or {}).get("modulator") or {}).get("sens_mult")}


# ── погода регионов спроса ─────────────────────────────────────────────────────
def _weather_regions(ticker: str) -> list:
    """Регионы каталога: сам тикер, затем корень контракта (BRV6 → BR, TTFV6 → TTF)."""
    for key in dict.fromkeys((ticker, ticker[:3], ticker[:2])):
        it = instruments.get(key) if key else None
        if it and it.get("weather"):
            return list(it["weather"])
    return []


async def _weather_layer(ticker: str) -> tuple[list, str]:
    regions = _weather_regions(ticker)
    if not regions:
        return [], ""
    if weather is None:
        return [], "ПОГОДА: слой недоступен: модуль weather не загрузился"
    rows = await weather.fetch_weather(regions)
    if not rows:
        return [], ("ПОГОДА: слой недоступен: open-meteo не ответил по регионам "
                    + ", ".join(str(r.get("name", "?")) for r in regions))
    return rows, ("ПОГОДА (open-meteo, регионы спроса на энергию; HDD7 — градусо-дни отопления "
                  "за 7 дн, база 18°C):\n" + weather.render_for_ai(rows))


# ── сканер стакана (реестр maya_scan по коду тикера) ───────────────────────────
def _scan_layer(ticker: str) -> tuple[dict | None, str]:
    """Агрегаты идущего/завершённого скана: ≥ MIN_TICKS_AGG тиков → текст, иначе пусто."""
    if maya_scan is None:
        return None, ""
    for key in dict.fromkeys((ticker, ticker[:3], ticker[:2])):
        if not key:
            continue
        st = maya_scan.status(key)
        if isinstance(st, dict) and (st.get("ticks") or 0) > 0:
            if st["ticks"] >= maya_scan.MIN_TICKS_AGG:
                return st, maya_scan.render_for_ai(st)
            return None, ""
    return None, ""


# ── астро: компактный рендер фактов, откат на полный ───────────────────────────
async def _astro_layer() -> tuple[str, str]:
    """(текст неба для ИИ, короткая строка). render_compact (≈3 тыс. знаков
    фактов) если есть в astro, при его сбое — render_for_ai; без обрезки."""
    ctx = await astro.acontext()
    if not ctx:
        return "", ""
    fn = getattr(astro, "render_compact", None)
    txt = ""
    if fn is not None:
        try:
            txt = fn(ctx)
        except Exception as e:                       # noqa: BLE001 — откат на полный рендер
            log.info("astro.render_compact: %s", str(e)[:80])
            txt = ""
    if not txt:
        txt = astro.render_for_ai(ctx)
    return txt or "", astro.short_line(ctx) or ""


# ── ряды закрытий ──────────────────────────────────────────────────────────────
async def _hourly(ticker: str, asset_class: str, figi: str | None) -> list[float]:
    if figi and tinkoff.enabled():
        try:
            cs = await asyncio.wait_for(tinkoff.candles(figi, "1h", KUR_DAYS), LAYER_TIMEOUT)
            if len(cs) >= kuramoto.MIN_BARS:
                return _closes(cs)
        except Exception as e:                       # noqa: BLE001
            log.info("tinkoff 1h %s: %s", ticker, str(e)[:80])
    cs = await asyncio.wait_for(moex.candles(ticker, asset_class, "1h", KUR_DAYS), LAYER_TIMEOUT)
    return _closes(cs)


async def _leader(code: str) -> tuple[str, list[float]]:
    it = instruments.get(code) or {}
    ac = it.get("asset_class") or "futures"
    cs = await asyncio.wait_for(moex.candles(code, ac, "1h", KUR_DAYS), LAYER_TIMEOUT)
    return code, _closes(cs)


async def _series(ticker: str, asset_class: str, figi: str | None,
                  errors: list[str]) -> tuple[dict[str, list[float]], list[float]]:
    """Часовые закрытия поводырей + тикера (14 дней) — общее сырьё Курамото,
    синхронизации и бифуркаций. Возврат: (series, own_hourly)."""
    jobs = [_leader(c) for c in LEADERS if c != ticker] + [_hourly(ticker, asset_class, figi)]
    res = await asyncio.gather(*jobs, return_exceptions=True)
    series: dict[str, list[float]] = {}
    for r in res[:-1]:
        if isinstance(r, Exception):
            errors.append(f"курамото поводырь: {str(r)[:80]}")
        elif r[1]:
            series[r[0]] = r[1]
    last = res[-1]
    own: list[float] = []
    if isinstance(last, Exception):
        errors.append(f"курамото {ticker}: {str(last)[:80]}")
    else:
        own = last
        if own:
            series[ticker] = own
    return series, own


async def _daily(ticker: str, asset_class: str, d: dict, figi: str | None) -> list[float]:
    tail = _closes(d.get("candles_daily_tail"))
    if len(tail) >= bifurcation.MIN_BARS:
        return tail
    if figi and tinkoff.enabled():
        try:
            cs = _closes(await asyncio.wait_for(tinkoff.candles(figi, "1d", 200), LAYER_TIMEOUT))
            if len(cs) >= bifurcation.MIN_BARS:
                return cs
        except Exception as e:                       # noqa: BLE001
            log.info("tinkoff 1d %s: %s", ticker, str(e)[:80])
    cs = _closes(await asyncio.wait_for(moex.candles(ticker, asset_class, "1d", 200), LAYER_TIMEOUT))
    return cs if len(cs) >= bifurcation.MIN_BARS else tail


# ── полный контекст ────────────────────────────────────────────────────────────
async def build(ticker: str, asset_class: str, *, full: bool = True) -> dict:
    ticker = (ticker or "").upper().strip()
    errors: list[str] = []
    out: dict = {"dossier": {}, "text": "", "price": None, "astro": "", "astro_line": "",
                 "aether": "", "kuramoto": None, "kuramoto_text": "",
                 "bifurcation": None, "bif_text": "",
                 "wyckoff_text": "", "xray": None, "xray_text": "",
                 "reactor": None, "reactor_text": "", "calendar": None, "calendar_text": "",
                 "sync": None, "sync_text": "", "weather": None, "weather_text": "",
                 "oracle": None, "directive": None, "oracle_text": "",
                 "scan": None, "scan_text": "",
                 "errors": errors, "ts": time.time()}

    # 1. досье биржи
    d: dict = {}
    try:
        d = await asyncio.wait_for(pipeline.collect_dossier(ticker, asset_class), DOSSIER_TIMEOUT)
        out["dossier"] = d
        for e in d.get("errors") or []:
            errors.append(f"досье: {str(e)[:120]}")
    except Exception as e:                           # noqa: BLE001
        errors.append(f"досье не собралось: {str(e)[:120]}")
        log.warning("досье %s: %s", ticker, str(e)[:120])
    if not isinstance(d, dict):
        d = {}
    try:
        if d:
            out["text"] = compress.clip(pipeline.render_dossier(d, full=full), DOSSIER_LIMIT, "досье")
    except Exception as e:                           # noqa: BLE001
        errors.append(f"рендер досье: {str(e)[:100]}")
    try:
        out["price"] = _f(((d.get("price") or {}).get("price")))
    except Exception:                                # noqa: BLE001
        out["price"] = None
    price = out["price"]
    inst = d.get("instrument") if isinstance(d.get("instrument"), dict) else {}
    figi = inst.get("figi") or inst.get("uid")
    norm = _norm_spread_bps(inst.get("min_price_increment"), price)
    if price:
        _NORM_CACHE[ticker] = norm

    # 2. слои из досье (синхронные, дешёвые): эфир, Вайкофф, рентген стакана
    try:
        if d.get("aether"):
            out["aether"] = aether.render_for_ai(d["aether"])
    except Exception as e:                           # noqa: BLE001
        errors.append(f"эфир: {str(e)[:100]}")
    try:
        wy = d.get("wyckoff")
        if not wy and d and wyckoff is not None:
            wy = wyckoff.analyze_dossier(d)
        out["wyckoff_text"] = render_wyckoff(wy, price)
    except Exception as e:                           # noqa: BLE001
        errors.append(f"вайкофф: {str(e)[:100]}")
        out["wyckoff_text"] = f"Вайкофф: слой недоступен: {str(e)[:100]}"
    xr, n_pr = None, 0
    try:
        xr, vol_norm, n_pr = _xray_of(d, price, norm)
        out["xray"] = xr
        out["xray_text"] = render_xray(xr, d.get("orderbook"), d.get("tape"), norm=norm,
                                       vol_norm=vol_norm, n_prices=n_pr)
    except Exception as e:                           # noqa: BLE001
        errors.append(f"рентген: {str(e)[:100]}")
        out["xray_text"] = f"микроструктура: слой недоступен: {str(e)[:100]}"

    # 3. независимые слои параллельно: астро, реактор неба, ряды поводырей,
    #    дневные закрытия, погода регионов спроса
    async def _reactor() -> dict:
        if reactor_sky is None:
            raise RuntimeError("модуль reactor_sky недоступен")
        return await asyncio.to_thread(reactor_sky.reactor_context, ticker)

    (a_txt, a_line), rc, (series, own_hourly), daily_closes, (wx, wx_txt) = await asyncio.gather(
        _guard("астро", _astro_layer(), errors, default=("", "")),
        _guard("реактор неба", _reactor(), errors),
        _guard("курамото", _series(ticker, asset_class, figi, errors), errors, default=({}, [])),
        _guard("дневные закрытия", _daily(ticker, asset_class, d, figi), errors, default=[]),
        _guard("погода", _weather_layer(ticker), errors, default=([], None)))
    out["astro"], out["astro_line"] = a_txt or "", a_line or ""
    try:
        kur = kuramoto.graph(series) if len(series) >= kuramoto.MIN_SERIES else None
        out["kuramoto"] = kur
        out["kuramoto_text"] = render_kuramoto(kur)
    except Exception as e:                           # noqa: BLE001
        errors.append(f"курамото: {str(e)[:100]}")
        out["kuramoto_text"] = render_kuramoto(None)
    if isinstance(rc, dict):
        out["reactor"] = rc
        try:
            out["reactor_text"] = reactor_sky.render_for_ai(rc) or "РЕАКТОР НЕБА: пусто"
        except Exception as e:                       # noqa: BLE001
            errors.append(f"реактор неба рендер: {str(e)[:100]}")
            out["reactor_text"] = f"РЕАКТОР НЕБА: слой недоступен: {str(e)[:100]}"
    else:
        out["reactor_text"] = "РЕАКТОР НЕБА: слой недоступен: " + _why(errors, "реактор неба")
    if wx_txt is None:
        out["weather_text"] = "ПОГОДА: слой недоступен: " + _why(errors, "погода")
    else:
        out["weather"], out["weather_text"] = (wx or None), wx_txt

    # 4. зависимые слои параллельно: бифуркации, календарь + тег среды, синхронизация
    closes_bif = own_hourly if len(own_hourly) >= BIF_MIN_HOURLY else daily_closes
    base = "1h" if len(own_hourly) >= BIF_MIN_HOURLY else "1d"
    ob = d.get("orderbook") if isinstance(d.get("orderbook"), dict) else {}

    async def _bif():
        if len(closes_bif) < bifurcation.MIN_BARS:
            return None
        bif = await asyncio.to_thread(bifurcation.bifurcation_context, closes_bif)
        bif["base"] = base
        return bif

    async def _cal() -> tuple[dict | None, dict | None]:
        if bif_calendar is None:
            raise RuntimeError("модуль bif_calendar недоступен")
        cal = await asyncio.to_thread(
            bif_calendar.calendar,
            daily_closes if len(daily_closes) >= bif_calendar.MIN_BARS else None,
            1440.0, reactor=(rc if isinstance(rc, dict) else None), now_ts=time.time())
        tag = None
        if tagger is not None:
            closes_tag = own_hourly if len(own_hourly) >= tagger.MIN_BARS else daily_closes
            if len(closes_tag) >= tagger.MIN_BARS:
                tag = await asyncio.to_thread(
                    tagger.tag_now, closes_tag, spread_bps=ob.get("spread_bps"),
                    norm_spread_bps=norm, now_ts=time.time(),
                    sens_mult=((cal.get("modulator") or {}).get("sens_mult")))
        return cal, tag

    leaders = {k: v for k, v in series.items() if k != ticker}

    async def _sync():
        if aether_resonance is None:
            raise RuntimeError("модуль aether_resonance недоступен")
        n_ok = sum(1 for v in leaders.values() if len(v) >= aether_resonance.MIN_LEN)
        if n_ok < 2 or len(own_hourly) < aether_resonance.MIN_LEN:
            return {"note": f"нужно ≥2 поводыря и ряд тикера длиной ≥{aether_resonance.MIN_LEN}"}
        return await asyncio.to_thread(aether_resonance.sync_rupture, own_hourly, leaders)

    bif, cal_tag, sr = await asyncio.gather(
        _guard("бифуркации", _bif(), errors),
        _guard("календарь", _cal(), errors, default=(None, None)),
        _guard("синхронизация", _sync(), errors))
    if isinstance(bif, dict):
        out["bifurcation"] = bif
        out["bif_text"] = f"[база {base}, {len(closes_bif)} баров]\n" + render_bifurcation(bif)
    else:
        out["bif_text"] = render_bifurcation(None)
    cal, tag = cal_tag if isinstance(cal_tag, tuple) else (None, None)
    if isinstance(cal, dict):
        out["calendar"] = dict(cal, tag=tag)
        out["calendar_text"] = render_calendar(cal, tag)
    else:
        out["calendar_text"] = "КАЛЕНДАРЬ СРЕДЫ: слой недоступен: " + _why(errors, "календарь")
    if isinstance(sr, dict):
        out["sync"] = sr if sr.get("leader") else None
        out["sync_text"] = render_sync(sr, len(leaders), len(own_hourly))
    else:
        out["sync_text"] = render_sync(None, len(leaders), len(own_hourly)).replace(
            "мало рядов", _why(errors, "синхронизация"))

    # 5. сканер стакана (память процесса) и свод голосов 4.x: оракул + директива
    try:
        out["scan"], out["scan_text"] = _scan_layer(ticker)
    except Exception as e:                           # noqa: BLE001
        errors.append(f"сканер: {str(e)[:100]}")
        out["scan"], out["scan_text"] = None, ""
    v: dict | str = "расчёта нет"
    try:
        if oracle is None:
            raise RuntimeError("модуль oracle недоступен")
        v = oracle.verdict(_oracle_inputs(d, xr, bif if isinstance(bif, dict) else None,
                                          rc if isinstance(rc, dict) else None, cal, out["scan"],
                                          price, n_pr))
        out["oracle"] = v
    except Exception as e:                           # noqa: BLE001
        errors.append(f"оракул: {str(e)[:100]}")
        v = str(e)[:100] or type(e).__name__
    dv: dict | str = "расчёта нет"
    try:
        if directive is None:
            raise RuntimeError("модуль directive недоступен")
        wave, _ = _wave_of(d)
        dv = directive.directive(d.get("maya") if isinstance(d.get("maya"), dict) else None,
                                 rc if isinstance(rc, dict) else None, wave.get("slope"))
        out["directive"] = dv
    except Exception as e:                           # noqa: BLE001
        errors.append(f"директива: {str(e)[:100]}")
        dv = str(e)[:100] or type(e).__name__
    try:
        out["oracle_text"] = render_oracle(v, dv)
    except Exception as e:                           # noqa: BLE001
        errors.append(f"свод голосов: {str(e)[:100]}")
        out["oracle_text"] = f"СВОД ГОЛОСОВ СТАРОЙ ПИФИИ: слой недоступен: {str(e)[:100]}"
    return out


# ── лёгкий снимок для перепроверки ────────────────────────────────────────────
def _xray_line(xr, book: dict, norm: float) -> str:
    if not isinstance(xr, dict) or not xr.get("available"):
        return "Рентген: " + str((xr or {}).get("note") or "стакана нет")
    m = xr.get("metrics") or {}
    s = (f"Рентген: OBI {_f(m.get('obi'), 0.0):+.2f}, CVD {_f(m.get('cvd'), 0.0):+.0f}, "
         f"VPIN {_f(m.get('vpin'), 0.0):.2f}, спред {_fp(book.get('spread_bps'))} bps "
         f"(норма {norm:.1f}), Казимир a={_fp(m.get('casimir_a'))}; классификатор: "
         f"{xr.get('actor')} ({_f(xr.get('confidence'), 0.0):.2f})")
    sig = xr.get("signals") or []
    if sig:
        s += f"; сигналы: {', '.join(sig)}"
    return s + f"; {_posture_note(xr)}"


def _maya_line(my) -> str:
    if not isinstance(my, dict) or not my.get("available"):
        return "Майя: " + str((my or {}).get("note") or "стакана нет")
    p = my.get("pull") or {}
    parts = [f"тяга {p.get('side') or 'нет (симметрия)'} (вверх {p.get('score_up')}/вниз {p.get('score_dn')})"]
    for key, name in (("vacuum_up", "над"), ("vacuum_down", "под")):
        v = my.get(key)
        if v:
            parts.append(f"вакуум {name} {_fp(v.get('p_from'))}…{_fp(v.get('p_to'))} "
                         f"(глубина {v.get('depth')}, {v.get('dist_levels')} ур. от лучшей)")
    walls = []
    for key, name in (("wall_ask", "ask"), ("wall_bid", "bid")):
        w = my.get(key)
        walls.append(f"{name} {_fp(w.get('p'))} ×{w.get('mult')} ({_fp(w.get('q'))} лотов)" if w
                     else f"{name} нет")
    parts.append("стены " + " / ".join(walls))
    ag = my.get("aggressor")
    if ag:
        parts.append(f"{ag.get('word')} ({_fp(ag.get('ratio'))})"
                     + ("" if ag.get("with_pull") is None else
                        ", с тягой согласен" if ag.get("with_pull") else ", против тяги"))
    return "Майя: " + "; ".join(parts)


async def light(ticker: str, figi: str | None, asset_class: str) -> dict:
    ticker = (ticker or "").upper().strip()
    L: list[str] = []
    price = None
    book = None
    xr = my = None
    if figi and tinkoff.enabled():
        lp, ob, tr = await asyncio.gather(tinkoff.last_price(figi),
                                          tinkoff.orderbook(figi, depth=LIGHT_DEPTH),
                                          tinkoff.last_trades(figi, minutes=15), return_exceptions=True)
        if isinstance(lp, dict) and _f(lp.get("price")):
            price = _f(lp["price"])
        if isinstance(ob, dict):
            book = ob
            if price is None and _f(ob.get("last_price")):
                price = _f(ob["last_price"])
        head = f"{ticker}: цена {price if price is not None else 'н/д'} ({ai_v5.now_msk_str()})"
        cp = _f((book or {}).get("close_price"))
        if price and cp:
            head += f", к закрытию {(price / cp - 1) * 100:+.2f}%"
        L.append(head)
        if book:
            imb = book.get("imbalance")
            depth = len(book.get("levels_bid") or book.get("top_bids") or []) or LIGHT_DEPTH
            L.append(f"Стакан({depth}): bid {book.get('best_bid')} / ask {book.get('best_ask')}, спред "
                     f"{book.get('spread')} ({book.get('spread_bps')} bps), объём bid {book.get('bid_vol')} / "
                     f"ask {book.get('ask_vol')}, дисбаланс {imb:+.2f}" if isinstance(imb, (int, float))
                     else f"Стакан({depth}): bid {book.get('best_bid')} / ask {book.get('best_ask')}")
            wb, wa = book.get("wall_bid"), book.get("wall_ask")
            if wb or wa:
                L.append("Стены: " + ", ".join(
                    x for x in ((f"bid {wb['p']}×{wb['q']}" if wb else ""),
                                (f"ask {wa['p']}×{wa['q']}" if wa else "")) if x))
            if book.get("limit_up") or book.get("limit_down"):
                L.append(f"Планки: {book.get('limit_down')} … {book.get('limit_up')}")
        else:
            L.append("Стакан: пуст/недоступен (ночь, аукцион, планка?)")
        if isinstance(tr, dict) and tr.get("count"):
            ar = tr.get("aggressor_ratio")
            who = ("покупатели" if (ar or 0) > 0.55 else "продавцы" if (ar or 0) < 0.45 else "паритет")
            L.append(f"Лента 15 мин: {tr['count']} сделок, объём {tr.get('buy_vol', 0) + tr.get('sell_vol', 0)}, "
                     f"агрессор — {who} ({(ar or 0) * 100:.0f}% покупок), дельта {tr.get('delta'):+d}, "
                     f"средняя сделка {tr.get('avg_trade_size')}")
            big = tr.get("largest_prints") or []
            if big:
                L.append("Крупные: " + ", ".join(f"{b['side']} {b['q']}@{b['p']}" for b in big[:3]))
        else:
            L.append("Лента 15 мин: сделок нет/недоступна")
        # рентген и Майя из свежего стакана (методы 4.x; норма спреда — из build)
        if book:
            tape = tr if isinstance(tr, dict) else None
            norm = _NORM_CACHE.get(ticker, NORM_SPREAD_MIN_BPS)
            try:
                if microstructure is None:
                    raise RuntimeError("модуль microstructure недоступен")
                xr = microstructure.xray_live(book, tape, norm_spread_bps=norm)
                L.append(_xray_line(xr, book, norm))
            except Exception as e:                   # noqa: BLE001
                L.append(f"Рентген: недоступен ({str(e)[:60]})")
            try:
                if maya is None:
                    raise RuntimeError("модуль maya недоступен")
                my = maya.analyze(book, tape)
                L.append(_maya_line(my))
            except Exception as e:                   # noqa: BLE001
                L.append(f"Майя: недоступна ({str(e)[:60]})")
    else:
        try:
            mp = await moex.last_price(ticker, asset_class)
        except Exception as e:                       # noqa: BLE001
            mp = None
            L.append(f"MOEX недоступен: {str(e)[:80]}")
        if mp:
            price = _f(mp.get("price"))
            L.append(f"{ticker}: цена {price} (MOEX{', устарела' if mp.get('stale') else ''}, "
                     f"{ai_v5.now_msk_str()}), изменение за день {mp.get('change_pct')}%"
                     f", объём дня {mp.get('volume_today')}")
            if mp.get("bid") or mp.get("offer"):
                L.append(f"Лучшие: bid {mp.get('bid')} / ask {mp.get('offer')} (стакан и лента без Tinkoff недоступны)")
        else:
            L.append(f"{ticker}: цены нет (MOEX молчит, токена Tinkoff нет)")
    return {"text": "\n".join(L), "price": price, "book": book, "xray": xr, "maya": my}


if __name__ == "__main__":
    import math
    from collections import deque

    # ── рендеры на синтетике ──
    n = 300
    xs = [100.0 * math.exp(0.002 * math.sin(i * 0.3) + 0.0005 * i) for i in range(n)]
    ys = [100.0 * math.exp(0.002 * math.sin(i * 0.3 + 0.2) + 0.0004 * i) for i in range(n)]
    zs = [100.0 * math.exp(0.003 * math.cos(i * 0.7)) for i in range(n)]
    kur = kuramoto.graph({"MX": xs, "BR": ys, "TEST": zs})
    assert kur and "r" in kur
    kt = render_kuramoto(kur)
    assert "КУРАМОТО" in kt and "Гейт-смысл" in kt and kur["leader"] in kt
    assert "графа нет" in render_kuramoto(None)
    bif = bifurcation.bifurcation_context(xs)
    bt = render_bifurcation(bif)
    assert "БИФУРКАЦИИ" in bt and "Хёрст" in bt and "Пригожин" in bt, bt
    assert "расчёта нет" in render_bifurcation(None)
    assert render_wyckoff(None) == "Вайкофф: расчёта нет (мало свечей)"
    assert "стакана нет" in render_xray(None) and "стакана нет" in render_xray({"available": False})
    ro = render_oracle("нет модуля", "нет модуля")
    assert "оракул: слой недоступен" in ro and "директива: слой недоступен" in ro and "Калибровка" in ro
    assert "слой недоступен" in render_sync(None, 1, 0) and "слой недоступен" in render_calendar(None)
    assert _norm_spread_bps(0.01, 100.0) == 3.0 and abs(_norm_spread_bps(0.5, 100.0) - 125.0) < 1e-9
    assert _weather_regions("SBER") == [] and _weather_regions("BRV6") and _weather_regions("TTFV6")

    # ── синтетические свечи: D1 с полной хронологией Вайкоффа (SC → AR → ST →
    #    спринг → SOS → LPS), H1 — волна, 5-мин — хвост xs ──
    def daily_candles():
        closes = ([106.0, 106.8, 106.2, 107.0, 106.4, 106.9, 106.1, 106.6, 106.3, 106.7,
                   106.2, 106.8, 106.4, 106.9, 106.5]                                     # 0-14 флет
                  + [105.4, 104.6, 103.9, 103.0, 100.4]                                   # 15-19 слив, SC
                  + [101.8, 103.4, 102.6, 101.1, 99.6]                                    # AR, ST
                  + [101.4, 102.2, 101.7, 102.6, 101.9, 102.8, 102.1, 101.5, 102.4, 101.8,
                     102.7, 102.0, 101.3, 102.1, 101.6, 102.5, 101.9, 101.2, 102.0, 101.5,
                     102.3, 101.7, 101.0, 101.6, 100.9]                                   # бокс
                  + [100.4]                                                              # спринг
                  + [101.2, 101.9, 102.4, 102.0, 102.9, 103.3, 103.5]                     # рост
                  + [104.6, 104.5])                                                       # SOS, LPS
        vols = [1000] * 60
        vols[19], vols[20], vols[24], vols[50], vols[58], vols[59] = 3400, 1600, 600, 700, 1700, 650
        out, prev = [], closes[0]
        for i, c in enumerate(closes):
            o = prev
            h, l = max(o, c) + 0.25, min(o, c) - 0.25
            if i == 19:
                l, h = 99.1, max(o, c) + 0.2
            if i == 24:
                l = 99.35
            if i == 50:
                l, h = 98.6, max(o, c) + 0.1
            if i == 58:
                h, l = 105.0, min(o, c) - 0.2
            if i == 59:
                o, l = 104.6, 104.25
            out.append({"t": f"2026-{6 + i // 30:02d}-{1 + i % 30:02d}", "o": round(o, 2),
                        "h": round(h, 2), "l": round(l, 2), "c": round(c, 2), "v": vols[i]})
            prev = c
        return out

    def hourly_candles():
        out, prev = [], 104.0
        for i in range(80):
            c = round(104.0 + 0.8 * math.sin(i * 0.35) + 0.01 * i, 2)
            out.append({"t": f"2026-09-{14 + i // 24:02d}T{i % 24:02d}:00:00Z", "o": prev,
                        "h": round(max(prev, c) + 0.15, 2), "l": round(min(prev, c) - 0.15, 2),
                        "c": c, "v": 300 + (i % 7) * 40})
            prev = c
        return out

    def intraday_candles():
        base = xs[-120:]
        out, prev = [], base[0]
        for i, v in enumerate(base):
            c = round(104.9 * v / xs[-1], 4)             # в масштабе цены (≈104.9 в конце)
            out.append({"t": f"2026-09-17T{10 + i // 12:02d}:{(i % 12) * 5:02d}:00Z", "o": prev,
                        "h": round(max(prev, c) + 0.03, 4), "l": round(min(prev, c) - 0.03, 4),
                        "c": c, "v": 50 + (i % 5) * 10})
            prev = c
        return out

    bids = [{"p": round(104.89 - 0.01 * i, 2), "q": 1000 if i == 5 else 100} for i in range(50)]
    asks = [{"p": round(104.9 + 0.01 * i, 2), "q": 1 if 10 <= i <= 14 else 100} for i in range(50)]
    ob_fake = {"best_bid": 104.89, "best_ask": 104.9, "spread": 0.01, "spread_bps": 0.95,
               "bid_vol": sum(b["q"] for b in bids), "ask_vol": sum(a["q"] for a in asks),
               "imbalance": 0.15, "top_bids": bids[:10], "top_asks": asks[:10],
               "levels_bid": bids, "levels_ask": asks,
               "wall_bid": {"p": 104.84, "q": 1000}, "wall_ask": None,
               "limit_up": None, "limit_down": None, "last_price": 104.9, "close_price": 104.0}
    tape_fake = {"count": 400, "window_min": 90, "buy_vol": 7000, "sell_vol": 3000, "delta": 4000,
                 "aggressor_ratio": 0.7, "buy_trades": 260, "sell_trades": 140,
                 "avg_trade_size": 25.0, "largest_prints": [{"q": 300, "p": 104.85, "side": "BUY"}]}
    dc, hc, ic = daily_candles(), hourly_candles(), intraday_candles()
    wy_real = wyckoff.analyze_dossier({"candles_daily_tail": dc, "candles_hourly": hc})
    assert wy_real.get("daily") and wy_real.get("hourly")
    assert {e["type"] for e in wy_real["daily"]["events_all"]} >= {"SC", "SPRING", "SOS", "LPS"}

    async def fake_dossier(t, ac):
        return {"ticker": t, "asset_class": ac, "source": "tinkoff", "price": {"price": 104.9},
                "instrument": {"figi": "F1", "uid": "U1", "name": "Тест",
                               "min_price_increment": 0.01, "lot": 1, "sector": "test"},
                "orderbook": ob_fake, "tape": tape_fake,
                "candles_intraday_5m": ic, "candles_hourly": hc, "candles_daily_tail": dc,
                "daily_metrics": {"last_close": 104.5, "prev_close": 104.6, "rsi14": 58.0,
                                  "atr14": 1.2, "ema9": 103.9, "ema21": 103.0, "ema50": 102.4,
                                  "ema200": 101.0, "above_ema50": True, "above_ema200": True,
                                  "low_20d": 98.6, "high_20d": 105.0, "avg_volume_20d": 60000.0},
                "intraday_metrics": {"vwap": 104.6, "vs_vwap_pct": 0.29, "session_volume": 4200},
                "wyckoff": wy_real, "maya": maya.analyze(ob_fake, tape_fake), "aether": None,
                "errors": ["тест: помеха досье"]}

    ws = [100.0 * math.exp(0.0025 * math.sin(i * 0.45) + 0.0002 * i) for i in range(n)]

    async def fake_candles(code, ac, iv, days):
        return [{"c": v} for v in (xs if code == "MX" else ys if code == "BR" else ws if code == "SI" else zs)]

    async def fake_actx(*a, **k):
        raise RuntimeError("нет эфемерид")

    async def check_astro_layer():
        saved = (astro.acontext, getattr(astro, "render_compact", None), astro.render_for_ai, astro.short_line)

        async def actx_ok(*a, **k):
            return {"mode": "precise"}
        astro.acontext = actx_ok
        astro.render_for_ai = lambda c: "ПОЛНЫЙ"
        astro.short_line = lambda c: "строка"
        try:
            astro.render_compact = lambda c, **k: "КОМПАКТ " + "ф" * 3000
            t, sl = await _astro_layer()
            assert t.startswith("КОМПАКТ") and len(t) > 3000 and sl == "строка"

            def boom_c(c, **k):
                raise ValueError("компакт сломан")
            astro.render_compact = boom_c
            assert (await _astro_layer()) == ("ПОЛНЫЙ", "строка")     # откат на полный
            astro.render_compact = lambda c, **k: ""
            assert (await _astro_layer())[0] == "ПОЛНЫЙ"              # пустой компакт → полный
            astro.acontext = fake_actx
            try:
                await _astro_layer()
                assert False, "ожидали исключение без эфемерид"
            except RuntimeError:
                pass
        finally:
            astro.acontext, rc_, astro.render_for_ai, astro.short_line = saved
            if rc_ is not None:
                astro.render_compact = rc_
            else:
                delattr(astro, "render_compact")

    pipeline.collect_dossier = fake_dossier
    pipeline.render_dossier = lambda d, full=True: "ДОСЬЕ " + str(d.get("ticker")) + " x" * 30000
    moex.candles = fake_candles
    astro.acontext = fake_actx
    tinkoff.enabled = lambda: False

    async def main():
        await check_astro_layer()
        t0 = time.time()
        ctx = await build("TEST", "futures")
        dt_build = time.time() - t0
        assert ctx["price"] == 104.9
        # досье в десятки тысяч знаков НЕ обрезается (потолок 150 000 — только авария; обычное
        # досье 3–15 тыс., синтетика теста — десятки тысяч: далеко ниже потолка)
        assert DOSSIER_LIMIT == 150_000
        assert 30000 < len(ctx["text"]) < DOSSIER_LIMIT and "обрезано" not in ctx["text"], len(ctx["text"])
        assert ctx["kuramoto"] and "КУРАМОТО" in ctx["kuramoto_text"]
        assert ctx["bifurcation"] and "БИФУРКАЦИИ" in ctx["bif_text"] and "база 1h" in ctx["bif_text"]
        assert any("астро" in e for e in ctx["errors"]) and any("досье" in e for e in ctx["errors"])
        assert ctx["astro"] == "" and ctx["aether"] == ""
        # ── новые слои 4.x ──
        wt = ctx["wyckoff_text"]
        assert "ВАЙКОФФ D1" in wt and "ВАЙКОФФ H1" in wt and "до льда" in wt and "ближе к" in wt, wt
        assert "СПРИНГ" in wt and "ЗНАК СИЛЫ" in wt and "% от цены" in wt and "Как читают" in wt
        assert "лёд 98.6 … крик 105" in wt and "ближе к крику" in wt, wt
        xt = ctx["xray_text"]
        assert "OBI" in xt and "CVD +4000" in xt and "VPIN 0.40" in xt and "классификатор" in xt, xt
        assert "Хёрст" in xt and "норме 3.0" in xt and "прокси-нормы" in xt and ctx["xray"]["available"]
        assert "к VWAP +0." in xt, xt
        ot = ctx["oracle_text"]
        assert "оракул" in ot and "директива" in ot and "голоса (" in ot and "Калибровка" in ot, ot
        assert ctx["oracle"]["state"] in ("ХАОС", "СИНГУЛЯРНОСТЬ", "У ПОРОГА", "ПАРЛАМЕНТ")
        assert {"xray", "maya", "bif", "trend", "wyckoff"} <= set(ctx["oracle"]["voices"]), ctx["oracle"]["voices"]
        assert ctx["directive"]["dir"] in ("long", "short", "flat") and "стакан" in ctx["directive"]["sources"]
        ct = ctx["calendar_text"]
        assert "КАЛЕНДАРЬ СРЕДЫ" in ct and "плотность среды" in ct and "модулятор фаз" in ct, ct
        assert isinstance(ctx["calendar"], dict) and "modulator" in ctx["calendar"]
        rt = ctx["reactor_text"]
        assert "РЕАКТОР" in rt or "недоступен" in rt, rt
        st_ = ctx["sync_text"]
        assert "СИНХРОНИЗАЦИЯ" in st_ and "ведущий" in st_ and ctx["sync"]["leader"] in LEADERS, st_
        assert "захват Адлера" in st_ and "IMD-резонанс" in st_ and "SYNC_RUPTURE" in st_
        assert ctx["scan_text"] == "" and ctx["scan"] is None
        assert ctx["weather_text"] == "" and ctx["weather"] is None
        for k in ("wyckoff_text", "xray_text", "reactor_text", "calendar_text", "sync_text",
                  "weather_text", "oracle_text", "scan_text", "astro", "aether", "kuramoto_text", "bif_text"):
            assert isinstance(ctx[k], str), k
        low = (wt + xt + ot + ct + st_).lower()
        for bad in ("покупай", "продавай", "входи в", "buy now"):
            assert bad not in low, bad
        print(f"build(TEST) за {dt_build:.1f} с; пробелы: {ctx['errors']}")
        print("── wyckoff_text ──\n" + wt)
        print("── xray_text ──\n" + xt)
        print("── oracle_text ──\n" + ot)
        print("── calendar_text ──\n" + ct)
        print("── sync_text ──\n" + st_)
        print("── reactor_text ──\n" + rt[:600] + ("…" if len(rt) > 600 else ""))

        # «обрезано» появляется только выше DOSSIER_LIMIT (подменяем потолок через глобал)
        g = globals()
        saved = g["DOSSIER_LIMIT"]
        g["DOSSIER_LIMIT"] = 5000
        try:
            ctx_clip = await build("TEST", "futures")
        finally:
            g["DOSSIER_LIMIT"] = saved
        assert "обрезано" in ctx_clip["text"] and len(ctx_clip["text"]) <= 5000 + 200

        # погода: регионы по корню контракта, рендер через фейковый open-meteo
        async def fake_weather(regions):
            return [{"region": r["name"], "temp_now": 7.5, "wind_now": 4.1, "avg_7d": 6.0,
                     "min_7d": 1.0, "max_7d": 11.0, "heating_degree_days_7d": 84.0,
                     "signal": "холодно — повышенный спрос на газ"} for r in regions]
        saved_w = weather.fetch_weather
        weather.fetch_weather = fake_weather
        try:
            wx, wtxt = await _weather_layer("BRV6")
        finally:
            weather.fetch_weather = saved_w
        assert wx and "ПОГОДА" in wtxt and "Мексиканский залив" in wtxt and "HDD7=84.0" in wtxt, wtxt
        assert await _weather_layer("SBER") == ([], "")

        # слой упал целиком — миссия не падает; сканер (память процесса) живёт отдельно от биржи
        async def boom(*a, **k):
            raise RuntimeError("биржа молчит")
        pipeline.collect_dossier = boom
        moex.candles = boom
        snaps = []
        tn_cnt = 500
        for i in range(210):
            if i and i % 5 == 0:
                tn_cnt += 60 if (i // 5) % 10 == 9 else 1
            snaps.append({"ts": 1000.0 + i * 3, "bid": 100.0, "ask": 100.1, "spread_bps": 10.0,
                          "pull": ("вверх" if i % 5 < 3 else "вниз"), "s_up": 0.6, "s_dn": 0.2,
                          "vu": ({"p_from": 101.1, "p_to": 101.5, "depth": 0.9} if i % 10 < 7
                                 else {"p_from": 103.0, "p_to": 103.4, "depth": 0.4}),
                          "vd": {"p_from": 98.0, "p_to": 98.4, "depth": 0.2 + 0.001 * i},
                          "wa": {"p": 102.0, "q": 500, "mult": 5.0},
                          "wb": {"p": round(99.5 - 0.01 * i, 2), "q": 900, "mult": 6.0},
                          "ar": 0.62, "imb": 0.1, "tn": tn_cnt})
        maya_scan._SCANS["TEST"] = {"snaps": deque(snaps), "errors": 0, "started": time.time() - 630,
                                    "until": time.time() + 3600, "note": None, "ether": None,
                                    "task": None}
        try:
            ctx2 = await build("TEST", "share")
        finally:
            maya_scan._SCANS.pop("TEST", None)
        assert ctx2["price"] is None and ctx2["kuramoto"] is None and ctx2["bifurcation"] is None
        assert "графа нет" in ctx2["kuramoto_text"] and ctx2["errors"]
        assert ctx2["wyckoff_text"] == "Вайкофф: расчёта нет (мало свечей)"
        assert "стакана нет" in ctx2["xray_text"]
        assert "КАЛЕНДАРЬ СРЕДЫ" in ctx2["calendar_text"] and "не выделен" in ctx2["calendar_text"]
        assert "плотность среды: расчёта нет" in ctx2["calendar_text"]
        assert "слой недоступен" in ctx2["sync_text"] and ctx2["weather_text"] == ""
        # биржа молчит: голосуют только небо (урезанный вес) и Хоукс сканера — стакана/ленты нет
        assert "оракул — состояние" in ctx2["oracle_text"] and ctx2["oracle"]["dir"] in ("long", "short", "flat")
        assert not ({"xray", "maya", "bif", "trend", "wyckoff"} & set(ctx2["oracle"]["voices"])), ctx2["oracle"]["voices"]
        assert "директива —" in ctx2["oracle_text"] and "стакан" not in ctx2["directive"]["sources"]
        assert ctx2["scan"] and ctx2["scan"]["ticks"] == 210 and "СКАНЕР СТАКАНА" in ctx2["scan_text"]
        assert ctx2["scan"]["hawkes"] and ctx2["oracle"]["hawkes_n"] == ctx2["scan"]["hawkes"]["n"]
        for k in ("wyckoff_text", "xray_text", "reactor_text", "calendar_text", "sync_text",
                  "weather_text", "oracle_text", "scan_text"):
            assert isinstance(ctx2[k], str), k
        print("── scan_text (при молчащей бирже) ──\n" + ctx2["scan_text"])

        # light(): без Tinkoff — MOEX
        async def fake_lp(t, ac):
            return {"price": 101.0, "change_pct": 1.2, "volume_today": 5000, "bid": 100.9, "offer": 101.1}
        moex.last_price = fake_lp
        lt = await light("TEST", None, "share")
        assert lt["price"] == 101.0 and "101.0" in lt["text"] and lt["book"] is None
        assert lt["xray"] is None and lt["maya"] is None
        # light(): с Tinkoff — стакан (50 уровней), лента, рентген и Майя
        tinkoff.enabled = lambda: True

        async def t_lp(f):
            return {"price": 102.0}

        async def t_ob(f, depth=10):
            b = [{"p": round(101.9 - 0.1 * i, 1), "q": 900 if i == 4 else 100} for i in range(50)]
            a = [{"p": round(102.1 + 0.1 * i, 1), "q": 1 if 6 <= i <= 10 else 100} for i in range(50)]
            return {"best_bid": 101.9, "best_ask": 102.1, "spread": 0.2, "spread_bps": 19.6,
                    "bid_vol": 300, "ask_vol": 100, "imbalance": 0.5, "close_price": 100.0,
                    "levels_bid": b, "levels_ask": a, "top_bids": b[:10], "top_asks": a[:10],
                    "wall_bid": {"p": 101.5, "q": 900}, "wall_ask": None}

        async def t_tr(f, minutes=15):
            return {"count": 40, "buy_vol": 700, "sell_vol": 300, "delta": 400, "aggressor_ratio": 0.7,
                    "avg_trade_size": 25.0, "largest_prints": [{"q": 100, "p": 102.0, "side": "BUY"}]}
        tinkoff.last_price, tinkoff.orderbook, tinkoff.last_trades = t_lp, t_ob, t_tr
        lt2 = await light("TEST", "F1", "share")
        assert lt2["price"] == 102.0 and lt2["book"]["best_bid"] == 101.9
        assert "+2.00%" in lt2["text"] and "покупатели" in lt2["text"] and "дисбаланс +0.50" in lt2["text"]
        assert "Рентген: OBI" in lt2["text"] and "Майя: тяга вверх" in lt2["text"], lt2["text"]
        assert lt2["xray"]["available"] and lt2["maya"]["available"] and lt2["maya"]["pull"]["side"] == "вверх"
        assert "классификатор" in lt2["text"] and "с тягой согласен" in lt2["text"]
        assert 5 <= lt2["text"].count("\n") + 1 <= 10, lt2["text"]
        print("── light() ──\n" + lt2["text"])

    asyncio.run(main())
    print("market_ctx self-test OK")
