#!/usr/bin/env python3
"""Точка входа CLI `prod-smoke`: read-only проверка прода rumap по ролям.

Примеры:
  scripts/prod-smoke.py --role all
  scripts/prod-smoke.py --role center --window 10m --format json
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from prod_smoke.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
