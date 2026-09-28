# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — ПАТОЛОГОАНАТОМ (Агент 3: реверс-инжиниринг ловушек ММ).

Проект PREDATOR, Агент 3 «Reverse Engineer & Pattern Finder». Задача
владельца: «искать в записанной базе УЛЬТРА-ПРОБИВЫ (сдвиг цены >1% без
отката). Нашёл — отмотай на 60с назад, изучи микроструктуру и макро-физику:
что сделал ММ перед срывом? Скрытая абсорбция? Возрос ли Хоукс? Куда
переставили плиту? Совпало ли с макро-пучностью? Выдай жёсткий паттерн».

Работает по БД Пылесоса (Агент 1) ИЛИ по переданным массивам — офлайн,
без сети, детерминированно. Конвейер:

  1. НАЙТИ ПРОБИВЫ: на сетке mid-цены ищем окна horizon, где |ΔP| ≥ move_thr
     и ход БЕЗ ОТКАТА (max отход против ≤ pullback_frac·ход) — это каскадный
     срыв, а не рыночный шум;
  2. ОТМОТАТЬ на lookback секунд до старта пробива → «предвестник-окно»;
  3. ИЗВЛЕЧЬ признаки предвестника (все из уже построенных движков):
     · тренд OBI и его дельта (куда перекладывали лимитки — плита едет);
     · CVD и знак потока (реальные деньги против рисунка стакана?);
     · сжатие спреда (Казимир: пружина перед прострелом);
     · ветвление Хоукса по СЫРЫМ таймстампам сделок (токсичность растёт?);
     · скрытая абсорбция (объём кипел, цена стояла — Кит держал айсберг);
     · тег плотности среды (Физик: наэлектризовано ли);
  4. АГРЕГИРОВАТЬ в СИГНАТУРУ: медианы признаков по всем пробивам одной
     стороны + доля пробоев, где признак «горит» — это и есть паттерн
     ловушки, машинно-читаемый (потребитель — Снайпер, Агент 4).

⚫ Паттерн — статистика прошлого, НЕ обещание будущего. Рынок не обязан
повторяться. НЕ сигнал. 18+. NO DUMMIES: мало данных → честный None/пусто.

