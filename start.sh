#!/usr/bin/env bash
# ПИФИЯ v5.1 «СОВЕТ» — запуск (Linux/macOS).
# Порядок: python → venv → ЗАВИСИМОСТИ СНАЧАЛА (видно, с проверкой и повторами) → режим → данные → старт.
cd "$(dirname "$0")" || exit 1
mkdir -p data
LOG="data/install.log"; : > "$LOG"
export PYTHONUTF8=1

echo "=========================================================="
echo "  ПИФИЯ v5.1 СОВЕТ — новости → совет → взгляд → миссия"
echo "=========================================================="

# 0. облачная папка?
case "$PWD" in
  *OneDrive*|*Yandex*|*Яндекс*|*"Google Drive"*|*Dropbox*|*iCloud*)
    echo "[ПИФИЯ] ВНИМАНИЕ: проект лежит в папке облачной синхронизации ($PWD)."
    echo "         Синхронизация блокирует файлы, пока pip ставит пакеты, и .venv ломается."
    echo "         Лучше перенести папку (например, в ~/pythia), удалить .venv и запустить снова.";;
esac

# 1. python 3.10+
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then echo "[ПИФИЯ] Python не найден. Поставь Python 3.12 и запусти снова."; exit 1; fi
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
echo "[ПИФИЯ] Python $PYVER ($PY)"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "[ПИФИЯ] нужен Python 3.10 или новее."; exit 1
fi

# 2. venv (битый — пересоздаём)
VPY=".venv/bin/python"
if [ -x "$VPY" ] && ! "$VPY" -c 'import sys' >/dev/null 2>&1; then
  echo "[ПИФИЯ] .venv сломан — пересоздаю..."; rm -rf .venv
fi
if [ ! -x "$VPY" ]; then
  echo "[ПИФИЯ] создаю виртуальное окружение..."
  "$PY" -m venv .venv || { echo "[ПИФИЯ] venv не создался — см. ошибку выше."; exit 1; }
fi

# 3. зависимости СНАЧАЛА: видно, в лог, с проверкой импорта и повторами
CHECK='import fastapi, uvicorn, httpx, feedparser, openai, pydantic, websockets, skyfield, numpy, jplephem'
echo "[ПИФИЯ] ставлю зависимости (лог: $LOG)..."
"$VPY" -m pip install --upgrade pip --prefer-binary --disable-pip-version-check 2>&1 | tee -a "$LOG"
ok=0
for try in 1 2 3; do
  echo "[ПИФИЯ] pip install -r requirements.txt (попытка $try из 3)"
  "$VPY" -m pip install --prefer-binary --disable-pip-version-check -r requirements.txt 2>&1 | tee -a "$LOG"
  if "$VPY" -c "$CHECK" 2>>"$LOG"; then ok=1; break; fi
  echo "[ПИФИЯ] пакеты не импортируются — повтор..."
done
if [ "$ok" != 1 ]; then
  echo "[ПИФИЯ] пересоздаю .venv и ставлю заново..."
  rm -rf .venv && "$PY" -m venv .venv && "$VPY" -m pip install --prefer-binary --disable-pip-version-check -r requirements.txt 2>&1 | tee -a "$LOG"
  if ! "$VPY" -c "$CHECK" 2>>"$LOG"; then
    echo; echo "[ПИФИЯ] УСТАНОВКА НЕ УДАЛАСЬ. Последние строки $LOG:"; tail -n 30 "$LOG"
    echo "  Обычные причины: нет интернета; облако/антивирус держат .venv; слишком новый Python (поставь 3.12 и удали .venv)."
    exit 1
  fi
fi
echo "[ПИФИЯ] зависимости OK"

# 4. режим (переменные окружения имеют приоритет: PYTHIA_DRY=1 ./start.sh)
if [ -z "$PYTHIA_DRY$PYTHIA_MOCK_AI" ] && [ -t 0 ]; then
  echo
  echo "  1 = БОЕВОЙ   (ключи из панели; с токеном Tinkoff заявки НАСТОЯЩИЕ)"
  echo "  2 = СУХОЙ    (всё работает, заявки только в data/trader_audit.jsonl)"
  echo "  3 = ДЕМО     (без ключей и сети: фейковые ИИ, новости и брокер)"
  echo
  read -r -p "Режим [1/2/3], Enter = 2 (сухой): " MODE
  case "$MODE" in
    1) ;;
    3) export PYTHIA_MOCK_AI=1 PYTHIA_MOCK_TINKOFF=1 PYTHIA_DRY=1 ;;
    *) export PYTHIA_DRY=1 ;;
  esac
fi

# 5. данные (эфемериды ~32 МБ один раз; в демо не нужны)
if [ "$PYTHIA_MOCK_AI" != "1" ]; then
  echo "[ПИФИЯ] проверяю данные (эфемериды для астро/эфира)..."
  "$VPY" -m backend.fetch_data || echo "[ПИФИЯ] ВНИМАНИЕ: данные не скачались — астро/эфир в упрощённом режиме."
fi

# 6. сервер должен импортироваться до старта
if ! RS_NO_BROWSER=1 "$VPY" -c 'from backend import server'; then
  echo; echo "[ПИФИЯ] сервер не стартует — причина в traceback выше. Пришли этот текст разработчику."; exit 1
fi

# 7. порт
PORT="${PORT:-8799}"
if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  echo "[ПИФИЯ] ВНИМАНИЕ: порт $PORT занят (уже запущена ПИФИЯ?). Закрой её или запусти PORT=8800 ./start.sh"
fi

# 8. старт
[ "$PYTHIA_MOCK_AI" = "1" ] && echo "[ПИФИЯ] режим: ДЕМО" || { [ "$PYTHIA_DRY" = "1" ] && echo "[ПИФИЯ] режим: СУХОЙ — заявки на биржу не уходят" || echo "[ПИФИЯ] режим: БОЕВОЙ — с токеном Tinkoff заявки настоящие"; }
echo "[ПИФИЯ] запуск на http://localhost:${PORT}  (Ctrl+C останавливает)"
exec "$VPY" -m uvicorn backend.server:app --host 0.0.0.0 --port "${PORT}"
