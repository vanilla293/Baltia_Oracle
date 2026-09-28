#!/usr/bin/env bash
# Сборка деплой-архива ПИФИЯ (Zero-Touch, v4.4.0).
# Пакует ТОЛЬКО код и данные поставки; секреты и базы — исключены жёстко.
# Запуск из корня проекта:  bash build_release.sh
set -euo pipefail
cd "$(dirname "$0")"

VER="$(python3 - <<'PY'
import re
print(re.search(r'APP_VERSION\s*=\s*"([^"]+)"', open("backend/config.py").read()).group(1))
PY
)"
OUT="pythia_${VER}_zerotouch.zip"

echo ">> проверка перед сборкой (готово = всё зелёное)"
python3 -m compileall -q backend
for m in oracle execution aether_resonance decision leverage hawkes microstructure bifurcation ai_pilot; do
  python3 -m backend.$m >/dev/null && echo "   self-test $m OK"
done
for m in grid_sim_test txt_backtester rnd_micro grid_search_honest grid_search_v2; do
  python3 -m backend.lab.$m >/dev/null && echo "   self-test lab.$m OK"
done

echo ">> сборка ${OUT}"
rm -f "$OUT"
# белый список: код + фронт + доки + разрешённые данные (mathieu, CA-серт).
# Исключаем секреты/базы/venv/кэш явно — как в .gitignore.
zip -q -r "$OUT" \
  backend frontend \
  requirements.txt start.sh start.bat selfcheck.py \
  README.md CLAUDE.md NEXT_CHAT.md MODULES_MAP.md ЗАПУСК.txt ЗАПУСК_ИИ_ПИЛОТ.txt \
  BACKTEST_PROTOCOL.md ЗАЩИТА_ПРОЕКТА.md docs_AETHER_ROADMAP.md \
  docs_KAK_USTROENO_v3.html docs_FORMULY_EFIRA_v3_0_0.html \
  build_release.sh \
  -x '*/__pycache__/*' '*.pyc' \
  -x 'backend/data/*' 'data/config_user.json' \
  -x '*.db' '*.db-wal' '*.db-shm' '*.db-journal' \
  -x '*.env' '*.key' '*.pem' '*/.venv/*' 'data/ticks.db*' \
  -x 'data/trades.jsonl' 'data/trader_audit.jsonl'
# mathieu_periods.json — часть поставки (bif_calendar), добавляем адресно
[ -f data/mathieu_periods.json ] && zip -q "$OUT" data/mathieu_periods.json

echo ">> проверка: секретов в архиве нет"
if unzip -l "$OUT" | grep -Eiq '(config_user\.json|\.env|\.key|ticks\.db|trades\.jsonl|trader_audit)'; then
  echo "!! в архив попал секрет/база — СБОРКА ОТКЛОНЕНА"; rm -f "$OUT"; exit 1
fi
echo ">> готово: ${OUT}"
unzip -l "$OUT" | tail -1
