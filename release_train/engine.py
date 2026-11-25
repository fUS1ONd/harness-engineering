"""Стейт-машина `release-train`: сбор состояния, решение и ровно один переход за вызов."""

from __future__ import annotations

from typing import Callable, Sequence

from .deploy import DeployProvider, DeployReport, DeployTarget, run_deploy
from .github import GitHubProvider
from .locks import LockStore
from .model import (
    ALERT_OPEN,
    HOLD_LABEL,
    RELEASE_TRAIN_LABEL,
    SETTLE,
    WAIT_DEPLOY,
    CheckRef,
    CiRun,
    Decision,
    TrainState,
    short_sha,
)

BASE_BRANCH = "main"
HEAD_BRANCH = "dev"
RELEASE_PR_TITLE = "release-train: dev → main"
BODY_MARKER = "<!-- release-train:head:{} -->"


def collect_state(
    provider: GitHubProvider,
    locks: LockStore,
    repo: str,
    *,
    base: str = BASE_BRANCH,
    head: str = HEAD_BRANCH,
) -> TrainState:
    """Один снимок GitHub + локального состояния; все решения дальше — по нему."""
    main_sha = provider.branch_head(repo, base)
    dev_sha = provider.branch_head(repo, head)
    deployed_sha = locks.deployed_sha(repo)
    deploy_compare = None
    if deployed_sha and deployed_sha != main_sha:
        deploy_compare = provider.compare(repo, deployed_sha, main_sha)
    return TrainState(
        repo=repo,
        main_sha=main_sha,
        dev_sha=dev_sha,
        dev_prs=provider.open_prs(repo, head),
        release_pr=provider.release_pr(repo, base=base, head=head),
        dev_ci=provider.branch_ci(repo, head),
        main_ci=provider.branch_ci(repo, base),
        compare=provider.compare(repo, base, head),
        deploy_compare=deploy_compare,
        paused_repo=provider.automation_paused(repo),
        paused_file=locks.paused(),
        deployed_sha=deployed_sha,
        base_branch=base,
        head_branch=head,
    )


def evaluate(
    state: TrainState,
    *,
    handled_comment: Callable[[str, str], bool] | None = None,
) -> Decision:
    """Один разрешённый переход; приоритет — стоп-краны, красный CI, мердж PR, промоушен, деплой.

    `handled_comment` отсекает уже отправленные сигналы о красном CI: иначе первый
    закомментированный провал маскировал бы следующий и тред молчал бы о второй поломке.
    """
    if state.paused:
        return Decision(
            action="paused",
            thread_action=SETTLE,
            summary=f"Стоп-кран активен ({state.pause_source}): ничего не меняю.",
            details={"pause": {"active": True, "source": state.pause_source}},
        )

    pr_failure = _pr_ci_failure(state, handled_comment)
    if pr_failure is not None:
        return pr_failure

    branch_failure = _branch_ci_failure(state, handled_comment)
    if branch_failure is not None:
        return branch_failure

    if state.ready_prs:
        pr = state.ready_prs[0]
        return Decision(
            action="merged_to_dev",
            thread_action=SETTLE,
            summary=(
                f"Вливаю PR #{pr.number} «{pr.title}» в `{state.head_branch}` "
                f"(sha {short_sha(pr.head_sha)})."
            ),
            details={"head_sha": pr.head_sha, "base": state.head_branch},
            pr=pr,
            lock_key=f"merge-pr:{pr.number}",
            lock_sha=pr.head_sha,
        )

    if state.dev_ci.state == "pending":
        active = ", ".join(run.workflow or "CI" for run in state.dev_ci.active) or "CI"
        return _noop(
            f"Жду завершения CI на `{state.head_branch}`: {active}.",
            {"ci": state.dev_ci.to_dict()},
        )

    if state.compare.ahead_by > 0:
        return _promotion(state, handled_comment)

    return _deploy_readiness(state)