Self-тест: python3 -m backend.patterns
"""
from __future__ import annotations

import json

import numpy as np

try:
    from . import hawkes, microstructure
except ImportError:                                        # запуск как скрипт
    import hawkes, microstructure  # noqa: E401

# ── пороги детектора (единственное место) ───────────────────────────────────
MOVE_THR = 0.01        # ультра-пробив: |ΔP| ≥ 1% (приказ владельца)
PULLBACK_FRAC = 0.30   # «без отката»: контр-ход ≤ 30% амплитуды пробива
HORIZON_S = 120.0      # окно поиска хода пробива (сек)
LOOKBACK_S = 60.0      # отмотка предвестника (сек — приказ владельца)
MIN_EVENTS = 3         # меньше пробоев — сигнатуру не строим (NO DUMMIES)
GAP_S = 60.0           # дедуп: пробои ближе — один эпизод


def _f(x, d=0.0):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


# ── извлечение mid-ряда и сделок из БД Пылесоса ─────────────────────────────
def load_from_db(db_path: str, figi: str) -> dict:
    """Прочитать сырьё Пылесоса: book (mid, obi, spread) и trades (t, side, qty)."""
    import sqlite3
    db = sqlite3.connect(db_path)
    try:
        book = db.execute(
            "SELECT ts_ms,best_bid,best_ask,obi,spread_bps FROM book "
            "WHERE figi=? ORDER BY ts_ms", (figi,)).fetchall()
        trades = db.execute(
            "SELECT ts_ms,price,qty,side FROM trades WHERE figi=? ORDER BY ts_ms",
            (figi,)).fetchall()
    finally:
        db.close()
    mid = [((bb + ba) / 2.0 if bb and ba else 0.0) for _, bb, ba, _, _ in book]
    return {
        "book_ts": [r[0] for r in book], "mid": mid,
        "obi": [r[3] for r in book], "spread_bps": [r[4] for r in book],
        "trade_ts": [r[0] for r in trades],
        "trade_side": [r[3] for r in trades],
        "trade_qty": [r[2] for r in trades],
    }


# ── поиск ультра-пробивов ───────────────────────────────────────────────────
def find_breakouts(book_ts, mid, *, move_thr=MOVE_THR, horizon_s=HORIZON_S,
                   pullback_frac=PULLBACK_FRAC, gap_s=GAP_S) -> list:
    """Каскадные срывы: |ΔP| ≥ move_thr за horizon без отката. Возврат:
    [{t0_ms, t1_ms, side, move_frac, i0}] — старт/финиш/сторона/амплитуда."""
    ts = [int(t) for t in (book_ts or [])]
    m = [_f(x) for x in (mid or [])]
    n = len(m)
    if n < 20:
        return []
    hor_ms = horizon_s * 1000.0
    events = []
    seen_t: list = []
    for i in range(n):
        if m[i] <= 0:
            continue
        j = i
        while j + 1 < n and ts[j + 1] - ts[i] <= hor_ms:
            j += 1
        if j <= i:
            continue
        seg = m[i:j + 1]
        p0 = seg[0]
        up = (max(seg) - p0) / p0
        dn = (p0 - min(seg)) / p0
        # какая сторона дала ход ≥ порога первой и без отката
        for side, amp in (("up", up), ("down", dn)):
            if amp < move_thr:
                continue
            if side == "up":
                peak = int(np.argmax(seg))
                counter = (p0 - min(seg[:peak + 1])) / p0
            else:
                peak = int(np.argmin(seg))
                counter = (max(seg[:peak + 1]) - p0) / p0
            if counter > pullback_frac * amp:
                continue
            # ЯКОРЬ t0 = ТОЧКА ОТРЫВА: первый бар, где направленный ход от p0
            # превысил max(0.1%, 15% амплитуды) — начало срыва, а не момент,
            # когда его впервые «видно» вперёд по окну (иначе предвестник-окно
            # уезжает в предыдущий флет)
            thr_lift = max(0.001, 0.15 * amp)
            lift = peak
            for k in range(peak + 1):
                mv = ((seg[k] - p0) / p0 if side == "up"
                      else (p0 - seg[k]) / p0)
                if mv >= thr_lift:
                    lift = k
                    break
            t_lift = ts[i + lift]
            if any(abs(t_lift - st) < gap_s * 1000.0 for st in seen_t):
                break                                  # тот же эпизод — дедуп
            seen_t.append(t_lift)
            events.append({"t0_ms": t_lift, "t1_ms": ts[i + peak],
                           "side": side, "move_frac": round(amp, 5),
                           "i0": i + lift})
            break
    return events


# ── признаки предвестника (окно −lookback…старт) ────────────────────────────
def _slope(xs) -> float:
    xs = [_f(x) for x in xs]
    if len(xs) < 3:
        return 0.0
    t = np.arange(len(xs), dtype=float)
    return float(np.polyfit(t, np.asarray(xs), 1)[0])


def precursor_features(data: dict, ev: dict, *, lookback_s=LOOKBACK_S) -> dict:
    """Признаки за lookback до старта пробива: OBI-тренд/дельта, CVD, сжатие
    спреда, ветвление Хоукса, абсорбция. Всё из уже построенных движков."""
    t0 = ev["t0_ms"]
    beg = t0 - lookback_s * 1000.0
    bts, obi, spr = data["book_ts"], data["obi"], data["spread_bps"]
    mid = data["mid"]
    win = [k for k, t in enumerate(bts) if beg <= t <= t0]
    if len(win) < 3:
        return {}
    obi_w = [obi[k] for k in win]
    spr_w = [spr[k] for k in win]
    mid_w = [mid[k] for k in win]
    # сжатие спреда: спред в конце против начала окна (Казимир — пружина)
    spr0 = spr_w[0] if spr_w[0] else 1e-9
    spread_squeeze = round((spr_w[0] - spr_w[-1]) / spr0, 3) if spr0 else 0.0
    # поток за окно: CVD и ветвление Хоукса по СЫРЫМ таймстампам
    tts, tside, tqty = data["trade_ts"], data["trade_side"], data["trade_qty"]
    idx = [k for k, t in enumerate(tts) if beg <= t <= t0]
    cvd = sum(_f(tqty[k]) * _f(tside[k]) for k in idx)
    raw_times = [tts[k] for k in idx]
    hk = hawkes.branching_times(raw_times) if len(raw_times) >= 60 else None
    # абсорбция: объём кипел (много сделок), а цена стояла (малый размах)
    vol = sum(_f(tqty[k]) for k in idx)
    move_in_win = (max(mid_w) - min(mid_w)) / mid_w[0] if mid_w[0] else 0.0
    absorbed = bool(vol > 0 and move_in_win < 0.002 and len(idx) >= 30)
    return {
        "obi_slope": round(_slope(obi_w), 5),
        "obi_delta": round(obi_w[-1] - obi_w[0], 4),
        "obi_end": round(_f(obi_w[-1]), 4),
        "cvd": round(cvd, 1),
        "cvd_with_side": bool((cvd > 0) == (ev["side"] == "up")),
        "spread_squeeze": spread_squeeze,
        "hawkes_n": (round(hk["n"], 3) if hk else None),
        "absorbed": absorbed,
        "n_trades": len(idx),
    }


# ── сигнатура: агрегат признаков по всем пробоям одной стороны ───────────────
def _median(xs):
    xs = sorted(_f(x) for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def signature(features_list: list, side: str) -> dict | None:
    """Свести признаки предвестников в ПАТТЕРН ловушки: медианы + доля
    пробоев, где признак «горит». Машинно-читаемо для Снайпера."""
    feats = [f for f in features_list if f]
    if len(feats) < MIN_EVENTS:
        return None
    n = len(feats)
    def frac(pred):
        return round(sum(1 for f in feats if pred(f)) / n, 3)
    sig = {
        "side": side, "n_events": n,
        "obi_slope_med": _median([f.get("obi_slope") for f in feats]),
        "obi_delta_med": _median([f.get("obi_delta") for f in feats]),
        "cvd_med": _median([f.get("cvd") for f in feats]),
        "spread_squeeze_med": _median([f.get("spread_squeeze") for f in feats]),
        "hawkes_n_med": _median([f.get("hawkes_n") for f in feats]),
        "frac_cvd_with_side": frac(lambda f: f.get("cvd_with_side")),
        "frac_absorbed": frac(lambda f: f.get("absorbed")),
        "frac_squeeze": frac(lambda f: _f(f.get("spread_squeeze")) > 0.2),
        "frac_hawkes_hot": frac(lambda f: _f(f.get("hawkes_n")) >= 0.8),
    }
    # словесный портрет ловушки
    marks = []
    if _f(sig["frac_squeeze"]) >= 0.5:
        marks.append("спред сжимался (Казимир-пружина)")
    if _f(sig["frac_absorbed"]) >= 0.4:
        marks.append("скрытая абсорбция (Кит держал айсберг)")
    if _f(sig["frac_hawkes_hot"]) >= 0.5:
        marks.append("токсичность Хоукса росла")
    if _f(sig["frac_cvd_with_side"]) >= 0.6:
        marks.append(f"реальный поток (CVD) заранее лёг в сторону {side}")
    obi_d = _f(sig["obi_delta_med"])
    if abs(obi_d) > 0.05:
        marks.append(f"плиту переставляли (OBI-дельта {obi_d:+.2f})")
    sig["portrait"] = ("ловушка ММ перед пробоем " + side + ": "
                       + ("; ".join(marks) if marks
                          else "явных предвестников не сложилось"))
    sig["frame"] = ("⚫ паттерн — статистика прошлого, НЕ обещание будущего; "
                    "рынок не обязан повторяться. НЕ сигнал. 18+")
    return sig


def mine(data: dict, **kw) -> dict:
    """Полный конвейер Патологоанатома: пробои → предвестники → сигнатуры
    вверх/вниз. data — из load_from_db или собранный вручную."""
    evs = find_breakouts(data.get("book_ts"), data.get("mid"), **{
        k: v for k, v in kw.items()
        if k in ("move_thr", "horizon_s", "pullback_frac", "gap_s")})
    feats_up, feats_dn = [], []
    for ev in evs:
        f = precursor_features(data, ev)
        (feats_up if ev["side"] == "up" else feats_dn).append(f)
    return {
        "n_breakouts": len(evs),
        "events": evs,
        "signature_up": signature(feats_up, "up"),
        "signature_down": signature(feats_dn, "down"),
        "note": (f"найдено пробоев: {len(evs)} "
                 f"(вверх {len(feats_up)}, вниз {len(feats_dn)}); "
                 "сигнатура строится от " + str(MIN_EVENTS) + " пробоев на сторону"),
    }


def save_signatures(result: dict, path: str) -> None:
    """Сигнатуры → JSON для Снайпера (Агент 4) — машинно-читаемый паттерн."""
    payload = {"signature_up": result.get("signature_up"),
               "signature_down": result.get("signature_down"),
               "n_breakouts": result.get("n_breakouts")}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ── self-test: синтетическая база с подложенными пробоями ───────────────────
if __name__ == "__main__":
    # строим синтетику: спокойный фон + 4 пробоя ВВЕРХ, у каждого предвестник
    # (сжатие спреда + рост односторонних сделок + перекладка OBI вверх)
    book_ts, mid, obi, spr = [], [], [], []
    trade_ts, trade_side, trade_qty = [], [], []
    t = 0
    base = 100.0
    breakout_starts = []
    for epi in range(4):
        # 90с спокойного фона (снимок стакана раз в секунду)
        for _ in range(90):
            book_ts.append(t*1000); mid.append(base); obi.append(0.0); spr.append(3.0)
            # редкие двусторонние сделки — фон
            if t % 3 == 0:
                trade_ts.append(t * 1000); trade_side.append(1 if t % 6 == 0 else -1)
                trade_qty.append(2.0)
            t += 1
        # 60с ПРЕДВЕСТНИКА: спред сжимается 3→0.6, OBI ползёт вверх, поток
        # односторонний buy и УЧАЩАЕТСЯ (каскад Хоукса)
        breakout_starts.append((len(book_ts)))       # индекс старта пробива
        for k in range(60):
            book_ts.append(t*1000); mid.append(base)
            obi.append(min(0.6, k / 100.0)); spr.append(max(0.6, 3.0 - k * 0.04))
            # учащающийся buy-поток: чем ближе к срыву, тем гуще
            reps = 1 + k // 12
            for _r in range(reps):
                trade_ts.append(t * 1000 + _r * 50)
                trade_side.append(1); trade_qty.append(3.0)
            t += 1
        # СРЫВ ВВЕРХ на +1.5% за ~30с без отката
        p = base
        for k in range(30):
            p = base * (1.0 + 0.015 * (k + 1) / 30.0)
            book_ts.append(t*1000); mid.append(p); obi.append(0.5); spr.append(1.0)
            trade_ts.append(t * 1000); trade_side.append(1); trade_qty.append(5.0)
            t += 1
        base = p                                     # новый уровень (без отката)
        # пауза между эпизодами
        for _ in range(30):
            book_ts.append(t*1000); mid.append(base); obi.append(0.0); spr.append(3.0)
            t += 1

    data = {"book_ts": book_ts, "mid": mid, "obi": obi, "spread_bps": spr,
            "trade_ts": trade_ts, "trade_side": trade_side, "trade_qty": trade_qty}

    # 1) находит 4 пробоя вверх (>1%, без отката); дедуп 150с (эпизоды через
    #    210с) склеивает двойную детекцию внутри одного срыва в одну
    evs = find_breakouts(book_ts, mid, gap_s=150.0)
    up = [e for e in evs if e["side"] == "up"]
    assert len(up) == 4, [(e["side"], e["move_frac"]) for e in evs]
    assert all(e["move_frac"] >= 0.01 for e in up)

    # 2) шум без пробоя не ловится
    flat_ts = list(range(0, 300))
    flat_mid = [100.0 + (0.01 if k % 2 else -0.01) for k in flat_ts]
    assert find_breakouts([x * 1000 for x in flat_ts], flat_mid) == []

    # 3) полный конвейер: сигнатура вверх строится и «горит» предвестниками
    res = mine(data, gap_s=150.0)
    assert res["n_breakouts"] >= 4
    sig = res["signature_up"]
    assert sig and sig["n_events"] == 4, sig
    assert sig["frac_squeeze"] >= 0.5, sig            # спред сжимался
    assert sig["frac_cvd_with_side"] >= 0.6, sig      # поток лёг вверх заранее
    assert sig["obi_delta_med"] > 0.05, sig           # плиту двигали вверх
    assert "ловушка ММ перед пробоем up" in sig["portrait"]

    # 4) сохранение сигнатур в JSON (для Снайпера)
    import tempfile, os
    tf = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tf.close()
    save_signatures(res, tf.name)
    loaded = json.load(open(tf.name, encoding="utf-8"))
    assert loaded["signature_up"]["n_events"] == 4
    os.unlink(tf.name)

    # 5) NO DUMMIES: мало пробоев → сигнатура None
    assert signature([], "up") is None
    assert signature([{"obi_slope": 0}], "up") is None    # < MIN_EVENTS

    # 6) детерминизм
    assert mine(data, gap_s=150.0)["signature_up"] == sig

    print("patterns self-test OK: Патологоанатом находит ультра-пробои (>1% "
          "без отката), мотает на 60с, извлекает предвестники (сжатие спреда, "
          "односторонний CVD, перекладку OBI, Хоукс), сводит в сигнатуру "
          "ловушки ММ и пишет JSON для Снайпера; NO DUMMIES, детерминизм")
