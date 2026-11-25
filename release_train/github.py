"""Единственный шов к GitHub: протокол провайдера и боевой адаптер `gh` CLI."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Callable, Protocol, Sequence
from urllib.parse import quote

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
WORKFLOW_NAME = "Проверка"
PENDING_STATES = frozenset({"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "EXPECTED"})
FAILED_CONCLUSIONS = frozenset({"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "ERROR", "STARTUP_FAILURE"})
SKIPPED_CONCLUSIONS = frozenset({"SKIPPED", "NEUTRAL", "CANCELLED"})
RESUME_VALUES = frozenset({"", "0", "false", "no", "off"})
PR_NUMBER_RE = re.compile(r"\(#(\d+)\)\s*$")
MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+)\b")
PR_URL_RE = re.compile(r"/pull/(\d+)")


class ProviderError(RuntimeError):
    """GitHub недоступен или ответил не по контракту: шаг не должен делать вид, что всё ок."""


class GitHubProvider(Protocol):
    def open_prs(self, repo: str, base: str) -> tuple[PullRequest, ...]: ...

    def release_pr(self, repo: str, base: str = "main", head: str = "dev") -> PullRequest | None: ...

    def branch_ci(self, repo: str, branch: str) -> BranchCi: ...

    def branch_head(self, repo: str, branch: str) -> str: ...

    def commit_pr(self, repo: str, sha: str) -> int | None: ...

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


def extract_pr_number(message: str | None) -> int | None:
    """Извлекает номер PR из заголовка merge- или squash-коммита."""
    text = (message or "").strip()
    if not text:
        return None
    subject = text.splitlines()[0]
    match = MERGE_PR_RE.search(subject) or PR_NUMBER_RE.search(subject)
    return int(match.group(1)) if match else None


def _check_rank(item: dict, index: int) -> tuple[int, int, str, str, str, int]:
    completed = item.get("completedAt") or ""
    started = item.get("startedAt") or ""
    latest = completed if completed >= started else started
    status = (item.get("status") or "").upper()
    conclusion = (item.get("conclusion") or item.get("state") or "").upper()
    non_skipped = 0 if (status == "COMPLETED" and conclusion in SKIPPED_CONCLUSIONS) else 1
    unstarted_pending = 1 if (not latest and (status in PENDING_STATES or conclusion in PENDING_STATES)) else 0
    return (non_skipped, unstarted_pending, latest, completed, started, index)


def summarize_checks(
    rollup: Sequence[dict] | None,
    *,
    workflow_name: str | None = WORKFLOW_NAME,
) -> CheckSummary:
    """Сводит `statusCheckRollup` рабочего workflow к одному состоянию с дедупликацией перезапусков."""
    latest_by_key: dict[tuple[str, str], tuple[tuple[int, int, str, str, str, int], dict]] = {}
    for index, item in enumerate(rollup or ()):
        item_workflow = item.get("workflowName") or ""
        if workflow_name is not None and item_workflow != workflow_name:
            continue
        name = item.get("name") or item.get("context") or "unknown"
        key = (item_workflow, name)
        rank = _check_rank(item, index)
        previous = latest_by_key.get(key)
        if previous is None or rank >= previous[0]:
            latest_by_key[key] = (rank, item)

    pending: list[CheckRef] = []
    failed: list[CheckRef] = []
    passed = 0
    skipped = 0
    for _, item in latest_by_key.values():
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


def to_pull_request(item: dict, *, workflow_name: str | None = WORKFLOW_NAME) -> PullRequest:
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
        checks=summarize_checks(item.get("statusCheckRollup"), workflow_name=workflow_name),
        changes_requested=has_other_changes_requested(item.get("reviews")),
        body=item.get("body") or "",
    )


def _runner(argv: Sequence[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), text=True, capture_output=True, timeout=timeout)


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
        workflow_name: str | None = WORKFLOW_NAME,
        gh: str = "gh",
        timeout: float = 30.0,
    ) -> None:
        self.gh = gh
        self.timeout = timeout
        self._run = run or (lambda argv: _runner(argv, timeout=self.timeout))
        self.workflow_file = workflow_file
        self.workflow_name = workflow_name

    def _exec(self, args: Sequence[str]) -> subprocess.CompletedProcess:
        argv = [self.gh, *args]
        command = args[0] if args else ""
        try:
            return self._run(argv)
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"gh {command}: таймаут {self.timeout:g} с") from exc
        except OSError as exc:
            raise ProviderError(f"gh {command}: {exc}") from exc

    def _ok(self, args: Sequence[str]) -> str:
        done = self._exec(args)
        if done.returncode != 0:
            reason = (done.stderr or done.stdout or "").strip().splitlines()
            detail = reason[0] if reason else f"код {done.returncode}"
            command = args[0] if args else ""
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
        return tuple(to_pull_request(item, workflow_name=self.workflow_name) for item in data or ())

    def release_pr(self, repo: str, base: str = "main", head: str = "dev") -> PullRequest | None:
        data = self._json(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--base",
                base,
                "--head",
                head,
                "--state",
                "open",
                "--limit",
                "1",
                "--json",
                PR_FIELDS,
            ]
        )
        for item in data or ():
            pr = to_pull_request(item, workflow_name=self.workflow_name)
            if pr.head_ref == head:
                return pr
        return None

    def branch_ci(self, repo: str, branch: str) -> BranchCi:
        data = self._json(
            [
                "api",
                f"repos/{repo}/actions/workflows/{quote(self.workflow_file, safe='')}/runs"
                f"?branch={quote(branch, safe='')}&event=push&per_page=20",
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
        return self._ok(
            ["api", f"repos/{repo}/commits/{quote(branch, safe='')}", "--jq", ".sha"]
        ).strip()

    def commit_pr(self, repo: str, sha: str) -> int | None:
        if not sha:
            return None
        data = self._json(
            ["api", f"repos/{repo}/commits/{quote(sha, safe='')}/pulls?per_page=5"]
        )
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("number") is not None:
                    return int(item["number"])
        return None

    def compare(self, repo: str, base: str, head: str) -> Comparison:
        data = self._json(
            ["api", f"repos/{repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}"]
        ) or {}
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
        done = self._exec(["variable", "get", PAUSE_VARIABLE, "--repo", repo])
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
        if method not in {"squash", "merge", "rebase"}:
            raise ValueError(f"неподдерживаемый способ мерджа: {method}")
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
                return to_pull_request(data, workflow_name=self.workflow_name)
        created = self.release_pr(repo, base=base, head=head)
        if created is None:
            raise ProviderError("release PR создан, но не найден в списке открытых")
        return created

    def update_release_pr(self, repo: str, number: int, *, body: str) -> None:
        self._ok(["pr", "edit", str(number), "--repo", repo, "--body", body])

    def comment_pr(self, repo: str, number: int, body: str) -> None:
        self._ok(["pr", "comment", str(number), "--repo", repo, "--body", body])