def _pr_ci_failure(
    state: TrainState,
    handled_comment: Callable[[str, str], bool] | None,
) -> Decision | None:
    for pr in state.release_train_prs:
        if pr.is_draft or pr.has_label(HOLD_LABEL):
            continue
        if pr.checks.state == "failed":
            key = f"comment:pr:{pr.number}"
            if handled_comment is not None and handled_comment(key, pr.head_sha):
                continue
            names = ", ".join(check.name for check in pr.checks.failed) or "CI"
            scope = f"PR #{pr.number}"
            return Decision(
                action="ci_failed",
                thread_action=SETTLE,
                summary=(
                    f"CI упал на {scope} ({names}). Комментарий с ссылкой на прогон — "
                    f"в PR #{pr.number}, тред закрываю."
                ),
                details={
                    "scope": scope,
                    "failed": [check.to_dict() for check in pr.checks.failed],
                    "head_sha": pr.head_sha,
                },
                pr=pr,
                comment_target=pr.number,
                comment_body=_ci_comment(scope, pr.checks.failed, None, pr.head_sha),
                lock_key=key,
                lock_sha=pr.head_sha,
            )
    return None


def _branch_ci_failure(
    state: TrainState,
    handled_comment: Callable[[str, str], bool] | None,
) -> Decision | None:
    for branch_ci, sha, branch in (
        (state.dev_ci, state.dev_sha, state.head_branch),
        (state.main_ci, state.main_sha, state.base_branch),
    ):
        run = branch_ci.failed_run(sha)
        if run is None:
            continue
        marker_sha = run.sha or run.url
        key = f"comment:branch:{branch}"
        if handled_comment is not None and handled_comment(key, marker_sha):
            continue
        scope = f"CI ветки `{branch}`"
        target = state.release_pr.number if state.release_pr else run.pr_number
        names = run.workflow or "workflow"
        return Decision(
            action="ci_failed",
            thread_action=SETTLE,
            summary=(
                f"Красный {scope}: {names}"
                + (f", PR #{target}" if target else "")
                + f". Прогон: {run.url or '—'}. Тред закрываю."
            ),
            details={
                "scope": scope,
                "failed": [{"name": names, "url": run.url}],
                "sha": run.sha,
                "branch": branch,
            },
            run=run,
            comment_target=target,
            comment_body=_ci_comment(scope, _failed_refs(run), run, marker_sha),
            lock_key=key,
            lock_sha=marker_sha,
        )
    return None


