"""ОРАКУЛ // ПИФИЯ — финансовая астрология (метод У.Д. Ганна) по РЕАЛЬНОМУ небу.

Считает суточную карту по РЕАЛЬНЫМ эфемеридам JPL (de440.bsp / любой *.bsp рядом).

ЧТО ЗДЕСЬ НАСТОЯЩЕЕ 3D (правка «небо, которого нет» → небо, которое есть):
  🔵 положение тела — apparent, ДОЛГОТА И ШИРОТА. β больше не выбрасывается
     (`_latlon_at`); она доживает до потребителя в поле planets[].lat.
  🔵 «где тело» — РЕАЛЬНОЕ созвездие из 88 IAU по границам Делпорта, с учётом
     широты (realsky3d.constellation_of_ecliptic). Поле planets[].con.
     У Плутона β до ±17°, у Луны/Меркурия/Сатурна тоже — они реально стоят в
     Ките и Змееносце, а не там, куда их клала плоская проекция.
  🔵 асцендент и дома — по реальным границам созвездий (houses_real): их 13, они
     неравные (Дева ~44°, Скорпион ~7°, Змееносец ~18.6°).
  🟡 тридцатиградусные «знаки» (поле sign) СОХРАНЕНЫ для старых потребителей,
     но это символьная сетка, а не небо: из-за прецессии она отстала от неба на
     ~25°. Везде, где знак попадает в текст для ИИ, он помечен 🟡 и стоит ПОСЛЕ
     реального созвездия. Равные 30° дома — отдельный ключ houses["equal"].
  ⚫ решение и ответственность — на операторе; небо не предсказывает событий.

Затмения и окна ретро считаются по эфемеридам (_eclipses_calc, _retro_windows):
таблицы 2026 (ECLIPSES, MERCURY_RX, OUTER_RX) с 2027 года пустеют, а DE440 живёт
до 2650-го; таблицы остались страховкой и lite-режимом. Для ИИ есть короткий
рендер фактов render_compact(ctx) — 2–3.5 тыс. знаков, без рамок, без 30°-сетки,
толкование школы отдельным абзацем; render_for_ai сохранён для старых потребителей.

Если skyfield/эфемерид нет — мягкий откат на аналитический движок (фаза Луны
аналитически + выверенная таблица 2026). Контракт current_context() сохранён в
обоих режимах: думающие стадии и фронт работают всегда. Если недоступен
realsky3d — 3D-слой честно отдаёт None + note, а разбор продолжает жить.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import threading
import time as _time
from datetime import date, datetime, timedelta, timezone

# ── 3D-слой настоящего неба (88 созвездий IAU, границы Делпорта, B1875) ──
# ABSOLUTE OFFLINE: модуль не ходит в сеть, зависимость ровно одна — numpy.
# Импорт в защите: нет модуля → слой честно молчит (None + note), разбор жив.
try:
    from . import realsky3d as _RS          # обычный импорт пакетом
except Exception:                            # noqa: BLE001
    try:
        import realsky3d as _RS              # запуск файла напрямую (self-тест)
    except Exception:                        # noqa: BLE001
        _RS = None
_RS_ERR = None if _RS is not None else "realsky3d недоступен (нет модуля/numpy)"

# ── константы ──────────────────────────────────────────────────────────
SIGNS = ["Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева", "Весы",
         "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы"]
SIGN_GLYPH = ["♈", "♉", "♊", "♋", "♌", "♍", "♎", "♏", "♐", "♑", "♒", "♓"]
ELEMENT = ["Огонь", "Земля", "Воздух", "Вода"] * 3  # по индексу знака
MODALITY = ["Кардинал", "Фикс", "Мутабельн"] * 4
PLANET_GLYPH = {"Солнце": "☉", "Луна": "☽", "Меркурий": "☿", "Венера": "♀",
                "Марс": "♂", "Юпитер": "♃", "Сатурн": "♄", "Уран": "♅",
                "Нептун": "♆", "Плутон": "♇"}
PLANET_ROLE = {
    "Солнце": "тренд/жизненная сила рынка", "Луна": "толпа, ликвидность, краткосрочные развороты",
    "Меркурий": "новости, сделки, связь — ретро = дезинформация/ложные пробои",
    "Венера": "деньги, ценности, аппетит к риску", "Марс": "агрессия, импульс, проливы/выносы",
    "Юпитер": "расширение, оптимизм, пузыри", "Сатурн": "сжатие, страх, дно/потолок, дисциплина",
    "Уран": "шок, гэп, неожиданность", "Нептун": "иллюзия, пузырь/туман, сырьё-нефть",
    "Плутон": "власть крупного капитала, трансформация, кризис"}
# 🔵 Королевские/ключевые неподвижные звёзды: эклиптические λ И β на J2000.
# Раньше здесь лежала одна долгота, а широта не существовала как понятие — и орб
# «≤1.5° по долготе» объявлял соединение с Фомальгаутом телу, до которого по небу
# больше 21°. Плоскость вместо сферы. Теперь пара (λ, β), а сепарация считается
# истинным углом на сфере (_royal_hits). Долгота прецессируется к дате.
ROYAL_STARS = {  # имя: (λ_J2000°, β_J2000°)
    "Альдебаран (Страж Востока)": (69.79, -5.47),
    "Регул (Страж Севера)": (149.83, +0.46),
    "Антарес (Страж Запада)": (249.76, -4.57),
    "Фомальгаут (Страж Юга)": (333.86, -21.14),
    "Спика (удача/богатство)": (203.84, -2.05),
    "Альголь (опасность)": (56.17, +22.43),
}
# общая прецессия в долготе, °/год (50.2877″/год) — сдвиг λ звезды от J2000 к дате
PRECESSION_DEG_PER_YEAR = 50.2877 / 3600.0

# 🟡 Глифы созвездий — УКРАШЕНИЕ ИНТЕРФЕЙСА, а не ответ. Настоящих типографских
# знаков у 88 созвездий нет: они есть только у 12 «знаковых» + ⛎ Змееносец.
# Для остальных честно ставится нейтральная звёздочка, а не выдуманный символ.
CON_GLYPH = {"Ari": "♈", "Tau": "♉", "Gem": "♊", "Cnc": "♋", "Leo": "♌",
             "Vir": "♍", "Lib": "♎", "Sco": "♏", "Sgr": "♐", "Cap": "♑",
             "Aqr": "♒", "Psc": "♓", "Oph": "⛎"}
CON_GLYPH_OTHER = "✦"   # созвездие вне 13 эклиптических — глифа не существует
# аспекты Ганна: угол → (имя, орб для светил, орб для планет, тип)
ASPECTS = [(0, "Соединение", 8, 6, "нейтр"), (60, "Секстиль", 5, 4, "гарм"),
           (90, "Квадрат", 7, 6, "напряж"), (120, "Трин", 7, 5, "гарм"),
           (180, "Оппозиция", 8, 6, "напряж"),
           (45, "Полуквадрат", 2.5, 2, "напряж"), (135, "Полутораквадрат", 2.5, 2, "напряж")]
LUMINARIES = ("Солнце", "Луна")

# Москва (мунданная карта рынка MOEX по умолчанию)
DEFAULT_LAT = 55.7558
DEFAULT_LON = 37.6173


# ══════════════════════════════════════════════════════════════════════
# ОТКАТ: аналитическая фаза Луны + таблица 2026 (без зависимостей)
# ══════════════════════════════════════════════════════════════════════
_SYNODIC = 29.530588853
_REF_NEW_MOON_JD = 2451550.1


def _jd(dt: datetime) -> float:
    y, m = dt.year, dt.month
    d = dt.day + dt.hour / 24 + dt.minute / 1440 + dt.second / 86400
    if m <= 2:
        y -= 1; m += 12
    a = y // 100; b = 2 - a + a // 4
    return (math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1))
            + d + b - 1524.5)


def _phase_name(frac: float):
    if frac < 0.0335 or frac > 0.9665: return "Новолуние", "🌑"
    if frac < 0.2165: return "Растущий серп", "🌒"
    if frac < 0.2835: return "Первая четверть", "🌓"
    if frac < 0.4665: return "Растущая Луна", "🌔"
    if frac < 0.5335: return "Полнолуние", "🌕"
    if frac < 0.7165: return "Убывающая Луна", "🌖"
    if frac < 0.7835: return "Последняя четверть", "🌗"
    return "Убывающий серп", "🌘"


def moon_phase(dt: datetime) -> dict:
    age = ((_jd(dt) - _REF_NEW_MOON_JD) % _SYNODIC + _SYNODIC) % _SYNODIC
    frac = age / _SYNODIC
    illum = (1 - math.cos(2 * math.pi * frac)) / 2
    name, em = _phase_name(frac)
    return {"age_days": round(age, 1), "fraction": round(frac, 3),
            "illumination": round(illum * 100), "name": name, "emoji": em,
            "waxing": frac < 0.5,
            "days_to_full": round(((0.5 - frac) % 1) * _SYNODIC, 1),
            "days_to_new": round(((1.0 - frac) % 1) * _SYNODIC, 1)}


# 🟡 Резервные таблицы окон на 2026: используются в lite-режиме (нет эфемерид) и
# как страховка precise-режима, если расчёт по эфемеридам (_eclipses_calc,
# _retro_windows) дал сбой. Имена в скобках — тропическая 30°-сетка, а не
# созвездия: помечены 🟡, чтобы ИИ не приняла их за ответ на вопрос «где тело».
# В precise-режиме ретро берётся из измеренной скорости, окна и затмения — из
# эфемерид, положение — из реального созвездия.
MERCURY_RX = [
    (date(2026, 2, 25), date(2026, 3, 20), "Меркурий ретро (тропич. Рыбы/Водолей 🟡)"),
    (date(2026, 6, 29), date(2026, 7, 23), "Меркурий ретро (тропич. Рак/Лев 🟡)"),
    (date(2026, 10, 24), date(2026, 11, 13), "Меркурий ретро (тропич. Скорпион 🟡)"),
]
OUTER_RX = [
    (date(2025, 11, 11), date(2026, 3, 11), "Юпитер ретро"),
    # по эфемеридам DE440: станции 26.07 и 10.12.2026 (раньше здесь стояли даты 2025 года)
    (date(2026, 7, 26), date(2026, 12, 10), "Сатурн ретро"),
    (date(2026, 10, 3), date(2026, 11, 13), "Венера ретро"),
]
ECLIPSES = [
    (date(2026, 2, 17), "Кольцеобразное солнечное затмение (тропич. Водолей 🟡)"),
    (date(2026, 3, 3), "Полное лунное затмение (тропич. Дева 🟡)"),
    (date(2026, 8, 12), "Полное солнечное затмение (тропич. Лев 🟡, видно в Европе)"),
    (date(2026, 8, 28), "Частное лунное затмение (тропич. Рыбы 🟡)"),
]


def _window_state(today: date, windows):
    active, upcoming = [], []
    for s, e, lbl in windows:
        if s <= today <= e:
            active.append({"label": lbl, "since": s.isoformat(),
                           "until": e.isoformat(), "days_left": (e - today).days})
        elif s > today:
            upcoming.append({"label": lbl, "start": s.isoformat(),
                             "in_days": (s - today).days})
    upcoming.sort(key=lambda x: x["in_days"])
    return active, upcoming


def _eclipse_block(today: date) -> dict:
    nxt = [{"label": l, "date": d.isoformat(), "in_days": (d - today).days}
           for d, l in ECLIPSES if d >= today]
    nxt.sort(key=lambda x: x["in_days"])
    rec = [{"label": l, "date": d.isoformat(), "days_ago": (today - d).days}
           for d, l in ECLIPSES if 0 <= (today - d).days <= 5]
    return {"eclipse_next": nxt[:2], "eclipse_recent": rec,
            "near_eclipse": bool(nxt and nxt[0]["in_days"] <= 5) or bool(rec)}


# ══════════════════════════════════════════════════════════════════════
# ТОЧНЫЙ ДВИЖОК (skyfield + de440/любой .bsp)
# ══════════════════════════════════════════════════════════════════════
_SF = None  # кэш загруженного движка


def _find_ephemeris() -> str | None:
    env = os.environ.get("PYTHIA_EPHEMERIS")
    if env and os.path.exists(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = [os.path.join(here, "..", "data"), os.path.join(here, "data"),
            os.path.join(here, ".."), here, "/mnt/user-data/uploads"]
    prefer = ["de440.bsp", "de440s.bsp", "de441.bsp", "de430.bsp", "de421.bsp"]
    for d in dirs:
        for name in prefer:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
    for d in dirs:  # любой .bsp
        try:
            for f in sorted(os.listdir(d)):
                if f.lower().endswith(".bsp"):
                    return os.path.join(d, f)
        except Exception:
            pass
    return None


_SF_LOCK = threading.Lock()


def _load_sf():
    """Ленивая загрузка skyfield+эфемерид. None если недоступно.
    Под замком: раньше два потока (скан + анализ) могли пройти проверку
    одновременно и загрузить 120-МБ de440 ДВАЖДЫ — всплеск памяти и краш."""
    global _SF
    if _SF is not None:
        return _SF or None
    with _SF_LOCK:
        return _load_sf_locked()


def _load_sf_locked():
    global _SF
    if _SF is not None:
        return _SF or None
    try:
        from skyfield.api import load, load_file
        from skyfield.framelib import ecliptic_frame
        path = _find_ephemeris()
        if not path:
            _SF = False
            return None
        ts = load.timescale(builtin=True)
        eph = load_file(path)
        earth = eph["earth"]
        bodies = {"Солнце": "sun", "Луна": "moon", "Меркурий": "mercury",
                  "Венера": "venus", "Марс": "mars barycenter",
                  "Юпитер": "jupiter barycenter", "Сатурн": "saturn barycenter",
                  "Уран": "uranus barycenter", "Нептун": "neptune barycenter",
                  "Плутон": "pluto barycenter"}
        # доступность тел
        avail = {}
        tt = ts.utc(2026, 1, 1)
        for nm, key in bodies.items():
            try:
                earth.at(tt).observe(eph[key]).apparent()
                avail[nm] = key
            except Exception:
                pass
        _SF = {"ts": ts, "eph": eph, "earth": earth, "frame": ecliptic_frame,
               "bodies": avail, "path": path}
        return _SF
    except Exception:
        _SF = False
        return None


def _latlon_at(sf, key, t) -> tuple[float, float, float]:
    """🔵 Видимое (apparent) положение тела: ШИРОТА, долгота, расстояние.

    Здесь раньше стояло `_, lon, _ = ...` — эклиптическая широта β и дистанция
    выбрасывались в мусор, и небо становилось плоским. β возвращается всегда:
    без неё нельзя сказать, в каком созвездии тело реально стоит.
    """
    lat, lon, dist = sf["earth"].at(t).observe(sf["eph"][key]).apparent().frame_latlon(sf["frame"])
    return float(lat.degrees), float(lon.degrees) % 360, float(dist.au)


def _lon_at(sf, key, t) -> float:
    """Тонкая обёртка над _latlon_at — только долгота.
    Оставлена для мест, где широта действительно не нужна: скорость по долготе,
    элонгация Луна−Солнце (фаза), поиск станций и лунаций."""
    return _latlon_at(sf, key, t)[1]


# ══════════════════════════════════════════════════════════════════════
# 3D-СЛОЙ: РЕАЛЬНОЕ СОЗВЕЗДИЕ ВМЕСТО ТРОПИЧЕСКОГО ЛОМТЯ
# Всё в try/except: сбой 3D-слоя не роняет разбор, поля честно None + note.
# ══════════════════════════════════════════════════════════════════════

def _jd_utc(dt_obj: datetime) -> float:
    """Юлианская дата UTC для 3D-слоя (тот же _jd, отдельное имя по смыслу)."""
    return _jd(dt_obj)


def _drift(jd: float):
    """🔵/🟡 Насколько тропическая сетка отстала от неба (градусы), или None.
    Единственная точка входа: любой вызов 3D-слоя обязан быть в защите, иначе
    сбой слоя роняет весь precise-расчёт и разбор молча падает в lite-режим."""
    if _RS is None:
        return None
    try:
        d = _RS.drift_deg(jd)
        return None if d is None else round(float(d), 2)
    except Exception:                            # noqa: BLE001 — защита слоя
        return None


def _sky_of(lon: float, lat: float, jd: float) -> dict:
    """🔵 Ответ на вопрос «ГДЕ ТЕЛО»: реальное созвездие из 88 IAU по границам
    Делпорта, с учётом эклиптической широты.

    Возвращает также band — что дала бы та же долгота при выброшенной широте
    (β=0). Расхождение real≠band и есть цена плоского неба: Сатурн 172 суток
    2026 года стоит в Ките, а плоская проекция все эти дни звала его Рыбами.

    Нет 3D-модуля или сбой → все поля None + честный note (NO DUMMIES).
    """
    if _RS is None:
        return {"con": None, "con_code": None, "con_lat": None, "con_glyph": None,
                "zodiacal": None, "con_band": None, "con_band_code": None,
                "beta_matters": None, "note": f"🔵 3D-слой недоступен: {_RS_ERR}"}
    try:
        real = _RS.constellation_of_ecliptic(lon, lat, jd)
        band = _RS.constellation_of_ecliptic(lon, 0.0, jd)
        if "error" in real:
            return {"con": None, "con_code": None, "con_lat": None, "con_glyph": None,
                    "zodiacal": None, "con_band": None, "con_band_code": None,
                    "beta_matters": None, "note": "🔵 3D-слой: " + str(real["error"])}
        code = real.get("code")
        bcode = band.get("code") if isinstance(band, dict) else None
        return {"con": real.get("name"), "con_code": code,
                "con_lat": real.get("name_lat"),
                "con_glyph": CON_GLYPH.get(code, CON_GLYPH_OTHER) if code else None,
                "zodiacal": real.get("zodiacal"),
                "con_band": band.get("name") if isinstance(band, dict) else None,
                "con_band_code": bcode,
                # True = широта увела тело в другое созвездие; плоское небо здесь врёт
                "beta_matters": (None if (code is None or bcode is None) else code != bcode),
                "note": real.get("note")}
    except Exception as e:                       # noqa: BLE001 — защита слоя
        return {"con": None, "con_code": None, "con_lat": None, "con_glyph": None,
                "zodiacal": None, "con_band": None, "con_band_code": None,
                "beta_matters": None, "note": f"🔵 3D-слой упал: {type(e).__name__}: {e}"}


def _unit_ecl(lon: float, lat: float) -> tuple[float, float, float]:
    """Единичный вектор из эклиптических (λ, β) — для истинных углов на сфере."""
    la, be = math.radians(lon), math.radians(lat)
    cb = math.cos(be)
    return cb * math.cos(la), cb * math.sin(la), math.sin(be)


def _sep3d(lon_a: float, lat_a: float, lon_b: float, lat_b: float) -> float:
    """🔵 ИСТИННЫЙ угол между двумя телами на сфере (градусы), с широтами.

    Скалярное произведение единичных векторов — та же формула, что уже принята
    аудитом в aether_deep.scalar_stress; вторую не изобретаем. Угол берётся через
    atan2(|a×b|, a·b), а не через acos: у малых углов acos теряет знаки после
    запятой (acos(1−ε) шумит на ~1e-6°), а здесь точность держится до нуля.
    """
    ax, ay, az = _unit_ecl(lon_a, lat_a)
    bx, by, bz = _unit_ecl(lon_b, lat_b)
    dot = ax * bx + ay * by + az * bz
    cx, cy, cz = ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
    return math.degrees(math.atan2(math.sqrt(cx * cx + cy * cy + cz * cz), dot))


def _arcs_from_houses(hreal: dict) -> list:
    """Дуги реальных созвездий вдоль эклиптики из результата houses_real:
    [(lon_start, lon_end, code, name, номер дома), ...]. Пусто при сбое."""
    try:
        return [(float(h["lon_start_deg"]), float(h["lon_end_deg"]),
                 h["code"], h["name"], int(h["house"]))
                for h in (hreal or {}).get("houses", [])]
    except Exception:                            # noqa: BLE001
        return []


def _in_arc(lon: float, s: float, e: float) -> bool:
    w = (e - s) % 360.0 or 360.0
    return ((lon - s) % 360.0) < w


def _deg_in_con(lon: float, arcs: list) -> tuple:
    """Градус тела ВНУТРИ дуги его созвездия + номер реального дома.

    🔵 Измеримая величина: сколько градусов эклиптики тело прошло от границы
    Делпорта. Дуги считаны при β=0, поэтому величина честна только для тела,
    чьё реальное созвездие совпало с поясным. Иначе — (None, дом) и в тексте
    печатается абсолютная λ, а не подогнанное число.
    """
    for s, e, code, name, hno in arcs:
        if _in_arc(lon, s, e):
            return (lon - s) % 360.0, code, name, hno
    return None, None, None, None


def _speed(sf, key, t, dt_obj: datetime) -> float:
    # центральная разность ±12 часов → °/сутки
    a = _lon_at(sf, key, sf["ts"].utc((dt_obj - timedelta(hours=12)).year,
        (dt_obj - timedelta(hours=12)).month, (dt_obj - timedelta(hours=12)).day,
        (dt_obj - timedelta(hours=12)).hour, (dt_obj - timedelta(hours=12)).minute))
    b = _lon_at(sf, key, sf["ts"].utc((dt_obj + timedelta(hours=12)).year,
        (dt_obj + timedelta(hours=12)).month, (dt_obj + timedelta(hours=12)).day,
        (dt_obj + timedelta(hours=12)).hour, (dt_obj + timedelta(hours=12)).minute))
    d = (b - a + 540) % 360 - 180
    return d  # °/сутки


def _sign_proj(lon: float):
    """🟡 ПРОЕКЦИЯ НА 30°-СЕТКУ, А НЕ ОТВЕТ «ГДЕ ТЕЛО».

    Двенадцать равных ломтей по 30° от точки весеннего равноденствия. Из-за
    прецессии сетка отстала от реального неба примерно на 25°: тело с λ=131°
    сетка зовёт «Лев», а стоит оно в Раке. Настоящий ответ даёт _sky_of().
    Функция сохранена: на ней держатся стихии, модальности, равные дома и
    совместимость со старым фронтом.
    """
    i = int(lon // 30) % 12
    return i, lon % 30


_sign_of = _sign_proj   # старое имя — чтобы ничего не отвалилось молча


def _fmt_dms(deg_in_sign: float) -> str:
    d = int(deg_in_sign)
    m = int(round((deg_in_sign - d) * 60))
    if m == 60:
        d += 1; m = 0
    return f"{d}°{m:02d}′"


def _mean_obliquity(jd: float) -> float:
    T = (jd - 2451545.0) / 36525.0
    return 23.439291 - 0.0130042 * T - 1.64e-7 * T * T + 5.04e-7 * T ** 3


def _houses_real_block(jd: float, lat: float, lon_east: float, planets: list) -> dict | None:
    """🔵 Дома по РЕАЛЬНЫМ границам созвездий вдоль эклиптики (их 13, неравные).

    Асцендент — точка эклиптики, поэтому β=0 у него по построению, и реальное
    созвездие асцендента — честный ответ, а не проекция. Каждой планете тут же
    проставляется house_real и градус внутри дуги её созвездия.
    Сбой/нет модуля → None, и разбор продолжает жить на равных домах.
    """
    if _RS is None:
        return None
    try:
        hr = _RS.houses_real(jd, lat, lon_east)
        if not isinstance(hr, dict) or "error" in hr or not hr.get("houses"):
            return None
        arcs = _arcs_from_houses(hr)
        for p in planets:
            dg, code, name, hno = _deg_in_con(p["lon"], arcs)
            p["house_real"] = hno
            # градус внутри дуги честен только если широта не увела тело прочь
            p["con_deg_num"] = (round(dg, 2) if (dg is not None and code == p.get("con_code"))
                                else None)
            p["con_deg"] = (_fmt_dms(dg) if p["con_deg_num"] is not None else None)
            if p["con_deg_num"] is None:
                p["con_deg_note"] = ("🔵 градус внутри дуги не определён: широта увела тело "
                                     "из поясного созвездия — печатается абсолютная λ")
        hr["note"] = ("🔵 дома по реальным границам созвездий (Делпорт): их 13 и они "
                      "неравные — асцендент проходит их рывками, а не по 30°")
        return hr
    except Exception:                            # noqa: BLE001 — защита слоя
        return None


def _houses(sf, t, dt_obj: datetime, lat: float, lon_east: float, planets: list) -> dict:
    """Asc/MC + дома по знакам (whole-sign) + расстановка планет по домам."""
    try:
        gmst_h = t.gmst  # часы
    except Exception:
        gmst_h = (18.697374558 + 24.06570982441908 * (t.tt - 2451545.0)) % 24
    ramc = (gmst_h * 15.0 + lon_east) % 360.0
    eps = math.radians(_mean_obliquity(t.tt if hasattr(t, "tt") else _jd(dt_obj)))
    th = math.radians(ramc)
    phi = math.radians(lat)
    mc = math.degrees(math.atan2(math.sin(th), math.cos(th) * math.cos(eps))) % 360
    asc = math.degrees(math.atan2(math.cos(th),
          -(math.sin(th) * math.cos(eps) + math.tan(phi) * math.sin(eps)))) % 360
    asc_sign = int(asc // 30) % 12
    cusps = [{"house": i + 1, "sign": SIGNS[(asc_sign + i) % 12],
              "glyph": SIGN_GLYPH[(asc_sign + i) % 12]} for i in range(12)]
    placements = {}
    for p in planets:
        h = ((int(p["lon"] // 30) % 12) - asc_sign) % 12 + 1
        p["house"] = h
        placements.setdefault(h, []).append(p["name"])
    asc_i, asc_d = _sign_proj(asc)
    mc_i, mc_d = _sign_proj(mc)
    out = {"system": "Whole Sign", "lat": lat, "lon": lon_east,
           "asc": round(asc, 2), "asc_sign": SIGNS[asc_i], "asc_glyph": SIGN_GLYPH[asc_i],
           "asc_deg": _fmt_dms(asc_d),
           "mc": round(mc, 2), "mc_sign": SIGNS[mc_i], "mc_glyph": SIGN_GLYPH[mc_i],
           "mc_deg": _fmt_dms(mc_d), "cusps": cusps, "placements": placements}
    # ── 🟡 то же самое, но честно названное проекцией (для новых потребителей) ──
    out["equal"] = {"system": "Whole Sign 30°", "cusps": cusps, "placements": placements,
                    "asc_sign": SIGNS[asc_i], "mc_sign": SIGNS[mc_i],
                    "note": "🟡 равные 30° дома от знака асцендента — символьная "
                            "проекция, не реальные границы неба"}
    out["equal_note"] = out["equal"]["note"]
    # ── 🔵 настоящие дома: реальные границы созвездий ──
    jd = _jd_utc(dt_obj)
    hr = _houses_real_block(jd, lat, lon_east, planets)
    out["real"] = hr
    if hr:
        out["asc_con"] = hr.get("asc_constellation")
        out["n_houses_real"] = hr.get("n_houses")
        out["system_real"] = "88 IAU / границы Делпорта (13 неравных домов)"
    else:
        out["asc_con"] = None
        out["real_note"] = ("🔵 реальные дома недоступны: "
                            + (_RS_ERR or "houses_real вернул ошибку"))
    # реальное созвездие MC (MC тоже лежит на эклиптике, β=0 — проекции нет)
    sky_mc = _sky_of(mc, 0.0, jd)
    out["mc_con"] = sky_mc.get("con")
    out["mc_con_code"] = sky_mc.get("con_code")
    out["drift_deg"] = _drift(jd)
    return out


def _aspects(planets: list) -> list:
    out = []
    by = {p["name"]: p for p in planets}
    names = [p["name"] for p in planets]
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = by[names[i]], by[names[j]]
            sep = abs((a["lon"] - b["lon"] + 180) % 360 - 180)
            for ang, nm, orb_l, orb_p, typ in ASPECTS:
                orb = orb_l if (a["name"] in LUMINARIES or b["name"] in LUMINARIES) else orb_p
                diff = abs(sep - ang)
                if diff <= orb:
                    # сходящийся (applying) = через сутки орб меньше: аспект набирает силу
                    la = (a["lon"] + a["speed"]) % 360
                    lb = (b["lon"] + b["speed"]) % 360
                    sep_next = abs((la - lb + 180) % 360 - 180)
                    applying = abs(sep_next - ang) < diff - 1e-9
                    rec = {"a": a["name"], "b": b["name"], "aspect": nm,
                           "angle": ang, "orb": round(diff, 2), "type": typ,
                           "exact": diff <= 1.0, "applying": applying,
                           "glyph_a": PLANET_GLYPH.get(a["name"], ""),
                           "glyph_b": PLANET_GLYPH.get(b["name"], "")}
                    # 🔵 ДОПОЛНИТЕЛЬНОЕ поле: истинный угол на сфере, с широтами.
                    # Аспект по долготе — канон школы и остаётся нетронутым (orb,
                    # sep, applying). sep3d показывает, насколько тела разведены
                    # НА САМОМ ДЕЛЕ: в квадрате по долготе они могут быть разнесены
                    # на несколько лишних градусов из-за β. Не заменяет orb.
                    try:
                        la, lb = a.get("lat"), b.get("lat")
                        if la is not None and lb is not None:
                            s3 = _sep3d(a["lon"], la, b["lon"], lb)
                            rec["sep3d"] = round(s3, 2)
                            rec["sep_lon"] = round(sep, 2)
                            rec["orb3d"] = round(abs(s3 - ang), 2)
                            rec["d_beta"] = round(abs(la - lb), 2)
                        else:
                            rec["sep3d"] = None
                            rec["sep3d_note"] = "🔵 нет широт — истинный угол не считался"
                    except Exception:            # noqa: BLE001 — защита слоя
                        rec["sep3d"] = None
                        rec["sep3d_note"] = "🔵 истинный угол не посчитан"
                    out.append(rec)
                    break
    # сильные вперёд: точные, потом по орбу; напряжённые приоритетнее
    rank = {"напряж": 0, "нейтр": 1, "гарм": 2}
    out.sort(key=lambda x: (not x["exact"], x["orb"], rank.get(x["type"], 3)))
    return out


def _stations(sf, dt_obj: datetime, days: int = 130) -> list:
    """Ближайшие станции (ретро↔дирекция) Меркурий..Плутон — точки разворота."""
    res = []
    for nm in ["Меркурий", "Венера", "Марс", "Юпитер", "Сатурн", "Уран", "Нептун", "Плутон"]:
        key = sf["bodies"].get(nm)
        if not key:
            continue
        prev = None
        step = 2 if nm in ("Меркурий", "Венера", "Марс") else 5
        d0 = dt_obj
        found = None
        for k in range(0, days + 1, step):
            dd = d0 + timedelta(days=k)
            sp = _speed(sf, key, sf["ts"].utc(dd.year, dd.month, dd.day), dd)
            if prev is not None and (prev[1] < 0) != (sp < 0):
                # станция между prev и dd — уточним бисекцией
                lo, hi = prev[0], dd
                for _ in range(8):
                    mid = lo + (hi - lo) / 2
                    spm = _speed(sf, key, sf["ts"].utc(mid.year, mid.month, mid.day), mid)
                    if (spm < 0) == (prev[1] < 0):
                        lo = mid
                    else:
                        hi = mid
                turn = "ретро" if sp < 0 else "дирекция"
                b_lat, lon, _d = _latlon_at(sf, key, sf["ts"].utc(mid.year, mid.month, mid.day))
                si, sd = _sign_proj(lon)
                sky = _sky_of(lon, b_lat, _jd_utc(mid))   # 🔵 где станция реально
                found = {"planet": nm, "glyph": PLANET_GLYPH.get(nm, ""),
                         "type": "станция → " + turn, "date": mid.date().isoformat(),
                         "in_days": (mid.date() - dt_obj.date()).days,
                         "sign": SIGNS[si], "deg": _fmt_dms(sd),
                         "lat": round(b_lat, 4), "con": sky.get("con"),
                         "con_glyph": sky.get("con_glyph")}
                break
            prev = (dd, sp)
        if found:
            res.append(found)
    res.sort(key=lambda x: x["in_days"])
    return res


def _moon_ingress(sf, dt_obj: datetime, days: int = 12) -> tuple[list, list]:
    """Ингрессии Луны — краткосрочные развороты толпы. ДВА ЧЕСТНО РАЗНЫХ списка:

    grid 🟡 — пересечение 30°-границы символьной сетки. Это НЕ вход в созвездие:
              граница сетки не совпадает ни с одной границей Делпорта.
    real 🔵 — вход Луны в РЕАЛЬНОЕ созвездие, с учётом широты (β до ±5.3°).
              Именно поэтому Луна может уйти в Кита или Змееносца, которых в
              двенадцатичастной сетке нет вообще.
    """
    key = sf["bodies"].get("Луна")
    if not key:
        return [], []
    grid_out, real_out = [], []
    step_h = 6
    prev_sign = prev_code = prev_dt = None
    n = int(days * 24 / step_h)

    def sample(d):
        b_lat, lon, _dd = _latlon_at(sf, key, sf["ts"].utc(d.year, d.month, d.day, d.hour, d.minute))
        return lon, b_lat

    for k in range(n + 1):
        dd = dt_obj + timedelta(hours=k * step_h)
        lon, b_lat = sample(dd)
        s = int(lon // 30) % 12
        code = _sky_of(lon, b_lat, _jd_utc(dd)).get("con_code")
        # ── 🟡 смена ломтя 30°-сетки ──
        if prev_sign is not None and s != prev_sign:
            lo, hi = prev_dt, dd
            for _ in range(7):
                mid = lo + (hi - lo) / 2
                lm, _bl = sample(mid)
                if int(lm // 30) % 12 == prev_sign:
                    lo = mid
                else:
                    hi = mid
            grid_out.append({"into": SIGNS[s], "glyph": SIGN_GLYPH[s],
                             "when": mid.strftime("%Y-%m-%d %H:%M"),
                             "in_hours": round((mid - dt_obj).total_seconds() / 3600, 1),
                             "kind": "🟡 пересечение 30°-границы сетки, "
                                     "к границам созвездий отношения не имеет"})
        # ── 🔵 вход в реальное созвездие ──
        if prev_code is not None and code is not None and code != prev_code:
            lo, hi = prev_dt, dd
            for _ in range(8):
                mid = lo + (hi - lo) / 2
                lm, bm = sample(mid)
                if _sky_of(lm, bm, _jd_utc(mid)).get("con_code") == prev_code:
                    lo = mid
                else:
                    hi = mid
            nm = _sky_of(*sample(hi)[:2], _jd_utc(hi))
            real_out.append({"into": nm.get("con"), "glyph": nm.get("con_glyph"),
                             "code": nm.get("con_code"), "zodiacal": nm.get("zodiacal"),
                             "when": mid.strftime("%Y-%m-%d %H:%M"),
                             "in_hours": round((mid - dt_obj).total_seconds() / 3600, 1),
                             "kind": "🔵 вход в реальное созвездие (границы Делпорта, с широтой)"})
        prev_sign, prev_code, prev_dt = s, code, dd
    return grid_out[:6], real_out[:6]


def _lunations(sf, dt_obj: datetime, days: int = 32) -> list:
    """Ближайшие новолуние/полнолуние — узлы настроения."""
    ks, km = sf["bodies"].get("Солнце"), sf["bodies"].get("Луна")
    if not (ks and km):
        return []
    out = []
    prev = None
    for k in range(0, days * 4 + 1):
        dd = dt_obj + timedelta(hours=k * 6)
        el = (_lon_at(sf, km, sf["ts"].utc(dd.year, dd.month, dd.day, dd.hour))
              - _lon_at(sf, ks, sf["ts"].utc(dd.year, dd.month, dd.day, dd.hour))) % 360
        for target, label in ((0, "Новолуние"), (180, "Полнолуние")):
            cur = (el - target + 180) % 360 - 180
            if prev is not None and prev[1].get(target) is not None:
                p = prev[1][target]
                if (p < 0) != (cur < 0) and abs(cur - p) < 180:
                    lo, hi = prev[0], dd
                    for _ in range(8):
                        mid = lo + (hi - lo) / 2
                        elm = (_lon_at(sf, km, sf["ts"].utc(mid.year, mid.month, mid.day, mid.hour, mid.minute))
                               - _lon_at(sf, ks, sf["ts"].utc(mid.year, mid.month, mid.day, mid.hour, mid.minute))) % 360
                        cm = (elm - target + 180) % 360 - 180
                        if (cm < 0) == (p < 0):
                            lo = mid
                        else:
                            hi = mid
                    lon = _lon_at(sf, ks, sf["ts"].utc(mid.year, mid.month, mid.day, mid.hour))
                    lam = lon if target == 0 else (lon + 180) % 360
                    si, sd = _sign_proj(lam)
                    # лунация — точка эклиптики (β≈0 по построению): проекции нет
                    sky = _sky_of(lam, 0.0, _jd_utc(mid))
                    out.append({"type": label, "when": mid.strftime("%Y-%m-%d %H:%M"),
                                "in_days": round((mid - dt_obj).total_seconds() / 86400, 1),
                                "sign": SIGNS[si], "deg": _fmt_dms(sd),
                                "con": sky.get("con"), "con_glyph": sky.get("con_glyph")})
            prev = prev or (dd, {})
        # сохранить текущее
        cur0 = (el - 0 + 180) % 360 - 180
        cur180 = (el - 180 + 180) % 360 - 180
        prev = (dd, {0: cur0, 180: cur180})
    out = [o for o in out if o["in_days"] >= 0]
    out.sort(key=lambda x: x["in_days"])
    seen = set(); uniq = []
    for o in out:
        k = (o["type"], round(o["in_days"]))
        if k in seen:
            continue
        seen.add(k); uniq.append(o)
    return uniq[:2]


# ══════════════════════════════════════════════════════════════════════
# ЗАТМЕНИЯ И ОКНА РЕТРО — ПО ЭФЕМЕРИДАМ, А НЕ ПО ТАБЛИЦЕ 2026
# Таблицы ECLIPSES/MERCURY_RX/OUTER_RX с 2027 года пустеют, эфемериды DE440
# доживают до 2650 года. Здесь то же самое считается по небу; таблица остаётся
# страховкой (сбой расчёта, lite-режим). Всё в try/except: сбой → пусто.
# ══════════════════════════════════════════════════════════════════════
_ECL_CACHE: dict = {}            # (дата, дней назад, дней вперёд) → список затмений
_ECL_CACHE_MAX = 64
SOLAR_ECLIPSE_BETA = 1.55        # |β| Луны в новолуние, ниже — затмение где-то на Земле есть, °
SOLAR_CENTRAL_BETA = 0.95        # |β| ниже — центральное (полное/кольцеобразное), иначе частное, °
_LUNAR_KIND = {0: "полутеневое", 1: "частное", 2: "полное"}   # коды eclipselib


def _ts_of(sf, d: datetime):
    """skyfield-время из datetime UTC (до минуты — как везде в модуле)."""
    return sf["ts"].utc(d.year, d.month, d.day, d.hour, d.minute)


def _eclipses_calc(sf, dt_obj: datetime, days_back: int = 10, days_fwd: int = 400) -> list:
    """Затмения по эфемеридам в окне [dt−days_back, dt+days_fwd], по времени.

    Лунные — skyfield.eclipselib.lunar_eclipses (Explanatory Supplement 11.2.3);
    код 0 полутеневое, 1 частное, 2 полное. Солнечные — по геоцентрической
    эклиптической широте Луны в момент новолуния (almanac.moon_phases, фаза 0):
    |β| < 1.55° → затмение где-то на Земле есть, |β| < 0.95° → центральное
    (полное или кольцеобразное; какое именно — здесь не различается), иначе
    частное. Это эклиптические пределы, а не видимость из конкретной точки.
    Элемент: {type, kind, date, when, in_days (<0 — прошедшее)}.
    Кэш по дате (модульный dict); любой сбой → [] — таблица подстрахует.
    """
    key = (dt_obj.date().isoformat(), int(days_back), int(days_fwd))
    hit = _ECL_CACHE.get(key)
    if hit is not None:
        return [dict(e) for e in hit]
    out = []
    try:
        from skyfield import almanac, eclipselib
        eph, earth = sf["eph"], sf["earth"]
        t0 = _ts_of(sf, dt_obj - timedelta(days=days_back))
        t1 = _ts_of(sf, dt_obj + timedelta(days=days_fwd))
        today = dt_obj.date()

        def rec(t, typ, kind):
            d = t.utc_datetime()
            return {"type": typ, "kind": kind, "date": d.date().isoformat(),
                    "when": d.strftime("%Y-%m-%d %H:%M"),
                    "in_days": (d.date() - today).days, "_tt": float(t.tt)}

        try:                                     # лунные
            lt, ly, _det = eclipselib.lunar_eclipses(t0, t1, eph)
            for ti, yi in zip(lt, ly):
                out.append(rec(ti, "лунное", _LUNAR_KIND.get(int(yi), "?")))
        except Exception:                        # noqa: BLE001 — слой в защите
            pass
        try:                                     # солнечные: новолуние + широта Луны
            pt, ph = almanac.find_discrete(t0, t1, almanac.moon_phases(eph))
            for ti, pi in zip(pt, ph):
                if int(pi) != 0:
                    continue
                lat_m, _lon, _d = earth.at(ti).observe(eph["moon"]).apparent().frame_latlon(sf["frame"])
                beta = abs(float(lat_m.degrees))
                if beta < SOLAR_ECLIPSE_BETA:
                    r = rec(ti, "солнечное",
                            "центральное" if beta < SOLAR_CENTRAL_BETA else "частное")
                    r["moon_beta"] = round(beta, 2)
                    out.append(r)
        except Exception:                        # noqa: BLE001
            pass
        out.sort(key=lambda e: e["_tt"])
        for e in out:
            e.pop("_tt", None)
    except Exception:                            # noqa: BLE001 — сбой → пусто
        out = []
    if out:                                      # пустой результат не кэшируем: сбой мог быть временным
        if len(_ECL_CACHE) >= _ECL_CACHE_MAX:
            _ECL_CACHE.clear()
        _ECL_CACHE[key] = [dict(e) for e in out]
    return out


def _eclipse_label(e: dict) -> str:
    return f"{e.get('type', '?')} затмение ({e.get('kind', '?')})"


def _eclipse_block_calc(calc: list) -> dict:
    """Тот же формат, что у _eclipse_block (eclipse_next/eclipse_recent/near_eclipse),
    но из списка _eclipses_calc; плюс сам список — eclipses_calc (до 6)."""
    def row(e):
        return {"label": _eclipse_label(e), "date": e["date"],
                "type": e.get("type"), "kind": e.get("kind"), "when": e.get("when")}
    nxt = [dict(row(e), in_days=e["in_days"]) for e in calc if e["in_days"] >= 0]
    nxt.sort(key=lambda x: x["in_days"])
    rec = [dict(row(e), days_ago=-e["in_days"]) for e in calc if -5 <= e["in_days"] < 0]
    return {"eclipse_next": nxt[:2], "eclipse_recent": rec,
            "near_eclipse": bool(nxt and nxt[0]["in_days"] <= 5) or bool(rec),
            "eclipses_calc": calc[:6]}


def _retro_windows(sf, dt_obj: datetime, planet: str = "Меркурий",
                   back: int = 45, fwd: int = 220) -> tuple[list, list]:
    """Окна ретроградности планеты по измеренной скорости (эфемериды).

    Скорость по долготе (_speed, центральная разность ±12 ч) снимается с шагом
    2 дня (Меркурий/Венера/Марс; 5 — внешние) от dt−back до dt+fwd; смена
    знака → бисекция (7 итераций, до часа). Возвращает (active, upcoming) в
    формате таблиц MERCURY_RX/_window_state:
      active   — [{label, since, until, days_left}] если планета ретро сейчас, иначе [];
      upcoming — до двух будущих окон [{label, start, in_days, until}].
    Граница окна вне диапазона расчёта → None, а не выдуманная дата.
    """
    key = sf["bodies"].get(planet)
    if not key:
        return [], []
    step = 2 if planet in ("Меркурий", "Венера", "Марс") else 5
    label = f"{planet} ретро"
    samples = []
    d = dt_obj - timedelta(days=back)
    end = dt_obj + timedelta(days=fwd)
    while d <= end:
        samples.append((d, _speed(sf, key, None, d)))
        d += timedelta(days=step)
    stations = []                                # (момент, "ретро" | "дирекция")
    for (d0, s0), (d1, s1) in zip(samples, samples[1:]):
        if (s0 < 0) == (s1 < 0):
            continue
        lo, hi = d0, d1
        for _ in range(7):
            mid = lo + (hi - lo) / 2
            if (_speed(sf, key, None, mid) < 0) == (s0 < 0):
                lo = mid
            else:
                hi = mid
        stations.append((lo + (hi - lo) / 2, "ретро" if s1 < 0 else "дирекция"))
    today = dt_obj.date()
    before = [s for s in stations if s[0] <= dt_obj]
    after = [s for s in stations if s[0] > dt_obj]
    active = []
    if _speed(sf, key, None, dt_obj) < 0:        # та же мерка, что planets[].retro
        since = before[-1][0] if (before and before[-1][1] == "ретро") else None
        until = after[0][0] if (after and after[0][1] == "дирекция") else None
        active.append({"label": label,
                       "since": since.date().isoformat() if since else None,
                       "until": until.date().isoformat() if until else None,
                       "days_left": (until.date() - today).days if until else None})
    upcoming = []
    for i, (when, kind) in enumerate(after):
        if kind != "ретро":
            continue
        nxt = after[i + 1] if i + 1 < len(after) else None
        upcoming.append({"label": label, "start": when.date().isoformat(),
                         "in_days": (when.date() - today).days,
                         "until": nxt[0].date().isoformat() if (nxt and nxt[1] == "дирекция") else None})
        if len(upcoming) == 2:
            break
    return active, upcoming


def _ang(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def _figures(planets: list) -> list:
    """Аспектные фигуры/конфигурации (Стеллиум, Тау-квадрат, Большой трин,
    Большой крест, Йод) — несут больше веса, чем одиночный аспект."""
    import itertools
    P = [p for p in planets]
    L = {p["name"]: p["lon"] for p in P}
    G = {p["name"]: PLANET_GLYPH.get(p["name"], "") for p in P}
    names = [p["name"] for p in P]
    figs = []

    def hit(a, b, ang, orb):
        return abs(_ang(L[a], L[b]) - ang) <= orb

    def gl(seq):
        return "".join(G[x] for x in seq)

    # Стеллиум: цепочка ≥3 планет, соседи в пределах 8° (с переходом через 0°)
    sp = sorted(P, key=lambda x: x["lon"])
    n = len(sp)
    ext = sp + [dict(p, lon=p["lon"] + 360) for p in sp if p["lon"] < 24]
    visited = set()
    for k in range(n):
        if sp[k]["name"] in visited:
            continue
        chain = [ext[k]]
        visited.add(ext[k]["name"])
        j = k + 1
        while j < len(ext) and (ext[j]["lon"] - chain[-1]["lon"]) <= 8 \
                and ext[j]["name"] not in {c["name"] for c in chain}:
            chain.append(ext[j]); visited.add(ext[j]["name"]); j += 1
        if len(chain) >= 3:
            span = round(chain[-1]["lon"] - chain[0]["lon"])
            si = int((chain[0]["lon"] % 360) // 30) % 12
            figs.append({"type": "Стеллиум", "planets": [c["name"] for c in chain],
                         "glyphs": gl(c["name"] for c in chain),
                         "note": f"скопление {len(chain)} планет в пределах {span}° (30°-сетка: {SIGNS[si]} 🟡) — мощный фокус энергии/темы"})

    gc_sets = []
    # Большой крест: 4 планеты — 2 оппозиции + 4 квадрата
    for combo in itertools.combinations(names, 4):
        a, b, c, d = combo
        perms = [((a, b), (c, d)), ((a, c), (b, d)), ((a, d), (b, c))]
        for (o1, o2) in perms:
            if hit(o1[0], o1[1], 180, 7) and hit(o2[0], o2[1], 180, 7):
                sq = all(hit(x, y, 90, 7) for x in o1 for y in o2)
                if sq:
                    figs.append({"type": "Большой крест", "planets": list(combo),
                                 "glyphs": gl(combo),
                                 "note": "большое напряжение по всем 4 углам — кризис/перелом, требует разрядки"})
                    gc_sets.append(set(combo))
                    break

    # Тау-квадрат: оппозиция + третья в квадрате к обеим (не часть большого креста)
    for combo in itertools.combinations(names, 3):
        a, b, c = combo
        for opp in [(a, b, c), (a, c, b), (b, c, a)]:
            x, y, apex = opp
            if hit(x, y, 180, 7) and hit(apex, x, 90, 7) and hit(apex, y, 90, 7):
                if any(set(combo) <= s for s in gc_sets):
                    break
                figs.append({"type": "Тау-квадрат", "planets": [x, y, apex],
                             "apex": apex, "glyphs": gl([x, y, apex]),
                             "note": f"напряжение с разрядкой через {apex} ({PLANET_GLYPH.get(apex,'')}) — точка давления и действия"})
                break

    gt_sets = []
    # Большой трин: три планеты взаимно в трине
    for combo in itertools.combinations(names, 3):
        a, b, c = combo
        if hit(a, b, 120, 7) and hit(b, c, 120, 7) and hit(a, c, 120, 7):
            si = int(L[a] // 30) % 12
            figs.append({"type": "Большой трин", "planets": list(combo),
                         "glyphs": gl(combo), "element": ELEMENT[si],
                         "note": f"гармоничный треугольник (стихия {ELEMENT[si]} 🟡 — производная от 30°-сетки) — лёгкий поток, поддержка тренда, но инертность"})
            gt_sets.append(set(combo))

    # Йод (Перст судьбы): секстиль + два квинконса к вершине
    for combo in itertools.combinations(names, 3):
        a, b, c = combo
        for sx in [(a, b, c), (a, c, b), (b, c, a)]:
            x, y, apex = sx
            if hit(x, y, 60, 5) and hit(apex, x, 150, 3.5) and hit(apex, y, 150, 3.5):
                figs.append({"type": "Йод", "planets": [x, y, apex],
                             "apex": apex, "glyphs": gl([x, y, apex]),
                             "note": f"перст судьбы — вынужденный поворот/предназначение через {apex} ({PLANET_GLYPH.get(apex,'')})"})
                break

    order = {"Большой крест": 0, "Тау-квадрат": 1, "Йод": 2, "Большой трин": 3, "Стеллиум": 4}
    figs.sort(key=lambda f: order.get(f["type"], 9))
    return figs


def _royal_hits(planets: list, year: float = 2026.0) -> list:
    """🔵 Соединения с королевскими звёздами по ИСТИННОМУ углу на сфере.

    Было: орб по одной долготе, широта звезды не существовала. Из-за этого
    Фомальгаут (β −21.1°) и Альголь (β +22.4°) «соединялись» с телами, до которых
    по небу больше 20° — та же плоскость вместо сферы, что и в тропическом знаке.
    Теперь звезда имеет (λ, β), λ прецессируется к дате, а сепарация считается
    через единичные векторы. Долготный орб оставлен рядом как orb_lon —
    видно, насколько плоская мерка врала.
    """
    out = []
    dlam = PRECESSION_DEG_PER_YEAR * (float(year) - 2000.0)
    for p in planets:
        plat = p.get("lat")
        for star, (slon0, slat) in ROYAL_STARS.items():
            slon = (slon0 + dlam) % 360.0
            d_lon = abs((p["lon"] - slon + 180) % 360 - 180)
            if plat is None:                     # нет широты — честно только долгота
                if d_lon <= 1.5:
                    out.append({"planet": p["name"], "glyph": PLANET_GLYPH.get(p["name"], ""),
                                "star": star, "orb": round(d_lon, 2), "orb_lon": round(d_lon, 2),
                                "sep3d": None,
                                "note": "🔵 нет широты тела — истинный угол не проверен"})
                continue
            d3 = _sep3d(p["lon"], plat, slon, slat)
            if d3 <= 1.5:
                out.append({"planet": p["name"], "glyph": PLANET_GLYPH.get(p["name"], ""),
                            "star": star, "orb": round(d3, 2), "sep3d": round(d3, 2),
                            "orb_lon": round(d_lon, 2), "star_lat": slat,
                            "note": "🔵 истинный угол на сфере (с широтами), не проекция"})
    out.sort(key=lambda r: r["orb"])
    return out


def _background(planets: list, aspects: list) -> dict:
    el = {"Огонь": 0, "Земля": 0, "Воздух": 0, "Вода": 0}
    mo = {"Кардинал": 0, "Фикс": 0, "Мутабельн": 0}
    retro = []
    for p in planets:
        i = int(p["lon"] // 30) % 12
        el[ELEMENT[i]] += 1
        mo[MODALITY[i]] += 1
        if p.get("retro"):
            retro.append(p["name"])
    dom_el = max(el, key=el.get)
    dom_mo = max(mo, key=mo.get)
    hard = [a for a in aspects if a["type"] == "напряж"]
    soft = [a for a in aspects if a["type"] == "гарм"]
    tone = "напряжённый (сжатие/волатильность)" if len(hard) > len(soft) else \
           ("гармоничный (поддержка тренда)" if len(soft) > len(hard) else "смешанный")
    return {"elements": el, "modalities": mo, "dominant_element": dom_el,
            "dominant_modality": dom_mo, "retrograde": retro,
            "retrograde_count": len(retro), "hard": len(hard), "soft": len(soft),
            "tone": tone}


def _sky_block(jd: float, year: float, planets: list) -> dict:
    """🔵 Паспорт неба разбора: чем считано, на сколько отстала тропическая сетка,
    у скольких тел она называет НЕ ТО созвездие. Число расхождения — не украшение:
    оно объясняет, почему привычный «знак» больше не совпадает с ответом.
    """
    out = {"engine": "realsky3d (88 IAU, границы Делпорта B1875, apparent λ+β)",
           "drift_deg": None, "mismatch": None, "total": len(planets),
           "beta_moved": None, "offline": True,
           "frame": {"механика 🔵": "созвездие, λ, β, углы — считаны по DE440 и "
                                    "границам Делпорта, проверяемо независимо",
                     "язык школы 🟡": "30°-знаки, стихии, модальности, равные дома — "
                                      "символьная сетка, не небо",
                     "за человеком ⚫": "небо не предсказывает событий; это фильтр "
                                        "тайминга, не торговый сигнал"}}
    try:
        out["drift_deg"] = _drift(jd)
        mism = [p["name"] for p in planets
                if p.get("con") and p.get("sign") and p["con"] != p["sign"]]
        moved = [p["name"] for p in planets if p.get("beta_matters")]
        out["mismatch"] = len(mism)
        out["mismatch_names"] = mism
        out["beta_moved"] = len(moved)
        out["beta_moved_names"] = moved
        out["note"] = (f"🟡 тропическая сетка отстала от неба на "
                       f"{out['drift_deg']}° (прецессия): не то созвездие у "
                       f"{len(mism)} тел из {len(planets)}"
                       if out["drift_deg"] is not None else
                       "🔵 3D-слой недоступен — расхождение не измерено")
    except Exception as e:                       # noqa: BLE001 — защита слоя
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _native(o):
    """Рекурсивно приводит numpy-скаляры к нативным типам Python (для JSON)."""
    if isinstance(o, dict):
        return {k: _native(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_native(v) for v in o]
    if isinstance(o, bool):
        return o
    if hasattr(o, "item") and not isinstance(o, (str, bytes)):
        try:
            return o.item()
        except Exception:
            return o
    return o


def _precise_context(dt_obj: datetime, lat: float, lon_east: float) -> dict | None:
    sf = _load_sf()
    if not sf:
        return None
    try:
        t = sf["ts"].utc(dt_obj.year, dt_obj.month, dt_obj.day, dt_obj.hour, dt_obj.minute)
        jd = _jd_utc(dt_obj)
        year = dt_obj.year + (dt_obj.timetuple().tm_yday - 1) / 365.25
        planets = []
        for nm, key in sf["bodies"].items():
            lat_b, lon, dist = _latlon_at(sf, key, t)   # 🔵 широта больше не в мусор
            sp = _speed(sf, key, t, dt_obj)
            si, sd = _sign_proj(lon)
            p = {"name": nm, "glyph": PLANET_GLYPH.get(nm, ""),
                 "lon": lon,
                 # 🔵 НАСТОЯЩЕЕ 3D: эклиптическая широта и расстояние
                 "lat": round(lat_b, 4), "dist_au": round(dist, 6),
                 # 🟡 проекция на 30°-сетку — сохранена для старых потребителей
                 "sign": SIGNS[si], "sign_glyph": SIGN_GLYPH[si],
                 "deg": _fmt_dms(sd), "deg_num": round(sd, 2),
                 "element": ELEMENT[si], "speed": round(sp, 4),
                 "retro": sp < 0,
                 "motion": ("ретро" if sp < 0 else "директ"),
                 "role": PLANET_ROLE.get(nm, "")}
            # 🔵 ОСНОВНОЙ ОТВЕТ «ГДЕ ТЕЛО» — реальное созвездие из 88 с широтой
            p.update(_sky_of(lon, lat_b, jd))
            p["sign_note"] = ("🟡 sign — проекция на равную 30°-сетку от точки весны, "
                              "не созвездие; реальное положение в поле con")
            planets.append(p)
        order = list(sf["bodies"].keys())
        planets.sort(key=lambda p: order.index(p["name"]))
        # Луна — фаза по реальной элонгации
        sun = next((p for p in planets if p["name"] == "Солнце"), None)
        moonp = next((p for p in planets if p["name"] == "Луна"), None)
        moon = moon_phase(dt_obj)
        if sun and moonp:
            el = (moonp["lon"] - sun["lon"]) % 360
            frac = el / 360.0
            nm, em = _phase_name(frac)
            illum = (1 - math.cos(math.radians(el))) / 2
            moon.update({"fraction": round(frac, 3), "illumination": round(illum * 100),
                         "name": nm, "emoji": em, "waxing": el < 180,
                         "sign": moonp["sign"], "sign_glyph": moonp["sign_glyph"],
                         "deg": moonp["deg"], "speed": moonp["speed"],
                         "elongation": round(el, 1),
                         # 🔵 Луна — самое «широтное» из быстрых тел (β до ±5.3°):
                         # она регулярно уходит из поясного созвездия совсем прочь
                         "lat": moonp.get("lat"), "con": moonp.get("con"),
                         "con_code": moonp.get("con_code"),
                         "con_glyph": moonp.get("con_glyph"),
                         "con_band": moonp.get("con_band"),
                         "zodiacal": moonp.get("zodiacal"),
                         "beta_matters": moonp.get("beta_matters")})
            rel = (moonp["speed"] or 13.2) - (sun["speed"] or 0.986)
            if rel > 0:   # тайминг от реальной элонгации, не от средней фазы
                moon["days_to_full"] = round(((180 - el) % 360) / rel, 1)
                moon["days_to_new"] = round(((360 - el) % 360) / rel, 1)
        houses = _houses(sf, t, dt_obj, lat, lon_east, planets)
        asp = _aspects(planets)
        figures = _figures(planets)
        stations = _stations(sf, dt_obj)
        ingress, ingress_real = _moon_ingress(sf, dt_obj)
        lun = _lunations(sf, dt_obj)
        royal = _royal_hits(planets, year)
        bg = _background(planets, asp)
        sky = _sky_block(jd, year, planets)
        today = dt_obj.date()
        # ── затмения: по эфемеридам; таблица 2026 — только страховка при сбое.
        # 40 дней назад — чтобы в короткий рендер попало «последнее затмение».
        ecl = _eclipse_block(today)
        ecl["eclipses_calc"] = []
        calc = _eclipses_calc(sf, dt_obj, days_back=40)
        if calc:
            ecl = _eclipse_block_calc(calc)
        # ── обратная совместимость со старым фронтом/конвейером ──
        merc = next((p for p in planets if p["name"] == "Меркурий"), None)
        try:                                     # окна ретро Меркурия по эфемеридам
            merc_active, merc_next = _retro_windows(sf, dt_obj, "Меркурий")
        except Exception:                        # noqa: BLE001 — таблица подстрахует
            merc_active, merc_next = _window_state(today, MERCURY_RX)
        if merc and merc["retro"]:
            # 🔵 лейбл по реальному созвездию; тропическое имя ушло из подписи
            merc_where = merc.get("con") or f"{merc['sign']} 🟡"
            merc_active = merc_active or [{"label": f"Меркурий ретро ({merc_where})",
                                          "days_left": None}]
        # ── внешние планеты: «ретро сейчас» — измеренная скорость, конец окна —
        # ближайшая станция «→ дирекция» из уже посчитанных stations (или None);
        # будущие окна — станции «→ ретро». Таблица OUTER_RX больше не нужна.
        outer_active, outer_next = [], []
        for p in planets:
            if p["name"] in ("Солнце", "Луна", "Меркурий") or not p.get("retro"):
                continue
            st = next((s for s in stations if s["planet"] == p["name"]
                       and s["type"].endswith("дирекция")), None)
            outer_active.append({"label": f"{p['name']} ретро", "since": None,
                                 "until": st["date"] if st else None,
                                 "days_left": st["in_days"] if st else None})
        for s in stations:
            if s["type"].endswith("ретро") and s["planet"] != "Меркурий":
                outer_next.append({"label": f"{s['planet']} ретро",
                                   "start": s["date"], "in_days": s["in_days"]})
        return _native({"mode": "precise", "ts": dt_obj.isoformat(),
                "ephemeris": os.path.basename(sf["path"]),
                "sky": sky, "jd": jd,
                "planets": planets, "moon": moon, "houses": houses, "aspects": asp,
                "figures": figures,
                "stations": stations, "moon_ingress": ingress,
                "moon_ingress_real": ingress_real, "lunations": lun,
                "royal": royal, "background": bg,
                "mercury_retro": bool(merc and merc["retro"]),
                "mercury_active": merc_active, "mercury_next": merc_next[:1],
                "outer_active": outer_active, "outer_next": outer_next[:2],
                **ecl})
    except Exception as e:
        import logging
        logging.getLogger("pythia.astro").warning("precise failed: %s", str(e)[:160])
        return None


# ══════════════════════════════════════════════════════════════════════
# ПУБЛИЧНЫЙ API
# ══════════════════════════════════════════════════════════════════════

_CTX_CACHE: dict = {"ts": 0.0, "ctx": None}
_CTX_LOCK = threading.Lock()
ASTRO_CACHE_SEC = 600.0


def cached_context(max_age: float = ASTRO_CACHE_SEC) -> dict:
    """Кэшированная суточная карта (расчёт тяжёлый: секунды CPU).
    Потокобезопасно; обновляется не чаще max_age."""
    now = _time.time()
    with _CTX_LOCK:
        if _CTX_CACHE["ctx"] is not None and now - _CTX_CACHE["ts"] < max_age:
            return _CTX_CACHE["ctx"]
    ctx = current_context()
    with _CTX_LOCK:
        _CTX_CACHE["ts"] = _time.time()
        _CTX_CACHE["ctx"] = ctx
    return ctx


async def acontext(max_age: float = ASTRO_CACHE_SEC) -> dict:
    """Асинхронная обёртка: тяжёлый расчёт уводится в поток, event loop не блокируется."""
    return await asyncio.to_thread(cached_context, max_age)


def peek_context() -> tuple[dict | None, float | None]:
    """Кэш как есть, БЕЗ расчёта: (ctx | None, возраст в секундах | None). Для панели
    (`/api/v5/state`): протухший кэш отдаётся сразу, пересчёт — в фоне через acontext()."""
    with _CTX_LOCK:
        ctx = _CTX_CACHE["ctx"]
        age = (_time.time() - _CTX_CACHE["ts"]) if ctx is not None else None
    return ctx, age


def current_context(dt: datetime | None = None, lat: float | None = None,
                    lon: float | None = None) -> dict:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    lat = DEFAULT_LAT if lat is None else lat
    lon = DEFAULT_LON if lon is None else lon
    ctx = _precise_context(dt, lat, lon)
    if ctx:
        return ctx
    # ── lite fallback ──
    today = dt.date()
    moon = moon_phase(dt)
    merc_active, merc_next = _window_state(today, MERCURY_RX)
    outer_active, outer_next = _window_state(today, OUTER_RX)
    ecl = _eclipse_block(today)
    return {"mode": "lite", "ts": dt.isoformat(), "moon": moon,
            "mercury_retro": bool(merc_active), "mercury_active": merc_active,
            "mercury_next": merc_next[:1], "outer_active": outer_active,
            "outer_next": outer_next[:2], "eclipses_calc": [], **ecl}


def render_for_ai(ctx: dict) -> str:
    if ctx.get("mode") == "precise":
        return _render_precise(ctx)
    return _render_lite(ctx)


def _render_lite(ctx: dict) -> str:
    """Небо без эфемерид: только факты, что есть (Луна по формуле, окна из
    таблиц), без директив; толкование школы — отдельным абзацем (GANN_GLOSSARY)."""
    m = ctx.get("moon") or {}
    L = ["НЕБО, упрощённый режим — нет эфемерид: только фаза Луны по формуле и таблицы окон "
         "(без положений тел, аспектов и станций)."]
    if m:
        bits = [str(m.get("name") or "?").lower()]
        if m.get("illumination") is not None:
            bits.append(f"освещённость {m['illumination']}%")
        if m.get("waxing") is not None:
            bits.append("растёт" if m["waxing"] else "убывает")
        if m.get("days_to_full") is not None:
            bits.append(f"до полнолуния {m['days_to_full']} дн")
        if m.get("days_to_new") is not None:
            bits.append(f"до новолуния {m['days_to_new']} дн")
        L.append("Луна: " + ", ".join(bits) + ".")
    if ctx.get("mercury_active"):
        a = ctx["mercury_active"][0]
        s_ = "Меркурий: ретро"
        if a.get("since") or a.get("until"):
            s_ += f" с {a.get('since') or '?'} по {a.get('until') or '?'}"
        if a.get("days_left") is not None:
            s_ += f", осталось {a['days_left']} дн"
        L.append(s_ + ".")
    elif ctx.get("mercury_next"):
        n = ctx["mercury_next"][0]
        L.append(f"Меркурий: директ; ретро с {n.get('start') or '?'} (через {n.get('in_days', '?')} дн).")
    outer = [_clean_label(str(a.get("label") or "")) for a in ctx.get("outer_active") or []]
    if outer:
        L.append("Ретро внешних планет (по таблице): " + "; ".join(x for x in outer if x) + ".")
    nxt = [f"{_clean_label(str(n.get('label') or ''))} через {n.get('in_days', '?')} дн"
           for n in ctx.get("outer_next") or []]
    if nxt:
        L.append("Станции впереди (по таблице): " + "; ".join(nxt) + ".")
    ecl = []
    for e in ctx.get("eclipse_recent") or []:
        ecl.append(f"недавнее {e.get('date') or '?'} {_clean_label(str(e.get('label') or ''))}, "
                   f"{e.get('days_ago', '?')} дн назад")
    for i, e in enumerate((ctx.get("eclipse_next") or [])[:2]):
        ecl.append(f"{'ближайшее' if i == 0 else 'затем'} {e.get('date') or '?'} "
                   f"{_clean_label(str(e.get('label') or ''))}, через {e.get('in_days', '?')} дн")
    if ecl:
        L.append("Затмения: " + "; ".join(ecl) + ".")
    L += ["", GANN_GLOSSARY]
    return "\n".join(L)


def _body_line(p: dict) -> str:
    """Одна строка тела для ИИ. Порядок жёсткий: сначала РЕАЛЬНОЕ созвездие,
    потом широта, и только потом — тропический ломоть с меткой 🟡."""
    g = p.get("con_glyph") or ""
    con = p.get("con")
    lat = p.get("lat")
    # где тело реально стоит
    if con:
        deg = p.get("con_deg_num")
        if deg is not None:
            where = f"{g} {con.upper()} {deg:.1f}° (реально)"
        else:                     # широта увела из поясного созвездия — честная λ
            where = f"{g} {con.upper()} (реально, λ{p['lon']:.2f}°)"
    else:
        where = f"λ{p['lon']:.2f}° (созвездие не определено: {p.get('note') or '3D-слой молчит'})"
    bits = [f"{p['glyph']} {p['name']:8s}", where]
    bits.append(f"β{lat:+.2f}°" if lat is not None else "β н/д")
    bits.append(f"тропич. {p['sign']} {p.get('deg_num', 0):.1f}° 🟡")
    bits.append(f"дом {p.get('house_real') or p.get('house', '?')}")
    bits.append(p["motion"] + (" ⟲РЕТРО" if p["retro"] else ""))
    line = "  " + " · ".join(bits)
    marks = []
    if p.get("beta_matters"):
        marks.append(f"β увела из «{p.get('con_band')}» — плоское небо здесь врёт")
    if p.get("zodiacal") is False:
        marks.append("созвездие ВНЕ 12-знаковой полосы")
    if con and p.get("sign") and con != p["sign"]:
        marks.append(f"сетка называет «{p['sign']}» — не то созвездие")
    if marks:
        line += "\n      ⚠ " + "; ".join(marks)
    line += f"\n      роль: {p['role']}"
    return line


def _render_precise(ctx: dict) -> str:
    m = ctx["moon"]; h = ctx["houses"]; bg = ctx["background"]
    sky = ctx.get("sky") or {}
    drift = sky.get("drift_deg")
    L = [f"СУТОЧНАЯ КАРТА (метод Ганна, реальные эфемериды {ctx['ephemeris']}, "
         f"мунданная карта Москва/MOEX, {ctx['ts'][:16]} UTC)",
         "",
         "═══ РАМКА (читай прежде чисел) ═══",
         "🔵 МЕХАНИКА — положение тел: видимые (apparent) эклиптические ДОЛГОТА И ШИРОТА "
         "по DE440 и РЕАЛЬНОЕ созвездие из 88 IAU по границам Делпорта. Это проверяемо.",
         "🟡 ЯЗЫК ШКОЛЫ — тридцатиградусные «знаки», стихии, модальности, равные дома: "
         "символьная сетка, а НЕ небо. Помечены 🟡 и стоят ПОСЛЕ реального ответа.",
         "⚫ ЗА ЧЕЛОВЕКОМ — небо не предсказывает событий. Это фильтр тайминга, "
         "не торговый сигнал; решение и риск на операторе."]
    if drift is not None:
        L.append(f"⚠ ТРОПИЧЕСКАЯ СЕТКА ОТСТАЛА ОТ НЕБА НА {drift:.1f}° (прецессия) — "
                 f"В ЭТОМ РАЗБОРЕ НЕБО НАСТОЯЩЕЕ. Не то созвездие сетка называет у "
                 f"{sky.get('mismatch', '?')} тел из {sky.get('total', '?')}. "
                 f"Если в тексте встретишь «знак» — это подпись 🟡, а не ответ на "
                 f"вопрос «где тело».")
    else:
        L.append("⚠ 3D-слой недоступен — расхождение сетки с небом не измерено: "
                 + str(sky.get("note") or "нет данных"))
    L += ["",
          "ПОЛОЖЕНИЕ ТЕЛ 🔵 (реальное созвездие IAU по границам Делпорта, apparent λ/β; "
          "тропический знак — подпись 🟡, не ответ):",
          "  Как читать строку: градус после имени созвездия — сколько градусов эклиптики "
          "тело прошло ОТ ГРАНИЦЫ ДЕЛПОРТА, а не от начала 30°-ломтя. Созвездия неравные, "
          "поэтому он может быть и 36° (Телец), и всего 6° (Скорпион). Где широта увела тело "
          "из поясного созвездия, градуса нет — печатается абсолютная λ, число не подгоняется. "
          "β — эклиптическая широта: она и есть третье измерение, из-за неё тело попадает "
          "в созвездие, которого в 12-знаковой полосе нет вовсе (Кит, Змееносец, Орион)."]
    for p in ctx["planets"]:
        L.append(_body_line(p))
    if sky.get("beta_moved"):
        L.append(f"  ⚠ У {sky['beta_moved']} тел ({', '.join(sky.get('beta_moved_names', []))}) "
                 f"эклиптическая широта увела их в ДРУГОЕ созвездие, чем даёт плоская "
                 f"проекция на пояс. Широта здесь не украшение — она меняет ответ.")
    mcon = m.get("con")
    m_where = (f"{m.get('con_glyph') or ''} {mcon.upper()} (реально)" if mcon
               else "созвездие не определено")
    m_beta = f"β{m['lat']:+.2f}°" if m.get("lat") is not None else "β н/д"
    L += ["",
          f"ЛУНА: {m['emoji']} {m['name']} · {m_where} · {m_beta} · "
          f"тропич. {m.get('sign')} {m.get('deg')} 🟡, освещ. {m['illumination']}%, "
          f"{'растущая' if m['waxing'] else 'убывающая'} (элонгация {m.get('elongation','?')}°). "
          f"Скорость {m.get('speed','?')}°/сут — главный краткосрочный триггер разворотов толпы."]
    if m.get("beta_matters"):
        L.append(f"  ⚠ Луна широтой уведена из «{m.get('con_band')}»: β у Луны доходит до ±5.3°, "
                 f"поэтому она регулярно стоит вне двенадцатичастной полосы.")
    # ── углы карты: Asc/MC лежат на эклиптике, β=0 — проекции нет ──
    asc_con = h.get("asc_con")
    L += ["",
          f"УГЛЫ КАРТЫ 🔵: Asc λ{h['asc']}° — реально {asc_con or '?'} "
          f"(сетка назвала бы «{h['asc_sign']}» 🟡) · "
          f"MC λ{h['mc']}° — реально {h.get('mc_con') or '?'} "
          f"(сетка: «{h['mc_sign']}» 🟡)."]
    hr = h.get("real")
    if hr and hr.get("houses"):
        L.append(f"ДОМА ПО РЕАЛЬНЫМ ГРАНИЦАМ СОЗВЕЗДИЙ 🔵: их {hr.get('n_houses')}, "
                 f"а НЕ 12, и они неравные — асцендент идёт по ним рывками. "
                 f"Дом 1 = {hr['houses'][0]['name']} "
                 f"({hr['houses'][0]['width_deg']:.1f}° шириной).")
        wide = sorted(hr["houses"], key=lambda x: -x["width_deg"])[:3]
        narr = sorted(hr["houses"], key=lambda x: x["width_deg"])[:2]
        L.append("  самые широкие: " + ", ".join(f"{w['name']} {w['width_deg']:.1f}°" for w in wide)
                 + "; самые узкие: " + ", ".join(f"{w['name']} {w['width_deg']:.1f}°" for w in narr)
                 + " — вес дома по времени неодинаков, это механика, а не символ.")
        occ_r = {}
        for p in ctx["planets"]:
            if p.get("house_real"):
                occ_r.setdefault(p["house_real"], []).append(p["name"])
        if occ_r:
            names = {x["house"]: x["name"] for x in hr["houses"]}
            L.append("  РАССТАНОВКА ПО РЕАЛЬНЫМ ДОМАМ: "
                     + "; ".join(f"дом {k} [{names.get(k,'?')}]: {', '.join(v)}"
                                 for k, v in sorted(occ_r.items())) + ".")
    else:
        L.append("  (реальные дома недоступны — " + str(h.get("real_note") or "3D-слой молчит") + ")")
    # ── 🟡 равные дома: сохранены как проекция, не как ответ ──
    occ = []
    for hn in range(1, 13):
        pls = h["placements"].get(hn)
        if pls:
            occ.append(f"дом {hn} [{h['cusps'][hn-1]['glyph']}{h['cusps'][hn-1]['sign']}]: " + ", ".join(pls))
    if occ:
        L.append("🟡 РАВНЫЕ 30°-ДОМА (символьная проекция, вторична к реальным): "
                 + "; ".join(occ) + ".")
    # аспекты
    if ctx["aspects"]:
        L += ["", "КЛЮЧЕВЫЕ АСПЕКТЫ (углы Ганна по ДОЛГОТЕ — канон метода; точные = рабочие сейчас).",
              "  Рядом sep3d 🔵 — истинный угол между телами на сфере, с широтами: "
              "показывает, насколько тела разведены НА САМОМ ДЕЛЕ. Аспект считается по орбу, "
              "sep3d его не отменяет, но большой разрыв орб↔sep3d = аспект слабее, чем выглядит."]
        for a in ctx["aspects"][:10]:
            ex = " ⚡ТОЧНЫЙ" if a["exact"] else ""
            ap = " →сходится" if a.get("applying") else (" ←расходится" if a.get("applying") is False else "")
            s3 = a.get("sep3d")
            tail = ""
            if s3 is not None:
                tail = f" · sep3d {s3}° (по долготе {a.get('sep_lon')}°, Δβ {a.get('d_beta')}°)"
                if a.get("orb3d") is not None and a["orb3d"] - a["orb"] >= 1.0:
                    tail += " ⚠ по-настоящему дальше, чем по долготе"
            L.append(f"  {a['glyph_a']} {a['a']} {a['aspect']} {a['glyph_b']} {a['b']} "
                     f"(орб {a['orb']}°, {a['type']}){ex}{ap}{tail}")
    # фигуры/конфигурации
    if ctx.get("figures"):
        L += ["", "АСПЕКТНЫЕ ФИГУРЫ (конфигурации — вес выше одиночного аспекта):"]
        for f in ctx["figures"]:
            L.append(f"  ◆ {f['type']}: {f['glyphs']} {', '.join(f['planets'])} — {f['note']}")
    # фон
    L += ["",
          f"ОБЩИЙ ФОН 🟡 (производная от 30°-сетки, не механика): доминирует стихия "
          f"{bg['dominant_element']}, модальность {bg['dominant_modality']}; "
          f"напряжённых аспектов {bg['hard']} / гармоничных {bg['soft']} → тон {bg['tone']}. "
          + (f"Ретроградны: {', '.join(bg['retrograde'])} (ретро 🔵 — измеренная скорость по долготе)."
             if bg['retrograde'] else "Ретроградных планет нет.")]
    # развороты-турниры
    turns = []
    for s in ctx.get("stations", [])[:4]:
        where = f"{s.get('con') or '?'} 🔵" + (f" / сетка «{s['sign']}» 🟡" if s.get('sign') else "")
        turns.append(f"{s['glyph']}{s['planet']} {s['type']} {s['date']} "
                     f"(через {s['in_days']}д, {where})")
    for ln in ctx.get("lunations", []):
        where = f"{ln.get('con') or '?'} 🔵 / сетка «{ln['sign']}» 🟡"
        turns.append(f"{ln['type']} {ln['when']} ({where}, через {ln['in_days']}д)")
    if turns:
        L += ["", "РАЗВОРОТЫ / ТАЙМИНГ (точки смены тренда по Ганну):", "  " + "\n  ".join(turns)]
    ing_r = ctx.get("moon_ingress_real", [])
    if ing_r:
        L.append("  🔵 Луна входит в РЕАЛЬНЫЕ созвездия: " +
                 ", ".join(f"→{x.get('glyph') or ''}{x['into']} {x['when']}" for x in ing_r[:4]))
    ing = ctx.get("moon_ingress", [])
    if ing:
        L.append("  🟡 Пересечения 30°-границ сетки (это НЕ вход в созвездие, "
                 "границы сетки с небом не совпадают): " +
                 ", ".join(f"→{x['glyph']}{x['into']} {x['when']}" for x in ing[:4]))
    for e in ctx.get("eclipse_recent", []):
        L.append(f"  ⚠ Недавнее затмение ({e['days_ago']}д назад): {e['label']} — зона слома тренда ±неделя.")
    if ctx.get("eclipse_next"):
        e = ctx["eclipse_next"][0]
        tag = "⚠ " if e["in_days"] <= 7 else ""
        L.append(f"  {tag}Ближайшее затмение через {e['in_days']}д: {e['label']}.")
    if ctx.get("royal"):
        L.append("  Соединения с королевскими звёздами 🔵 (истинный угол на сфере, с широтами): " +
                 ", ".join(f"{r['glyph']}{r['planet']}–{r['star']} (угол {r['orb']}°)"
                           for r in ctx["royal"]))
    L += ["",
          "ПРИМЕНЕНИЕ: используй карту как фильтр тайминга и направления. Жёсткие аспекты "
          "(квадрат/оппозиция) и станции планет = повышенная вероятность разворота/слома. "
          "Ретро-Меркурий = ложные пробои, дезинформация. Движение Луны и её вход в реальные "
          "созвездия = краткосрочные качели толпы. Затмения = слом тренда ±неделя. "
          "Это фильтр, не главный сигнал.",
          "ЧЕСТНОСТЬ ОТВЕТА: на вопрос «где тело» отвечай РЕАЛЬНЫМ созвездием (🔵). "
          "Тридцатиградусный знак называй только как подпись и только со словом «тропический» "
          "или пометкой 🟡. Не выдавай сетку за небо: она отстала на "
          + (f"{drift:.1f}°." if drift is not None else "величину, которую здесь не измерили.")
          + " ⚫ Небо не предсказывает событий: ни катастроф, ни курсов, ни судьбы стран. "
          "Разбор — сценарии и тайминг, решение и риск на операторе. Не торговый сигнал."]
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════════════
# КОРОТКИЙ РЕНДЕР ФАКТОВ ДЛЯ ИИ — render_compact
# Только небо как факты (2–3.5 тыс. знаков): без рамок «как читать», без
# 30°-сетки и её меток, без директив «используй как фильтр». Толкование школы
# Ганна — отдельным абзацем «как это обычно читают», и ИИ решает сам.
# render_for_ai выше не тронут: у него свои потребители.
# ══════════════════════════════════════════════════════════════════════
GANN_GLOSSARY = (
    "Как это обычно читают в школе Ганна (конвенция, не измеренный закон; проверка "
    "проекта на истории силы у неба не нашла — как учитывать, решай сам):\n"
    "  квадрат, оппозиция, полуквадраты — напряжение, ждут разворота или ускорения; "
    "трин, секстиль — поддержка хода; соединение — сложение тем двух тел;\n"
    "  станция планеты — смена темпа, точка разворота во времени; ретро-Меркурий — "
    "путаница в новостях, ложные пробои, срыв сделок; ретро внешних планет — пересмотр темы;\n"
    "  Луна — краткосрочные качели толпы, новолуние/полнолуние и вход в новое созвездие — "
    "узлы настроения; затмения — слом тренда в окне ±неделя;\n"
    "  роли тел: Солнце — тренд; Луна — толпа и ликвидность; Меркурий — новости, связь; "
    "Венера — деньги, аппетит к риску; Марс — импульс, выносы; Юпитер — расширение, "
    "оптимизм; Сатурн — сжатие, страх, дно и потолок; Уран — шок, гэп; Нептун — иллюзия, "
    "нефть; Плутон — крупный капитал, кризис.")


def _fmt_speed(sp) -> str:
    """°/сутки: Луна (≥10°) — два знака, остальные — три; нет числа → «?»."""
    if sp is None:
        return "?"
    return f"{sp:+.2f}" if abs(sp) >= 10 else f"{sp:+.3f}"


def _turn_of(station_type) -> str:
    """«станция → ретро» → «ретро»."""
    return (str(station_type or "").split("→")[-1].strip()) or "?"


def _clean_label(lbl) -> str:
    """Подпись из резервной таблицы без тропической пометки «(тропич. … 🟡)»."""
    return re.sub(r"\s*\(тропич[^)]*\)", "", str(lbl or "")).replace("🟡", "").strip() or "?"


def _eclipse_words(e: dict) -> str:
    """«солнечное (центральное)» из расчётной записи, иначе очищенная подпись таблицы."""
    if e.get("type") and e.get("kind"):
        return f"{e['type']} ({e['kind']})"
    return _clean_label(e.get("label"))


def render_compact(ctx: dict, *, glossary: bool = True) -> str:
    """Небо для ИИ коротко и по фактам (целевой объём 2–3.5 тыс. знаков).

    Только измеренное из ctx: положение тел (реальное созвездие, λ, β, скорость,
    дом по реальным границам), Луна, углы, аспекты по долготе, фигуры, ретро,
    станции, Меркурий, лунации, входы Луны, затмения, королевские звёзды.
    Без 30°-сетки, без меток 🟡/🔵/⚫, без рамок и директив. glossary=True —
    отдельный абзац «как это обычно читают в школе Ганна»; решает ИИ сам.
    Нет поля в ctx → строка пропускается или «?», ничего не выдумывается.
    lite-режим (нет эфемерид) → _render_lite(ctx).
    """
    if not isinstance(ctx, dict) or ctx.get("mode") != "precise":
        return _render_lite(ctx)
    planets = ctx.get("planets") or []
    m = ctx.get("moon") or {}
    h = ctx.get("houses") or {}
    L = []
    # ── шапка: чем считано и на какое место ──
    ts = str(ctx.get("ts") or "?")[:16].replace("T", " ")
    lat, lon = h.get("lat"), h.get("lon")
    if lat is None or lon is None:
        where = "место карты не задано"
    elif abs(lat - DEFAULT_LAT) < 1e-3 and abs(lon - DEFAULT_LON) < 1e-3:
        where = "карта на Москву"
    else:
        where = f"карта на φ{lat:.2f}° λ{lon:.2f}°"
    L.append(f"НЕБО {ts} UTC — эфемериды {ctx.get('ephemeris') or '?'}, реальные созвездия "
             f"IAU (границы Делпорта, широта учтена), {where}.")
    # ── тела ──
    if planets:
        real_h = any(p.get("house_real") for p in planets)
        L.append("Тело · созвездие · λ° · β° · °/сут · дом "
                 + ("(по реальным границам созвездий)" if real_h else "(равные 30° дома)"))
        for p in planets:
            lam, b = p.get("lon"), p.get("lat")
            lam_s = f"λ{lam:6.2f}" if lam is not None else "λ?"
            beta_s = f"β{b:+.2f}" if b is not None else "β?"
            motion = p.get("motion") or ("ретро" if p.get("retro") else "директ")
            L.append(f"  {p.get('glyph', '')} {str(p.get('name') or '?'):<8s} "
                     f"{str(p.get('con') or '?'):<10s} {lam_s} {beta_s} "
                     f"{_fmt_speed(p.get('speed'))} {motion:<6s}  "
                     f"дом {p.get('house_real') or p.get('house') or '?'}")
    # ── Луна ──
    if m:
        nm = str(m.get("name") or "?")
        bits = [nm[:1].lower() + nm[1:]]
        if m.get("illumination") is not None:
            bits.append(f"освещённость {m['illumination']}%")
        if m.get("waxing") is not None:
            bits.append("растёт" if m["waxing"] else "убывает")
        if m.get("elongation") is not None:
            bits.append(f"элонгация {m['elongation']}°")
        if m.get("days_to_full") is not None:
            bits.append(f"до полнолуния {m['days_to_full']} дн")
        if m.get("days_to_new") is not None:
            bits.append(f"до новолуния {m['days_to_new']} дн")
        L.append("Луна: " + ", ".join(bits) + ".")
    # ── углы карты (Asc/MC лежат на эклиптике — созвездие без проекции) ──
    if h.get("asc") is not None and h.get("mc") is not None:
        L.append(f"Углы карты: Asc λ{h['asc']}° ({h.get('asc_con') or '?'}), "
                 f"MC λ{h['mc']}° ({h.get('mc_con') or '?'}).")
    # ── аспекты по долготе (канон школы), до 12 ──
    asp = ctx.get("aspects") or []
    if asp:
        items = []
        for a in asp[:12]:
            q = [f"орб {a.get('orb', '?')}°"]
            if a.get("exact"):
                q.append("точный")
            if a.get("applying") is True:
                q.append("сходится")
            elif a.get("applying") is False:
                q.append("расходится")
            items.append(f"{a.get('a', '?')} {str(a.get('aspect') or '?').lower()} "
                         f"{a.get('b', '?')} ({', '.join(q)})")
        L.append("Аспекты по долготе: " + "; ".join(items) + ".")
    else:
        L.append("Аспекты по долготе: в орбах нет.")
    # ── фигуры ──
    figs = ctx.get("figures") or []
    if figs:
        L.append("Фигуры: " + "; ".join(
            f"{f.get('type') or '?'}: {', '.join(f.get('planets') or []) or '?'}"
            for f in figs) + ".")
    # ── ретроградные ──
    retro = (ctx.get("background") or {}).get("retrograde")
    if retro is None:
        retro = [p["name"] for p in planets if p.get("retro")]
    L.append(("Ретроградны: " + ", ".join(retro) + ".") if retro else "Ретроградных планет нет.")
    # ── станции впереди ──
    st = ctx.get("stations") or []
    if st:
        L.append("Станции впереди: " + "; ".join(
            f"{s.get('planet') or '?'} → {_turn_of(s.get('type'))} {s.get('date') or '?'} "
            f"(через {s.get('in_days', '?')} дн, {s.get('con') or '?'})"
            for s in st[:6]) + ".")
    # ── Меркурий: окно ретро ──
    ma = ctx.get("mercury_active") or []
    mn = ctx.get("mercury_next") or []
    if ctx.get("mercury_retro"):
        a = ma[0] if ma else {}
        s = "Меркурий: ретро"
        if a.get("since"):
            s += f" с {a['since']}"
        if a.get("until"):
            s += f" по {a['until']}"
        if a.get("days_left") is not None:
            s += f", осталось {a['days_left']} дн"
        L.append(s + ".")
    else:
        s = "Меркурий: директ"
        if mn:
            n = mn[0]
            s += f"; ретро с {n.get('start') or '?'} (через {n.get('in_days', '?')} дн)"
            if n.get("until"):
                s += f" по {n['until']}"
        L.append(s + ".")
    # ── лунации ──
    lun = ctx.get("lunations") or []
    if lun:
        L.append("Лунации: " + "; ".join(
            f"{x.get('type') or '?'} {str(x.get('when') or '?')[:10]} "
            f"(через {x.get('in_days', '?')} дн, {x.get('con') or '?'})" for x in lun) + ".")
    # ── входы Луны в реальные созвездия, до 4 ──
    ing = ctx.get("moon_ingress_real") or []
    if ing:
        L.append("Луна входит в созвездия: " + "; ".join(
            f"→ {x.get('into') or '?'} {x.get('when') or '?'}" for x in ing[:4]) + ".")
    # ── затмения: последнее прошедшее (из расчёта) и ближайшие ──
    calc = ctx.get("eclipses_calc") or []
    past = [e for e in calc if isinstance(e.get("in_days"), int) and e["in_days"] < 0]
    bits = []
    if past:
        e = past[-1]
        bits.append(f"последнее {e.get('date') or '?'} {_eclipse_words(e)}, {-e['in_days']} дн назад")
    elif ctx.get("eclipse_recent"):
        e = ctx["eclipse_recent"][0]
        bits.append(f"недавнее {e.get('date') or '?'} {_eclipse_words(e)}, "
                    f"{e.get('days_ago', '?')} дн назад")
    nxt = ctx.get("eclipse_next") or []
    for i, e in enumerate(nxt[:2]):
        bits.append(f"{'ближайшее' if i == 0 else 'затем'} {e.get('date') or '?'} "
                    f"{_eclipse_words(e)}, через {e.get('in_days', '?')} дн")
    if bits:
        L.append("Затмения: " + "; ".join(bits) + ".")
    # ── королевские звёзды (истинный угол на сфере) ──
    royal = ctx.get("royal") or []
    if royal:
        L.append("Королевские звёзды: " + "; ".join(
            f"{r.get('planet') or '?'} — {str(r.get('star') or '?').split(' (')[0]} "
            f"(угол {r.get('orb', '?')}°)" for r in royal) + ".")
    if glossary:
        L += ["", GANN_GLOSSARY]
    return "\n".join(L)


def short_line(ctx: dict) -> str:
    m = ctx["moon"]
    bits = [f"{m['emoji']} {m['name']} {m['illumination']}%"]
    if ctx.get("mode") == "precise" and (m.get("con") or m.get("sign")):
        # 🔵 в короткой строке тоже сначала реальное созвездие, знак — в скобках 🟡
        if m.get("con"):
            bits[0] = f"{m['emoji']} Луна {m['con']}".strip()
        else:
            bits[0] = f"{m['emoji']} Луна {m['sign']} {m.get('deg','')}".strip()
        sun = next((p for p in ctx.get("planets", []) if p["name"] == "Солнце"), None)
        if sun:
            bits.append(f"☉{sun.get('con') or sun['sign']}")
        ex = next((a for a in ctx.get("aspects", []) if a["exact"]), None)
        if ex:
            bits.append(f"⚡{ex['glyph_a']}{ex['aspect'][:3]}{ex['glyph_b']}")
        st = ctx.get("stations", [])
        if st and st[0]["in_days"] <= 14:
            bits.append(f"{st[0]['glyph']}станция {st[0]['in_days']}д")
    if ctx.get("mercury_retro"):
        bits.append("☿RETRO")
    elif ctx.get("mercury_next"):
        bits.append(f"☿→R {ctx['mercury_next'][0]['in_days']}д")
    if ctx.get("eclipse_next"):
        bits.append(f"🌘{ctx['eclipse_next'][0]['in_days']}д")
    return " · ".join(bits)


# ══════════════════════════════════════════════════════════════════════
# SELF-ТЕСТ — настоящие assert: величины, границы, монотонность.
# Раньше у этого модуля не было ни одного assert про положение тел, и
# врать он мог сколько угодно — ловить было нечем. Запуск:
#     python3 backend/astro.py
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys

    print("=" * 78)
    print("  astro.py — self-тест НАСТОЯЩЕГО 3D (доказательство, а не «вроде работает»)")
    print("=" * 78)

    # ── [1] 3D-модуль на месте и офлайн ──
    assert _RS is not None, "realsky3d не импортировался — 3D-слоя нет"
    print("\n[1] realsky3d импортирован, зависимость только numpy — OK")

    # ── [2] контрольные точки из шапки con88.py: широта решает ──
    JD26 = _RS.jd_from_utc(2026, 8, 3, 15, 0)
    JD00 = 2451545.0
    for lam, bet, want, want_flat in ((248.0, -17.0, "Скорпион", "Змееносец"),
                                      (40.0, -12.0, "Кит", "Овен"),
                                      (300.0, +20.0, "Орёл", "Козерог")):
        s = _sky_of(lam, bet, JD00)
        assert s["con"] == want, f"λ{lam} β{bet}: ждали {want}, получили {s['con']}"
        assert s["con_band"] == want_flat, f"λ{lam} β=0: ждали {want_flat}, дано {s['con_band']}"
        assert s["beta_matters"] is True, f"λ{lam} β{bet}: широта обязана менять ответ"
    print("[2] широта решает: λ248 β−17 → Скорпион (плоско было бы Змееносец);")
    print("    λ40 β−12 → Кит (плоско Овен); λ300 β+20 → Орёл (плоско Козерог) — OK")

    # ── [3] при β=0 реальное созвездие обязано совпасть с поясной проекцией ──
    for lam in range(0, 360, 7):
        s = _sky_of(float(lam), 0.0, JD26)
        assert s["con"] == s["con_band"], f"λ{lam}: при β=0 real≠band ({s['con']}/{s['con_band']})"
        assert s["beta_matters"] is False, f"λ{lam}: при β=0 широта не может ничего менять"
    print("[3] при β=0 real == band на 52 долготах — OK (проекция честна только на эклиптике)")

    # ── [4] Змееносец существует, и сетка его не знает ──
    oph = [lam for lam in range(240, 270) if _sky_of(float(lam), 0.0, JD26)["con_code"] == "Oph"]
    assert len(oph) >= 15, f"Змееносец на эклиптике обязан занимать ~18°, найдено {len(oph)}°"
    assert "Змееносец" not in SIGNS, "в 30°-сетке Змееносца нет — она не небо"
    print(f"[4] Змееносец занимает на эклиптике ~{len(oph)}°, в 12-знаковой сетке его нет — OK")

    # ── [5] расхождение сетки с небом ──
    d = _RS.drift_deg(JD26)
    assert d is not None and 23.5 <= d <= 25.5, f"расхождение вне 23.5…25.5: {d}"
    print(f"[5] тропическая сетка отстала от неба на {d:.2f}° — OK (в границах 23.5…25.5)")

    # ── [6] истинный угол на сфере ──
    assert abs(_sep3d(0, 0, 90, 0) - 90.0) < 1e-9, "ортогональные долготы дают 90°"
    assert abs(_sep3d(10, 0, 10, 0)) < 1e-9, "совпавшие тела дают 0°"
    assert abs(_sep3d(0, -21.14, 0, 0) - 21.14) < 1e-6, "одна долгота, но разные широты — угол = Δβ"
    s_flat, s_true = 0.0, _sep3d(333.86, 0.0, 333.86, -21.14)
    assert s_true > 20.0, "Фомальгаут не может «соединяться» с телом на эклиптике"
    print(f"[6] истинный угол: ⊥=90°, совпадение=0°, Фомальгаут при равной долготе "
          f"отстоит на {s_true:.2f}° (плоская мерка давала {s_flat:.2f}°) — OK")

    # ── [7] живой конвейер: полный расчёт ──
    ctx = current_context(datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc))
    assert ctx.get("mode") == "precise", f"нужен precise-режим, получен {ctx.get('mode')}"
    P = ctx["planets"]
    assert len(P) >= 10, f"тел должно быть ≥10, получено {len(P)}"
    for p in P:
        assert p.get("lat") is not None, f"{p['name']}: широта выброшена — это и был баг"
        assert -90.0 <= p["lat"] <= 90.0, f"{p['name']}: β={p['lat']} вне диапазона"
        assert p.get("con"), f"{p['name']}: реальное созвездие пустое"
        assert p.get("con_code") in _RS.CON88_RU, f"{p['name']}: код созвездия неизвестен"
        assert p.get("sign") in SIGNS, f"{p['name']}: 30°-проекция обязана сохраниться"
        assert 0.0 <= p["lon"] < 360.0, f"{p['name']}: λ вне круга"
    print(f"[7] полный расчёт: {len(P)} тел, у всех есть β и реальное созвездие — OK")

    # ── [8] у Плутона и Луны широта заметно не нулевая ──
    lats = {p["name"]: abs(p["lat"]) for p in P}
    assert lats.get("Плутон", 0) > 1.0, f"β Плутона обязана быть заметной, дано {lats.get('Плутон')}"
    assert lats.get("Луна", 0) > 0.05, f"β Луны обязана быть ненулевой, дано {lats.get('Луна')}"
    assert lats.get("Меркурий", 0) > 0.05, f"β Меркурия обязана быть ненулевой"
    print(f"[8] β Плутона {lats['Плутон']:.2f}°, Луны {lats['Луна']:.2f}°, "
          f"Меркурия {lats['Меркурий']:.2f}° — широта живая, не ноль — OK")

    # ── [9] тропика действительно врёт (та самая беда владельца) ──
    mism = [p["name"] for p in P if p["con"] != p["sign"]]
    assert len(mism) >= 7, f"на эту дату сетка обязана врать у ≥7 тел, найдено {len(mism)}"
    sun = next(p for p in P if p["name"] == "Солнце")
    moon = next(p for p in P if p["name"] == "Луна")
    assert sun["sign"] == "Лев" and sun["con"] == "Рак", \
        f"03.08.2026: Солнце названо {sun['sign']}, стоит в {sun['con']}"
    assert moon["sign"] == "Овен" and moon["con"] == "Рыбы", \
        f"03.08.2026: Луна названа {moon['sign']}, стоит в {moon['con']}"
    print(f"[9] сетка называет НЕ ТО созвездие у {len(mism)} тел из {len(P)}; "
          f"Солнце «Лев» → Рак, Луна «Овен» → Рыбы — слова владельца подтверждены — OK")

    # ── [10] дома по реальным границам ──
    hr = ctx["houses"]["real"]
    assert hr and hr.get("n_houses") == 13, f"домов по небу должно быть 13, дано {hr and hr.get('n_houses')}"
    assert abs(hr["total_width_deg"] - 360.0) < 1e-3, f"сумма ширин {hr['total_width_deg']} ≠ 360°"
    assert hr["total_rise_hours"] and 23.0 < hr["total_rise_hours"] < 24.5, \
        f"сумма времён восхождения {hr['total_rise_hours']} не похожа на звёздные сутки"
    widths = [h["width_deg"] for h in hr["houses"]]
    assert max(widths) > 40.0 and min(widths) < 10.0, \
        "реальные дома обязаны быть НЕРАВНЫМИ (Дева ~44°, Скорпион ~7°)"
    assert ctx["houses"].get("equal"), "равные дома обязаны остаться отдельным ключом"
    assert "🟡" in ctx["houses"]["equal"]["note"], "равные дома обязаны быть помечены как проекция"
    assert len(ctx["houses"]["cusps"]) == 12, "старый ключ cusps обязан выжить (совместимость)"
    print(f"[10] домов по реальным границам {hr['n_houses']} (не 12), сумма ширин "
          f"{hr['total_width_deg']:.1f}°, сумма времён {hr['total_rise_hours']:.2f}ч, "
          f"шире всех {max(widths):.1f}°, уже всех {min(widths):.1f}° — OK")

    # ── [11] аспекты: канон цел, sep3d добавлен рядом ──
    A = ctx["aspects"]
    assert A, "аспекты пропали — канон сломан"
    for a in A:
        assert "orb" in a and a["orb"] >= 0, "долготный орб обязан остаться нетронутым"
        assert "sep3d" in a, "истинный угол обязан присутствовать рядом"
        if a["sep3d"] is not None:
            assert 0.0 <= a["sep3d"] <= 180.0, f"sep3d={a['sep3d']} вне диапазона"
            assert a["sep3d"] >= a["sep_lon"] - 1e-6 or a["d_beta"] > 0, \
                "истинный угол не может быть меньше долготного без разницы широт"
    dif = max((abs(a["sep3d"] - a["sep_lon"]) for a in A if a.get("sep3d") is not None), default=0)
    print(f"[11] аспектов {len(A)}: orb по долготе цел, sep3d рядом; "
          f"максимальное расхождение долгота↔сфера {dif:.2f}° — OK")

    # ── [12] королевские звёзды: плоская мерка больше не врёт ──
    for r in ctx.get("royal", []):
        assert r["orb"] <= 1.5, f"{r['star']}: угол {r['orb']}° прошёл сквозь порог 1.5°"
    far = _sep3d(56.17 + PRECESSION_DEG_PER_YEAR * 26, 0.0, 56.17 + PRECESSION_DEG_PER_YEAR * 26, 22.43)
    assert far > 20.0, "Альголь при равной долготе обязан остаться далеко"
    print(f"[12] королевские звёзды по истинному углу: соединений {len(ctx.get('royal', []))}, "
          f"Альголь при совпавшей долготе честно в {far:.1f}° — OK")

    # ── [13] текст для ИИ: небо первым, тропика подписью ──
    txt = render_for_ai(ctx)
    assert "тропическая сетка отстала от неба" in txt.lower() or "ТРОПИЧЕСКАЯ СЕТКА ОТСТАЛА" in txt, \
        "в блоке обязана быть строка про расхождение"
    assert "РАК" in txt and "тропич. Лев" in txt, "реальное созвездие Солнца обязано стоять раньше знака"
    i_con, i_sign = txt.index("РАК"), txt.index("тропич. Лев")
    assert i_con < i_sign, "реальное созвездие обязано идти ПЕРЕД тропическим знаком"
    assert "🔵" in txt and "🟡" in txt and "⚫" in txt, "рамка 🔵/🟡/⚫ обязана быть в блоке"
    assert "тропический зодиак, дом, движение" not in txt, "старый заголовок обязан исчезнуть"
    print("[13] текст для ИИ: реальное созвездие раньше знака, рамка 🔵🟡⚫ на месте, "
          "строка про расхождение есть — OK")

    # ── [14] короткая строка и совместимость ──
    sl = short_line(ctx)
    assert isinstance(sl, str) and len(sl) > 3, "короткая строка пуста"
    assert "Рыбы" in sl, f"короткая строка обязана нести реальное созвездие Луны: '{sl}'"
    for key in ("planets", "moon", "houses", "aspects", "background", "mercury_active"):
        assert key in ctx, f"ключ {key} пропал — сломана совместимость"
    for key in ("asc", "mc", "asc_sign", "mc_sign", "cusps", "placements"):
        assert key in ctx["houses"], f"houses.{key} пропал — фронт рассыплется"
    print(f"[14] короткая строка: '{sl}'")
    print("     старые ключи на месте — совместимость цела — OK")

    # ── [15] защита слоёв: мусор не роняет разбор ──
    bad = _sky_of(float("nan"), 0.0, JD26)
    assert bad["con"] is None and bad.get("note"), "мусор обязан дать None + note, а не падение"
    assert _deg_in_con(10.0, []) == (None, None, None, None), "пустые дуги — честный None"
    print("[15] защита слоёв: мусор на входе → None + note, процесс жив — OK")

    # ── [16] затмения по эфемеридам, а не по таблице 2026 ──
    ctx2 = current_context(datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc))
    en = ctx2["eclipse_next"]
    assert en and en[0]["date"] == "2026-08-12" and "солнечное" in en[0]["label"], \
        f"12.08.2026 — полное солнечное затмение, получено {en}"
    calc2 = ctx2["eclipses_calc"]
    assert any(e["type"] == "лунное" and e["date"] == "2026-08-28" for e in calc2), \
        f"лунное 28.08.2026 обязано быть в расчёте: {calc2}"
    assert all(k in e for e in calc2 for k in ("type", "kind", "date", "when", "in_days")), \
        "форма записи затмения нарушена"
    assert calc2 == sorted(calc2, key=lambda e: e["when"]), "затмения обязаны идти по времени"
    for key in ("eclipse_next", "eclipse_recent", "near_eclipse", "mercury_retro",
                "mercury_active", "mercury_next", "outer_active", "outer_next"):
        assert key in ctx2, f"ключ {key} пропал — старый рендер и фронт его читают"
    # окно 2026-09-01…2027-09-01 — контрольные даты 2027 (таблица ECLIPSES их не знает)
    c27 = _eclipses_calc(_load_sf(), datetime(2026, 9, 1, tzinfo=timezone.utc),
                         days_back=0, days_fwd=365)
    got = {(e["type"], e["date"]) for e in c27}
    for want in (("лунное", "2027-02-20"), ("лунное", "2027-07-18"), ("лунное", "2027-08-17"),
                 ("солнечное", "2027-02-06"), ("солнечное", "2027-08-02")):
        assert want in got, f"затмение {want} не найдено: {sorted(got)}"
    assert not any(d >= date(2027, 1, 1) for d, _l in ECLIPSES), \
        "таблица ECLIPSES не знает 2027 — расчёт обязан жить без неё"
    print(f"[16] затмения по эфемеридам: ближайшее к 03.08.2026 — {en[0]['label']} {en[0]['date']}, "
          f"в 2027 найдено {len(c27)} (таблица 2027 не знает) — OK")

    # ── [17] окна ретро Меркурия по скорости, внешние — по станциям ──
    mnx = ctx2["mercury_next"]
    assert mnx and mnx[0]["in_days"] > 0, f"впереди обязано быть окно ретро: {mnx}"
    d_start = date.fromisoformat(mnx[0]["start"])
    assert abs((d_start - date(2026, 10, 24)).days) <= 1, \
        f"ретро Меркурия начинается 24.10.2026 ±1, получено {mnx[0]['start']}"
    assert mnx[0].get("until") and abs((date.fromisoformat(mnx[0]["until"]) - date(2026, 11, 13)).days) <= 1, \
        f"конец окна 13.11.2026 ±1, получено {mnx[0].get('until')}"
    assert ctx2["mercury_retro"] is False and ctx2["mercury_active"] == [], \
        "03.08.2026 Меркурий директ — активного окна быть не должно"
    # Сатурн ретро 26.07–10.12.2026 по эфемеридам (старая таблица носила даты 2025 года:
    # 13.07–28.11 — именно такую ошибку и лечит расчёт по небу)
    sat = next((a for a in ctx2["outer_active"] if a["label"].startswith("Сатурн")), None)
    assert sat, f"Сатурн ретро 26.07–10.12.2026 обязан быть в outer_active: {ctx2['outer_active']}"
    assert sat["until"] and abs((date.fromisoformat(sat["until"]) - date(2026, 12, 10)).days) <= 2, \
        f"дирекция Сатурна 10.12.2026 ±2, получено {sat['until']}"
    sat_w = _retro_windows(_load_sf(), datetime(2026, 8, 3, tzinfo=timezone.utc), "Сатурн", back=60, fwd=200)
    assert sat_w[0] and sat_w[0][0]["since"] == "2026-07-26" and sat_w[0][0]["until"] == "2026-12-10", \
        f"окно Сатурна по эфемеридам 26.07–10.12.2026: {sat_w}"
    assert all(n["label"].endswith("ретро") and n["in_days"] > 0 for n in ctx2["outer_next"]), \
        f"outer_next — только будущие станции «→ ретро»: {ctx2['outer_next']}"
    # прямой вызов: внутри окна 24.10–13.11.2026
    act, upc = _retro_windows(_load_sf(), datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc), "Меркурий")
    assert act and act[0]["since"] == "2026-10-24" and act[0]["until"] == "2026-11-13" \
        and act[0]["days_left"] == 12, f"окно 24.10–13.11.2026: {act}"
    assert upc and upc[0]["start"] > "2026-11-13", f"следующее окно обязано быть позже: {upc}"
    print(f"[17] Меркурий ретро с {mnx[0]['start']} по {mnx[0]['until']} (через {mnx[0]['in_days']} дн); "
          f"Сатурн ретро до {sat['until']}; внутри окна 01.11: осталось {act[0]['days_left']} дн — OK")

    # ── [18] короткий рендер: факты без рамок и 30°-сетки ──
    tc = render_compact(ctx2)
    for must in ("НЕБО", "Аспекты по долготе", "Как это обычно читают", "Рак", "2026-08-12",
                 "Меркурий: директ", "Ретроградны", "Луна:"):
        assert must in tc, f"в коротком рендере нет «{must}»"
    for ban in ("тропич", "🟡", "🔵", "⚫", "ПРИМЕНЕНИЕ", "ЧЕСТНОСТЬ", "роль:"):
        assert ban not in tc, f"в коротком рендере не место «{ban}»"
    assert len(tc) < 6000, f"короткий рендер слишком длинный: {len(tc)}"
    tc0 = render_compact(ctx2, glossary=False)
    assert len(tc0) < len(tc) and "обычно читают" not in tc0, "glossary=False обязан убрать абзац школы"
    assert "тропич" not in tc0 and "Рак" in tc0
    assert render_for_ai(ctx2).startswith("СУТОЧНАЯ КАРТА"), "старый рендер обязан остаться прежним"
    print(f"[18] render_compact: {len(tc)} знаков (старый render_for_ai: {len(render_for_ai(ctx2))}), "
          f"без глоссария {len(tc0)}; рамок и сетки нет — OK")

    # ── [19] lite-контекст (нет эфемерид) рендерится тем же входом ──
    d0 = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)
    ma_l, mn_l = _window_state(d0.date(), MERCURY_RX)
    oa_l, on_l = _window_state(d0.date(), OUTER_RX)
    lite = {"mode": "lite", "ts": d0.isoformat(), "moon": moon_phase(d0),
            "mercury_retro": bool(ma_l), "mercury_active": ma_l, "mercury_next": mn_l[:1],
            "outer_active": oa_l, "outer_next": on_l[:2], "eclipses_calc": [],
            **_eclipse_block(d0.date())}
    tl = render_compact(lite)
    assert "упрощённый режим" in tl and tl == render_for_ai(lite), "lite обязан идти через _render_lite"
    assert _eclipse_block_calc([]) == {"eclipse_next": [], "eclipse_recent": [],
                                       "near_eclipse": False, "eclipses_calc": []}
    assert _clean_label("Полное солнечное затмение (тропич. Лев 🟡, видно в Европе)") == \
        "Полное солнечное затмение"
    print("[19] lite-контекст рендерится; пустой расчёт затмений — честно пусто — OK")

    print("\n" + "=" * 78)
    print("  ВСЕ ASSERT ПРОЙДЕНЫ.")
    print("=" * 78)
    sys.exit(0)
