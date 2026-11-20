#!/usr/bin/env python3
"""Верификация (V) капсулы land-ticket: проверяет постусловия в effort_worktree."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def run_git(cwd: str, args: list[str]) -> str:
    proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git -C {cwd} {' '.join(args)} failed (code {proc.returncode}):\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def main() -> int:
    input_path = sys.argv[1] if len(sys.argv) > 1 else os.environ["SKILLCACHE_INPUT"]
    with open(input_path, "r", encoding="utf-8") as f:
        inp = json.load(f)

    effort_wt = str(Path(inp["effort_worktree"]).resolve())
    effort_branch = inp["effort_branch"]
    ticket_wt = str(Path(inp["ticket_worktree"]).resolve())
    merged_head = inp["merged_head"]
    cleanup = bool(inp.get("cleanup", True))

    branch = run_git(effort_wt, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch != effort_branch:
        raise RuntimeError(f"Ветка effort_worktree='{branch}', ожидалась '{effort_branch}'")

    head = run_git(effort_wt, ["rev-parse", "HEAD"])
    if head != merged_head:
        raise RuntimeError(f"HEAD effort_worktree={head}, ожидался {merged_head}")

    status = run_git(effort_wt, ["status", "--porcelain"])
    if status:
        raise RuntimeError(f"effort_worktree грязный после merge:\n{status}")

    if cleanup:
        wt_list = run_git(effort_wt, ["worktree", "list", "--porcelain"])
        for line in wt_list.splitlines():
            if line.startswith("worktree "):
                wt_path = str(Path(line[len("worktree "):]).resolve())
                if wt_path == ticket_wt:
                    raise RuntimeError(f"ticket_worktree всё ещё числится в git worktree list: {ticket_wt}")

    print(json.dumps({"verified": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
