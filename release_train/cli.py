"""CLI `release-train`: `scan` (снимок), `step` (один переход), `deploy` (выкатка по слоям)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence, TextIO

from prod_smoke.cli import parse_window

from .deploy import (
    DEFAULT_SMOKE_ATTEMPTS,
    DEFAULT_SMOKE_INTERVAL_SECONDS,
    DeployProvider,
    SshDeployProvider,
    render_deploy_text,
)
from .engine import (
    BASE_BRANCH,
    HEAD_BRANCH,
    collect_state,
    deploy_train,
    evaluate,
    handled_comment,
    step,
)
from .github import GhCliProvider, GitHubProvider, ProviderError
from .locks import FileLockStore, LockStore
from .model import ALERT_OPEN, Decision, TrainState, short_sha

DEFAULT_REPO = "rumap/rumap"


def default_state_dir() -> Path:
    value = os.environ.get("RELEASE_TRAIN_STATE_DIR")
    return Path(value) if value else Path.home() / ".cache" / "release-train"


def default_pause_file() -> Path:
    value = os.environ.get("RELEASE_TRAIN_PAUSE_FILE")
    return Path(value) if value else Path("/tmp/release-train.paused")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release-train",
        description="Событийный конвейер rumap: PR → dev → main → прод по метке release-train.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/name (по умолчанию {DEFAULT_REPO})")
    common.add_argument("--base", default=BASE_BRANCH, help="ветка релиза (по умолчанию main)")
    common.add_argument("--head", default=HEAD_BRANCH, help="ветка разработки (по умолчанию dev)")
    common.add_argument(
        "--state-dir",
        default=None,
        help="каталог замков и журнала выкатки (по умолчанию ~/.cache/release-train)",
    )
    common.add_argument(
        "--pause-file",
        default=None,
        help="локальный стоп-кран (по умолчанию /tmp/release-train.paused)",
    )
    common.add_argument(
        "--format",
        choices=("json", "text", "both"),
        default="json",
        help="формат вывода: json, text или both (по умолчанию json)",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", parents=[common], help="собрать состояние поезда и рекомендацию")
    sub.add_parser("step", parents=[common], help="выполнить ровно один разрешённый переход")
    p_deploy = sub.add_parser("deploy", parents=[common], help="выкатить слои point → worker → center")
    p_deploy.add_argument(
        "--sha",
        default=None,
        help="подтверждённый владельцем sha main: если main ушёл вперёд, выкатка отменяется",
    )
    p_deploy.add_argument(
        "--window",
        type=parse_window,
        default=300,
        metavar="ОКНО",
        help="окно наблюдения prod-smoke после слоя: 300, 90s, 5m (по умолчанию 5m)",
    )
    p_deploy.add_argument(
        "--smoke-attempts",
        type=int,
        default=DEFAULT_SMOKE_ATTEMPTS,
        help=f"число попыток prod-smoke после слоя (по умолчанию {DEFAULT_SMOKE_ATTEMPTS})",
    )
    p_deploy.add_argument(
        "--smoke-interval",
        type=float,
        default=DEFAULT_SMOKE_INTERVAL_SECONDS,
        help=f"пауза в секундах между попытками prod-smoke (по умолчанию {DEFAULT_SMOKE_INTERVAL_SECONDS:g})",
    )
    return parser


def render_scan_text(state: TrainState, decision: Decision) -> str:
    lines = [
        f"release-train scan: {state.repo} ({state.head_branch} → {state.base_branch})",
        f"  main {short_sha(state.main_sha)} / dev {short_sha(state.dev_sha)}, "
        f"впереди на {state.compare.ahead_by} коммит(ов)",
        f"  dev CI: {state.dev_ci.state}, main CI: {state.main_ci.state}",
        f"  стоп-кран: {state.pause_source or 'нет'}",
        f"  выкачено: {short_sha(state.deployed_sha) or 'неизвестно'}",
    ]
    for pr in state.release_train_prs:
        blocked = pr.blocked_reason()
        lines.append(
            f"  PR #{pr.number} «{pr.title}»: "
            + (f"ждёт ({blocked})" if blocked else "готов к мерджу в dev")
        )
    if state.ignored_prs:
        ignored = ", ".join(f"#{pr.number}" for pr in state.ignored_prs)
        lines.append(f"  без метки release-train: {ignored}")
    release_pr = f"#{state.release_pr.number}" if state.release_pr else "нет"
    lines.append(f"  release PR: {release_pr}")
    lines.append(f"  рекомендация: {decision.action} ({decision.thread_action}) — {decision.summary}")
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    provider: GitHubProvider | None = None,
    locks: LockStore | None = None,
    deployer: DeployProvider | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    lock_store = locks or FileLockStore(
        args.state_dir or default_state_dir(),
        args.pause_file or default_pause_file(),
    )
    github = provider or GhCliProvider()
    try:
        return _dispatch(args, github, lock_store, deployer, out)
    except ProviderError as exc:
        # Webhook читает stdout: падение GitHub обязано быть JSON-сигналом, а не трейсбеком.
        payload = {
            "ok": False,
            "action": "error",
            "thread_action": ALERT_OPEN,
            "summary": f"GitHub недоступен: {exc}",
            "details": {"error": str(exc)},
        }
        _emit(out, args.format, payload, payload["summary"])
        print(payload["summary"], file=err)
        return 1


def _dispatch(
    args: argparse.Namespace,
    github: GitHubProvider,
    lock_store: LockStore,
    deployer: DeployProvider | None,
    out: TextIO,
) -> int:
    if args.command == "scan":
        state = collect_state(github, lock_store, args.repo, base=args.base, head=args.head)
        decision = evaluate(state, handled_comment=handled_comment(lock_store, args.repo))
        payload = state.to_dict()
        payload.update(
            {
                "ok": True,
                "recommended_action": decision.action,
                "recommended_thread_action": decision.thread_action,
                "recommended_summary": decision.summary,
            }
        )
        _emit(out, args.format, payload, render_scan_text(state, decision))
        return 0

    if args.command == "step":
        result = step(github, lock_store, args.repo, base=args.base, head=args.head)
        _emit(out, args.format, result, _render_result_text("release-train step", result))
        return 0 if result.get("ok") else 1

    deploy_provider = deployer or SshDeployProvider(smoke_window=args.window)
    report = deploy_train(
        github,
        lock_store,
        deploy_provider,
        args.repo,
        base=args.base,
        expected_sha=args.sha,
        smoke_attempts=args.smoke_attempts,
        smoke_interval=args.smoke_interval,
    )
    text = render_deploy_text(report)
    payload = {
        "ok": report.ok,
        "action": report.action,
        "thread_action": report.thread_action,
        "summary": report.summary(),
        "text": text,
        "details": report.to_dict(),
    }
    _emit(out, args.format, payload, text)
    return 0 if report.ok else 1


def _emit(out: TextIO, fmt: str, payload: dict, text: str) -> None:
    if fmt in ("text", "both"):
        print(text, file=out)
    if fmt == "both":
        print("--- JSON ---", file=out)
    if fmt in ("json", "both"):
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=out)


def _render_result_text(title: str, result: dict) -> str:
    return (
        f"{title}: {result.get('action')} → {result.get('thread_action')}\n"
        f"  {result.get('summary')}"
    )
