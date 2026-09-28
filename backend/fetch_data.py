"""ОРАКУЛ // ПИФИЯ — докачка тяжёлых данных ДО старта (закон REAL SKY:
в рантайме расчёта сети нет; качаем один раз сюда, backend/data/).

Что качает:
  • de440s.bsp (~32 МБ, JPL) — эфемериды для астро-карты и эфирного слоя.
    Компактная версия de440 (1849–2150) — для астрологии тождественна.
  • hipparcos_hyg.csv НЕ нужен Пифии: звёздный каталог живёт только в полном
    натальном движке REAL SKY; здесь звёзд нет — честно не качаем. Если
    когда-нибудь понадобится: PYTHIA_FETCH_HYG=1.

Потоково, докачка во временный файл + атомарное переименование: оборванный
огрызок никогда не подменит рабочий файл. Уже скачан → мгновенный выход.
"""
from __future__ import annotations

import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

SOURCES = [
    {"name": "de440s.bsp",
     "url": "https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/de440s.bsp",
     "min_size": 25_000_000,
     "label": "эфемериды планет (DE440s, JPL)",
     "skip_if_any": ("de440.bsp", "de440s.bsp", "de441.bsp", "de430.bsp",
                     "de421.bsp")},
]
if os.environ.get("PYTHIA_FETCH_HYG") == "1":
    SOURCES.append({
        "name": "hipparcos_hyg.csv",
        "url": ("https://raw.githubusercontent.com/astronexus/HYG-Database/"
                "main/hyg/CURRENT/hygdata_v41.csv"),
        "min_size": 20_000_000,
        "label": "каталог звёзд (HYG v4.1; Пифии не обязателен)",
        "skip_if_any": ("hipparcos_hyg.csv",)})


def _have(name: str) -> str | None:
    for d in (DATA, HERE, os.path.join(HERE, "..")):
        p = os.path.join(d, name)
        if os.path.exists(p) and os.path.getsize(p) > 1_000_000:
            return p
    return None


def _fetch(src: dict) -> bool:
    for alt in src["skip_if_any"]:
        p = _have(alt)
        if p:
            print(f"[данные] {src['label']}: уже есть ({os.path.basename(p)})")
            return True
    os.makedirs(DATA, exist_ok=True)
    dst = os.path.join(DATA, src["name"])
    tmp = dst + ".part"
    print(f"[данные] качаю {src['label']} → {dst}")
    try:
        req = urllib.request.Request(src["url"],
                                     headers={"User-Agent": "pythia-fetch/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = 0
            while True:
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
                if total % (1 << 22) < (1 << 18):
                    print(f"    … {total // (1 << 20)} МБ", flush=True)
        if os.path.getsize(tmp) < src["min_size"]:
            raise IOError(f"файл подозрительно мал ({os.path.getsize(tmp)} байт)")
        os.replace(tmp, dst)                       # атомарно
        print(f"[данные] ✓ готово: {src['name']} "
              f"({os.path.getsize(dst) // (1 << 20)} МБ)")
        return True
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        print(f"[данные] ✗ не скачалось: {e}\n"
              f"    Астро/эфир поднимутся в упрощённом режиме; можно положить "
              f"любой de440*.bsp в backend/data/ руками или задать "
              f"PYTHIA_EPHEMERIS=путь.")
        return False


def main() -> int:
    ok = True
    for src in SOURCES:
        ok = _fetch(src) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
