"""ЗАГРУЗЧИК ДНЕВНОЙ ИСТОРИИ НЕФТИ — ЕДИНСТВЕННОЕ МЕСТО С СЕТЬЮ.

Закон ABSOLUTE OFFLINE: в рантайме расчёта сети нет. Этот модуль работает
ДО расчёта, как ephem_fetch: скачивает историю и кладёт локальный файл
ds/brent_daily.json. Стенд backend.lab.oil_frs читает только его.

В /scratchpad/candles_cache.json нефть BRU6 есть, но это ~3 месяца дневных
баров (63 шт.) — для замера эффекта по корзинам Ψ этого мало на порядок.
Поэтому тянем две независимые дневные серии Brent:

  FRED  DCOILBRENTEU — Brent Europe спот (первоисточник EIA), с 1987-05-20,
        ~9.9 тыс. торговых дней. Самая глубокая история.
  YF    BZ=F         — ICE Brent фронтальный фьючерс (то, чем по сути и
        является BRU6 на МосБирже), с 2007-07-30, ~4.7 тыс. дней.

Две серии нужны не для красоты: спот и фьючерс — разные инструменты с
разной микроструктурой, и эффект обязан жить на обоих, иначе он свойство
одного тикера, а не нефти.

python3 -m backend.lab.oil_fetch
"""
import json
import os
import urllib.request
from datetime import datetime, timezone

OUT = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/brent_daily.json"
FRED_RAW = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/brent_fred_raw.csv"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILBRENTEU"
YF_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/BZ%3DF"
          "?period1=0&period2=2000000000&interval=1d")
UA = {"User-Agent": "Mozilla/5.0"}


def _get(url: str, timeout: int = 120) -> bytes:
    """urllib, а если он упёрся в прокси/таймаут — тот же запрос через curl."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:                                   # noqa: BLE001
        import subprocess
        p = subprocess.run(["curl", "-sS", "-L", "--max-time", str(timeout),
                            "-H", f"User-Agent: {UA['User-Agent']}", url],
                           capture_output=True, timeout=timeout + 30)
        if p.returncode != 0 or not p.stdout:
            raise
        return p.stdout


def fetch_fred() -> list:
    """[[дата, close], ...] — Brent Europe спот. Пропуски ('.') выкидываем.

    FRED периодически рвёт HTTP/2-соединение и уходит в таймаут. Если сеть
    не дала ответа, но сырой CSV уже лежит рядом с прошлой закачки — берём
    его. Это не «сеть в рантайме»: файл получен раньше и лежит локально."""
    try:
        txt = _get(FRED_URL).decode("utf-8", "replace")
        open(FRED_RAW, "w", encoding="utf-8").write(txt)
    except Exception:                                   # noqa: BLE001
        if not os.path.exists(FRED_RAW):
            raise
        print(f"    (сеть молчит — читаю локальную копию {FRED_RAW})")
        txt = open(FRED_RAW, encoding="utf-8").read()
    out = []
    for line in txt.splitlines()[1:]:
        parts = line.strip().split(",")
        if len(parts) < 2 or parts[1] in (".", ""):
            continue
        try:
            out.append([parts[0], float(parts[1])])
        except ValueError:
            continue
    return out


def fetch_yahoo() -> list:
    """[[дата, close], ...] — ICE Brent фронтальный фьючерс."""
    d = json.loads(_get(YF_URL).decode("utf-8", "replace"))
    res = d["chart"]["result"][0]
    ts = res["timestamp"]
    cl = res["indicators"]["quote"][0]["close"]
    out = []
    for t, c in zip(ts, cl):
        if c is None:
            continue
        day = datetime.fromtimestamp(t, timezone.utc).date().isoformat()
        out.append([day, float(c)])
    return out


def run():
    blob = {"_note": ("дневные закрытия Brent; скачано oil_fetch до расчёта, "
                      "в рантайме стендов сеть не используется"),
            "_fetched": datetime.now(timezone.utc).isoformat()}
    for name, fn in (("BRENT_SPOT", fetch_fred), ("BRENT_FUT", fetch_yahoo)):
        try:
            rows = fn()
            blob[name] = rows
            print(f"  {name}: {len(rows)} дней  {rows[0][0]} … {rows[-1][0]}")
        except Exception as exc:                       # noqa: BLE001
            blob[name] = []
            print(f"  {name}: НЕ СКАЧАНО ({exc.__class__.__name__}: {exc})")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(blob, open(OUT, "w", encoding="utf-8"))
    print(f"  → {OUT}")
    return blob


if __name__ == "__main__":
    b = run()
    # РЕАЛЬНЫЕ ПРОВЕРКИ, а не факт запуска
    assert b["BRENT_SPOT"], "спот не скачался — стенд останется без глубины"
    assert len(b["BRENT_SPOT"]) > 8000, len(b["BRENT_SPOT"])
    assert b["BRENT_SPOT"][0][0] < "1990-01-01"
    assert len(b["BRENT_FUT"]) > 4000, len(b["BRENT_FUT"])
    for nm in ("BRENT_SPOT", "BRENT_FUT"):
        ds = [r[0] for r in b[nm]]
        assert ds == sorted(ds), f"{nm}: даты не по возрастанию"
        assert len(set(ds)) == len(ds), f"{nm}: дубли дат"
        assert all(r[1] > 0 for r in b[nm]), f"{nm}: неположительная цена"
    print("oil_fetch: OK")
