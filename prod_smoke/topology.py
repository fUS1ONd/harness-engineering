"""Топология прода `rumap`: роли, хосты и проверки по умолчанию.

Значения сняты с живого прода 2026-11-25: центр — `rumap-center:/opt/rumap-center`,
точка и рабочий `ru` — `home:~/rumap-v2/deploy/*`, рабочий `control` — сервис compose центра.

Логи Caddy не сканируются: её `error` — эхо недоступного центра (502 на `/worker/tasks`
во время перезапуска), а не отдельная поломка. Prometheus на точке — наблюдатель, его
здоровье не входит в слой сервиса: метрики читаются прямо с `/metrics` замера.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

CENTER_METRICS = "http://127.0.0.1:8090/metrics"
POINT_STATUS = "http://127.0.0.1:9110/status"
POINT_METRICS = "http://127.0.0.1:9110/metrics"
DOCKER_ROOT: tuple[str, ...] = ("docker",)
DOCKER_SUDO: tuple[str, ...] = ("sudo", "-n", "docker")


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    scan_logs: bool = True


@dataclass(frozen=True)
class UnitSpec:
    name: str


@dataclass(frozen=True)
class HealthProbe:
    name: str
    url: str
    expect_status: int = 200
    fresh_json_field: str | None = None
    max_age_seconds: int = 180
    forbid_json: tuple[tuple[str, str], ...] = ()
    body_contains: str | None = None


@dataclass(frozen=True)
class MetricProbe:
    name: str
    url: str
    series: str
    min_value: float = 1.0


@dataclass(frozen=True)
class NodeSpec:
    name: str
    host: str
    docker: tuple[str, ...]
    containers: tuple[ContainerSpec, ...] = ()
    units: tuple[UnitSpec, ...] = ()
    health: tuple[HealthProbe, ...] = ()
    metrics: tuple[MetricProbe, ...] = ()


def default_roles(hosts: Mapping[str, str] | None = None) -> dict[str, tuple[NodeSpec, ...]]:
    overrides = dict(hosts or {})
    center_host = overrides.get("center", "rumap-center")
    point_host = overrides.get("point", "home")
    worker_ru_host = overrides.get("worker-ru", "home")
    worker_control_host = overrides.get("worker-control", "rumap-center")

    center = NodeSpec(
        name="center",
        host=center_host,
        docker=DOCKER_ROOT,
        containers=(
            ContainerSpec("rumap-center-rumap-center-1"),
            ContainerSpec("rumap-center-rumap-worker-1"),
            ContainerSpec("rumap-center-caddy-1", scan_logs=False),
        ),
        units=(UnitSpec("rumap-center-socks.service"),),
        health=(
            HealthProbe(name="health", url=CENTER_METRICS, body_contains="rumap_blockcheck_worker_online"),
            HealthProbe(
                name="feed",
                url=POINT_STATUS,
                fresh_json_field="observed_at",
                forbid_json=(("phase", "DOWN"),),
            ),
        ),
        metrics=(
            MetricProbe(name="worker-control", url=CENTER_METRICS, series='rumap_blockcheck_worker_online{role="control"}'),
            MetricProbe(name="worker-ru", url=CENTER_METRICS, series='rumap_blockcheck_worker_online{role="ru"}'),
        ),
    )

    worker_ru = NodeSpec(
        name="worker-ru",
        host=worker_ru_host,
        docker=DOCKER_SUDO,
        containers=(ContainerSpec("rumap-worker-rumap-worker-1"),),
    )
    worker_control = NodeSpec(
        name="worker-control",
        host=worker_control_host,
        docker=DOCKER_ROOT,
        containers=(ContainerSpec("rumap-center-rumap-worker-1"),),
        health=(
            HealthProbe(name="health", url=CENTER_METRICS, body_contains="rumap_blockcheck_worker_online"),
        ),
        metrics=(
            MetricProbe(name="worker-control", url=CENTER_METRICS, series='rumap_blockcheck_worker_online{role="control"}'),
            MetricProbe(name="worker-ru", url=CENTER_METRICS, series='rumap_blockcheck_worker_online{role="ru"}'),
        ),
    )

    point = NodeSpec(
        name="point",
        host=point_host,
        docker=DOCKER_SUDO,
        containers=(ContainerSpec("rumap-point-rumapd-1"),),
        units=(UnitSpec("rumap-center-tunnel.service"),),
        health=(
            HealthProbe(
                name="health",
                url=POINT_STATUS,
                fresh_json_field="observed_at",
                forbid_json=(("phase", "DOWN"),),
            ),
            HealthProbe(name="metrics", url=POINT_METRICS, body_contains="rumap_point_journal_bytes"),
        ),
    )

    return {
        "center": (center,),
        "worker": (worker_ru, worker_control),
        "point": (point,),
    }
