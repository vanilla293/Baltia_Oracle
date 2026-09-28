#!/usr/bin/env bash
# Запуск Baltia Oracle на Linux / macOS: ./start.sh
# Сам найдёт Python ≥ 3.10, создаст окружение .venv, поставит зависимости
# (заново — только если поменялся requirements.txt), проверит .env и запустит бота.
# Другой Python можно указать явно: PYTHON=/usr/bin/python3.12 ./start.sh
set -e

cd "$(dirname "${BASH_SOURCE[0]}")"

say()  { printf '%s\n' "$*"; }
fail() { printf '\n✖ %s\n' "$*" >&2; exit 1; }

py_ok() {  # это Python 3.10 или новее?
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

# ── 1. Python ───────────────────────────────────────────────────────────
if [ -n "${PYTHON:-}" ] && ! py_ok "$PYTHON"; then
    say "PYTHON=$PYTHON не подходит (нужен 3.10+) — ищу другой."
fi
PY=""
for c in "${PYTHON:-}" python3 python3.13 python3.12 python3.11 python3.10 python3.14 python; do
    [ -n "$c" ] || continue
    if command -v "$c" >/dev/null 2>&1 && py_ok "$c"; then
        PY="$c"
        break
    fi
done

if [ -z "$PY" ]; then
    found="не нашёл вообще"
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1; then
            found="$("$c" --version 2>&1 || true)"
            break
        fi
    done
    fail "Нужен Python 3.10 или новее, а у тебя: ${found}.
  Ubuntu/Debian:  sudo apt install python3 python3-venv
  macOS:          brew install python@3.12
  Или без Python — через Docker:  docker compose up -d"
fi

# ── 2. Окружение .venv ──────────────────────────────────────────────────
if [ ! -x .venv/bin/python ] || ! py_ok .venv/bin/python; then
    rm -rf .venv
    say "Создаю окружение .venv ($("$PY" --version 2>&1))…"
    if ! "$PY" -m venv .venv; then
        rm -rf .venv
        fail "Не смог создать окружение.
  На Ubuntu/Debian не хватает пакета:  sudo apt install python3-venv
  (или python3.X-venv под твою версию), потом запусти ./start.sh ещё раз."
    fi
    .venv/bin/python -m pip install -q --disable-pip-version-check --upgrade pip >/dev/null 2>&1 || true
fi

# ── 3. Зависимости (только если requirements.txt поменялся) ────────────
STAMP=.venv/.requirements.installed
if ! { command -v cmp >/dev/null 2>&1 && cmp -s requirements.txt "$STAMP"; }; then
    say "Ставлю зависимости (первый раз — минута-две)…"
    if ! .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt; then
        fail "Зависимости не поставились — проверь интернет и запусти ./start.sh ещё раз."
    fi
    cp requirements.txt "$STAMP"
fi

# ── 4. Настройки .env ───────────────────────────────────────────────────
if [ ! -f .env ]; then
    cp .env.example .env
    say ""
    say "Создал файл .env с настройками. Открой его и впиши как минимум:"
    say "  BOT_TOKEN=         — токен бота от @BotFather"
    say "  DEEPSEEK_API_KEY=  — ключ с platform.deepseek.com"
    say "  GROQ_API_KEY=      — для голосовых (бесплатно, console.groq.com)"
    say "Потом запусти ./start.sh ещё раз."
    exit 1
fi

has_setting() {  # задана ли переменная: в окружении или непустая в .env
    local name="$1"
    [ -n "${!name:-}" ] && return 0
    grep -Eq "^[[:space:]]*(export[[:space:]]+)?${name}[[:space:]]*=[[:space:]]*[^[:space:]#]" .env
}

has_setting BOT_TOKEN \
    || fail "В .env пустой BOT_TOKEN — возьми токен у @BotFather (/newbot) и впиши."
has_setting DEEPSEEK_API_KEY || has_setting LLM_API_KEY \
    || fail "В .env пустой DEEPSEEK_API_KEY — создай ключ на platform.deepseek.com → API keys и впиши."
has_setting OWNER_ID \
    || say "OWNER_ID пока пустой: напиши боту /start — он пришлёт твой id. Впиши его в .env и перезапусти."

# ── 5. Локальный whisper, если выбран ───────────────────────────────────
if grep -Eiq '^[[:space:]]*(export[[:space:]]+)?STT_PROVIDER[[:space:]]*=[[:space:]]*local' .env \
        && ! .venv/bin/python -c 'import faster_whisper' >/dev/null 2>&1; then
    say "STT_PROVIDER=local — ставлю faster-whisper (это надолго, пара сотен МБ)…"
    .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements-voice.txt \
        || say "faster-whisper не поставился — голосовые пока работать не будут (подробности выше)."
fi

# ── 6. Запуск ───────────────────────────────────────────────────────────
mkdir -p data
say "Запускаю бота. Остановить — Ctrl+C."
exec .venv/bin/python -m oracle "$@"
