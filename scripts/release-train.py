#!/usr/bin/env python3
"""Точка входа CLI `release-train`: scan / step / deploy для rumap/rumap.

Примеры:
  scripts/release-train.py scan
  scripts/release-train.py step --repo rumap/rumap
  scripts/release-train.py deploy --sha <sha main> --format both
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from release_train.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
