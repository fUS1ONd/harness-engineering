#!/usr/bin/env python3
"""Юнит-тесты `release-train`: подменяется только I/O-шов (GitHub, замки, деплой, smoke)."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from prod_smoke.cli import main as prod_smoke_main
from release_train import (
    ALERT_OPEN,
    BranchCi,
    CheckRef,
    CheckSummary,
    CiRun,
    Comparison,
    DeployTarget,
    FileLockStore,
    GhCliProvider,
    InProcessSmokeProvider,
    ProviderError,
    PullRequest,
    SETTLE,
    SmokeOutcome,
    TrainState,
    WAIT_DEPLOY,
    collect_state,
    deploy_train,
    evaluate,
    release_pr_body,
    run_deploy,
    step,
    summarize_checks,
    to_pull_request,
)
from release_train.cli import main
from release_train.deploy import CommandResult
from release_train.github import PR_FIELDS
from release_train.model import HOLD_LABEL, RELEASE_TRAIN_LABEL

from test_prod_smoke import healthy_fake, inspect_ok, NOW

REPO = "rumap/rumap"
MAIN_SHA = "aa" * 20
DEV_SHA = "bb" * 20
OLDER_SHA = "cc" * 20
PR_HEAD = "dd" * 20
RUN_URL = "https://github.com/rumap/rumap/actions/runs/42"


def passed(count: int = 1) -> CheckSummary:
    return CheckSummary(state="passed", passed=count)


def failed_check(name: str = "Проверка", url: str = RUN_URL) -> CheckSummary:
    return CheckSummary(state="failed", passed=1, failed=(CheckRef(name, url),))


def pending_check(name: str = "Проверка") -> CheckSummary:
    return CheckSummary(state="pending", pending=(CheckRef(name, RUN_URL),))


def make_pr(
    number: int,
    *,
    title: str = "Правка",
    labels: tuple[str, ...] = (RELEASE_TRAIN_LABEL,),
    draft: bool = False,
    mergeable: str = "MERGEABLE",
    checks: CheckSummary | None = None,
    changes_requested: bool = False,
    head_sha: str = PR_HEAD,
    base: str = "dev",
    head_ref: str = "feat/x",
    body: str = "",
) -> PullRequest:
    return PullRequest(
        number=number,
        title=title,
        url=f"https://github.com/{REPO}/pull/{number}",
        base_ref=base,
        head_ref=head_ref,
        head_sha=head_sha,
        labels=labels,
        is_draft=draft,
        mergeable=mergeable,
        checks=checks or passed(),
        changes_requested=changes_requested,
        body=body,
    )


def run_ok(sha: str, workflow: str = "Проверка") -> CiRun:
    return CiRun(workflow=workflow, status="completed", conclusion="success", url=RUN_URL, sha=sha)


def run_failed(
    sha: str,
    workflow: str = "Проверка",
    pr_number: int | None = None,
    commit_message: str = "",
) -> CiRun:
    return CiRun(
        workflow=workflow,
        status="completed",
        conclusion="failure",
        url=RUN_URL,
        sha=sha,
        pr_number=pr_number,
        commit_message=commit_message,
    )


class FakeGitHub:
    def __init__(self, *, main_sha: str = MAIN_SHA, dev_sha: str = DEV_SHA) -> None:
        self.heads = {"main": main_sha, "dev": dev_sha}
        self.prs: dict[str, list[PullRequest]] = {"dev": [], "main": []}
        self.ci: dict[str, BranchCi] = {"dev": BranchCi("dev"), "main": BranchCi("main")}
        self.commit_prs: dict[str, int] = {}
        self.compare_ = Comparison()
        self.deploy_compare: Comparison | None = None
        self.paused = False
        self.pause_after: int | None = None
        self.paused_calls = 0
        self.merged: list[tuple[int, str, bool, str]] = []
        self.created: list[dict] = []
        self.updated: list[tuple[int, str]] = []
        self.comments: list[tuple[int, str]] = []

    def open_prs(self, repo: str, base: str) -> tuple[PullRequest, ...]:
        return tuple(self.prs.get(base, ()))

    def release_pr(self, repo: str, base: str = "main", head: str = "dev") -> PullRequest | None:
        for pr in self.prs.get(base, ()):
            if pr.head_ref == head:
                return pr
        return None

    def branch_ci(self, repo: str, branch: str) -> BranchCi:
        return self.ci[branch]

    def branch_head(self, repo: str, branch: str) -> str:
        return self.heads.get(branch, "")

    def commit_pr(self, repo: str, sha: str) -> int | None:
        return self.commit_prs.get(sha)

    def compare(self, repo: str, base: str, head: str) -> Comparison:
        if base == "main" and head == "dev":
            return self.compare_
        return self.deploy_compare or Comparison()

    def automation_paused(self, repo: str) -> bool:
        self.paused_calls += 1
        if self.pause_after is not None and self.paused_calls <= self.pause_after:
            return False
        return self.paused

    def merge_pr(
        self,
        repo: str,
        number: int,
        sha: str,
        *,
        delete_branch: bool = False,
        method: str = "squash",
    ) -> None:
        self.merged.append((number, sha, delete_branch, method))
        target = "dev" if delete_branch else "main"
        self.prs[target] = [pr for pr in self.prs[target] if pr.number != number]
        if delete_branch:
            self.heads["dev"] = sha
        else:
            self.heads["main"] = sha
            self.compare_ = Comparison()

    def open_release_pr(
        self,
        repo: str,
        *,
        base: str,
        head: str,
        title: str,
        body: str,
        labels: Sequence[str],
    ) -> PullRequest:
        self.created.append(
            {"base": base, "head": head, "title": title, "body": body, "labels": tuple(labels)}
        )
        created = make_pr(
            900 + len(self.created),
            title=title,
            labels=tuple(labels),
            base=base,
            head_ref=head,
            head_sha=self.heads["dev"],
            checks=CheckSummary("none"),
            body=body,
        )
        self.prs["main"].append(created)
        return created

    def update_release_pr(self, repo: str, number: int, *, body: str) -> None:
        self.updated.append((number, body))
        self.prs["main"] = [
            replace(pr, body=body) if pr.number == number else pr for pr in self.prs["main"]
        ]

    def comment_pr(self, repo: str, number: int, body: str) -> None:
        self.comments.append((number, body))


class FakeLocks:
    def __init__(self, *, deployed: str | None = None, paused: bool = False) -> None:
        self.held: set[tuple[str, str, str]] = set()
        self.deployed = deployed
        self._paused = paused

    def acquire(self, repo: str, key: str, sha: str) -> bool:
        token = (repo, key, sha)
        if token in self.held:
            return False
        self.held.add(token)
        return True

    def release(self, repo: str, key: str, sha: str) -> None:
        self.held.discard((repo, key, sha))

    def contains(self, repo: str, key: str, sha: str) -> bool:
        return (repo, key, sha) in self.held

    def paused(self) -> bool:
        return self._paused

    def deployed_sha(self, repo: str) -> str | None:
        return self.deployed

    def mark_deployed(self, repo: str, sha: str) -> None:
        self.deployed = sha


class FakeDeployer:
    def __init__(
        self,
        *,
        fail_deploy: Sequence[str] = (),
        fail_smoke: Sequence[str] = (),
        fail_smoke_times: dict[str, int] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_deploy = set(fail_deploy)
        self.fail_smoke = set(fail_smoke)
        self.fail_smoke_remaining = dict(fail_smoke_times or {})

    def deploy(self, target: DeployTarget) -> CommandResult:
        self.calls.append(("deploy", target.node))
        ok = target.node not in self.fail_deploy
        argv = ("ssh", "--", target.host, target.command())
        if ok:
            return CommandResult(argv, 0, "Container started", "")
        return CommandResult(argv, 1, "", "error: compose up failed")

    def prune(self, target: DeployTarget) -> CommandResult:
        self.calls.append(("prune", target.node))
        argv = ("ssh", "--", target.host, target.prune_command())
        return CommandResult(argv, 0, "Deleted Images", "")

    def smoke(self, role: str) -> SmokeOutcome:
        self.calls.append(("smoke", role))
        remaining = self.fail_smoke_remaining.get(role, 0)
        if remaining > 0:
            self.fail_smoke_remaining[role] = remaining - 1
            ok = False
        else:
            ok = role not in self.fail_smoke
        report = {
            "role": role,
            "ok": ok,
            "failures": []
            if ok
            else [
                {
                    "role": role,
                    "node": f"{role}-node",
                    "host": "host",
                    "kind": "logs",
                    "target": "container",
                    "detail": "1 совпадений ERROR/PANIC/fatal",
                    "evidence": ['{"level":"ERROR","msg":"лента недоступна"}'],
                }
            ],
        }
        return SmokeOutcome(role=role, ok=ok, report=report, text=f"prod-smoke {role}")


def state_with(**overrides) -> TrainState:
    defaults = TrainState(repo=REPO, main_sha=MAIN_SHA, dev_sha=DEV_SHA)
    return replace(defaults, **overrides)


class OptInTest(unittest.TestCase):
    def test_pr_without_label_is_ignored(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["dev"] = BranchCi("dev", (run_ok(DEV_SHA),))
        github.prs["dev"] = [make_pr(7, labels=("bug",))]

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "noop")
        self.assertEqual(github.merged, [])
        payload = collect_state(github, locks, REPO).to_dict()
        self.assertEqual(payload["pull_requests"], [])
        self.assertEqual(payload["ignored_pull_requests"][0]["reason"], "no_label")

    def test_green_labeled_pr_merges_to_dev(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.prs["dev"] = [make_pr(11, title="Ускорить замер")]

        result = step(github, locks, REPO)

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "merged_to_dev")
        self.assertEqual(result["thread_action"], SETTLE)
        self.assertEqual(github.merged, [(11, PR_HEAD, True, "squash")])
        self.assertIn("#11", result["summary"])

    def test_conflicts_draft_and_foreign_changes_requested_block_merge(self) -> None:
        cases = {
            "conflict": make_pr(1, mergeable="CONFLICTING"),
            "draft": make_pr(2, draft=True),
            "changes_requested": make_pr(3, changes_requested=True),
            "ci_failed": make_pr(4, checks=failed_check()),
        }
        for reason, pr in cases.items():
            with self.subTest(reason=reason):
                github, locks = FakeGitHub(), FakeLocks()
                github.prs["dev"] = [pr]
                result = step(github, locks, REPO)
                self.assertEqual(github.merged, [])
                if reason == "ci_failed":
                    self.assertEqual(result["action"], "ci_failed")
                else:
                    self.assertEqual(result["action"], "noop")
                    payload = collect_state(github, locks, REPO).to_dict()
                    self.assertEqual(payload["pull_requests"][0]["blocked"], reason)

    def test_hold_label_stops_single_pr(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.prs["dev"] = [make_pr(5, labels=(RELEASE_TRAIN_LABEL, HOLD_LABEL))]

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "noop")
        self.assertEqual(github.merged, [])
        payload = collect_state(github, locks, REPO).to_dict()
        self.assertEqual(payload["pull_requests"][0]["blocked"], "hold")

    def test_removed_label_stops_train(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.prs["dev"] = [make_pr(6, labels=())]

        self.assertEqual(step(github, locks, REPO)["action"], "noop")
        self.assertEqual(github.merged, [])


class BatchTest(unittest.TestCase):
    def test_all_ready_prs_merge_before_promotion(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.prs["dev"] = [
            make_pr(21, head_sha="11" * 20),
            make_pr(22, head_sha="22" * 20),
        ]
        github.compare_ = Comparison(ahead_by=2, commits=("fix: раз", "fix: два"), pull_requests=(21, 22))

        first = step(github, locks, REPO)
        second = step(github, locks, REPO)
        github.ci["dev"] = BranchCi("dev", (run_ok(github.heads["dev"]),))
        third = step(github, locks, REPO)

        self.assertEqual(first["action"], "merged_to_dev")
        self.assertEqual(second["action"], "merged_to_dev")
        self.assertEqual([item[0] for item in github.merged], [21, 22])
        self.assertEqual(third["action"], "opened_release_pr")
        self.assertEqual(github.created[0]["base"], "main")
        self.assertEqual(github.created[0]["head"], "dev")

    def test_waits_for_active_dev_ci(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["dev"] = BranchCi(
            "dev",
            (CiRun(workflow="Проверка", status="in_progress", sha=DEV_SHA, url=RUN_URL),),
        )
        github.compare_ = Comparison(ahead_by=1, commits=("fix: раз",))

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "noop")
        self.assertEqual(result["details"]["ci"]["state"], "pending")
        self.assertEqual(github.merged, [])
        self.assertEqual(github.created, [])

    def test_ready_pr_still_merges_while_dev_ci_runs(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["dev"] = BranchCi(
            "dev",
            (CiRun(workflow="Проверка", status="in_progress", sha=DEV_SHA, url=RUN_URL),),
        )
        github.prs["dev"] = [make_pr(32)]

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "merged_to_dev")
        self.assertEqual([item[0] for item in github.merged], [32])


class PromotionTest(unittest.TestCase):
    def test_opens_release_pr_when_dev_green_and_ahead(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["dev"] = BranchCi("dev", (run_ok(DEV_SHA),))
        github.compare_ = Comparison(ahead_by=2, commits=("feat: один", "feat: два"), pull_requests=(41, 42))

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "opened_release_pr")
        self.assertEqual(result["thread_action"], SETTLE)
        created = github.created[0]
        self.assertEqual(created["labels"], (RELEASE_TRAIN_LABEL,))
        self.assertIn("feat: один", created["body"])
        self.assertIn("#41", created["body"])

    def test_merges_green_release_pr(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["dev"] = BranchCi("dev", (run_ok(DEV_SHA),))
        github.compare_ = Comparison(ahead_by=1, commits=("feat: один",), pull_requests=(51,))
        body = f"батч\n\n<!-- release-train:head:{DEV_SHA} -->"
        github.prs["main"] = [
            make_pr(60, title="release", base="main", head_ref="dev", head_sha=DEV_SHA, body=body)
        ]

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "merged_release_pr")
        self.assertEqual(github.merged, [(60, DEV_SHA, False, "merge")])
        self.assertEqual(github.heads["main"], DEV_SHA)

    def test_stale_release_pr_body_is_updated_during_merge(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["dev"] = BranchCi("dev", (run_ok(DEV_SHA),))
        github.compare_ = Comparison(ahead_by=1, commits=("feat: один",))
        github.prs["main"] = [
            make_pr(61, base="main", head_ref="dev", head_sha=DEV_SHA, body="старое описание")
        ]

        merged = step(github, locks, REPO)

        self.assertEqual(merged["action"], "merged_release_pr")
        self.assertTrue(merged["details"]["body_updated"])
        self.assertEqual(github.updated[0][0], 61)
        self.assertIn(DEV_SHA, github.updated[0][1])
        self.assertEqual(github.merged, [(61, DEV_SHA, False, "merge")])

    def test_release_pr_hold_and_label_removal_block_promotion(self) -> None:
        cases = {
            "hold": (RELEASE_TRAIN_LABEL, HOLD_LABEL),
            "no label": (),
        }
        for reason, labels in cases.items():
            with self.subTest(reason=reason):
                github, locks = FakeGitHub(), FakeLocks()
                github.ci["dev"] = BranchCi("dev", (run_ok(DEV_SHA),))
                github.compare_ = Comparison(ahead_by=1, commits=("feat: один",))
                github.prs["main"] = [
                    make_pr(
                        62,
                        base="main",
                        head_ref="dev",
                        head_sha=DEV_SHA,
                        labels=labels,
                        body=f"<!-- release-train:head:{DEV_SHA} -->",
                    )
                ]

                result = step(github, locks, REPO)

                self.assertEqual(result["action"], "noop")
                self.assertEqual(github.merged, [])
                self.assertEqual(github.created, [])

    def test_release_pr_with_conflict_waits(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["dev"] = BranchCi("dev", (run_ok(DEV_SHA),))
        github.compare_ = Comparison(ahead_by=1, commits=("feat: один",))
        github.prs["main"] = [
            make_pr(
                63,
                base="main",
                head_ref="dev",
                head_sha=DEV_SHA,
                mergeable="CONFLICTING",
                body=f"<!-- release-train:head:{DEV_SHA} -->",
            )
        ]

        self.assertEqual(step(github, locks, REPO)["action"], "noop")
        self.assertEqual(github.merged, [])

    def test_promotion_requires_green_dev_ci(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["dev"] = BranchCi("dev", (run_failed(DEV_SHA),))
        github.compare_ = Comparison(ahead_by=1, commits=("feat: один",))

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "ci_failed")
        self.assertEqual(github.created, [])

    def test_release_pr_with_stale_head_sha_waits_for_sync(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["dev"] = BranchCi("dev", (run_ok(DEV_SHA),))
        github.compare_ = Comparison(ahead_by=1, commits=("feat: один",))
        github.prs["main"] = [
            make_pr(
                64,
                base="main",
                head_ref="dev",
                head_sha=OLDER_SHA,
                body=f"<!-- release-train:head:{OLDER_SHA} -->",
            )
        ]

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "noop")
        self.assertEqual(result["thread_action"], SETTLE)
        self.assertEqual(github.merged, [])
        self.assertEqual(github.updated, [])


class CiFailureTest(unittest.TestCase):
    def test_failed_pr_ci_comments_and_settles_once(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.prs["dev"] = [make_pr(71, checks=failed_check("Тесты"))]

        first = step(github, locks, REPO)
        second = step(github, locks, REPO)

        self.assertEqual(first["action"], "ci_failed")
        self.assertEqual(first["thread_action"], SETTLE)
        self.assertEqual(len(github.comments), 1)
        number, body = github.comments[0]
        self.assertEqual(number, 71)
        self.assertIn(RUN_URL, body)
        self.assertIn("release-train:ci-failed", body)
        self.assertEqual(second["action"], "noop")
        self.assertEqual(len(github.comments), 1)

    def test_handled_dev_failure_does_not_mask_main_failure(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["dev"] = BranchCi("dev", (run_failed(DEV_SHA, pr_number=41),))
        github.ci["main"] = BranchCi("main", (run_failed(MAIN_SHA, "Проверка", pr_number=42),))

        first = step(github, locks, REPO)
        second = step(github, locks, REPO)

        self.assertEqual(first["action"], "ci_failed")
        self.assertEqual(second["action"], "ci_failed")
        self.assertEqual([number for number, _ in github.comments], [41, 42])
        self.assertIn("main", second["summary"])

    def test_failed_branch_ci_resolves_pr_via_commit_api(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["main"] = BranchCi("main", (run_failed(MAIN_SHA, "Проверка", pr_number=None),))
        github.commit_prs[MAIN_SHA] = 77

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "ci_failed")
        self.assertEqual(result["thread_action"], SETTLE)
        self.assertEqual(github.comments[0][0], 77)

    def test_failed_branch_ci_resolves_pr_from_commit_message(self) -> None:
        cases = (
            ("main", MAIN_SHA, "Merge pull request #88 from rumap/dev", 88),
            ("dev", DEV_SHA, "fix: поломка (#89)", 89),
        )
        for branch, sha, message, expected_pr in cases:
            with self.subTest(branch=branch):
                github, locks = FakeGitHub(), FakeLocks()
                github.ci[branch] = BranchCi(
                    branch, (run_failed(sha, "Проверка", pr_number=None, commit_message=message),)
                )

                result = step(github, locks, REPO)

                self.assertEqual(result["action"], "ci_failed")
                self.assertEqual(result["thread_action"], SETTLE)
                self.assertEqual(github.comments[0][0], expected_pr)

    def test_failed_branch_ci_without_pr_alerts_open_and_does_not_lock(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["main"] = BranchCi(
            "main",
            (run_failed(MAIN_SHA, "Проверка", pr_number=None, commit_message="chore: прямой пуш"),),
        )

        first = step(github, locks, REPO)
        second = step(github, locks, REPO)

        self.assertEqual(first["action"], "ci_failed")
        self.assertEqual(first["thread_action"], ALERT_OPEN)
        self.assertEqual(github.comments, [])
        self.assertEqual(locks.held, set())
        self.assertEqual(second["action"], "ci_failed")
        self.assertEqual(second["thread_action"], ALERT_OPEN)

    def test_failed_dev_ci_comments_on_release_pr(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["dev"] = BranchCi("dev", (run_failed(DEV_SHA, "Проверка"),))
        github.prs["main"] = [
            make_pr(80, base="main", head_ref="dev", head_sha=DEV_SHA, body="")
        ]

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "ci_failed")
        self.assertEqual(github.comments[0][0], 80)
        self.assertIn(RUN_URL, result["summary"])
        self.assertEqual(result["thread_action"], SETTLE)

    def test_failed_main_ci_comments_on_run_pr(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["main"] = BranchCi("main", (run_failed(MAIN_SHA, "Проверка", pr_number=42),))

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "ci_failed")
        self.assertEqual(github.comments[0][0], 42)
        self.assertIn("main", result["summary"])

    def test_failed_branch_ci_blocks_green_pr_merge(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.prs["dev"] = [make_pr(72)]
        github.ci["dev"] = BranchCi("dev", (run_failed(DEV_SHA),))

        self.assertEqual(step(github, locks, REPO)["action"], "ci_failed")
        self.assertEqual(github.merged, [])

    def test_stale_failed_run_is_not_reported(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["dev"] = BranchCi("dev", (run_failed("ee" * 20),))

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "noop")
        self.assertEqual(github.comments, [])


class ReadyToDeployTest(unittest.TestCase):
    def test_green_main_ci_signals_deploy_and_keeps_thread(self) -> None:
        github, locks = FakeGitHub(), FakeLocks(deployed=OLDER_SHA)
        github.compare_ = Comparison()
        github.deploy_compare = Comparison(
            ahead_by=2,
            commits=("feat: один (#91)", "feat: два (#92)"),
            pull_requests=(91, 92),
        )
        github.ci["main"] = BranchCi("main", (run_ok(MAIN_SHA),))

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "ready_to_deploy")
        self.assertEqual(result["thread_action"], WAIT_DEPLOY)
        self.assertEqual(result["details"]["main_sha"], MAIN_SHA)
        self.assertEqual(result["details"]["pull_requests"], [91, 92])
        self.assertEqual(len(result["details"]["commits"]), 2)
        self.assertIn("деплой", result["summary"])

    def test_already_deployed_main_is_noop(self) -> None:
        github, locks = FakeGitHub(), FakeLocks(deployed=MAIN_SHA)
        github.ci["main"] = BranchCi("main", (run_ok(MAIN_SHA),))

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "noop")
        self.assertIn("уже выкачен", result["summary"])

    def test_main_ci_pending_is_noop(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["main"] = BranchCi(
            "main", (CiRun(workflow="Проверка", status="queued", sha=MAIN_SHA, url=RUN_URL),)
        )

        self.assertEqual(step(github, locks, REPO)["action"], "noop")


class PauseTest(unittest.TestCase):
    def test_repo_variable_pause_blocks_mutations(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.paused = True
        github.prs["dev"] = [make_pr(81)]

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "paused")
        self.assertEqual(result["thread_action"], SETTLE)
        self.assertEqual(github.merged, [])
        self.assertEqual(locks.held, set())

    def test_pause_file_blocks_mutations(self) -> None:
        github, locks = FakeGitHub(), FakeLocks(paused=True)
        github.prs["dev"] = [make_pr(82)]

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "paused")
        self.assertIn("pause_file", result["details"]["pause"]["source"])
        self.assertEqual(github.merged, [])

    def test_pause_is_rechecked_before_mutation(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.prs["dev"] = [make_pr(83)]
        github.paused = True
        github.pause_after = 1

        result = step(github, locks, REPO)

        self.assertEqual(result["action"], "paused")
        self.assertEqual(github.merged, [])


class IdempotencyTest(unittest.TestCase):
    def test_repeat_webhook_does_not_merge_twice(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        ready = make_pr(84)
        github.prs["dev"] = [ready]
        step(github, locks, REPO)

        github.prs["dev"] = [ready]
        result = step(github, locks, REPO)

        self.assertEqual(len(github.merged), 1)
        self.assertEqual(result["action"], "noop")
        self.assertEqual(result["details"]["reason"], "already_done")

    def test_failed_mutation_releases_lock(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.prs["dev"] = [make_pr(85)]
        original = github.merge_pr

        def boom(
            repo: str,
            number: int,
            sha: str,
            *,
            delete_branch: bool = False,
            method: str = "squash",
        ) -> None:
            raise RuntimeError("gh: 502")

        github.merge_pr = boom  # type: ignore[method-assign]

        result = step(github, locks, REPO)

        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "error")
        self.assertEqual(result["thread_action"], ALERT_OPEN)
        self.assertEqual(locks.held, set())
        github.merge_pr = original  # type: ignore[method-assign]
        self.assertEqual(step(github, locks, REPO)["action"], "merged_to_dev")


class DeployTest(unittest.TestCase):
    def ready_github(self) -> FakeGitHub:
        github = FakeGitHub()
        github.ci["main"] = BranchCi("main", (run_ok(MAIN_SHA),))
        return github

    def deploy(self, github: FakeGitHub, locks: FakeLocks, deployer: FakeDeployer, **kwargs):
        kwargs.setdefault("sleep", lambda _: None)
        kwargs.setdefault("smoke_attempts", 1)
        return deploy_train(github, locks, deployer, REPO, **kwargs)

    def test_three_layers_in_order_with_smoke_gates_and_post_smoke_prune(self) -> None:
        github, locks, deployer = self.ready_github(), FakeLocks(), FakeDeployer()

        report = self.deploy(github, locks, deployer)

        self.assertTrue(report.ok)
        self.assertEqual(report.thread_action, SETTLE)
        self.assertEqual(
            deployer.calls,
            [
                ("deploy", "point"),
                ("smoke", "point"),
                ("deploy", "worker-ru"),
                ("deploy", "worker-control"),
                ("smoke", "worker"),
                ("deploy", "center"),
                ("smoke", "center"),
                ("prune", "center"),
            ],
        )
        self.assertEqual(locks.deployed, MAIN_SHA)
        self.assertIn("point → worker → center", report.summary())

    def test_fail_fast_on_point_smoke_stops_worker_and_center(self) -> None:
        github, locks = self.ready_github(), FakeLocks()
        deployer = FakeDeployer(fail_smoke={"point"})

        report = self.deploy(github, locks, deployer)

        self.assertFalse(report.ok)
        self.assertEqual(report.failed_layer, "point")
        self.assertEqual(report.thread_action, ALERT_OPEN)
        self.assertEqual(report.untouched, ("worker-ru", "worker-control", "center"))
        self.assertEqual(deployer.calls, [("deploy", "point"), ("smoke", "point")])
        self.assertIsNone(locks.deployed)
        self.assertFalse(any(step.stage == "rollback" for step in report.steps))
        evidence = " ".join(e for step in report.steps for e in step.evidence)
        self.assertIn("ERROR", evidence)
        self.assertIn("авто-откат", report.summary())

    def test_fail_fast_on_worker_smoke_leaves_center_untouched(self) -> None:
        github, locks = self.ready_github(), FakeLocks()
        deployer = FakeDeployer(fail_smoke={"worker"})

        report = self.deploy(github, locks, deployer)

        self.assertFalse(report.ok)
        self.assertEqual(report.failed_layer, "worker")
        self.assertEqual(report.untouched, ("center",))
        self.assertNotIn(("deploy", "center"), deployer.calls)
        self.assertNotIn(("smoke", "center"), deployer.calls)
        self.assertNotIn(("prune", "center"), deployer.calls)

    def test_fail_fast_on_center_smoke_does_not_prune(self) -> None:
        github, locks = self.ready_github(), FakeLocks()
        deployer = FakeDeployer(fail_smoke={"center"})

        report = self.deploy(github, locks, deployer)

        self.assertFalse(report.ok)
        self.assertEqual(report.failed_layer, "center")
        self.assertEqual(report.thread_action, ALERT_OPEN)
        self.assertNotIn(("prune", "center"), deployer.calls)
        self.assertIsNone(locks.deployed)

    def test_deploy_failure_before_smoke_also_stops(self) -> None:
        github, locks = self.ready_github(), FakeLocks()
        deployer = FakeDeployer(fail_deploy={"worker-control"})

        report = self.deploy(github, locks, deployer)

        self.assertFalse(report.ok)
        self.assertEqual(report.failed_layer, "worker")
        self.assertEqual([call for call in deployer.calls if call[0] == "smoke"], [("smoke", "point")])

    def test_smoke_warmup_retries_before_passing(self) -> None:
        github, locks = self.ready_github(), FakeLocks()
        deployer = FakeDeployer(fail_smoke_times={"worker": 2})
        sleeps: list[float] = []

        report = deploy_train(
            github,
            locks,
            deployer,
            REPO,
            smoke_attempts=4,
            smoke_interval=3.0,
            sleep=sleeps.append,
        )

        self.assertTrue(report.ok)
        self.assertEqual(sleeps, [3.0, 3.0])
        worker_smoke_step = next(s for s in report.steps if s.layer == "worker" and s.stage == "smoke")
        self.assertIn("попытка 3/4", worker_smoke_step.detail)
        self.assertIn(("prune", "center"), deployer.calls)

    def test_smoke_warmup_exhausts_attempts_and_fails_fast(self) -> None:
        github, locks = self.ready_github(), FakeLocks()
        deployer = FakeDeployer(fail_smoke={"point"})
        sleeps: list[float] = []

        report = deploy_train(
            github,
            locks,
            deployer,
            REPO,
            smoke_attempts=3,
            smoke_interval=2.0,
            sleep=sleeps.append,
        )

        self.assertFalse(report.ok)
        self.assertEqual(report.failed_layer, "point")
        self.assertEqual(sleeps, [2.0, 2.0])
        self.assertEqual(deployer.calls.count(("smoke", "point")), 3)
        self.assertIsNone(locks.deployed)

    def test_deploy_blocked_by_repo_pause(self) -> None:
        github, locks, deployer = self.ready_github(), FakeLocks(), FakeDeployer()
        github.paused = True

        report = self.deploy(github, locks, deployer)

        self.assertFalse(report.ok)
        self.assertEqual(report.action, "paused")
        self.assertEqual(deployer.calls, [])
        self.assertEqual(report.thread_action, SETTLE)

    def test_deploy_blocked_by_pause_file(self) -> None:
        github, locks, deployer = self.ready_github(), FakeLocks(paused=True), FakeDeployer()

        report = self.deploy(github, locks, deployer)

        self.assertFalse(report.ok)
        self.assertEqual(report.action, "paused")
        self.assertEqual(deployer.calls, [])

    def test_deploy_refuses_sha_that_moved(self) -> None:
        github, locks, deployer = self.ready_github(), FakeLocks(), FakeDeployer()

        report = self.deploy(github, locks, deployer, expected_sha=OLDER_SHA)

        self.assertFalse(report.ok)
        self.assertEqual(report.action, "sha_moved")
        self.assertEqual(report.thread_action, ALERT_OPEN)
        self.assertEqual(deployer.calls, [])

    def test_deploy_refuses_when_main_ci_not_green_for_target_sha(self) -> None:
        cases = {
            "none": BranchCi("main"),
            "pending": BranchCi(
                "main",
                (
                    CiRun(workflow="Проверка", status="in_progress", sha=MAIN_SHA, url=RUN_URL),
                    run_ok(MAIN_SHA),
                ),
            ),
            "failed": BranchCi("main", (run_failed(MAIN_SHA),)),
            "stale_sha": BranchCi("main", (run_ok(OLDER_SHA),)),
        }
        for name, branch_ci in cases.items():
            with self.subTest(case=name):
                github, locks, deployer = FakeGitHub(), FakeLocks(), FakeDeployer()
                github.ci["main"] = branch_ci

                report = self.deploy(github, locks, deployer)

                self.assertFalse(report.ok)
                self.assertEqual(report.action, "ci_not_ready")
                self.assertEqual(report.thread_action, ALERT_OPEN)
                self.assertEqual(deployer.calls, [])
                self.assertIsNone(locks.deployed)

    def test_repeated_deploy_after_success_is_recorded(self) -> None:
        github, locks, deployer = self.ready_github(), FakeLocks(), FakeDeployer()
        self.deploy(github, locks, deployer)

        deployer.calls.clear()
        second = self.deploy(github, locks, deployer)

        self.assertTrue(second.ok)
        self.assertEqual(locks.deployed, MAIN_SHA)

    def test_partial_deploy_plan_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            run_deploy(
                MAIN_SHA,
                FakeDeployer(),
                targets=(DeployTarget(layer="center", node="center", host="h", directory="/d"),),
                smoke_attempts=1,
                sleep=lambda _: None,
            )


class ProdSmokeIntegrationTest(unittest.TestCase):
    def test_healthy_transport_report_is_accepted(self) -> None:
        provider = InProcessSmokeProvider(healthy_fake(), window_seconds=300, now=lambda: NOW)

        outcome = provider.smoke("center")

        self.assertTrue(outcome.ok, outcome.failures)
        self.assertEqual(outcome.report["role"], "center")
        self.assertEqual(outcome.report["failures"], [])

    def test_crash_loop_report_stops_deploy_with_evidence(self) -> None:
        fake = healthy_fake()
        fake.inspect[("rumap-center", "rumap-center-rumap-center-1")] = inspect_ok(
            restarts=3, started="2026-11-25T16:44:00Z"
        )
        provider = InProcessSmokeProvider(fake, window_seconds=300, now=lambda: NOW)
        deployer = FakeDeployer()
        deployer.smoke = provider.smoke  # type: ignore[method-assign]

        report = run_deploy(MAIN_SHA, deployer, smoke_attempts=1, sleep=lambda _: None)

        self.assertFalse(report.ok)
        self.assertEqual(report.failed_layer, "center")
        self.assertIn("RestartCount=3", " ".join(report.steps[-1].evidence))
        self.assertNotIn(("prune", "center"), deployer.calls)

    def test_prod_smoke_json_contract_roundtrip(self) -> None:
        out = io.StringIO()
        code = prod_smoke_main(
            ["--role", "point", "--format", "json"],
            transport=healthy_fake(),
            now=lambda: NOW,
            stdout=out,
        )
        report = json.loads(out.getvalue())

        self.assertEqual(code, 0)
        outcome = SmokeOutcome(role="point", ok=report["ok"], report=report)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.failures, ())


class FileLockStoreTest(unittest.TestCase):
    def test_locks_are_idempotent_and_releasable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileLockStore(Path(tmp), pause_file=Path(tmp) / "paused")
            self.assertTrue(store.acquire(REPO, "merge-pr:1", PR_HEAD))
            self.assertFalse(store.acquire(REPO, "merge-pr:1", PR_HEAD))
            self.assertTrue(store.contains(REPO, "merge-pr:1", PR_HEAD))
            self.assertTrue(store.acquire(REPO, "merge-pr:1", "ff" * 20))
            store.release(REPO, "merge-pr:1", PR_HEAD)
            self.assertFalse(store.contains(REPO, "merge-pr:1", PR_HEAD))
            self.assertFalse(store.paused())

    def test_pause_file_and_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pause = Path(tmp) / "paused"
            store = FileLockStore(Path(tmp) / "state", pause_file=pause)
            self.assertIsNone(store.deployed_sha(REPO))
            store.mark_deployed(REPO, MAIN_SHA)
            self.assertEqual(store.deployed_sha(REPO), MAIN_SHA)
            pause.write_text("", encoding="utf-8")
            self.assertTrue(store.paused())


class GitHubAdapterTest(unittest.TestCase):
    def test_summarize_checks_filters_workflow_and_ignores_foreign_checks(self) -> None:
        rollup = [
            {"workflowName": "Проверка", "name": "Тесты", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {
                "workflowName": "Проверка",
                "name": "Линт",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "detailsUrl": RUN_URL,
            },
            {"workflowName": "Проверка", "name": "Сборка", "status": "IN_PROGRESS", "conclusion": ""},
            {"workflowName": "Release Train Trigger", "name": "notify", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"workflowName": "PR Review Gate Trigger", "name": "notify", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"context": "legacy", "state": "PENDING"},
            {"workflowName": "Проверка", "name": "Шум", "status": "COMPLETED", "conclusion": "SKIPPED"},
        ]

        summary = summarize_checks(rollup)

        self.assertEqual(summary.state, "failed")
        self.assertEqual([check.name for check in summary.failed], ["Линт"])
        self.assertEqual(summary.failed[0].url, RUN_URL)
        self.assertEqual({check.name for check in summary.pending}, {"Сборка"})
        self.assertEqual(summary.passed, 1)
        self.assertEqual(summary.skipped, 1)

        only_trigger = [
            {"workflowName": "Release Train Trigger", "name": "notify", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ]
        self.assertEqual(summarize_checks(only_trigger).state, "none")

    def test_summarize_checks_deduplicates_reruns_keeping_latest(self) -> None:
        rerun_green = [
            {
                "workflowName": "Проверка",
                "name": "Тесты (center)",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-11-25T10:10:00Z",
                "completedAt": "2026-11-25T10:15:00Z",
            },
            {
                "workflowName": "Проверка",
                "name": "Тесты (center)",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "startedAt": "2026-11-25T10:00:00Z",
                "completedAt": "2026-11-25T10:05:00Z",
                "detailsUrl": RUN_URL,
            },
        ]
        summary_green = summarize_checks(rerun_green)
        self.assertEqual(summary_green.state, "passed")
        self.assertEqual(summary_green.passed, 1)
        self.assertEqual(summary_green.failed, ())

        rerun_in_progress = [
            {
                "workflowName": "Проверка",
                "name": "Тесты (center)",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "startedAt": "2026-11-25T10:00:00Z",
                "completedAt": "2026-11-25T10:05:00Z",
            },
            {
                "workflowName": "Проверка",
                "name": "Тесты (center)",
                "status": "IN_PROGRESS",
                "conclusion": "",
                "startedAt": "2026-11-25T10:10:00Z",
                "completedAt": None,
            },
        ]
        summary_pending = summarize_checks(rerun_in_progress)
        self.assertEqual(summary_pending.state, "pending")
        self.assertEqual(summary_pending.failed, ())

    def test_summarize_checks_prefers_executed_check_over_later_skipped_duplicate(self) -> None:
        skipped_over_success = [
            {
                "workflowName": "Проверка",
                "name": "Задетые части",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-11-25T10:00:00Z",
                "completedAt": "2026-11-25T10:01:00Z",
            },
            {
                "workflowName": "Проверка",
                "name": "Задетые части",
                "status": "COMPLETED",
                "conclusion": "SKIPPED",
                "startedAt": "2026-11-25T10:02:00Z",
                "completedAt": "2026-11-25T10:02:05Z",
            },
        ]
        summary_ok = summarize_checks(skipped_over_success)
        self.assertEqual(summary_ok.state, "passed")
        self.assertEqual(summary_ok.passed, 1)
        self.assertEqual(summary_ok.skipped, 0)

        skipped_over_failure = [
            {
                "workflowName": "Проверка",
                "name": "Задетые части",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "startedAt": "2026-11-25T10:00:00Z",
                "completedAt": "2026-11-25T10:01:00Z",
                "detailsUrl": RUN_URL,
            },
            {
                "workflowName": "Проверка",
                "name": "Задетые части",
                "status": "COMPLETED",
                "conclusion": "SKIPPED",
                "startedAt": "2026-11-25T10:02:00Z",
                "completedAt": "2026-11-25T10:02:05Z",
            },
        ]
        summary_fail = summarize_checks(skipped_over_failure)
        self.assertEqual(summary_fail.state, "failed")
        self.assertEqual([check.name for check in summary_fail.failed], ["Задетые части"])
        self.assertEqual(summary_fail.skipped, 0)

    def test_pull_request_parsing_and_changes_requested(self) -> None:
        item = {
            "number": 12,
            "title": "fix",
            "url": "https://github.com/rumap/rumap/pull/12",
            "baseRefName": "dev",
            "headRefName": "fix/x",
            "headRefOid": PR_HEAD,
            "isDraft": False,
            "labels": [{"name": "release-train"}],
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [
                {"workflowName": "Проверка", "name": "Тесты", "status": "COMPLETED", "conclusion": "SUCCESS"}
            ],
            "reviews": [
                {"author": {"login": "reviewer"}, "state": "APPROVED"},
                {"author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED"},
            ],
            "body": "тело",
            "author": {"login": "fus1ond"},
        }

        pr = to_pull_request(item)

        self.assertTrue(pr.has_label(RELEASE_TRAIN_LABEL))
        self.assertTrue(pr.changes_requested)
        self.assertEqual(pr.blocked_reason(), "changes_requested")
        self.assertEqual(pr.head_sha, PR_HEAD)

    def test_gh_provider_parses_runs_variable_and_compare(self) -> None:
        release_pr_payload = {
            "number": 90,
            "title": "release-train: dev → main",
            "url": "https://github.com/rumap/rumap/pull/90",
            "baseRefName": "main",
            "headRefName": "dev",
            "headRefOid": DEV_SHA,
            "isDraft": False,
            "labels": [{"name": "release-train"}],
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [],
            "reviews": [],
            "author": {"login": "bot"},
            "body": "",
        }
        responses = {
            ("gh", "pr", "list", "--repo", REPO, "--base", "dev", "--state", "open", "--limit", "100", "--json", PR_FIELDS): [],
            (
                "gh",
                "pr",
                "list",
                "--repo",
                REPO,
                "--base",
                "main",
                "--head",
                "dev",
                "--state",
                "open",
                "--limit",
                "1",
                "--json",
                PR_FIELDS,
            ): [release_pr_payload],
            ("gh", "api", f"repos/{REPO}/actions/workflows/ci.yml/runs?branch=dev&event=push&per_page=20"): {
                "workflow_runs": [
                    {
                        "name": "Проверка",
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": RUN_URL,
                        "head_sha": DEV_SHA,
                        "created_at": "2026-11-25T10:00:00Z",
                        "head_commit": {"message": "feat: один (#55)\n\nтело"},
                        "pull_requests": [{"number": 55}],
                    },
                    {
                        "name": "Проверка",
                        "status": "in_progress",
                        "conclusion": None,
                        "html_url": RUN_URL,
                        "head_sha": DEV_SHA,
                        "created_at": "2026-11-25T09:00:00Z",
                    },
                ]
            },
            ("gh", "api", f"repos/{REPO}/commits/{DEV_SHA}/pulls?per_page=5"): [{"number": 55}],
            ("gh", "api", f"repos/{REPO}/compare/main...dev"): {
                "ahead_by": 2,
                "behind_by": 0,
                "commits": [
                    {"commit": {"message": "feat: один (#55)"}},
                    {"commit": {"message": "fix: два (#56)\n\nтело"}},
                ],
            },
        }

        seen: list[tuple[str, ...]] = []

        def runner(argv: Sequence[str]):
            class Done:
                def __init__(self, text: str) -> None:
                    self.returncode = 0
                    self.stdout = text
                    self.stderr = ""

            key = tuple(argv)
            seen.append(key)
            return Done(json.dumps(responses.get(key, [])))

        provider = GhCliProvider(run=runner)
        ci = provider.branch_ci(REPO, "dev")
        compare = provider.compare(REPO, "main", "dev")
        rel = provider.release_pr(REPO, base="main", head="dev")
        pr_num = provider.commit_pr(REPO, DEV_SHA)

        self.assertTrue(all(call[0] == "gh" for call in seen))
        self.assertEqual(ci.state, "pending")
        self.assertEqual(ci.latest_completed.commit_message, "feat: один (#55)")
        self.assertEqual(ci.latest_completed.pr_number, 55)
        self.assertEqual(ci.active[0].status, "in_progress")
        self.assertEqual(compare.ahead_by, 2)
        self.assertEqual(compare.pull_requests, (55, 56))
        self.assertEqual(provider.open_prs(REPO, "dev"), ())
        self.assertIsNotNone(rel)
        self.assertEqual(rel.number, 90)
        self.assertEqual(pr_num, 55)

    def test_gh_provider_wraps_timeout_and_os_error(self) -> None:
        import subprocess as _sp

        def raise_timeout(argv: Sequence[str]):
            raise _sp.TimeoutExpired(argv, 30)

        def raise_os_error(argv: Sequence[str]):
            raise FileNotFoundError("gh not found")

        with self.assertRaises(ProviderError) as ctx_t:
            GhCliProvider(run=raise_timeout).open_prs(REPO, "dev")
        self.assertIn("таймаут", str(ctx_t.exception))

        with self.assertRaises(ProviderError) as ctx_o:
            GhCliProvider(run=raise_os_error).automation_paused(REPO)
        self.assertIn("gh not found", str(ctx_o.exception))

    def test_automation_pause_variable_is_fail_safe(self) -> None:
        class Done:
            def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
                self.stdout = stdout
                self.returncode = returncode
                self.stderr = stderr

        cases = {
            "true\n": True,
            "1\n": True,
            "paused\n": True,
            "что-то незнакомое\n": True,
            "false\n": False,
            "0\n": False,
            "\n": False,
        }
        for stdout, expected in cases.items():
            with self.subTest(stdout=stdout):
                provider = GhCliProvider(run=lambda argv, out=stdout: Done(out))
                self.assertEqual(provider.automation_paused(REPO), expected)

        missing = GhCliProvider(
            run=lambda argv: Done("", 1, "variable RELEASE_TRAIN_PAUSED not found")
        )
        self.assertFalse(missing.automation_paused(REPO))

        broken = GhCliProvider(run=lambda argv: Done("", 1, "HTTP 401: Bad credentials"))
        with self.assertRaises(ProviderError):
            broken.automation_paused(REPO)

    def test_open_release_pr_uses_created_url(self) -> None:
        calls: list[tuple[str, ...]] = []
        item = {
            "number": 77,
            "title": "release-train: dev → main",
            "url": "https://github.com/rumap/rumap/pull/77",
            "baseRefName": "main",
            "headRefName": "dev",
            "headRefOid": DEV_SHA,
            "isDraft": False,
            "labels": [{"name": "release-train"}],
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [],
            "reviews": [],
            "author": {"login": "bot"},
            "body": "батч",
        }

        class Done:
            def __init__(self, stdout: str) -> None:
                self.stdout = stdout
                self.returncode = 0
                self.stderr = ""

        def runner(argv: Sequence[str]):
            calls.append(tuple(argv))
            if tuple(argv[:3]) == ("gh", "pr", "create"):
                return Done("https://github.com/rumap/rumap/pull/77\n")
            if tuple(argv[:3]) == ("gh", "pr", "view"):
                return Done(json.dumps(item))
            return Done("[]")

        pr = GhCliProvider(run=runner).open_release_pr(
            REPO,
            base="main",
            head="dev",
            title="release-train: dev → main",
            body="батч",
            labels=(RELEASE_TRAIN_LABEL,),
        )

        self.assertEqual(pr.number, 77)
        self.assertTrue(pr.has_label(RELEASE_TRAIN_LABEL))
        create = next(call for call in calls if call[:3] == ("gh", "pr", "create"))
        self.assertEqual(create[create.index("--label") + 1], RELEASE_TRAIN_LABEL)

    def test_default_targets_commands_expand_home_and_quote_custom_paths(self) -> None:
        from release_train.deploy import LAYER_ORDER, default_targets

        targets = default_targets()
        self.assertEqual(LAYER_ORDER, ("point", "worker", "center"))
        self.assertEqual([target.node for target in targets], ["point", "worker-ru", "worker-control", "center"])

        by_node = {target.node: target for target in targets}
        self.assertEqual(
            by_node["center"].command(),
            "cd /opt/rumap-center && docker compose pull rumap-center && docker compose up -d rumap-center",
        )
        self.assertTrue(by_node["center"].prune)
        self.assertEqual(
            by_node["center"].prune_command(),
            "cd /opt/rumap-center && docker image prune -f",
        )
        self.assertNotIn("image prune", by_node["center"].command())
        self.assertTrue(by_node["worker-ru"].command().startswith('cd "$HOME"/rumap-v2/deploy/worker && '))
        self.assertTrue(by_node["point"].command().startswith('cd "$HOME"/rumap-v2/deploy/point && '))
        self.assertNotIn("'$HOME", by_node["worker-ru"].command())
        self.assertNotIn("'$HOME", by_node["point"].command())

        custom = DeployTarget(
            layer="center",
            node="custom",
            host="host",
            directory="/opt/my app; rm -rf /",
        )
        self.assertTrue(custom.command().startswith("cd '/opt/my app; rm -rf /' && "))


class CliTest(unittest.TestCase):
    def test_scan_json_contract(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["dev"] = BranchCi("dev", (run_ok(DEV_SHA),))
        github.prs["dev"] = [make_pr(91), make_pr(92, labels=())]
        out = io.StringIO()

        code = main(["scan"], provider=github, locks=locks, stdout=out)

        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["recommended_action"], "merged_to_dev")
        self.assertEqual([pr["number"] for pr in payload["pull_requests"]], [91])
        self.assertEqual(payload["ignored_pull_requests"][0]["number"], 92)
        self.assertIn("dev_ci", payload)
        self.assertIn("main_ci", payload)
        self.assertIn("pause", payload)

    def test_step_json_has_thread_contract(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.prs["dev"] = [make_pr(93)]
        out = io.StringIO()

        code = main(["step"], provider=github, locks=locks, stdout=out)

        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(
            set(payload) >= {"ok", "action", "thread_action", "summary", "details"},
            True,
        )
        self.assertIn(payload["action"], {"merged_to_dev", "noop"})
        self.assertIn(payload["thread_action"], {SETTLE, WAIT_DEPLOY})

    def test_deploy_cli_exit_codes_and_text(self) -> None:
        github, locks = FakeGitHub(), FakeLocks()
        github.ci["main"] = BranchCi("main", (run_ok(MAIN_SHA),))
        out = io.StringIO()
        code = main(
            ["deploy", "--format", "both", "--smoke-attempts", "1"],
            provider=github,
            locks=locks,
            deployer=FakeDeployer(),
            stdout=out,
        )
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("release-train deploy: OK", text)
        payload = json.loads(text.split("--- JSON ---", 1)[1])
        self.assertEqual(payload["action"], "deployed")
        self.assertEqual(payload["thread_action"], SETTLE)
        self.assertIn("release-train deploy: OK", payload["text"])

        github_fail = FakeGitHub()
        github_fail.ci["main"] = BranchCi("main", (run_ok(MAIN_SHA),))
        out_fail = io.StringIO()
        code_fail = main(
            ["deploy", "--format", "text", "--smoke-attempts", "1"],
            provider=github_fail,
            locks=FakeLocks(),
            deployer=FakeDeployer(fail_smoke={"point"}),
            stdout=out_fail,
        )
        self.assertEqual(code_fail, 1)
        self.assertIn("release-train deploy: FAIL", out_fail.getvalue())

    def test_deploy_cli_paused_exit_code(self) -> None:
        github = FakeGitHub()
        github.paused = True
        out = io.StringIO()

        code = main(
            ["deploy", "--format", "json"],
            provider=github,
            locks=FakeLocks(),
            deployer=FakeDeployer(),
            stdout=out,
        )

        payload = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["action"], "paused")

    def test_github_failure_is_json_error_not_traceback(self) -> None:
        class BrokenGitHub(FakeGitHub):
            def branch_head(self, repo: str, branch: str) -> str:
                raise ProviderError("gh: 502 Bad Gateway")

        out, err = io.StringIO(), io.StringIO()

        code = main(["scan"], provider=BrokenGitHub(), locks=FakeLocks(), stdout=out, stderr=err)

        payload = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["action"], "error")
        self.assertEqual(payload["thread_action"], ALERT_OPEN)
        self.assertIn("502", err.getvalue())


class DecisionContractTest(unittest.TestCase):
    def test_ready_to_deploy_never_settles(self) -> None:
        state = state_with(
            main_ci=BranchCi("main", (run_ok(MAIN_SHA),)),
            compare=Comparison(),
            deployed_sha=OLDER_SHA,
            deploy_compare=Comparison(ahead_by=1, commits=("feat (#1)",), pull_requests=(1,)),
        )

        decision = evaluate(state)

        self.assertEqual(decision.action, "ready_to_deploy")
        self.assertEqual(decision.thread_action, WAIT_DEPLOY)
        self.assertNotEqual(decision.thread_action, SETTLE)

    def test_release_pr_body_marks_head(self) -> None:
        state = state_with(compare=Comparison(ahead_by=1, commits=("feat: один (#7)",), pull_requests=(7,)))

        body = release_pr_body(state)

        self.assertIn("feat: один (#7)", body)
        self.assertIn("#7", body)
        self.assertIn(f"<!-- release-train:head:{DEV_SHA} -->", body)


if __name__ == "__main__":
    unittest.main()
