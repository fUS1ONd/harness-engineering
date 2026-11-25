"""Проверки `prod-smoke`: read-only прогон по ролям через подменяемый транспорт."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from .model import CheckResult, NodeReport, RoleReport, SmokeReport
from .topology import ContainerSpec, HealthProbe, MetricProbe, NodeSpec, default_roles
from .transport import HttpResult, Transport

ROLE_ORDER = ("center", "worker", "point")
LOG_PATTERN = re.compile(r"(?i)\b(?:error|panic|fatal)\b")
MAX_EVIDENCE_LINES = 5
MAX_EVIDENCE_CHARS = 500


class _CachedHttpTransport:
    """Кэширует GET-ответы в пределах одного прогона, чтобы не бить по SSH-лимитам хоста."""

    def __init__(self, inner: Transport) -> None:
        self._inner = inner
        self._http_cache: dict[tuple[str, str], HttpResult] = {}

    def exec(self, host: str, argv: Sequence[str], timeout: float = 30.0):
        return self._inner.exec(host, argv, timeout=timeout)

    def http_get(self, host: str, url: str, timeout: float = 10.0) -> HttpResult:
        key = (host, url)
        if key not in self._http_cache:
            self._http_cache[key] = self._inner.http_get(host, url, timeout=timeout)
        return self._http_cache[key]


def run_smoke(
    role: str,
    transport: Transport,
    *,
    window_seconds: int = 300,
    now: Callable[[], datetime] | None = None,
    roles: Mapping[str, Sequence[NodeSpec]] | None = None,
) -> SmokeReport:
    """Проверяет прод в режиме read-only и возвращает готовый к JSON отчёт."""
    if role not in (*ROLE_ORDER, "all"):
        raise ValueError(f"неизвестная роль: {role}")
    if window_seconds <= 0:
        raise ValueError("окно наблюдения должно быть положительным")
    clock = now or (lambda: datetime.now(timezone.utc))
    topology = roles if roles is not None else default_roles()
    selected = ROLE_ORDER if role == "all" else (role,)
    cached_transport = _CachedHttpTransport(transport)

    started_at = clock()
    reports = []
    for name in selected:
        nodes = tuple(
            _run_node(spec, cached_transport, window_seconds, clock) for spec in topology[name]
        )
        reports.append(RoleReport(role=name, nodes=nodes))
    return SmokeReport(
        role=role,
        window_seconds=window_seconds,
        started_at=started_at,
        finished_at=clock(),
        roles=tuple(reports),
    )


def _run_node(
    spec: NodeSpec,
    transport: Transport,
    window_seconds: int,
    clock: Callable[[], datetime],
) -> NodeReport:
    checks: list[CheckResult] = []
    for container in spec.containers:
        checks.extend(_container_checks(spec, container, transport, window_seconds, clock))
    for unit in spec.units:
        checks.extend(_unit_checks(spec, unit.name, transport, window_seconds, clock))
    for probe in spec.health:
        checks.extend(_health_checks(spec, probe, transport, clock))
    for probe in spec.metrics:
        checks.append(_metric_check(spec, probe, transport))
    for container in spec.containers:
        if container.scan_logs:
            checks.append(_log_check(spec, container.name, transport, window_seconds))
    return NodeReport(node=spec.name, host=spec.host, checks=tuple(checks))


def _container_checks(
    spec: NodeSpec,
    container: ContainerSpec,
    transport: Transport,
    window_seconds: int,
    clock: Callable[[], datetime],
) -> list[CheckResult]:
    argv = (
        *spec.docker,
        "inspect",
        "-f",
        "{{.State.Status}}|{{.RestartCount}}|{{.State.StartedAt}}",
        container.name,
    )
    try:
        done = transport.exec(spec.host, argv)
    except Exception as exc:
        reason = f"транспорт: {exc}"
        return [
            _fail("container", container.name, f"не удалось прочитать состояние: {reason}"),
            _fail("restarts", container.name, f"состояние неизвестно: {reason}"),
        ]
    if done.returncode != 0:
        reason = _first_line(done.stderr) or f"код {done.returncode}"
        return [
            _fail("container", container.name, f"не удалось прочитать состояние: {reason}"),
            _fail("restarts", container.name, f"состояние неизвестно: {reason}"),
        ]

    parts = done.stdout.strip().split("|")
    if len(parts) != 3:
        reason = f"неразборный ответ docker inspect: {done.stdout.strip()!r}"
        return [
            _fail("container", container.name, reason),
            _fail("restarts", container.name, reason),
        ]
    state, count_raw, started_raw = parts
    if state != "running":
        return [
            _fail("container", container.name, f"состояние {state}, а не running"),
            _fail("restarts", container.name, f"контейнер не running ({state})"),
        ]

    started = _parse_ts(started_raw)
    if started is None:
        return [
            _pass("container", container.name, "running"),
            _fail("restarts", container.name, f"время запуска не разобрано: {started_raw!r}"),
        ]

    try:
        restart_count = int(count_raw)
    except ValueError:
        return [
            _pass("container", container.name, "running"),
            _fail("restarts", container.name, f"RestartCount не число: {count_raw!r}"),
        ]

    age = (clock() - started).total_seconds()
    status = _pass("container", container.name, f"running, uptime {_fmt_age(age)}")
    if restart_count > 0 and age <= window_seconds:
        restarts = _fail(
            "restarts",
            container.name,
            f"перезапуск в окне: RestartCount={restart_count}, запуск {_fmt_age(age)} назад",
        )
    elif restart_count > 0:
        restarts = _pass(
            "restarts",
            container.name,
            f"RestartCount={restart_count}, последний запуск {_fmt_age(age)} назад (вне окна)",
        )
    else:
        restarts = _pass("restarts", container.name, "RestartCount=0")
    return [status, restarts]


def _unit_checks(
    spec: NodeSpec,
    unit: str,
    transport: Transport,
    window_seconds: int,
    clock: Callable[[], datetime],
) -> list[CheckResult]:
    argv = (
        "systemctl",
        "show",
        "-p",
        "ActiveState",
        "-p",
        "SubState",
        "-p",
        "ActiveEnterTimestamp",
        "-p",
        "NRestarts",
        "--timestamp=unix",
        unit,
    )
    try:
        done = transport.exec(spec.host, argv)
    except Exception as exc:
        reason = f"транспорт: {exc}"
        return [
            _fail("unit", unit, f"не удалось прочитать состояние: {reason}"),
            _fail("unit_restarts", unit, f"состояние неизвестно: {reason}"),
        ]
    if done.returncode != 0:
        reason = _first_line(done.stderr) or f"код {done.returncode}"
        return [
            _fail("unit", unit, f"не удалось прочитать состояние: {reason}"),
            _fail("unit_restarts", unit, f"состояние неизвестно: {reason}"),
        ]

    fields = {}
    for line in done.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key.strip()] = value.strip()
    active = fields.get("ActiveState", "unknown")
    sub = fields.get("SubState", "unknown")
    if active != "active" or sub != "running":
        unit_check = _fail("unit", unit, f"{active}/{sub}, а не active/running")
    else:
        unit_check = _pass("unit", unit, f"{active}/{sub}")

    entered = _parse_unit_timestamp(fields.get("ActiveEnterTimestamp", ""))
    raw_restarts = fields.get("NRestarts", "0")
    try:
        restarts_count = int(raw_restarts)
    except ValueError:
        restarts = _fail("unit_restarts", unit, f"NRestarts не разобран: {raw_restarts!r}")
    else:
        if entered is None:
            restarts = _fail("unit_restarts", unit, "время запуска не разобрано")
        else:
            age = (clock() - entered).total_seconds()
            if restarts_count > 0 and age <= window_seconds:
                restarts = _fail(
                    "unit_restarts",
                    unit,
                    f"перезапуск в окне: NRestarts={restarts_count}, активен {_fmt_age(age)}",
                )
            else:
                restarts = _pass(
                    "unit_restarts",
                    unit,
                    f"NRestarts={restarts_count}, активен {_fmt_age(age)}",
                )
    return [unit_check, restarts]


def _health_checks(
    spec: NodeSpec,
    probe: HealthProbe,
    transport: Transport,
    clock: Callable[[], datetime],
) -> list[CheckResult]:
    try:
        response = transport.http_get(spec.host, probe.url)
    except Exception as exc:
        response = HttpResult(probe.url, None, "", f"транспорт: {exc}")

    problem: str | None = None
    payload: dict | None = None
    if response.status is None:
        problem = f"нет ответа: {response.error or 'причина неизвестна'}"
    elif response.status != probe.expect_status:
        problem = f"HTTP {response.status}, ожидался {probe.expect_status}"
    elif probe.body_contains is not None and probe.body_contains not in response.body:
        problem = f"в ответе нет {probe.body_contains!r}"
    elif probe.fresh_json_field is not None or probe.forbid_json:
        try:
            decoded = json.loads(response.body)
        except ValueError as exc:
            problem = f"ответ не JSON: {exc}"
        else:
            if not isinstance(decoded, dict):
                problem = "ответ JSON не объект"
            else:
                payload = decoded
                for field, value in probe.forbid_json:
                    if str(payload.get(field)) == value:
                        problem = f"{field}={value}"
                        break

    evidence = (_snippet(response.body),) if problem and response.body else ()
    checks = [
        CheckResult(
            kind="health",
            target=probe.url,
            ok=problem is None,
            detail=problem or f"HTTP {response.status}",
            evidence=evidence,
        )
    ]
    if probe.fresh_json_field is not None:
        checks.append(_heartbeat_check(probe, payload, problem, clock))
    return checks


def _heartbeat_check(
    probe: HealthProbe,
    payload: dict | None,
    problem: str | None,
    clock: Callable[[], datetime],
) -> CheckResult:
    if problem is not None:
        return _fail("heartbeat", probe.url, f"нет данных heartbeat: {problem}")
    raw = payload.get(probe.fresh_json_field) if payload else None
    if not isinstance(raw, str):
        return _fail("heartbeat", probe.url, f"в ответе нет {probe.fresh_json_field}")
    observed_at = _parse_ts(raw)
    if observed_at is None:
        return _fail("heartbeat", probe.url, f"{probe.fresh_json_field} не разобран: {raw!r}")
    age = (clock() - observed_at).total_seconds()
    if age > probe.max_age_seconds:
        return _fail(
            "heartbeat",
            probe.url,
            f"heartbeat устарел: {_fmt_age(age)} назад (порог {probe.max_age_seconds}s)",
        )
    if age < -probe.max_age_seconds:
        return _fail("heartbeat", probe.url, f"время из будущего: {_fmt_age(-age)} вперёд")
    return _pass("heartbeat", probe.url, f"{probe.fresh_json_field} {_fmt_age(age)} назад")


def _metric_check(spec: NodeSpec, probe: MetricProbe, transport: Transport) -> CheckResult:
    try:
        response = transport.http_get(spec.host, probe.url)
    except Exception as exc:
        return _fail("metrics", probe.series, f"нет ответа: транспорт: {exc}")
    if response.status is None:
        return _fail("metrics", probe.series, f"нет ответа: {response.error or 'причина неизвестна'}")
    if response.status != 200:
        return _fail("metrics", probe.series, f"HTTP {response.status} на {probe.url}")
    value = _metric_value(response.body, probe.series)
    if value is None:
        return _fail("metrics", probe.series, f"серия {probe.series} не найдена на {probe.url}")
    if math.isnan(value) or value < probe.min_value:
        return _fail(
            "metrics",
            probe.series,
            f"значение {probe.series} = {value:g} меньше {probe.min_value:g}",
        )
    return _pass("metrics", probe.series, f"{value:g} (порог {probe.min_value:g})")


def _log_check(
    spec: NodeSpec,
    container: str,
    transport: Transport,
    window_seconds: int,
) -> CheckResult:
    argv = (*spec.docker, "logs", "--since", f"{window_seconds}s", container)
    try:
        done = transport.exec(spec.host, argv)
    except Exception as exc:
        return _fail("logs", container, f"логи недоступны: транспорт: {exc}")
    if done.returncode != 0:
        reason = _first_line(done.stderr) or f"код {done.returncode}"
        return _fail("logs", container, f"логи недоступны: {reason}")

    text = done.stdout
    if done.stderr:
        text = text + ("" if not text or text.endswith("\n") else "\n") + done.stderr
    matches = [line for line in text.splitlines() if LOG_PATTERN.search(line)]
    window = _fmt_window(window_seconds)
    if not matches:
        return _pass("logs", container, f"ERROR/PANIC/fatal за {window} нет")
    evidence = tuple(_snippet(line) for line in matches[:MAX_EVIDENCE_LINES])
    return CheckResult(
        kind="logs",
        target=container,
        ok=False,
        detail=f"{len(matches)} совпадений ERROR/PANIC/fatal за {window}",
        evidence=evidence,
    )


def _metric_value(body: str, series: str) -> float | None:
    for line in body.splitlines():
        if not line or line.startswith("#") or not line.startswith(series):
            continue
        rest = line[len(series) :]
        if not rest or not rest[0].isspace():
            continue
        tokens = rest.split()
        if not tokens:
            continue
        try:
            return float(tokens[0])
        except ValueError:
            return None
    return None


def _parse_ts(raw: str) -> datetime | None:
    value = raw.strip()
    if not value or value.startswith("n/a") or value.startswith("0001-01-01"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_unit_timestamp(raw: str) -> datetime | None:
    value = raw.strip()
    if value.startswith("@"):
        value = value[1:]
    if not value.isdigit():
        return None
    ts = int(value)
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _fmt_age(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m" if secs == 0 else f"{minutes}m {secs}s"
    return f"{secs}s"


def _fmt_window(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return _snippet(line.strip())
    return ""


def _snippet(text: str) -> str:
    one_line = " ".join(text.split())
    if len(one_line) > MAX_EVIDENCE_CHARS:
        return one_line[:MAX_EVIDENCE_CHARS] + "…"
    return one_line


def _pass(kind: str, target: str, detail: str) -> CheckResult:
    return CheckResult(kind=kind, target=target, ok=True, detail=detail)


def _fail(kind: str, target: str, detail: str, evidence: tuple[str, ...] = ()) -> CheckResult:
    return CheckResult(kind=kind, target=target, ok=False, detail=detail, evidence=evidence)
