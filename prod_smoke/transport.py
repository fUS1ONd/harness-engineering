"""Транспорт `prod-smoke`: единственный шов к хостам (SSH-команды и HTTP-пробы)."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class ExecResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class HttpResult:
    url: str
    status: int | None
    body: str
    error: str = ""


class Transport(Protocol):
    def exec(self, host: str, argv: Sequence[str], timeout: float = 30.0) -> ExecResult: ...

    def http_get(self, host: str, url: str, timeout: float = 10.0) -> HttpResult: ...


class SshTransport:
    """Read-only: только `ssh` с пакетным режимом и `curl` на удалённом хосте."""

    def __init__(self, ssh: str = "ssh", connect_timeout: int = 8) -> None:
        self.ssh = ssh
        self.connect_timeout = connect_timeout

    def exec(self, host: str, argv: Sequence[str], timeout: float = 30.0) -> ExecResult:
        argv = tuple(argv)
        command = [
            self.ssh,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            host,
            shlex.join(argv),
        ]
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ExecResult(argv, 255, "", f"таймаут {timeout:g} с: ssh {host}")
        except OSError as exc:
            return ExecResult(argv, 255, "", f"ssh {host}: {exc}")
        return ExecResult(argv, done.returncode, done.stdout, done.stderr)

    def http_get(self, host: str, url: str, timeout: float = 10.0) -> HttpResult:
        argv = (
            "curl",
            "-sS",
            "--max-time",
            str(max(1, int(timeout))),
            "-w",
            "\n%{http_code}",
            url,
        )
        done = self.exec(host, argv, timeout=timeout + 5)
        if done.returncode != 0:
            error = done.stderr.strip() or f"curl завершился с кодом {done.returncode}"
            return HttpResult(url, None, done.stdout, error)
        body, separator, code = done.stdout.rpartition("\n")
        if not separator:
            return HttpResult(url, None, done.stdout, "curl не вернул код ответа")
        try:
            status = int(code.strip())
        except ValueError:
            return HttpResult(url, None, body, f"неразборный код ответа curl: {code.strip()!r}")
        return HttpResult(url, status, body, done.stderr.strip())
