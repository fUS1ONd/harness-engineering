#!/usr/bin/env python3
"""Шаг 2 капсулы land-ticket: запуск полной команды проверок в ticket_worktree."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    input_path = sys.argv[1] if len(sys.argv) > 1 else os.environ["SKILLCACHE_INPUT"]
    with open(input_path, "r", encoding="utf-8") as f:
        inp = json.load(f)

    ticket_wt = str(Path(inp["ticket_worktree"]).resolve())
    check_command = (inp.get("check_command") or "").strip()

    if not check_command:
        print(json.dumps({"checks_passed": True, "check_duration_sec": 0.0}, ensure_ascii=False))
        return 0

    start = time.monotonic()
    proc = subprocess.run(
        ["bash", "-lc", check_command],
        cwd=ticket_wt,
        capture_output=True,
        text=True,
    )
    elapsed = round(time.monotonic() - start, 3)

    if proc.returncode != 0:
        msg = [f"Команда проверки '{check_command}' завершилась с кодом {proc.returncode} за {elapsed}s."]
        if proc.stdout.strip():
            msg.append(f"stdout (tail):\n{proc.stdout.strip()[-2500:]}")
        if proc.stderr.strip():
            msg.append(f"stderr (tail):\n{proc.stderr.strip()[-2500:]}")
        print("\n".join(msg), file=sys.stderr)
        return 1

    print(json.dumps({"checks_passed": True, "check_duration_sec": elapsed}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
