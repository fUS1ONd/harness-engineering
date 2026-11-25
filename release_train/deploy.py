"""Поэтапная выкатка: провайдер слоёв, `prod-smoke` и жёсткий fail-fast без отката."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from prod_smoke.cli import render_text as render_smoke_text
from prod_smoke.core import run_smoke
from prod_smoke.transport import SshTransport, Transport

from .model import ALERT_OPEN, SETTLE

LAYER_ORDER = ("center", "worker", "point")
MAX_EVIDENCE_LINES = 8
MAX_EVIDENCE_CHARS = 500


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class SmokeOutcome:
    role: str
    ok: bool
    report: dict | None = None
    text: str = ""
    error: str = ""

    @property
    def failures(self) -> tuple[dict, ...]:
        if not self.report:
            return ()
        return tuple(self.report.get("failures") or ())


@dataclass(frozen=True)
class DeployTarget:
    """Один узел слоя: каталог compose и префикс docker (`sudo -n` на home)."""

    layer: str
    node: str
    host: str
    directory: str
    docker: tuple[str, ...] = ("docker",)
    services: tuple[str, ...] = ()
    prune: bool = False

    def command(self) -> str:
        steps = [
            (*self.docker, "compose", "pull", *self.services),
            (*self.docker, "compose", "up", "-d", *self.services),
        ]
        if self.prune:
            steps.append((*self.docker, "image", "prune", "-f"))
        return f"cd {shlex.quote(self.directory)} && " + " && ".join(shlex.join(step) for step in steps)

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "node": self.node,
            "host": self.host,
            "directory": self.directory,
            "command": self.command(),
        }


def default_targets() -> tuple[DeployTarget, ...]:
    """Порядок узлов внутри слоя — как в `docs/vykladka.md` у репозитория `rumap`."""
    return (
        DeployTarget(
            layer="center",
            node="center",
            host="rumap-center",
            directory="/opt/rumap-center",
            docker=("docker",),
            prune=True,
        ),
        DeployTarget(
            layer="worker",
            node="worker-ru",
            host="home",
            directory='"$HOME/rumap-v2/deploy/worker"',
            docker=("sudo", "-n", "docker"),
        ),
        DeployTarget(
            layer="worker",
            node="worker-control",
            host="rumap-center",
            directory="/opt/rumap-center",
            docker=("docker",),
            services=("rumap-worker",),
        ),
        DeployTarget(
            layer="point",
            node="point",
            host="home",
            directory='"$HOME/rumap-v2/deploy/point"',
            docker=("sudo", "-n", "docker"),
            services=("rumapd",),
        ),
    )


class SmokeProvider(Protocol):
    def smoke(self, role: str) -> SmokeOutcome: ...


class DeployProvider(SmokeProvider, Protocol):
    def deploy(self, target: DeployTarget) -> CommandResult: ...


class SshDeployProvider:
    """Боевой провайдер: `ssh -- host 'cd <dir> && docker compose pull && up -d'`."""

    def __init__(
        self,
        *,
        ssh: str = "ssh",
        connect_timeout: int = 15,
        timeout: float = 600.0,
        smoke_window: int = 300,
        runner: Callable[[Sequence[str], float], subprocess.CompletedProcess] | None = None,
        smoke: SmokeProvider | None = None,
    ) -> None:
        self.ssh = ssh
        self.connect_timeout = connect_timeout
        self.timeout = timeout
        self._run = runner or _run
        self._smoke = smoke or InProcessSmokeProvider(
            SshTransport(ssh=ssh), window_seconds=smoke_window
        )

    def deploy(self, target: DeployTarget) -> CommandResult:
        argv = (
            self.ssh,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "--",
            target.host,
            target.command(),
        )
        try:
            done = self._run(argv, self.timeout)
        except subprocess.TimeoutExpired:
            return CommandResult(argv, 255, "", f"таймаут {self.timeout:g} с: ssh {target.host}")
        except OSError as exc:
            return CommandResult(argv, 255, "", f"ssh {target.host}: {exc}")
        return CommandResult(argv, done.returncode, done.stdout or "", done.stderr or "")

    def smoke(self, role: str) -> SmokeOutcome:
        return self._smoke.smoke(role)


def _run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), text=True, capture_output=True, timeout=timeout)


class InProcessSmokeProvider:
    """Вызывает `prod_smoke` в процессе: один и тот же код и формат отчёта, без разбора stdout."""

    def __init__(
        self,
        transport: Transport,
        *,
        window_seconds: int = 300,
        now: Callable | None = None,
    ) -> None:
        self.transport = transport
        self.window_seconds = window_seconds
        self.now = now

    def smoke(self, role: str) -> SmokeOutcome:
        try:
            report = run_smoke(
                role,
                self.transport,
                window_seconds=self.window_seconds,
                now=self.now,
            )
        except Exception as exc:
            return SmokeOutcome(role=role, ok=False, error=f"prod-smoke упал: {exc}")
        return SmokeOutcome(
            role=role,
            ok=report.ok,
            report=report.to_dict(),
            text=render_smoke_text(report),
        )


@dataclass(frozen=True)
class DeployStep:
    layer: str
    stage: str
    target: str
    ok: bool
    detail: str = ""
    command: tuple[str, ...] = ()
    returncode: int = 0
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "stage": self.stage,
            "target": self.target,
            "ok": self.ok,
            "detail": self.detail,
            "command": list(self.command),
            "returncode": self.returncode,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class DeployReport:
    ok: bool
    sha: str | None = None
    steps: tuple[DeployStep, ...] = ()
    failed_layer: str | None = None
    untouched: tuple[str, ...] = ()
    reason: str = ""
    action: str = "deployed"
    thread_action: str = SETTLE

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "action": self.action,
            "thread_action": self.thread_action,
            "sha": self.sha,
            "failed_layer": self.failed_layer,
            "untouched": list(self.untouched),
            "reason": self.reason,
            "steps": [step.to_dict() for step in self.steps],
        }

    def summary(self) -> str:
        if self.action == "paused":
            return f"Выкатка заблокирована стоп-краном: {self.reason}. Ничего не тронуто."
        if self.action == "sha_moved":
            return f"Выкатка отменена: {self.reason}"
        if self.ok:
            return (
                "Выкатка выполнена на всех 3 слоях: center → worker → point, "
                f"prod-smoke зелёный (sha {self.sha or '?'})."
            )
        return (
            f"Выкатка остановлена на слое {self.failed_layer}: {self.reason}. "
            "Следующие слои не тронуты, авто-откат не выполнялся — решение за владельцем."
        )


def run_deploy(
    sha: str | None,
    provider: DeployProvider,
    *,
    targets: Sequence[DeployTarget] | None = None,
) -> DeployReport:
    """Строго `center → smoke → worker → smoke → point → smoke`, остановка на первом провале."""
    plan = tuple(targets if targets is not None else default_targets())
    missing = [layer for layer in LAYER_ORDER if not any(target.layer == layer for target in plan)]
    if missing:
        raise ValueError(f"в плане выкатки нет слоёв: {', '.join(missing)}")
    steps: list[DeployStep] = []

    for layer in LAYER_ORDER:
        layer_targets = [target for target in plan if target.layer == layer]
        for target in layer_targets:
            result = provider.deploy(target)
            steps.append(
                DeployStep(
                    layer=layer,
                    stage="deploy",
                    target=target.node,
                    ok=result.ok,
                    detail=f"{target.host}: {target.directory} — код {result.returncode}",
                    command=result.argv,
                    returncode=result.returncode,
                    evidence=_command_evidence(result),
                )
            )
            if not result.ok:
                return _failed(
                    steps,
                    sha,
                    plan,
                    layer,
                    f"деплой узла {target.node} вернул код {result.returncode}",
                )

        outcome = provider.smoke(layer)
        steps.append(
            DeployStep(
                layer=layer,
                stage="smoke",
                target=layer,
                ok=outcome.ok,
                detail=(
                    f"prod-smoke --role {layer}: OK"
                    if outcome.ok
                    else outcome.error or f"prod-smoke --role {layer}: провалов {len(outcome.failures)}"
                ),
                evidence=_smoke_evidence(outcome),
            )
        )
        if not outcome.ok:
            return _failed(
                steps,
                sha,
                plan,
                layer,
                outcome.error or f"prod-smoke на слое {layer} провалился",
            )

    return DeployReport(ok=True, sha=sha, steps=tuple(steps), action="deployed", thread_action=SETTLE)


def _failed(
    steps: list[DeployStep],
    sha: str | None,
    plan: Sequence[DeployTarget],
    layer: str,
    reason: str,
) -> DeployReport:
    touched = {step.target for step in steps if step.stage == "deploy"}
    untouched = tuple(target.node for target in plan if target.node not in touched)
    return DeployReport(
        ok=False,
        sha=sha,
        steps=tuple(steps),
        failed_layer=layer,
        untouched=untouched,
        reason=reason,
        action="deploy_failed",
        thread_action=ALERT_OPEN,
    )


def _command_evidence(result: CommandResult) -> tuple[str, ...]:
    text = "\n".join(part for part in (result.stdout, result.stderr) if part)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return tuple(_snippet(line) for line in lines[-MAX_EVIDENCE_LINES:])


def _smoke_evidence(outcome: SmokeOutcome) -> tuple[str, ...]:
    if outcome.error:
        return (outcome.error,)
    lines: list[str] = []
    for failure in outcome.failures:
        if not isinstance(failure, dict):
            continue
        lines.append(
            f"[{failure.get('role')}/{failure.get('node')} ({failure.get('host')})] "
            f"{failure.get('kind')} {failure.get('target')}: {failure.get('detail')}"
        )
        for snippet in (failure.get("evidence") or ())[:2]:
            lines.append(f"    {snippet}")
        if len(lines) >= MAX_EVIDENCE_LINES * 2:
            break
    return tuple(_snippet(line) for line in lines[: MAX_EVIDENCE_LINES * 2])


def _snippet(text: str) -> str:
    one_line = " ".join(text.split())
    if len(one_line) > MAX_EVIDENCE_CHARS:
        return one_line[:MAX_EVIDENCE_CHARS] + "…"
    return one_line


def render_deploy_text(report: DeployReport) -> str:
    state = "OK" if report.ok else "FAIL"
    lines = [f"release-train deploy: {state} — {report.summary()}"]
    for step in report.steps:
        mark = "✔" if step.ok else "✘"
        lines.append(f"  {mark} {step.stage} {step.target}: {step.detail}")
        for snippet in step.evidence:
            lines.append(f"      {snippet}")
    if not report.ok and report.action not in {"paused", "sha_moved"}:
        if report.untouched:
            lines.append(f"Не тронуто: {', '.join(report.untouched)}")
        lines.append("Авто-откат не выполнялся — решение об откате или фиксе за владельцем.")
    return "\n".join(lines)
