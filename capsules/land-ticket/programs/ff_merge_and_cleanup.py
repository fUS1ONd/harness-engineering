#!/usr/bin/env python3
"""Шаг 3 капсулы land-ticket: git merge --ff-only в effort_worktree и безопасная очистка worktree/ветки."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def run_git(cwd: str, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git -C {cwd} {' '.join(args)} failed (code {proc.returncode}):\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def main() -> int:
    input_path = sys.argv[1] if len(sys.argv) > 1 else os.environ["SKILLCACHE_INPUT"]
    with open(input_path, "r", encoding="utf-8") as f:
        inp = json.load(f)

    effort_wt = str(Path(inp["effort_worktree"]).resolve())
    effort_branch = inp["effort_branch"]
    ticket_wt = str(Path(inp["ticket_worktree"]).resolve())
    ticket_branch = inp["ticket_branch"]
    rebased_head = inp["rebased_head"]
    cleanup = bool(inp.get("cleanup", True))

    current_branch = run_git(effort_wt, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if current_branch != effort_branch:
        raise RuntimeError(
            f"effort_worktree ({effort_wt}) на ветке '{current_branch}', ожидалась '{effort_branch}'"
        )

    run_git(effort_wt, ["fetch", ticket_wt, rebased_head])
    run_git(effort_wt, ["merge", "--ff-only", "FETCH_HEAD"])

    merged_head = run_git(effort_wt, ["rev-parse", "HEAD"]).stdout.strip()
    if merged_head != rebased_head:
        raise RuntimeError(f"Ожидался HEAD={rebased_head}, после merge получен {merged_head}")

    worktree_removed = False
    branch_deleted = False

    if cleanup:
        # Все закоммиченные изменения уже влиты через --ff-only и проверены (merged_head == rebased_head),
        # поэтому --force безопасно убирает временный worktree даже если check_command оставил untracked-артефакты (__pycache__ и т.п.).
        run_git(effort_wt, ["worktree", "remove", "--force", ticket_wt])
        worktree_removed = True

        # Удаляем временную ветку только если она не совпадает с effort_branch/main/dev и уже влита в HEAD
        protected = {effort_branch, "main", "master", "dev", "HEAD"}
        if ticket_branch and ticket_branch not in protected:
            ref_exists = run_git(
                effort_wt, ["show-ref", "--verify", "--quiet", f"refs/heads/{ticket_branch}"], check=False
            )
            if ref_exists.returncode == 0:
                is_anc = run_git(
                    effort_wt, ["merge-base", "--is-ancestor", ticket_branch, merged_head], check=False
                )
                if is_anc.returncode == 0:
                    run_git(effort_wt, ["branch", "-d", ticket_branch])
                    branch_deleted = True

    print(
        json.dumps(
            {
                "merged_head": merged_head,
                "worktree_removed": worktree_removed,
                "branch_deleted": branch_deleted,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
