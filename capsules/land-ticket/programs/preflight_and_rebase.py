#!/usr/bin/env python3
"""Шаг 1 капсулы land-ticket: preflight-проверка чистоты обоих worktree и rebase тикета на ветку усилия."""

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

    if effort_wt == ticket_wt:
        raise RuntimeError("effort_worktree и ticket_worktree не должны совпадать")

    for label, wt in (("effort_worktree", effort_wt), ("ticket_worktree", ticket_wt)):
        if not Path(wt).is_dir():
            raise RuntimeError(f"{label} не существует: {wt}")
        status = run_git(wt, ["status", "--porcelain"]).stdout.strip()
        if status:
            raise RuntimeError(f"{label} ({wt}) содержит незакоммиченные изменения:\n{status}")

    current_effort_branch = run_git(effort_wt, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if current_effort_branch != effort_branch:
        raise RuntimeError(
            f"effort_worktree ({effort_wt}) находится на ветке '{current_effort_branch}', "
            f"ожидалась '{effort_branch}'"
        )

    effort_head_before = run_git(effort_wt, ["rev-parse", "HEAD"]).stdout.strip()
    ticket_branch = run_git(ticket_wt, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    # Подтягиваем точный коммит effort_head_before во временный worktree без переписывания локальных веток
    run_git(ticket_wt, ["fetch", effort_wt, effort_head_before])
    fetched_head = run_git(ticket_wt, ["rev-parse", "FETCH_HEAD"]).stdout.strip()

    # Если ticket уже потомок effort_head_before — rebase не требуется
    anc = run_git(ticket_wt, ["merge-base", "--is-ancestor", fetched_head, "HEAD"], check=False)
    if anc.returncode != 0:
        rb = run_git(ticket_wt, ["rebase", fetched_head], check=False)
        if rb.returncode != 0:
            conflict_diag = (rb.stderr or rb.stdout).strip()
            run_git(ticket_wt, ["rebase", "--abort"], check=False)
            raise RuntimeError(
                f"Конфликт при rebase {ticket_wt} поверх {effort_branch} ({effort_head_before[:10]}). "
                f"Выполнен git rebase --abort. Диагностика:\n{conflict_diag}"
            )

    rebased_head = run_git(ticket_wt, ["rev-parse", "HEAD"]).stdout.strip()
    commits_raw = run_git(ticket_wt, ["rev-list", "--count", f"{fetched_head}..{rebased_head}"]).stdout.strip()
    commits_count = int(commits_raw)

    print(
        json.dumps(
            {
                "ticket_branch": ticket_branch,
                "rebased_head": rebased_head,
                "effort_head_before": effort_head_before,
                "commits_count": commits_count,
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
