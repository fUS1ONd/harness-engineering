#!/usr/bin/env python3
"""Тесты раннера scripts/skillcache-run и боевой капсулы capsules/land-ticket."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER = REPO_ROOT / "scripts" / "skillcache-run"
LAND_CAPSULE = REPO_ROOT / "capsules" / "land-ticket"


def run_cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["python3", str(RUNNER), *args],
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"skillcache-run {' '.join(args)} failed (code {proc.returncode}):\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def init_test_repo(base: Path) -> tuple[Path, Path, Path]:
    """Создаёт временный git-репо с веткой усилия effort/demo и worktree тикета t3/ticket-1."""
    main_repo = base / "repo"
    main_repo.mkdir()
    git(main_repo, "init", "-b", "main")
    git(main_repo, "config", "user.name", "Test User")
    git(main_repo, "config", "user.email", "test@example.com")

    (main_repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(main_repo, "add", "base.txt")
    git(main_repo, "commit", "-m", "initial")

    effort_wt = base / "wt-effort"
    git(main_repo, "worktree", "add", "-b", "effort/demo", str(effort_wt), "main")

    ticket_wt = base / "wt-ticket"
    git(main_repo, "worktree", "add", "-b", "t3/ticket-1", str(ticket_wt), "effort/demo")

    return main_repo, effort_wt, ticket_wt


class SkillCacheRunnerTests(unittest.TestCase):
    def test_qualify_land_ticket_gates_1_and_2(self) -> None:
        proc = run_cli("qualify", str(LAND_CAPSULE))
        report = json.loads(proc.stdout)
        self.assertTrue(report["gate1_structure"]["passed"])
        self.assertTrue(report["gate2_checks"]["passed"])
        self.assertTrue(report["gate3_replay"]["skipped"])

    def test_prompt_rendering_is_compact_and_hides_source(self) -> None:
        proc = run_cli("prompt", str(LAND_CAPSULE))
        out = proc.stdout
        self.assertIn("land-ticket", out)
        self.assertIn("effort_worktree", out)
        self.assertIn("skillcache-run run", out)
        # Исходный код программ не должен попадать в промпт модели
        self.assertNotIn("def run_git", out)

    def test_gate1_rejects_broken_dataflow(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cap_dir = Path(td) / "bad"
            cap_dir.mkdir()
            (cap_dir / "capsule.json").write_text(
                json.dumps(
                    {
                        "name": "bad",
                        "description": "broken dataflow",
                        "bindings": {},
                        "programs": {
                            "p1": {
                                "command": ["python3", "-c", "print('{}')"],
                                "inputs": ["missing_binding"],
                                "outputs": ["x"],
                            }
                        },
                        "steps": [
                            {"id": "s1", "type": "program", "program": "p1", "instruction": "run p1"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            proc = run_cli("qualify", str(cap_dir), check=False)
            self.assertNotEqual(proc.returncode, 0)
            report = json.loads(proc.stdout)
            self.assertFalse(report["gate1_structure"]["passed"])
            self.assertTrue(any("missing_binding" in e for e in report["gate1_structure"]["errors"]))

    def test_deoptimization_and_continue(self) -> None:
        """Если шаг 2 падает, раннер отдаёт его модели с диагностикой, а шаг 3 после continue едет скомпилированным."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cap = root / "cap"
            (cap / "programs").mkdir(parents=True)

            (cap / "programs" / "p1.py").write_text(
                "import json, sys\nd = json.load(open(sys.argv[1]))\nprint(json.dumps({'v1': d['x'] + 1}))\n",
                encoding="utf-8",
            )
            (cap / "programs" / "p2_fail.py").write_text(
                "import sys\nprint('boom on step 2', file=sys.stderr)\nsys.exit(2)\n",
                encoding="utf-8",
            )
            (cap / "programs" / "p3.py").write_text(
                "import json, sys\nd = json.load(open(sys.argv[1]))\nprint(json.dumps({'final': d['v1'] * d['v2']}))\n",
                encoding="utf-8",
            )
            (cap / "capsule.json").write_text(
                json.dumps(
                    {
                        "name": "deopt-demo",
                        "description": "demo",
                        "bindings": {"x": {"description": "input int"}},
                        "programs": {
                            "p1": {"command": ["python3", "programs/p1.py"], "inputs": ["x"], "outputs": ["v1"]},
                            "p2": {"command": ["python3", "programs/p2_fail.py"], "inputs": ["v1"], "outputs": ["v2"]},
                            "p3": {"command": ["python3", "programs/p3.py"], "inputs": ["v1", "v2"], "outputs": ["final"]},
                        },
                        "steps": [
                            {"id": "s1", "type": "program", "program": "p1", "instruction": "step 1"},
                            {"id": "s2", "type": "program", "program": "p2", "instruction": "step 2"},
                            {"id": "s3", "type": "program", "program": "p3", "instruction": "step 3"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            state = root / "state.json"

            # 1. Запуск без x -> needs_bindings
            r0 = json.loads(run_cli("run", str(cap), "--state", str(state), "{}").stdout)
            self.assertEqual(r0["status"], "needs_bindings")
            self.assertIn("x", r0["bindings_needed"])

            # 2. Передаём x=4 -> p1 проходит (v1=5), p2 падает -> your_steps с program_failed
            r1 = json.loads(run_cli("continue", "--state", str(state), json.dumps({"x": 4})).stdout)
            self.assertEqual(r1["status"], "your_steps")
            self.assertEqual(len(r1["completed"]), 1)
            self.assertEqual(r1["completed"][0]["result"]["v1"], 5)
            self.assertTrue(r1["steps"][0]["program_failed"])
            self.assertIn("boom on step 2", r1["steps"][0]["diagnostics"])
            self.assertTrue(r1["steps"][0]["program_source"].endswith("p2_fail.py"))
            self.assertIn("v2", r1["bindings_needed"])

            # 3. Модель выполняет шаг 2 вручную и передаёт v2=10 -> p3 выполняется скомпилированным (5 * 10 = 50)
            r2 = json.loads(run_cli("continue", "--state", str(state), json.dumps({"v2": 10})).stdout)
            self.assertEqual(r2["status"], "complete")
            self.assertEqual(r2["bindings"]["final"], 50)

    def test_land_ticket_e2e_green_single_call(self) -> None:
        """Боевой replay капсулы land-ticket: параллельный коммит в effort + rebase + check + ff-only + cleanup за 1 вызов."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _, effort_wt, ticket_wt = init_test_repo(base)

            # Коммит в effort_wt (неконфликтный)
            (effort_wt / "effort.txt").write_text("from effort\n", encoding="utf-8")
            git(effort_wt, "add", "effort.txt")
            git(effort_wt, "commit", "-m", "effort change")

            # Коммит в ticket_wt
            (ticket_wt / "ticket.txt").write_text("from ticket\n", encoding="utf-8")
            git(ticket_wt, "add", "ticket.txt")
            git(ticket_wt, "commit", "-m", "ticket change")

            state = base / "land_state.json"
            payload = {
                "effort_worktree": str(effort_wt),
                "effort_branch": "effort/demo",
                "ticket_worktree": str(ticket_wt),
                "check_command": "mkdir -p __pycache__ && touch __pycache__/x.pyc && test -f ticket.txt && test -f effort.txt",
                "cleanup": True,
            }

            report = json.loads(
                run_cli("run", str(LAND_CAPSULE), "--state", str(state), json.dumps(payload)).stdout
            )
            self.assertEqual(report["status"], "complete")
            self.assertEqual(len(report["completed"]), 4)
            self.assertEqual(report["steps"][0]["id"], "settle_and_mark_done")
            self.assertTrue(report["bindings"]["checks_passed"])
            self.assertTrue(report["bindings"]["worktree_removed"])
            self.assertTrue(report["bindings"]["branch_deleted"])
            self.assertTrue(report["bindings"]["verified"])
            self.assertFalse(ticket_wt.exists())
            self.assertTrue((effort_wt / "ticket.txt").is_file())
            self.assertTrue((effort_wt / "effort.txt").is_file())

    def test_land_ticket_rebase_conflict_deoptimizes_cleanly(self) -> None:
        """При конфликте rebase шаг безопасно делает --abort и возвращает program_failed без порчи effort_wt."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _, effort_wt, ticket_wt = init_test_repo(base)

            (effort_wt / "base.txt").write_text("effort version\n", encoding="utf-8")
            git(effort_wt, "add", "base.txt")
            git(effort_wt, "commit", "-m", "effort edit")
            effort_head_before = git(effort_wt, "rev-parse", "HEAD")

            (ticket_wt / "base.txt").write_text("ticket version\n", encoding="utf-8")
            git(ticket_wt, "add", "base.txt")
            git(ticket_wt, "commit", "-m", "ticket edit")

            state = base / "conflict_state.json"
            payload = {
                "effort_worktree": str(effort_wt),
                "effort_branch": "effort/demo",
                "ticket_worktree": str(ticket_wt),
                "check_command": "",
                "cleanup": True,
            }

            report = json.loads(
                run_cli("run", str(LAND_CAPSULE), "--state", str(state), json.dumps(payload)).stdout
            )
            self.assertEqual(report["status"], "your_steps")
            self.assertTrue(report["steps"][0]["program_failed"])
            self.assertIn("rebase --abort", report["steps"][0]["diagnostics"])
            # Оба worktree остались чистыми, effort_wt не сдвинулся
            self.assertEqual(git(effort_wt, "rev-parse", "HEAD"), effort_head_before)
            self.assertEqual(git(ticket_wt, "status", "--porcelain"), "")

    def test_land_ticket_failed_check_deoptimizes_then_continues(self) -> None:
        """Если check_command падает, модель чинит и передаёт continue -> merge и verify доезжают скомпилированными."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _, effort_wt, ticket_wt = init_test_repo(base)

            (ticket_wt / "feature.txt").write_text("ok\n", encoding="utf-8")
            git(ticket_wt, "add", "feature.txt")
            git(ticket_wt, "commit", "-m", "add feature")

            state = base / "check_fail_state.json"
            payload = {
                "effort_worktree": str(effort_wt),
                "effort_branch": "effort/demo",
                "ticket_worktree": str(ticket_wt),
                "check_command": "echo 'test failed' >&2; exit 1",
                "cleanup": True,
            }

            r1 = json.loads(
                run_cli("run", str(LAND_CAPSULE), "--state", str(state), json.dumps(payload)).stdout
            )
            self.assertEqual(r1["status"], "your_steps")
            self.assertEqual(r1["steps"][0]["id"], "run_checks")
            self.assertTrue(r1["steps"][0]["program_failed"])
            self.assertIn("test failed", r1["steps"][0]["diagnostics"])

            # Модель подтверждает, что проверка исправлена и пройдена, и продолжает план
            r2 = json.loads(
                run_cli(
                    "continue",
                    "--state",
                    str(state),
                    json.dumps({"checks_passed": True, "check_duration_sec": 0.1}),
                ).stdout
            )
            self.assertEqual(r2["status"], "complete")
            self.assertTrue(r2["bindings"]["verified"])
            self.assertFalse(ticket_wt.exists())
            self.assertTrue((effort_wt / "feature.txt").is_file())


if __name__ == "__main__":
    unittest.main()
