#!/usr/bin/env python3
"""Сборка архива Baltia Oracle для передачи на другой компьютер или другому человеку.

Обычно:  python scripts/package.py               → dist/baltia_oracle.zip (git archive HEAD, БЕЗ секретов).
С .env:  python scripts/package.py --with-env    → вложить в архив твой текущий .env (там токен и ключи!).

Главная защита у --with-env: сборка ОТКАЗЫВАЕТСЯ идти, если в .env пустой (или не числовой) OWNER_ID.
Именно так однажды уехал архив с общим токеном, но без владельца: распакованная копия отвечала
«я ещё не настроен» и, работая на том же токене, отбивала сообщения у рабочей копии — те самые
«пару раз ответил, потом не настроен». Архив с ключами — это секрет: не выкладывай его в общий доступ.

git archive берёт только то, что в репозитории, поэтому .env, data/, .venv, *.session в архив не
попадают сами (см. .gitignore). .env добавляется в архив ТОЛЬКО по явному --with-env.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "baltia_oracle/"          # с этой папкой архив распакуется в baltia_oracle/…


class PackageError(Exception):
    """Сборку продолжать нельзя (например, в .env нет OWNER_ID)."""


def _env_value(text: str, key: str) -> str:
    """Значение переменной key из текста .env (как это делает oracle.config._read_env: последнее
    непустое, снятие кавычек и хвостового ' #'-комментария). Нет строки — "" ."""
    out = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        k, _, v = line.partition("=")
        if k.strip() != key:
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        elif " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        if v:
            out = v          # последнее непустое значение
    return out


def owner_id_from_env_text(text: str) -> int:
    """OWNER_ID из текста .env → первое положительное число (как в config.load), иначе 0.
    0 значит «владельца нет»: и для пустой строки, и для «@имя»/имени — с таким архив шипить нельзя."""
    raw = _env_value(text, "OWNER_ID")
    for part in re.split(r"[,;\s]+", raw):
        if not part:
            continue
        try:
            v = int(part)
        except ValueError:
            continue
        if v > 0:
            return v
    return 0


def ensure_env_shippable(env_path: Path) -> int:
    """Проверить, что .env годится для отправки: есть числовой OWNER_ID. → сам owner_id.
    Иначе PackageError — сборку с --with-env прерываем, чтобы не уехал архив без владельца."""
    if not env_path.is_file():
        raise PackageError(f"нет файла {env_path} — с --with-env нечего вкладывать. "
                           f"Собери без --with-env или сначала заполни .env.")
    text = env_path.read_text(encoding="utf-8-sig", errors="replace")
    owner = owner_id_from_env_text(text)
    if owner <= 0:
        raise PackageError(
            "в .env пустой или не числовой OWNER_ID — отказываюсь класть такой .env в архив.\n"
            "Копия с токеном, но без владельца отвечает «я ещё не настроен» и мешает рабочей копии.\n"
            "Впиши числовой OWNER_ID (узнать: напиши боту /start) и собери заново — либо собери "
            "без --with-env (тогда каждый впишет свои токен и ключи сам).")
    if not _env_value(text, "BOT_TOKEN"):
        raise PackageError("в .env пустой BOT_TOKEN — вкладывать в архив нечего. "
                           "Заполни .env или собери без --with-env.")
    return owner


def build_archive(out_path: Path, *, with_env: bool = False) -> Path:
    """Собрать zip репозитория (git archive HEAD) в out_path. with_env — доложить текущий .env
    (после ensure_env_shippable). → путь к готовому архиву."""
    env_path = ROOT / ".env"
    if with_env:
        ensure_env_shippable(env_path)          # до сборки: не создавать файл, если шипить нельзя
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "archive", "--format=zip", f"--prefix={PREFIX}", "-o", str(out_path), "HEAD"],
            cwd=ROOT, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise PackageError(f"git archive не отработал ({e}); собери из git-репозитория с коммитом HEAD.")
    if with_env:
        with zipfile.ZipFile(out_path, "a", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(env_path, PREFIX + ".env")
    return out_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Собрать архив Baltia Oracle.")
    ap.add_argument("--with-env", action="store_true",
                    help="вложить текущий .env (твои токен и ключи!) — только с непустым OWNER_ID")
    ap.add_argument("-o", "--output", default=str(ROOT / "dist" / "baltia_oracle.zip"),
                    help="куда сохранить архив (по умолчанию dist/baltia_oracle.zip)")
    args = ap.parse_args(argv)
    out = Path(args.output)
    try:
        path = build_archive(out, with_env=args.with_env)
    except PackageError as e:
        print(f"\n✖ {e}", file=sys.stderr)
        return 1
    print(f"Готово: {path}")
    if args.with_env:
        print("⚠ В архиве лежит твой .env — это BOT_TOKEN и ключи. Не выкладывай его в открытый "
              "доступ и не отправляй незнакомым: у кого архив, у того и доступ к боту.")
    else:
        print("Секретов внутри нет: .env, data/ и *.session не включены. Каждый впишет свои токен "
              "и ключи в свой .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
