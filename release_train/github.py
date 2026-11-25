"""Единственный шов к GitHub: протокол провайдера и боевой адаптер `gh` CLI."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Callable, Protocol, Sequence

from .model import (
    BranchCi,
    CheckRef,
    CheckSummary,
    CiRun,
    Comparison,
    PullRequest,
)

PR_FIELDS = (
    "number,title,url,baseRefName,headRefName,headRefOid,isDraft,labels,"
    "mergeable,statusCheckRollup,reviews,author,body"
)
PAUSE_VARIABLE = "RELEASE_TRAIN_PAUSED"
PENDING_STATES = frozenset({"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "EXPECTED"})
FAILED_CONCLUSIONS = frozenset({"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "ERROR", "STARTUP_FAILURE"})
SKIPPED_CONCLUSIONS = frozenset({"SKIPPED", "NEUTRAL", "CANCELLED"})
RESUME_VALUES = frozenset({"", "0", "false", "no", "off"})
PR_NUMBER_RE = re.compile(r"\(#(\d+)\)\s*$")
PR_URL_RE = re.compile(r"/pull/(\d+)")


class ProviderError(RuntimeError):
    """GitHub недоступен или ответил не по контракту: шаг не должен делать вид, что всё ок."""


class GitHubProvider(Protocol):
    def open_prs(self, repo: str, base: str) -> tuple[PullRequest, ...]: ...

    def release_pr(self, repo: str, base: str = "main", head: str = "dev") -> PullRequest | None: ...

    def branch_ci(self, repo: str, branch: str) -> BranchCi: ...

    def branch_head(self, repo: str, branch: str) -> str: ...

    def compare(self, repo: str, base: str, head: str) -> Comparison: ...

    def automation_paused(self, repo: str) -> bool: ...

    def merge_pr(
        self,
        repo: str,
        number: int,
        sha: str,
        *,
        delete_branch: bool = False,
        method: str = "squash",
    ) -> None: ...

    def open_release_pr(
        self,
        repo: str,
        *,
        base: str,
        head: str,
        title: str,
        body: str,
        labels: Sequence[str],
    ) -> PullRequest: ...

    def update_release_pr(self, repo: str, number: int, *, body: str) -> None: ...

    def comment_pr(self, repo: str, number: int, body: str) -> None: ...


def summarize_checks(rollup: Sequence[dict] | None) -> CheckSummary:
    """Сводит `statusCheckRollup` к одному состоянию и списку упавших проверок."""
    pending: list[CheckRef] = []
    failed: list[CheckRef] = []
    passed = 0
    skipped = 0
    for item in rollup or ():
        name = item.get("name") or item.get("context") or "unknown"
        url = item.get("detailsUrl") or item.get("targetUrl") or ""
        status = (item.get("status") or "").upper()
        conclusion = (item.get("conclusion") or item.get("state") or "").upper()
        if status in PENDING_STATES or conclusion in PENDING_STATES or (not conclusion and status != "COMPLETED"):
            pending.append(CheckRef(name, url))
        elif conclusion in FAILED_CONCLUSIONS:
            failed.append(CheckRef(name, url))
        elif conclusion in SKIPPED_CONCLUSIONS:
            skipped += 1
        else:
            passed += 1
    if failed:
        state = "failed"
    elif pending:
        state = "pending"
    elif passed:
        state = "passed"
    else:
        state = "none"
    return CheckSummary(
        state=state,
        passed=passed,
        skipped=skipped,
        pending=tuple(pending),
        failed=tuple(failed),
    )


def has_other_changes_requested(reviews: Sequence[dict] | None) -> bool:
    """Активный `CHANGES_REQUESTED` любого ревьюера (по последнему отзыву автора)."""
    latest: dict[str, str] = {}
    for review in reviews or ():
        author = ((review.get("author") or {}).get("login") or "").lower()
        state = (review.get("state") or "").upper()
        if not author or state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        latest[author] = state
    return any(state == "CHANGES_REQUESTED" for state in latest.values())


def to_pull_request(item: dict) -> PullRequest:
    labels = tuple(
        label.get("name", "") if isinstance(label, dict) else str(label)
        for label in (item.get("labels") or ())
    )
    return PullRequest(
        number=int(item["number"]),
        title=item.get("title") or "",
        url=item.get("url") or "",
        base_ref=item.get("baseRefName") or "",
        head_ref=item.get("headRefName") or "",
        head_sha=item.get("headRefOid") or "",
        author=((item.get("author") or {}).get("login") or ""),
        labels=labels,
        is_draft=bool(item.get("isDraft")),
        mergeable=(item.get("mergeable") or "UNKNOWN").upper(),
        checks=summarize_checks(item.get("statusCheckRollup")),
        changes_requested=has_other_changes_requested(item.get("reviews")),
        body=item.get("body") or "",
    )


def _runner(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), text=True, capture_output=True)


class GhCliProvider:
    """Боевой провайдер: только `gh`, без прямых HTTP-вызовов и без локальных git-состояний.

    Прогоны читаются по файлу workflow: иначе «последним прогоном main» окажется любой
    триггерный workflow (например, `pr-review-trigger`), а не CI.
    """

    def __init__(
        self,
        run: Callable[[Sequence[str]], subprocess.CompletedProcess] | None = None,
        *,
        workflow_file: str = "ci.yml",
    ) -> None:
        self._run = run or _runner
        self.workflow_file = workflow_file

    def _ok(self, argv: Sequence[str]) -> str:
        done = self._run(argv)
        if done.returncode != 0:
            reason = (done.stderr or done.stdout or "").strip().splitlines()
            detail = reason[0] if reason else f"код {done.returncode}"
            command = argv[1] if len(argv) > 1 else ""
            raise ProviderError(f"gh {command}: {detail}")
        return done.stdout

    def _json(self, argv: Sequence[str]):
        text = self._ok(argv).strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"gh вернул не JSON: {exc}") from exc

    def open_prs(self, repo: str, base: str) -> tuple[PullRequest, ...]:
        data = self._json(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--base",
                base,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                PR_FIELDS,
            ]
        )
        return tuple(to_pull_request(item) for item in data or ())

    def release_pr(self, repo: str, base: str = "main", head: str = "dev") -> PullRequest | None:
        for pr in self.open_prs(repo, base):
            if pr.head_ref == head:
                return pr
        return None

    def branch_ci(self, repo: str, branch: str) -> BranchCi:
        data = self._json(
            [
                "api",
                f"repos/{repo}/actions/workflows/{self.workflow_file}/runs"
                f"?branch={branch}&per_page=20",
            ]
        ) or {}
        runs = []
        for item in data.get("workflow_runs") or ():
            head_commit = item.get("head_commit") or {}
            message = (head_commit.get("message") or "").strip()
            pull_requests = item.get("pull_requests") or []
            runs.append(
                CiRun(
                    workflow=item.get("name") or "",
                    status=(item.get("status") or "").lower(),
                    conclusion=(item.get("conclusion") or "").lower(),
                    url=item.get("html_url") or "",
                    sha=item.get("head_sha") or "",
                    branch=branch,
                    pr_number=(pull_requests[0].get("number") if pull_requests else None),
                    created_at=item.get("created_at") or "",
                    commit_message=message.splitlines()[0] if message else "",
                )
            )
        runs.sort(key=lambda run: run.created_at, reverse=True)
        return BranchCi(branch=branch, runs=tuple(runs))

    def branch_head(self, repo: str, branch: str) -> str:
        return self._ok(["api", f"repos/{repo}/commits/{branch}", "--jq", ".sha"]).strip()

    def compare(self, repo: str, base: str, head: str) -> Comparison:
        data = self._json(["api", f"repos/{repo}/compare/{base}...{head}"]) or {}
        commits: list[str] = []
        pull_requests: list[int] = []
        for item in data.get("commits") or ():
            message = ((item.get("commit") or {}).get("message") or "").strip()
            if not message:
                continue
            subject = message.splitlines()[0]
            commits.append(subject)
            match = PR_NUMBER_RE.search(subject)
            if match and int(match.group(1)) not in pull_requests:
                pull_requests.append(int(match.group(1)))
        return Comparison(
            ahead_by=int(data.get("ahead_by") or 0),
            behind_by=int(data.get("behind_by") or 0),
            commits=tuple(commits),
            pull_requests=tuple(pull_requests),
        )

    def automation_paused(self, repo: str) -> bool:
        done = self._run(["variable", "get", PAUSE_VARIABLE, "--repo", repo])
        if done.returncode != 0:
            reason = (done.stderr or "").lower()
            if "not found" in reason or "could not resolve" in reason:
                return False
            raise ProviderError(f"gh variable get {PAUSE_VARIABLE}: {(done.stderr or '').strip() or 'код ненулевой'}")
        value = done.stdout.strip().lower()
        # Незнакомое значение считаем паузой: стоп-кран обязан срабатывать, а не гадать.
        return value not in RESUME_VALUES

    def merge_pr(
        self,
        repo: str,
        number: int,
        sha: str,
        *,
        delete_branch: bool = False,
        method: str = "squash",
    ) -> None:
        argv = [
            "pr",
            "merge",
            str(number),
            "--repo",
            repo,
            f"--{method}",
            "--match-head-commit",
            sha,
        ]
        if delete_branch:
            argv.append("--delete-branch")
        self._ok(argv)

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
        argv = [
            "pr",
            "create",
            "--repo",
            repo,
            "--base",
            base,
            "--head",
            head,
            "--title",
            title,
            "--body",
            body,
        ]
        for label in labels:
            argv.extend(["--label", label])
        output = self._ok(argv)
        match = PR_URL_RE.search(output)
        if match:
            number = int(match.group(1))
            data = self._json(["pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS])
            if data:
                return to_pull_request(data)
        created = self.release_pr(repo, base=base, head=head)
        if created is None:
            raise ProviderError("release PR создан, но не найден в списке открытых")
        return created

    def update_release_pr(self, repo: str, number: int, *, body: str) -> None:
        self._ok(["pr", "edit", str(number), "--repo", repo, "--body", body])

    def comment_pr(self, repo: str, number: int, body: str) -> None:
        self._ok(["pr", "comment", str(number), "--repo", repo, "--body", body])