def _promotion(
    state: TrainState,
    handled_comment: Callable[[str, str], bool] | None = None,
) -> Decision:
    if state.dev_ci.state == "none":
        return _noop(
            f"`{state.head_branch}` опережает `{state.base_branch}`, но CI на нём ещё не запускался.",
            {"compare": state.compare.to_dict()},
        )
    if state.dev_ci.state != "passed":
        return _noop(
            f"CI на `{state.head_branch}` не зелёный ({state.dev_ci.state}) — промоушен ждёт.",
            {"ci": state.dev_ci.to_dict(), "compare": state.compare.to_dict()},
        )
    latest = state.dev_ci.latest_completed
    if latest is not None and latest.sha and latest.sha != state.dev_sha:
        return _noop(
            f"Зелёный CI относится к {short_sha(latest.sha)}, а `{state.head_branch}` на "
            f"{short_sha(state.dev_sha)} — жду прогон текущего коммита.",
            {"ci": state.dev_ci.to_dict(), "dev_sha": state.dev_sha},
        )

    batch = {
        "ahead_by": state.compare.ahead_by,
        "commits": list(state.compare.commits),
        "pull_requests": list(state.compare.pull_requests),
    }
    pr = state.release_pr
    if pr is None:
        return Decision(
            action="opened_release_pr",
            thread_action=SETTLE,
            summary=(
                f"Открываю release PR `{state.head_branch}` → `{state.base_branch}`: "
                f"{state.compare.ahead_by} коммит(ов), PR: {_pr_list(state.compare.pull_requests)}."
            ),
            details=dict(batch),
            lock_key="release-pr:create",
            lock_sha=state.dev_sha,
        )

    if pr.is_draft:
        return _noop(f"Release PR #{pr.number} в draft — жду готовности.", {"pull_request": pr.to_dict()})
    if not pr.has_label(RELEASE_TRAIN_LABEL):
        return _noop(
            f"С release PR #{pr.number} снята метка `{RELEASE_TRAIN_LABEL}` — не трогаю.",
            {"pull_request": pr.to_dict()},
        )
    if pr.has_label(HOLD_LABEL):
        return _noop(
            f"Release PR #{pr.number} под меткой `{HOLD_LABEL}` — жду.",
            {"pull_request": pr.to_dict()},
        )
    if pr.mergeable == "CONFLICTING":
        return _noop(
            f"Release PR #{pr.number} конфликтует с `{state.base_branch}`.",
            {"pull_request": pr.to_dict()},
        )
    if pr.changes_requested:
        return _noop(
            f"На release PR #{pr.number} висят чужие `CHANGES_REQUESTED`.",
            {"pull_request": pr.to_dict()},
        )
    if pr.checks.state == "failed":
        key = f"comment:pr:{pr.number}"
        if handled_comment is not None and handled_comment(key, pr.head_sha):
            return _noop(
                f"Красный CI release PR #{pr.number} уже отмечен комментарием — жду исправления.",
                {"pull_request": pr.to_dict()},
            )
        names = ", ".join(check.name for check in pr.checks.failed) or "CI"
        scope = f"release PR #{pr.number}"
        return Decision(
            action="ci_failed",
            thread_action=SETTLE,
            summary=f"CI упал на {scope} ({names}). Комментарий в PR #{pr.number}, тред закрываю.",
            details={
                "scope": scope,
                "failed": [check.to_dict() for check in pr.checks.failed],
                "head_sha": pr.head_sha,
            },
            pr=pr,
            comment_target=pr.number,
            comment_body=_ci_comment(scope, pr.checks.failed, None, pr.head_sha),
            lock_key=key,
            lock_sha=pr.head_sha,
        )
    if pr.checks.state == "pending":
        return _noop(f"CI release PR #{pr.number} ещё идёт.", {"pull_request": pr.to_dict()})
    if pr.checks.state != "passed":
        return _noop(f"У release PR #{pr.number} нет зелёного CI.", {"pull_request": pr.to_dict()})
    if pr.mergeable != "MERGEABLE":
        return _noop(f"Release PR #{pr.number} пока {pr.mergeable}.", {"pull_request": pr.to_dict()})
    if BODY_MARKER.format(state.dev_sha) not in pr.body:
        return Decision(
            action="updated_release_pr",
            thread_action=SETTLE,
            summary=(
                f"Обновляю описание release PR #{pr.number} под батч из "
                f"{state.compare.ahead_by} коммит(ов)."
            ),
            details=dict(batch),
            pr=pr,
            lock_key="release-pr:update",
            lock_sha=state.dev_sha,
        )
    return Decision(
        action="merged_release_pr",
        thread_action=SETTLE,
        summary=(
            f"Вливаю release PR #{pr.number} в `{state.base_branch}`: "
            f"{state.compare.ahead_by} коммит(ов), PR: {_pr_list(state.compare.pull_requests)}."
        ),
        details=dict(batch),
        pr=pr,
        lock_key="release-pr:merge",
        lock_sha=pr.head_sha,
    )


