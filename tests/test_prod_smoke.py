#!/usr/bin/env python3
"""Юнит-тесты `prod-smoke`: подменяется только транспорт (SSH/HTTP)."""

from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import Sequence

from prod_smoke import default_roles, run_smoke
from prod_smoke.cli import main, parse_window, render_text
from prod_smoke.transport import ExecResult, HttpResult

NOW = datetime(2026, 11, 25, 16, 45, tzinfo=timezone.utc)
OLD_DOCKER_START = "2026-11-20T00:00:00Z"
OLD_UNIT_START = "@1760000000"
CENTER_METRICS = "http://127.0.0.1:8090/metrics"
POINT_STATUS = "http://127.0.0.1:9110/status"
POINT_METRICS = "http://127.0.0.1:9110/metrics"
WORKER_ONLINE = (
    "# HELP rumap_blockcheck_worker_online 1 — рабочий роли на связи.\n"
    "# TYPE rumap_blockcheck_worker_online gauge\n"
    'rumap_blockcheck_worker_online{role="control"} 1\n'
    'rumap_blockcheck_worker_online{role="ru"} 1\n'
)
POINT_METRICS_BODY = (
    "# HELP rumap_point_journal_bytes Сколько занимает каталог журнала.\n"
    "rumap_point_journal_bytes 1.8e+07\n"
)


def iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def inspect_ok(state: str = "running", restarts: int = 0, started: str = OLD_DOCKER_START) -> ExecResult:
    return ExecResult(("docker", "inspect"), 0, f"{state}|{restarts}|{started}", "")


def unit_ok(
    active: str = "active",
    sub: str = "running",
    starts: int = 0,
    entered: str = OLD_UNIT_START,
) -> ExecResult:
    return ExecResult(
        ("systemctl", "show"),
        0,
        f"ActiveState={active}\nSubState={sub}\nActiveEnterTimestamp={entered}\nNRestarts={starts}\n",
        "",
    )


def status_body(observed_at: datetime, phase: str = "OFF") -> str:
    return json.dumps(
        {"phase": phase, "since": iso(observed_at - timedelta(days=1)), "observed_at": iso(observed_at), "seq": 1}
    )


class FakeTransport:
    """Транспорт по фактам: инспекция, логи и юниты — по имени, HTTP — по адресу."""

    def __init__(self) -> None:
        self.inspect: dict[tuple[str, str], ExecResult] = {}
        self.logs: dict[tuple[str, str], ExecResult] = {}
        self.units: dict[tuple[str, str], ExecResult] = {}
        self.http: dict[tuple[str, str], HttpResult] = {}
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def exec(self, host: str, argv: Sequence[str], timeout: float = 30.0) -> ExecResult:
        argv = tuple(argv)
        self.calls.append((host, argv))
        target = argv[-1]
        if "inspect" in argv:
            return self.inspect.get((host, target), ExecResult(argv, 1, "", f"Error: No such object: {target}"))
        if "logs" in argv:
            return self.logs.get((host, target), ExecResult(argv, 0, "", ""))
        if argv and argv[0] == "systemctl":
            return self.units.get((host, target), ExecResult(argv, 1, "", "Unit not found"))
        raise AssertionError(f"неожиданная команда: {host} {argv}")

    def http_get(self, host: str, url: str, timeout: float = 10.0) -> HttpResult:
        self.calls.append((host, ("http", url)))
        return self.http.get((host, url), HttpResult(url, None, "", "нет соединения"))


def healthy_fake(now: datetime = NOW) -> FakeTransport:
    fake = FakeTransport()
    bodies: dict[tuple[str, str], list[str]] = {}
    for nodes in default_roles().values():
        for node in nodes:
            for container in node.containers:
                fake.inspect[(node.host, container.name)] = inspect_ok()
            for unit in node.units:
                fake.units[(node.host, unit.name)] = unit_ok()
            for probe in node.health:
                if probe.fresh_json_field:
                    body = status_body(now - timedelta(seconds=30))
                elif "8090" in probe.url:
                    body = WORKER_ONLINE
                else:
                    body = POINT_METRICS_BODY
                bodies.setdefault((node.host, probe.url), []).append(body)
            for probe in node.metrics:
                bodies.setdefault((node.host, probe.url), []).append(f"{probe.series} 1")
    for key, chunks in bodies.items():
        fake.http[key] = HttpResult(key[1], 200, "\n".join(chunks), "")
    return fake


