"""Модель `release-train`: состояние поезда, решение шага и переходы.

Состояние собирается один раз за вызов (`scan`/`step`) и дальше только читается:
решение принимается чистой функцией `evaluate`, поэтому «что сделал бы поезд»
и «что он сделал» не расходятся.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

RELEASE_TRAIN_LABEL = "release-train"
HOLD_LABEL = "hold"

SETTLE = "settle"
WAIT_DEPLOY = "wait_deploy"
ALERT_OPEN = "alert_open"

FAILED_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "action_required", "error", "startup_failure"}
)
MUTATING_ACTIONS = frozenset(
    {"merged_to_dev", "opened_release_pr", "merged_release_pr"}
)


@dataclass(frozen=True)
class CheckRef:
    name: str
    url: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "url": self.url}


@dataclass(frozen=True)
class CheckSummary:
    state: str = "none"
    passed: int = 0
    skipped: int = 0
    pending: tuple[CheckRef, ...] = ()
    failed: tuple[CheckRef, ...] = ()

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "passed": self.passed,
            "skipped": self.skipped,
            "pending": [check.to_dict() for check in self.pending],
            "failed": [check.to_dict() for check in self.failed],
        }


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    url: str = ""
    base_ref: str = ""
    head_ref: str = ""
    head_sha: str = ""
    author: str = ""
    labels: tuple[str, ...] = ()
    is_draft: bool = False
    mergeable: str = "UNKNOWN"
    checks: CheckSummary = CheckSummary()
    changes_requested: bool = False
    body: str = ""

    def has_label(self, label: str) -> bool:
        target = label.lower()
        return any(item.lower() == target for item in self.labels)

    def blocked_reason(self) -> str | None:
        """Почему PR нельзя вливать автоматически; `None` — готов."""
        if self.has_label(HOLD_LABEL):
            return "hold"
        if self.is_draft:
            return "draft"
        if self.mergeable == "CONFLICTING":
            return "conflict"
        if self.changes_requested:
            return "changes_requested"
        if self.checks.state == "failed":
            return "ci_failed"
        if self.checks.state == "pending":
            return "ci_pending"
        if self.checks.state != "passed":
            return "no_ci"
        if self.mergeable != "MERGEABLE":
            return "not_mergeable"
        return None

    def to_dict(self) -> dict:
        blocked = self.blocked_reason()
        return {
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "head_sha": self.head_sha,
            "author": self.author,
            "labels": list(self.labels),
            "draft": self.is_draft,
            "mergeable": self.mergeable,
            "changes_requested": self.changes_requested,
            "checks": self.checks.to_dict(),
            "ready": blocked is None,
            "blocked": blocked,
        }


@dataclass(frozen=True)
class CiRun:
    workflow: str = ""
    status: str = ""
    conclusion: str = ""
    url: str = ""
    sha: str = ""
    branch: str = ""
    pr_number: int | None = None
    created_at: str = ""
    commit_message: str = ""

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict:
        return {
            "workflow": self.workflow,
            "status": self.status,
            "conclusion": self.conclusion,
            "url": self.url,
            "sha": self.sha,
            "branch": self.branch,
            "pr_number": self.pr_number,
            "created_at": self.created_at,
            "commit_message": self.commit_message,
        }


@dataclass(frozen=True)
class BranchCi:
    """Прогоны ветки, новые первыми: активные отдельно от последнего завершённого."""

    branch: str
    runs: tuple[CiRun, ...] = ()

    @property
    def active(self) -> tuple[CiRun, ...]:
        return tuple(run for run in self.runs if not run.completed)

    @property
    def latest_completed(self) -> CiRun | None:
        for run in self.runs:
            if run.completed:
                return run
        return None

    @property
    def state(self) -> str:
        if self.active:
            return "pending"
        latest = self.latest_completed
        if latest is None:
            return "none"
        if latest.conclusion in FAILED_CONCLUSIONS:
            return "failed"
        if latest.conclusion == "success":
            return "passed"
        return "none"

    def failed_run(self, sha: str | None = None) -> CiRun | None:
        if self.state != "failed":
            return None
        latest = self.latest_completed
        if latest is None:
            return None
        if sha and latest.sha and latest.sha != sha:
            return None
        return latest

    def to_dict(self) -> dict:
        return {
            "branch": self.branch,
            "state": self.state,
            "active": [run.to_dict() for run in self.active],
            "latest": self.latest_completed.to_dict() if self.latest_completed else None,
        }


@dataclass(frozen=True)
class Comparison:
    ahead_by: int = 0
    behind_by: int = 0
    commits: tuple[str, ...] = ()
    pull_requests: tuple[int, ...] = ()

    def to_dict(self) -> dict:
        return {
            "ahead_by": self.ahead_by,
            "behind_by": self.behind_by,
            "commits": list(self.commits),
            "pull_requests": list(self.pull_requests),
        }


@dataclass(frozen=True)
class TrainState:
    repo: str
    main_sha: str = ""
    dev_sha: str = ""
    dev_prs: tuple[PullRequest, ...] = ()
    release_pr: PullRequest | None = None
    dev_ci: BranchCi = BranchCi("dev")
    main_ci: BranchCi = BranchCi("main")
    compare: Comparison = Comparison()
    deploy_compare: Comparison | None = None
    paused_repo: bool = False
    paused_file: bool = False
    deployed_sha: str | None = None
    base_branch: str = "main"
    head_branch: str = "dev"

    @property
    def paused(self) -> bool:
        return self.paused_repo or self.paused_file

    @property
    def pause_source(self) -> str | None:
        if self.paused_repo:
            return "repo_variable:RELEASE_TRAIN_PAUSED"
        if self.paused_file:
            return "pause_file"
        return None

    @property
    def release_train_prs(self) -> tuple[PullRequest, ...]:
        selected = [pr for pr in self.dev_prs if pr.has_label(RELEASE_TRAIN_LABEL)]
        return tuple(sorted(selected, key=lambda pr: pr.number))

    @property
    def ignored_prs(self) -> tuple[PullRequest, ...]:
        return tuple(pr for pr in self.dev_prs if not pr.has_label(RELEASE_TRAIN_LABEL))

    @property
    def ready_prs(self) -> tuple[PullRequest, ...]:
        return tuple(pr for pr in self.release_train_prs if pr.blocked_reason() is None)

    @property
    def nothing_to_deploy(self) -> bool:
        return bool(self.main_sha) and self.main_sha == self.deployed_sha

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "base_branch": self.base_branch,
            "head_branch": self.head_branch,
            "main_sha": self.main_sha,
            "dev_sha": self.dev_sha,
            "dev_ahead_by": self.compare.ahead_by,
            "compare": self.compare.to_dict(),
            "deploy_compare": self.deploy_compare.to_dict() if self.deploy_compare else None,
            "pull_requests": [pr.to_dict() for pr in self.release_train_prs],
            "ignored_pull_requests": [
                {"number": pr.number, "title": pr.title, "reason": "no_label"}
                for pr in self.ignored_prs
            ],
            "release_pr": self.release_pr.to_dict() if self.release_pr else None,
            "dev_ci": self.dev_ci.to_dict(),
            "main_ci": self.main_ci.to_dict(),
            "pause": {"active": self.paused, "source": self.pause_source},
            "deployed_sha": self.deployed_sha,
        }


@dataclass(frozen=True)
class Decision:
    action: str
    thread_action: str = SETTLE
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    pr: PullRequest | None = None
    run: CiRun | None = None
    comment_target: int | None = None
    comment_body: str = ""
    lock_key: str | None = None
    lock_sha: str = ""

    @property
    def mutating(self) -> bool:
        if self.action == "ci_failed":
            return self.comment_target is not None
        return self.action in MUTATING_ACTIONS

    def to_dict(self) -> dict:
        details = dict(self.details)
        if self.pr is not None:
            details.setdefault("pull_request", self.pr.to_dict())
        if self.run is not None:
            details.setdefault("run", self.run.to_dict())
        if self.action == "ci_failed":
            details.setdefault("comment_target", self.comment_target)
            details.setdefault("comment_posted", self.comment_target is not None)
            details.setdefault(
                "comment", "posted" if self.comment_target is not None else "no_target"
            )
        return {
            "ok": True,
            "action": self.action,
            "thread_action": self.thread_action,
            "summary": self.summary,
            "details": details,
        }


def short_sha(sha: str | None) -> str:
    return (sha or "")[:7]
