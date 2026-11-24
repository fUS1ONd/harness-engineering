"""Обёртка white-box debug-хуков `ru.rumap.android.debug` через `am broadcast`.

Контракт зеркалит `DebugVerificationContract.kt` и `DebugVerificationController.kt`
из `rumap-android`: те же action, extras, wire-команды и допустимые значения target/preset.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from .android import CommandResult, CommandRunner, SubprocessRunner, find_local_adb
from .core import StandError

ACTION_VERIFICATION_COMMAND = "ru.rumap.android.debug.VERIFICATION_COMMAND"
DEFAULT_DEBUG_PACKAGE = "ru.rumap.android.debug"
DEBUG_RECEIVER_CLASS = "ru.rumap.android.debug.DebugVerificationReceiver"

COMMAND_NAVIGATE = "navigate"
COMMAND_MOCK_STATE = "mock_state"
COMMAND_RESET_STATE = "reset_state"
COMMAND_DUMP_DIAGNOSTICS = "dump_diagnostics"

WIRE_COMMANDS = frozenset(
    {
        COMMAND_NAVIGATE,
        COMMAND_MOCK_STATE,
        COMMAND_RESET_STATE,
        COMMAND_DUMP_DIAGNOSTICS,
    }
)

NAVIGATE_TARGETS = frozenset(
    {
        "home",
        "main",
        "check",
        "settings",
        "region",
        "anchors",
        "diaglog",
        "benchmark",
        "calibration",
        "calibration_advanced",
        "update",
        "advanced",
        "help",
        "battery_onboarding",
        "region_onboarding",
        "mandatory_update",
    }
)

MOCK_PRESETS = frozenset(
    {
        "clear",
        "none",
        "offline",
        "degraded_cellular",
        "vpn_active",
        "center_sync_error",
    }
)

_BROADCAST_COMPLETED = re.compile(r"Broadcast completed:\s*result=(-?\d+)")
_JSON_START = re.compile(r"[\[{]")


@dataclass(frozen=True)
class DebugHookResult:
    """Структурированный ответ debug-приёмника приложения."""

    ok: bool
    command: str
    detail: str = ""
    error: str | None = None
    output_file: str | None = None
    diagnostics: dict[str, Any] | None = None
    broadcast_result: int = 0
    raw_stdout: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_command(command: str) -> str:
    normalized = command.strip().lower().replace("-", "_")
    if normalized not in WIRE_COMMANDS:
        allowed = ", ".join(sorted(WIRE_COMMANDS))
        raise StandError(f"Неизвестная debug-команда: {command!r}. Допустимые: {allowed}")
    return normalized


def _normalize_target(target: str | None) -> str:
    normalized = (target or "").strip().lower()
    if normalized not in NAVIGATE_TARGETS:
        allowed = ", ".join(sorted(NAVIGATE_TARGETS))
        raise StandError(f"Неизвестный target: {target!r}. Допустимые: {allowed}")
    return normalized


def _normalize_preset(preset: str | None) -> str:
    normalized = (preset or "").strip().lower()
    if normalized not in MOCK_PRESETS:
        allowed = ", ".join(sorted(MOCK_PRESETS))
        raise StandError(f"Неизвестный mock-пресет: {preset!r}. Допустимые: {allowed}")
    return normalized


def build_broadcast_command(
    command: str,
    *,
    adb_bin: str,
    device: str | None = None,
    package: str = DEFAULT_DEBUG_PACKAGE,
    target: str | None = None,
    preset: str | None = None,
    error_message: str | None = None,
    output_path: str | None = None,
) -> list[str]:
    """Собирает argv для `adb shell am broadcast` с extras контракта debug-хуков."""
    wire_command = _normalize_command(command)
    extras: list[str] = ["--es", "command", wire_command]

    if wire_command == COMMAND_NAVIGATE:
        extras.extend(["--es", "target", _normalize_target(target)])
    elif wire_command == COMMAND_MOCK_STATE:
        extras.extend(["--es", "preset", _normalize_preset(preset)])
        message = (error_message or "").strip()
        if message:
            extras.extend(["--es", "error_message", message])
    elif wire_command == COMMAND_DUMP_DIAGNOSTICS:
        path = (output_path or "").strip()
        if path:
            extras.extend(["--es", "output_path", path])

    component = f"{(package or DEFAULT_DEBUG_PACKAGE).strip()}/{DEBUG_RECEIVER_CLASS}"
    argv: list[str] = [adb_bin]
    if device:
        argv.extend(["-s", device])
    # Приёмник отдаёт JSON через setResult только для ordered broadcast, поэтому --ordered обязателен.
    argv.extend(["shell", "am", "broadcast", "--ordered", "-a", ACTION_VERIFICATION_COMMAND, "-n", component])
    argv.extend(extras)
    return argv


def _decode_json_payload(text: str) -> dict[str, Any] | None:
    """Извлекает JSON-объект из `data="..."` ответа `am broadcast`."""
    marker = "data="
    idx = text.find(marker)
    haystack = text[idx + len(marker) :] if idx != -1 else text
    match = _JSON_START.search(haystack)
    if match is None:
        return None
    candidate = haystack[match.start() :].lstrip()

    decoder = json.JSONDecoder()
    try:
        payload, _end = decoder.raw_decode(candidate)
    except json.JSONDecodeError:
        payload = None

    if payload is None:
        unescaped = candidate.replace('\\"', '"').replace("\\\\", "\\")
        try:
            payload, _end = decoder.raw_decode(unescaped)
        except json.JSONDecodeError:
            payload = None

    if not isinstance(payload, dict):
        return None
    return payload


def parse_broadcast_output(text: str) -> tuple[int, dict[str, Any] | None]:
    """Разбирает `Broadcast completed: result=<n>, data="<json>"` в result и JSON-объект."""
    match = _BROADCAST_COMPLETED.search(text)
    if match is None:
        raise StandError(
            "Не удалось разобрать ответ `am broadcast`: нет строки 'Broadcast completed: result=…'"
        )
    result = int(match.group(1))
    return result, _decode_json_payload(text)


class DebugHooksClient:
    """Клиент white-box debug-хуков: валидирует команды и разбирает ответ приёмника."""

    def __init__(
        self,
        *,
        device: str | None = None,
        package: str = DEFAULT_DEBUG_PACKAGE,
        adb_bin: str | None = None,
        runner: CommandRunner | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.device = device
        self.package = package or DEFAULT_DEBUG_PACKAGE
        self._adb_bin = adb_bin
        self.runner: CommandRunner = runner or SubprocessRunner()
        self.timeout_s = timeout_s

    @property
    def adb_bin(self) -> str:
        if self._adb_bin is None:
            self._adb_bin = find_local_adb()
        return self._adb_bin

    def build_command(self, command: str, **extras: Any) -> list[str]:
        return build_broadcast_command(
            command,
            adb_bin=self.adb_bin,
            device=self.device,
            package=self.package,
            **extras,
        )

    def run_command(self, command: str, **extras: Any) -> DebugHookResult:
        wire_command = _normalize_command(command)
        argv = self.build_command(wire_command, **extras)
        result: CommandResult = self.runner.run(argv, timeout=self.timeout_s)
        if result.returncode != 0:
            details = result.stderr_text.strip() or result.stdout_text.strip() or f"code={result.returncode}"
            raise StandError(f"`am broadcast` завершился с ошибкой: {details}")

        raw = result.stdout_text
        broadcast_result, payload = parse_broadcast_output(raw)
        if payload is None:
            return DebugHookResult(
                ok=True,
                command=wire_command,
                broadcast_result=broadcast_result,
                raw_stdout=raw,
            )
        if payload.get("ok") is False:
            reason = payload.get("error") or "приёмник отклонил команду"
            raise StandError(f"Debug-команда '{wire_command}' отклонена: {reason}")

        return DebugHookResult(
            ok=True,
            command=str(payload.get("command") or wire_command),
            detail=str(payload.get("detail") or ""),
            error=payload.get("error"),
            output_file=payload.get("output_file"),
            diagnostics=payload.get("diagnostics"),
            broadcast_result=broadcast_result,
            raw_stdout=raw,
        )

    def navigate(self, target: str) -> DebugHookResult:
        return self.run_command(COMMAND_NAVIGATE, target=target)

    def mock_state(self, preset: str, error_message: str | None = None) -> DebugHookResult:
        return self.run_command(COMMAND_MOCK_STATE, preset=preset, error_message=error_message)

    def reset_state(self) -> DebugHookResult:
        return self.run_command(COMMAND_RESET_STATE)

    def dump_diagnostics(self, output_path: str | None = None) -> DebugHookResult:
        return self.run_command(COMMAND_DUMP_DIAGNOSTICS, output_path=output_path)