def _deploy_readiness(state: TrainState) -> Decision:
    if state.main_ci.state == "pending":
        return _noop(f"CI на `{state.base_branch}` идёт — жду результата.", {"ci": state.main_ci.to_dict()})
    if state.main_ci.state != "passed":
        return _noop(
            f"CI на `{state.base_branch}`: {state.main_ci.state} — жду зелёного прогона.",
            {"ci": state.main_ci.to_dict()},
        )
    latest = state.main_ci.latest_completed
    if latest is not None and latest.sha and latest.sha != state.main_sha:
        return _noop(
            f"CI относится к {short_sha(latest.sha)}, а `{state.base_branch}` на "
            f"{short_sha(state.main_sha)} — жду прогон текущего коммита.",
            {"ci": state.main_ci.to_dict(), "main_sha": state.main_sha},
        )
    if state.main_sha == state.deployed_sha:
        return _noop(
            f"`{state.base_branch}` ({short_sha(state.main_sha)}) уже выкачен — делать нечего.",
            {"main_sha": state.main_sha, "deployed_sha": state.deployed_sha},
        )

    compare = state.deploy_compare
    commits = list(compare.commits) if compare else _fallback_commits(latest)
    pull_requests = list(compare.pull_requests) if compare else []
    details = {
        "main_sha": state.main_sha,
        "deployed_sha": state.deployed_sha,
        "commits": commits,
        "pull_requests": pull_requests,
        "run": latest.to_dict() if latest else None,
    }
    batch = (
        f" {len(commits)} коммит(ов), PR: {_pr_list(pull_requests)}."
        if commits
        else "."
    )
    return Decision(
        action="ready_to_deploy",
        thread_action=WAIT_DEPLOY,
        summary=(
            f"`{state.base_branch}` собран и зелёный (sha {short_sha(state.main_sha)})."
            f"{batch} Тред не закрываю — жду команду «деплой»."
        ),
        details=details,
    )


def handled_comment(locks: LockStore, repo: str) -> Callable[[str, str], bool]:
    """Предикат «сигнал о красном CI уже отправлен» для `evaluate`."""
    return lambda key, sha: locks.contains(repo, key, sha)


def step(
    provider: GitHubProvider,
    locks: LockStore,
    repo: str,
    *,
    base: str = BASE_BRANCH,
    head: str = HEAD_BRANCH,
) -> dict:
    """Выполняет не больше одного мутирующего перехода за вызов."""
    state = collect_state(provider, locks, repo, base=base, head=head)
    decision = evaluate(state, handled_comment=handled_comment(locks, repo))
    if not decision.mutating:
        return decision.to_dict()

    paused_repo = provider.automation_paused(repo)
    if paused_repo or locks.paused():
        source = "repo_variable:RELEASE_TRAIN_PAUSED" if paused_repo else "pause_file"
        return Decision(
            action="paused",
            thread_action=SETTLE,
            summary=f"Стоп-кран включился перед мутацией ({source}): ничего не меняю.",
            details={"pause": {"active": True, "source": source}},
        ).to_dict()

    key = decision.lock_key or decision.action
    sha = decision.lock_sha
    if not locks.acquire(repo, key, sha):
        return {
            "ok": True,
            "action": "noop",
            "thread_action": SETTLE,
            "summary": (
                f"Шаг {decision.action} для {short_sha(sha)} уже отработан — "
                "повторный webhook ничего не меняет."
            ),
            "details": {
                "reason": "already_done",
                "intended_action": decision.action,
                "lock": {"key": key, "sha": sha},
            },
        }

    try:
        _apply(decision, provider, state)
    except Exception as exc:
        locks.release(repo, key, sha)
        return {
            "ok": False,
            "action": "error",
            "thread_action": ALERT_OPEN,
            "summary": f"Мутация {decision.action} упала: {exc}",
            "details": {
                "reason": "mutation_failed",
                "intended_action": decision.action,
                "error": str(exc),
                "lock": {"key": key, "sha": sha},
            },
        }
    return decision.to_dict()


def _apply(decision: Decision, provider: GitHubProvider, state: TrainState) -> None:
    repo = state.repo
    if decision.action == "merged_to_dev":
        provider.merge_pr(
            repo,
            decision.pr.number,
            decision.pr.head_sha,
            delete_branch=True,
            method="squash",
        )
    elif decision.action == "opened_release_pr":
        provider.open_release_pr(
            repo,
            base=state.base_branch,
            head=state.head_branch,
            title=RELEASE_PR_TITLE,
            body=release_pr_body(state),
            labels=(RELEASE_TRAIN_LABEL,),
        )
    elif decision.action == "updated_release_pr":
        provider.update_release_pr(repo, decision.pr.number, body=release_pr_body(state))
    elif decision.action == "merged_release_pr":
        # `dev → main` вливается merge-коммитом: squash развёл бы истории и `main...dev`
        # навсегда показывал бы «впереди», гоняя пустые релизы по кругу.
        provider.merge_pr(repo, decision.pr.number, decision.pr.head_sha, method="merge")
    elif decision.action == "ci_failed":
        if decision.comment_target is not None:
            provider.comment_pr(repo, decision.comment_target, decision.comment_body)
    else:
        raise RuntimeError(f"нет мутации для действия {decision.action}")


