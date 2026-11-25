"""Модель отчёта `prod-smoke`: узлы, проверки, сериализация в JSON."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class CheckResult:
    kind: str
    target: str
    ok: bool
    detail: str
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "target": self.target,
            "ok": self.ok,
            "detail": self.detail,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class NodeReport:
    node: str
    host: str
    checks: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def to_dict(self) -> dict:
        return {
            "node": self.node,
            "host": self.host,
            "ok": self.ok,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class RoleReport:
    role: str
    nodes: tuple[NodeReport, ...]

    @property
    def ok(self) -> bool:
        return all(node.ok for node in self.nodes)

    def to_dict(self) -> dict:
        return {"role": self.role, "ok": self.ok, "nodes": [node.to_dict() for node in self.nodes]}


@dataclass(frozen=True)
class Failure:
    role: str
    node: str
    host: str
    kind: str
    target: str
    detail: str
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "node": self.node,
            "host": self.host,
            "kind": self.kind,
            "target": self.target,
            "detail": self.detail,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class SmokeReport:
    role: str
    window_seconds: int
    started_at: datetime
    finished_at: datetime
    roles: tuple[RoleReport, ...]

    @property
    def ok(self) -> bool:
        return all(role.ok for role in self.roles)

    @property
    def checks_total(self) -> int:
        return sum(len(node.checks) for role in self.roles for node in role.nodes)

    @property
    def failures(self) -> tuple[Failure, ...]:
        found: list[Failure] = []
        for role in self.roles:
            for node in role.nodes:
                for check in node.checks:
                    if not check.ok:
                        found.append(
                            Failure(
                                role=role.role,
                                node=node.node,
                                host=node.host,
                                kind=check.kind,
                                target=check.target,
                                detail=check.detail,
                                evidence=check.evidence,
                            )
                        )
        return tuple(found)

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "window_seconds": self.window_seconds,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "ok": self.ok,
            "checks_total": self.checks_total,
            "failures": [failure.to_dict() for failure in self.failures],
            "roles": [role.to_dict() for role in self.roles],
        }


def _iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")