def failure_kinds(report) -> list[str]:
    return [f.kind for f in report.failures]


class HealthyNodeTest(unittest.TestCase):
    def test_all_roles_healthy(self) -> None:
        report = run_smoke("all", healthy_fake(), window_seconds=300, now=lambda: NOW)

        self.assertTrue(report.ok, report.failures)
        self.assertEqual([role.role for role in report.roles], ["center", "worker", "point"])
        self.assertGreaterEqual(report.checks_total, 15)
        self.assertEqual(report.failures, ())

    def test_each_role_alone_is_healthy(self) -> None:
        for role in ("center", "worker", "point"):
            with self.subTest(role=role):
                report = run_smoke(role, healthy_fake(), window_seconds=300, now=lambda: NOW)
                self.assertTrue(report.ok, report.failures)
                self.assertEqual([item.role for item in report.roles], [role])

    def test_json_report_shape(self) -> None:
        report = run_smoke("center", healthy_fake(), window_seconds=600, now=lambda: NOW)
        data = report.to_dict()

        self.assertEqual(data["role"], "center")
        self.assertEqual(data["window_seconds"], 600)
        self.assertTrue(data["ok"])
        self.assertEqual(data["checks_total"], len(report.roles[0].nodes[0].checks))
        center = data["roles"][0]
        self.assertEqual(center["role"], "center")
        self.assertEqual(center["nodes"][0]["node"], "center")
        kinds = {check["kind"] for check in center["nodes"][0]["checks"]}
        self.assertTrue({"container", "restarts", "unit", "health", "heartbeat", "metrics", "logs"} <= kinds)
        json.dumps(data)


class CrashLoopTest(unittest.TestCase):
    def test_container_restarted_inside_window(self) -> None:
        fake = healthy_fake()
        fake.inspect[("rumap-center", "rumap-center-rumap-center-1")] = inspect_ok(
            restarts=2, started=iso(NOW - timedelta(seconds=40))
        )

        report = run_smoke("center", fake, window_seconds=300, now=lambda: NOW)

        self.assertFalse(report.ok)
        self.assertIn("restarts", failure_kinds(report))
        detail = next(f.detail for f in report.failures if f.kind == "restarts")
        self.assertIn("RestartCount=2", detail)

    def test_restart_outside_window_is_not_a_failure(self) -> None:
        fake = healthy_fake()
        fake.inspect[("rumap-center", "rumap-center-rumap-center-1")] = inspect_ok(
            restarts=2, started=iso(NOW - timedelta(days=2))
        )

        report = run_smoke("center", fake, window_seconds=300, now=lambda: NOW)

        self.assertTrue(report.ok, report.failures)

    def test_container_not_running(self) -> None:
        fake = healthy_fake()
        fake.inspect[("home", "rumap-point-rumapd-1")] = inspect_ok(state="exited", started=iso(NOW))

        report = run_smoke("point", fake, window_seconds=300, now=lambda: NOW)

        self.assertFalse(report.ok)
        self.assertIn("container", failure_kinds(report))

    def test_unit_restarted_inside_window(self) -> None:
        fake = healthy_fake()
        fake.units[("home", "rumap-center-tunnel.service")] = unit_ok(
            starts=4, entered=f"@{int((NOW - timedelta(seconds=20)).timestamp())}"
        )

        report = run_smoke("point", fake, window_seconds=300, now=lambda: NOW)

        self.assertFalse(report.ok)
        self.assertIn("unit_restarts", failure_kinds(report))

    def test_unit_inactive(self) -> None:
        fake = healthy_fake()
        fake.units[("rumap-center", "rumap-center-socks.service")] = unit_ok(active="failed", sub="failed")

        report = run_smoke("center", fake, window_seconds=300, now=lambda: NOW)

        self.assertFalse(report.ok)
        self.assertIn("unit", failure_kinds(report))


