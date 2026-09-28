"""Запуск: python -m oracle"""
from __future__ import annotations

import asyncio
import sys

from .app import main


def run() -> int:
    try:
        return int(asyncio.run(main()) or 0)
    except KeyboardInterrupt:
        print("\nОстановлен.", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(run())
