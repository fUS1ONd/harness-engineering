"""CLI `prod-smoke`: аргументы, текстовый отчёт и код возврата."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from typing import Callable, Sequence, TextIO

from .core import run_smoke
from .model import SmokeReport
from .topology import default_roles
from .transport import SshTransport, Transport

WINDOW_TOKEN = re.compile(r"(\d+)([smh])")
WINDOW_UNITS = {"s": 1, "m": 60, "h": 3600}


def parse_window(value: str) -> int:
    """Принимает `300`, `90s`, `5m`, `1h30m` и возвращает секунды."""
    text = str(value).strip()
    if not text:
        raise ValueError("пустое окно наблюдения")
    if text.isdigit():
        seconds = int(text)
        if seconds <= 0:
            raise ValueError("окно наблюдения должно быть положительным")
        return seconds

    seconds = 0
    position = 0
    while position < len(text):
        match = WINDOW_TOKEN.match(text, position)
        if match is None:
            raise ValueError(f"не разобрано окно наблюдения: {value!r}")
        seconds += int(match.group(1)) * WINDOW_UNITS[match.group(2)]
        position = match.end()
    if seconds <= 0:
        raise ValueError("окно наблюдения должно быть положительным")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prod-smoke",
        description="Read-only проверка здоровья прода rumap по ролям (center, worker, point).",
    )
    parser.add_argument(
        "--role",
        choices=("center", "worker", "point", "all"),
        default="all",
        help="какую роль проверять (по умолчанию all)",
    )
    parser.add_argument(
        "--window",
        type=parse_window,
        default=300,
        metavar="ОКНО",
        help="окно наблюдения за логами и рестартами: 300, 90s, 5m, 1h30m (по умолчанию 5m)",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text", "both"),
        default="both",
        help="формат вывода: json, text или both (по умолчанию both)",
    )
    parser.add_argument("--center-host", help="хост центра (по умолчанию rumap-center)")
    parser.add_argument("--point-host", help="хост точки (по умолчанию home)")
    parser.add_argument("--worker-ru-host", help="хост рабочего ru (по умолчанию home)")
    parser.add_argument("--worker-control-host", help="хост рабочего control (по умолчанию rumap-center)")
    return parser


def render_text(report: SmokeReport) -> str:
    state = "OK" if report.ok else "FAIL"
    window = _fmt_window(report.window_seconds)
    lines = [
        f"prod-smoke: {state} — роль={report.role}, окно={window}, "
        f"проверок={report.checks_total}, провалов={len(report.failures)}"
    ]
    for role in report.roles:
        for node in role.nodes:
            node_state = "OK" if node.ok else "FAIL"
            lines.append(f"[{role.role}] {node.node} ({node.host}) — {node_state}, проверок {len(node.checks)}")
            for check in node.checks:
                mark = "✔" if check.ok else "✘"
                lines.append(f"  {mark} {check.kind} {check.target}: {check.detail}")
                for snippet in check.evidence:
                    lines.append(f"      {snippet}")
    if report.failures:
        lines.append("")
        lines.append(f"ПРОВАЛЫ ({len(report.failures)}):")
        for failure in report.failures:
            lines.append(
                f"  [{failure.role}/{failure.node}/{failure.host}] "
                f"{failure.kind} {failure.target}: {failure.detail}"
            )
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Transport | None = None,
    now: Callable[[], datetime] | None = None,
    stdout: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    out = stdout or sys.stdout
    hosts = {
        key: value
        for key, value in (
            ("center", args.center_host),
            ("point", args.point_host),
            ("worker-ru", args.worker_ru_host),
            ("worker-control", args.worker_control_host),
        )
        if value
    }
    report = run_smoke(
        args.role,
        transport or SshTransport(),
        window_seconds=args.window,
        now=now,
        roles=default_roles(hosts),
    )
    if args.format in ("text", "both"):
        print(render_text(report), file=out)
    if args.format == "both":
        print("--- JSON ---", file=out)
    if args.format in ("json", "both"):
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), file=out)
    return 0 if report.ok else 1


def _fmt_window(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"