class LogScanTest(unittest.TestCase):
    def test_error_panic_fatal_are_failures(self) -> None:
        fake = healthy_fake()
        fake.logs[("home", "rumap-point-rumapd-1")] = ExecResult(
            ("docker", "logs"),
            0,
            "\n".join(
                [
                    '{"level":"INFO","msg":"замер запущен"}',
                    '{"level":"ERROR","msg":"модем пропал"}',
                    '{"level":"PANIC","msg":"недостижимо"}',
                    "2026-11-25T16:40:00Z fatal: нет связи",
                ]
            ),
            "",
        )

        report = run_smoke("point", fake, window_seconds=300, now=lambda: NOW)

        self.assertFalse(report.ok)
        failure = next(f for f in report.failures if f.kind == "logs")
        self.assertIn("3", failure.detail)
        self.assertEqual(len(failure.evidence), 3)
        self.assertIn("PANIC", " ".join(failure.evidence))

    def test_warn_noise_is_not_a_failure(self) -> None:
        fake = healthy_fake()
        fake.logs[("rumap-center", "rumap-center-rumap-center-1")] = ExecResult(
            ("docker", "logs"),
            0,
            '{"level":"WARN","msg":"сводка об AS не записана","ошибка":"HTTP 504"}\n',
            "",
        )

        report = run_smoke("center", fake, window_seconds=300, now=lambda: NOW)

        self.assertTrue(report.ok, report.failures)

    def test_logs_unavailable(self) -> None:
        fake = healthy_fake()
        fake.logs[("rumap-center", "rumap-center-rumap-center-1")] = ExecResult(
            ("docker", "logs"), 1, "", "Error: No such container"
        )

        report = run_smoke("center", fake, window_seconds=300, now=lambda: NOW)

        self.assertFalse(report.ok)
        self.assertIn("logs", failure_kinds(report))


class HealthAndHeartbeatTest(unittest.TestCase):
    def test_health_endpoint_refused(self) -> None:
        fake = healthy_fake()
        fake.http[("rumap-center", CENTER_METRICS)] = HttpResult(
            CENTER_METRICS, None, "", "curl: (7) Failed to connect"
        )

        report = run_smoke("center", fake, window_seconds=300, now=lambda: NOW)

        self.assertFalse(report.ok)
        self.assertIn("health", failure_kinds(report))
        self.assertIn("Failed to connect", next(f.detail for f in report.failures if f.kind == "health"))

    def test_http_500_on_health(self) -> None:
        fake = healthy_fake()
        fake.http[("home", POINT_STATUS)] = HttpResult(POINT_STATUS, 500, "boom", "")

        report = run_smoke("point", fake, window_seconds=300, now=lambda: NOW)

        self.assertFalse(report.ok)
        self.assertIn("health", failure_kinds(report))

    def test_stale_point_heartbeat(self) -> None:
        fake = healthy_fake()
        fake.http[("home", POINT_STATUS)] = HttpResult(
            POINT_STATUS, 200, status_body(NOW - timedelta(minutes=15)), ""
        )

        report = run_smoke("point", fake, window_seconds=300, now=lambda: NOW)

        self.assertFalse(report.ok)
        heartbeat = next(f for f in report.failures if f.kind == "heartbeat")
        self.assertIn("устарел", heartbeat.detail)

    def test_missing_observed_at(self) -> None:
        fake = healthy_fake()
        fake.http[("home", POINT_STATUS)] = HttpResult(
            POINT_STATUS, 200, json.dumps({"phase": "OFF"}), ""
        )

        report = run_smoke("point", fake, window_seconds=300, now=lambda: NOW)

        self.assertFalse(report.ok)
        self.assertIn("heartbeat", failure_kinds(report))

    def test_point_phase_down_is_unhealthy(self) -> None:
        fake = healthy_fake()
        fake.http[("home", POINT_STATUS)] = HttpResult(
            POINT_STATUS, 200, status_body(NOW - timedelta(seconds=10), phase="DOWN"), ""
        )

        report = run_smoke("point", fake, window_seconds=300, now=lambda: NOW)

        self.assertFalse(report.ok)
        self.assertIn("health", failure_kinds(report))

    def test_stale_center_feed(self) -> None:
        fake = healthy_fake()
        fake.http[("rumap-center", POINT_STATUS)] = HttpResult(
            POINT_STATUS, 200, status_body(NOW - timedelta(minutes=15)), ""
        )

        report = run_smoke("center", fake, window_seconds=300, now=lambda: NOW)

        self.assertFalse(report.ok)
        self.assertIn("heartbeat", failure_kinds(report))

    def test_missing_worker_online_metric(self) -> None:
        fake = healthy_fake()
        fake.http[("rumap-center", CENTER_METRICS)] = HttpResult(
            CENTER_METRICS,
            200,
            'rumap_blockcheck_worker_online{role="control"} 1\n',
            "",
        )

        report = run_smoke("worker", fake, window_seconds=300, now=lambda: NOW)

        self.assertFalse(report.ok)
        metric = next(f for f in report.failures if f.kind == "metrics")
        self.assertIn('role="ru"', metric.detail)

    def test_worker_online_zero(self) -> None:
        fake = healthy_fake()
        fake.http[("rumap-center", CENTER_METRICS)] = HttpResult(
            CENTER_METRICS,
            200,
            'rumap_blockcheck_worker_online{role="control"} 0\n'
            'rumap_blockcheck_worker_online{role="ru"} 1\n',
            "",
        )

        report = run_smoke("worker", fake, window_seconds=300, now=lambda: NOW)

        self.assertFalse(report.ok)
        metric = next(f for f in report.failures if f.kind == "metrics")
        self.assertIn("role=\"control\"", metric.detail)


