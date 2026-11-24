#!/usr/bin/env python3
"""Unit-тесты white-box debug-хуков `ru.rumap.android.debug` (без реального adb)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from app_verify.android import CommandResult
from app_verify.cli import run_cli
from app_verify.core import StandError
from app_verify.debug_hooks import (
    ACTION_VERIFICATION_COMMAND,
    DEFAULT_DEBUG_PACKAGE,
    DEBUG_RECEIVER_CLASS,
    MOCK_PRESETS,
    NAVIGATE_TARGETS,
    DebugHooksClient,
    build_broadcast_command,
    parse_broadcast_output,
)


class _FakeRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[list[str]] = []
        self.timeouts: list[float | None] = []

    def run(self, args, *, timeout=None, input_bytes=None, env=None):
        self.calls.append(list(args))
        self.timeouts.append(timeout)
        return self.result


def _broadcast_stdout(payload: dict, *, result: int = -1, escaped: bool = False) -> str:
    raw = json.dumps(payload, ensure_ascii=False)
    if escaped:
        raw = raw.replace('"', '\\"')
    return f'Broadcast completed: result={result}, data="{raw}"\n'


class BroadcastCommandTests(unittest.TestCase):
    def test_navigate_builds_exact_am_broadcast_invocation(self) -> None:
        cmd = build_broadcast_command(
            "navigate",
            adb_bin="/opt/android-sdk/platform-tools/adb",
            device="127.0.0.1:15565",
            target="settings",
        )
        self.assertEqual(
            cmd,
            [
                "/opt/android-sdk/platform-tools/adb",
                "-s",
                "127.0.0.1:15565",
                "shell",
                "am",
                "broadcast",
                "--ordered",
                "-a",
                ACTION_VERIFICATION_COMMAND,
                "-n",
                f"{DEFAULT_DEBUG_PACKAGE}/{DEBUG_RECEIVER_CLASS}",
                "--es",
                "command",
                "navigate",
                "--es",
                "target",
                "settings",
            ],
        )

    def test_package_override_changes_component(self) -> None:
        cmd = build_broadcast_command(
            "navigate",
            adb_bin="adb",
            package="ru.rumap.android.staging.debug",
            target="home",
        )
        self.assertNotIn("-s", cmd)
        component = cmd[cmd.index("-n") + 1]
        self.assertEqual(
            component,
            "ru.rumap.android.staging.debug/ru.rumap.android.debug.DebugVerificationReceiver",
        )

    def test_mock_state_includes_preset_and_optional_error_message(self) -> None:
        cmd = build_broadcast_command(
            "mock_state",
            adb_bin="adb",
            preset="center_sync_error",
            error_message="Синхронизация недоступна",
        )
        self.assertEqual(cmd[-9:], [
            "--es",
            "command",
            "mock_state",
            "--es",
            "preset",
            "center_sync_error",
            "--es",
            "error_message",
            "Синхронизация недоступна",
        ])

    def test_reset_state_and_dump_diagnostics_extras(self) -> None:
        reset = build_broadcast_command("reset_state", adb_bin="adb")
        self.assertEqual(reset[-3:], ["--es", "command", "reset_state"])

        dump = build_broadcast_command(
            "dump_diagnostics",
            adb_bin="adb",
            output_path="/data/user/0/ru.rumap.android.debug/cache/diag.json",
        )
        self.assertEqual(
            dump[-6:],
            [
                "--es",
                "command",
                "dump_diagnostics",
                "--es",
                "output_path",
                "/data/user/0/ru.rumap.android.debug/cache/diag.json",
            ],
        )

    def test_cli_style_command_aliases_are_normalized(self) -> None:
        cmd = build_broadcast_command("mock-state", adb_bin="adb", preset="offline")
        self.assertIn("mock_state", cmd)

    def test_invalid_target_and_preset_are_rejected_before_adb(self) -> None:
        with self.assertRaises(StandError):
            build_broadcast_command("navigate", adb_bin="adb", target="not-a-screen")
        with self.assertRaises(StandError):
            build_broadcast_command("mock_state", adb_bin="adb", preset="explode")
        with self.assertRaises(StandError):
            build_broadcast_command("navigate", adb_bin="adb")
        with self.assertRaises(StandError):
            build_broadcast_command("teleport", adb_bin="adb")

    def test_contract_sets_match_rumap_android(self) -> None:
        self.assertEqual(
            NAVIGATE_TARGETS,
            frozenset(
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
            ),
        )
        self.assertEqual(
            MOCK_PRESETS,
            frozenset(
                {
                    "clear",
                    "none",
                    "offline",
                    "degraded_cellular",
                    "vpn_active",
                    "center_sync_error",
                }
            ),
        )
        self.assertEqual(ACTION_VERIFICATION_COMMAND, "ru.rumap.android.debug.VERIFICATION_COMMAND")


class BroadcastParsingTests(unittest.TestCase):
    def test_parses_plain_json_payload(self) -> None:
        result, payload = parse_broadcast_output(
            _broadcast_stdout({"ok": True, "command": "navigate", "detail": "home"})
        )
        self.assertEqual(result, -1)
        self.assertEqual(payload["command"], "navigate")
        self.assertEqual(payload["detail"], "home")

    def test_parses_escaped_json_payload(self) -> None:
        result, payload = parse_broadcast_output(
            _broadcast_stdout({"ok": True, "command": "mock_state", "detail": "offline"}, escaped=True)
        )
        self.assertEqual(result, -1)
        self.assertEqual(payload["detail"], "offline")

    def test_parses_payload_with_nested_diagnostics_and_multiline_noise(self) -> None:
        text = (
            "Broadcasting: Intent { act=ru.rumap.android.debug.VERIFICATION_COMMAND }\n"
            + _broadcast_stdout(
                {
                    "ok": True,
                    "command": "dump_diagnostics",
                    "diagnostics": {"screen": {"active_screen": "home"}, "storage": {"db_files": ["rumap.db"]}},
                }
            )
            + "Broadcast completed: result=-1\n"
        )
        result, payload = parse_broadcast_output(text)
        self.assertEqual(result, -1)
        self.assertEqual(payload["diagnostics"]["screen"]["active_screen"], "home")
        self.assertEqual(payload["diagnostics"]["storage"]["db_files"], ["rumap.db"])

    def test_missing_broadcast_banner_raises(self) -> None:
        with self.assertRaises(StandError):
            parse_broadcast_output("error: no such receiver\n")

    def test_payload_absent_is_tolerated(self) -> None:
        result, payload = parse_broadcast_output("Broadcast completed: result=-1\n")
        self.assertEqual(result, -1)
        self.assertIsNone(payload)


class DebugHooksClientTests(unittest.TestCase):
    def test_navigate_returns_structured_result(self) -> None:
        runner = _FakeRunner(
            CommandResult(
                returncode=0,
                stdout=_broadcast_stdout({"ok": True, "command": "navigate", "detail": "check"}).encode(),
            )
        )
        client = DebugHooksClient(adb_bin="adb", device="emulator-5554", runner=runner)
        res = client.navigate("check")
        self.assertTrue(res.ok)
        self.assertEqual(res.command, "navigate")
        self.assertEqual(res.detail, "check")
        self.assertEqual(res.broadcast_result, -1)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][:2], ["adb", "-s"])

    def test_mock_state_returns_diagnostics(self) -> None:
        diagnostics = {"mock_preset": "vpn_active", "screen": {"active_screen": "home"}}
        runner = _FakeRunner(
            CommandResult(
                returncode=0,
                stdout=_broadcast_stdout(
                    {"ok": True, "command": "mock_state", "detail": "vpn_active", "diagnostics": diagnostics}
                ).encode(),
            )
        )
        client = DebugHooksClient(adb_bin="adb", runner=runner)
        res = client.mock_state("vpn_active")
        self.assertEqual(res.diagnostics, diagnostics)
        self.assertEqual(res.to_dict()["output_file"], None)

    def test_rejected_command_raises_stand_error(self) -> None:
        runner = _FakeRunner(
            CommandResult(
                returncode=0,
                stdout=_broadcast_stdout(
                    {"ok": False, "command": "navigate", "error": "unknown_target:nowhere"}
                ).encode(),
            )
        )
        client = DebugHooksClient(adb_bin="adb", runner=runner)
        with self.assertRaises(StandError) as ctx:
            client.navigate("home")
        self.assertIn("unknown_target", str(ctx.exception))

    def test_adb_failure_raises_stand_error_and_does_not_parse_stdout(self) -> None:
        runner = _FakeRunner(
            CommandResult(returncode=1, stdout=b"", stderr="error: device offline".encode())
        )
        client = DebugHooksClient(adb_bin="adb", runner=runner)
        with self.assertRaises(StandError) as ctx:
            client.reset_state()
        self.assertIn("device offline", str(ctx.exception))

    def test_invalid_target_never_invokes_runner(self) -> None:
        runner = _FakeRunner(CommandResult(returncode=0, stdout=b""))
        client = DebugHooksClient(adb_bin="adb", runner=runner)
        with self.assertRaises(StandError):
            client.navigate("nowhere")
        self.assertEqual(runner.calls, [])

    def test_dump_diagnostics_passes_output_path(self) -> None:
        runner = _FakeRunner(
            CommandResult(
                returncode=0,
                stdout=_broadcast_stdout(
                    {"ok": True, "command": "dump_diagnostics", "output_file": "/cache/diag.json"}
                ).encode(),
            )
        )
        client = DebugHooksClient(adb_bin="adb", runner=runner)
        res = client.dump_diagnostics("/cache/diag.json")
        self.assertEqual(res.output_file, "/cache/diag.json")
        self.assertIn("output_path", runner.calls[0])
        self.assertIn("/cache/diag.json", runner.calls[0])


class DebugCliTests(unittest.TestCase):
    def _write_fake_adb(self, root: Path, payload: dict) -> Path:
        adb = root / "adb"
        line = _broadcast_stdout(payload).replace("'", "'\\''")
        adb.write_text(f"#!/bin/sh\nprintf '%s' '{line}'\n", encoding="utf-8")
        adb.chmod(0o755)
        return adb

    def test_cli_debug_navigate_outputs_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            adb = self._write_fake_adb(
                Path(td), {"ok": True, "command": "navigate", "detail": "settings"}
            )
            out = io.StringIO()
            err = io.StringIO()
            rc = run_cli(
                ["--adb-bin", str(adb), "--json", "debug", "navigate", "settings"],
                stdout=out,
                stderr=err,
            )
            self.assertEqual(rc, 0, err.getvalue())
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["detail"], "settings")
            self.assertEqual(payload["command"], "navigate")

    def test_cli_debug_failure_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            adb = Path(td) / "adb"
            adb.write_text("#!/bin/sh\nprintf '%s' 'error: device offline' >&2\nexit 1\n", encoding="utf-8")
            adb.chmod(0o755)
            out = io.StringIO()
            err = io.StringIO()
            rc = run_cli(
                ["--adb-bin", str(adb), "--json", "debug", "reset-state"],
                stdout=out,
                stderr=err,
            )
            self.assertEqual(rc, 1)
            payload = json.loads(out.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIn("device offline", payload["error"])

    def test_cli_debug_rejects_unknown_preset(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        rc = run_cli(
            ["--json", "debug", "mock-state", "--preset", "explode"],
            stdout=out,
            stderr=err,
        )
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