def deploy_train(
    provider: GitHubProvider,
    locks: LockStore,
    deployer: DeployProvider,
    repo: str,
    *,
    base: str = BASE_BRANCH,
    expected_sha: str | None = None,
    targets: Sequence[DeployTarget] | None = None,
) -> DeployReport:
    """Стоп-кран, сверка подтверждённого sha и поэтапная выкатка с журналом."""
    if locks.paused() or provider.automation_paused(repo):
        source = "pause_file" if locks.paused() else "repo_variable:RELEASE_TRAIN_PAUSED"
        return DeployReport(ok=False, action="paused", thread_action=SETTLE, reason=source)

    sha = provider.branch_head(repo, base)
    if expected_sha and expected_sha != sha:
        return DeployReport(
            ok=False,
            sha=sha,
            action="sha_moved",
            thread_action=ALERT_OPEN,
            reason=(
                f"`{base}` уже на {short_sha(sha)}, а подтверждён {short_sha(expected_sha)} — "
                "выкатывать неподтверждённый коммит не буду"
            ),
        )

    report = run_deploy(sha, deployer, targets=targets)
    if report.ok:
        locks.mark_deployed(repo, sha)
    return report


def release_pr_body(state: TrainState) -> str:
    compare = state.compare
    lines = [
        f"Батч `{state.head_branch}` → `{state.base_branch}`: {compare.ahead_by} коммит(ов).",
        "",
        "### Коммиты",
    ]
    if compare.commits:
        lines.extend(f"- {commit}" for commit in compare.commits)
    else:
        lines.append("- (сравнение пустое)")
    if compare.pull_requests:
        lines.extend(["", "### PR в батче"])
        lines.extend(f"- #{number}" for number in compare.pull_requests)
    lines.extend(
        [
            "",
            "Автоматический PR конвейера `release-train`: вливается только после зелёного CI на `dev`.",
            "",
            BODY_MARKER.format(state.dev_sha),
        ]
    )
    return "\n".join(lines)


def _ci_comment(
    scope: str,
    failed: Sequence[CheckRef],
    run: CiRun | None,
    sha: str,
) -> str:
    lines = [f"🔴 **release-train: CI не прошёл** — {scope}.", ""]
    if run is not None:
        if run.workflow:
            lines.append(
                f"Workflow: `{run.workflow}`, ветка `{run.branch or '—'}`, "
                f"sha `{short_sha(run.sha)}`."
            )
        if run.url:
            lines.append(f"Упавший прогон: {run.url}")
    elif failed and failed[0].url:
        lines.append(f"Упавшая проверка: {failed[0].url}")
    names = [check.name for check in failed if check.name]
    if names:
        lines.append("Проверки: " + ", ".join(f"`{name}`" for name in names))
    lines.extend(
        [
            "",
            "Тред webhook закрыт. Исправьте причину и запушьте снова — "
            "следующий проход подхватит PR автоматически.",
            f"<!-- release-train:ci-failed:{sha} -->",
        ]
    )
    return "\n".join(lines)


def _failed_refs(run: CiRun) -> tuple[CheckRef, ...]:
    return (CheckRef(name=run.workflow or "workflow", url=run.url),)


def _pr_list(numbers: Sequence[int]) -> str:
    if not numbers:
        return "—"
    return ", ".join(f"#{number}" for number in numbers)


def _fallback_commits(run: CiRun | None) -> list[str]:
    if run is not None and run.commit_message:
        return [run.commit_message]
    return []


def _noop(summary: str, details: dict | None = None) -> Decision:
    return Decision(
        action="noop",
        thread_action=SETTLE,
        summary=summary,
        details=dict(details or {}),
    )