class CliTest(unittest.TestCase):
    def test_json_output_and_exit_zero(self) -> None:
        out = io.StringIO()
        code = main(
            ["--role", "center", "--format", "json"],
            transport=healthy_fake(),
            now=lambda: NOW,
            stdout=out,
        )

        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertTrue(data["ok"])

    def test_text_output(self) -> None:
        out = io.StringIO()
        code = main(
            ["--role", "point", "--format", "text"],
            transport=healthy_fake(),
            now=lambda: NOW,
            stdout=out,
        )

        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("prod-smoke: OK", text)
        self.assertIn("[point]", text)

    def test_both_output_has_renderable_json(self) -> None:
        out = io.StringIO()
        code = main(
            ["--role", "worker", "--format", "both"],
            transport=healthy_fake(),
            now=lambda: NOW,
            stdout=out,
        )

        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("--- JSON ---", text)
        data = json.loads(text.split("--- JSON ---", 1)[1])
        self.assertTrue(data["ok"])

    def test_failure_exit_code_and_details(self) -> None:
        fake = healthy_fake()
        fake.logs[("rumap-center", "rumap-center-rumap-center-1")] = ExecResult(
            ("docker", "logs"), 0, '{"level":"ERROR","msg":"центр упал"}', ""
        )
        out = io.StringIO()

        code = main(
            ["--role", "center", "--format", "text"],
            transport=fake,
            now=lambda: NOW,
            stdout=out,
        )

        text = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("prod-smoke: FAIL", text)
        self.assertIn("центр упал", text)

    def test_window_parsing(self) -> None:
        self.assertEqual(parse_window("300"), 300)
        self.assertEqual(parse_window("5m"), 300)
        self.assertEqual(parse_window("90s"), 90)
        self.assertEqual(parse_window("1h"), 3600)
        self.assertEqual(parse_window("1h30m"), 5400)
        for bad in ("", "0", "-5m", "abc", "5x"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                parse_window(bad)

    def test_window_reaches_docker_logs(self) -> None:
        fake = healthy_fake()
        code = main(
            ["--role", "center", "--format", "json", "--window", "10m"],
            transport=fake,
            now=lambda: NOW,
            stdout=io.StringIO(),
        )

        self.assertEqual(code, 0)
        logs_calls = [argv for _, argv in fake.calls if "logs" in argv]
        self.assertTrue(logs_calls)
        self.assertTrue(all(argv[argv.index("--since") + 1] == "600s" for argv in logs_calls))

    def test_host_override(self) -> None:
        roles = default_roles({"center": "center-2", "point": "point-2", "worker-ru": "worker-2"})

        self.assertEqual(roles["center"][0].host, "center-2")
        self.assertEqual(roles["point"][0].host, "point-2")
        self.assertEqual(roles["worker"][0].host, "worker-2")
        self.assertEqual(roles["worker"][1].host, "rumap-center")


class RenderTest(unittest.TestCase):
    def test_render_shows_failure_evidence(self) -> None:
        fake = healthy_fake()
        fake.logs[("home", "rumap-point-rumapd-1")] = ExecResult(
            ("docker", "logs"), 0, '{"level":"PANIC","msg":"модем"}', ""
        )
        report = run_smoke("point", fake, window_seconds=300, now=lambda: NOW)

        text = render_text(report)

        self.assertIn("✘", text)
        self.assertIn("ПРОВАЛЫ", text)
        self.assertIn("PANIC", text)


if __name__ == "__main__":
    unittest.main()
