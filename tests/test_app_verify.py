#!/usr/bin/env python3
"""Unit-тесты платформо-независимого CLI верификации и Android KVM-адаптера."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Mapping

from app_verify.android import (
    ALLOWED_HDD_ROOT,
    PNG_SIGNATURE,
    AndroidKvmAdapter,
    AndroidStandConfig,
    CommandResult,
    HddInvariantError,
    StandTimeoutError,
    build_remote_env,
    find_local_adb,
    validate_hdd_root,
)
from app_verify.cli import build_default_registry, run_cli
from app_verify.core import (
    AdapterRegistry,
    ArtifactRecord,
    InstallResult,
    LaunchResult,
    LogReport,
    PlatformAdapter,
    StandError,
    StandStatus,
    UnknownPlatformError,
    VerificationSession,
)
from app_verify.logcat import analyze_logcat


class _DummyAdapter(PlatformAdapter):
    platform = "dummy"

    def prepare(self) -> StandStatus:
        return StandStatus(platform="dummy", state="ready", target_id="d1", endpoint="local")

    def start(self, snapshot: str | None = None, reset: bool = True) -> StandStatus:
        return StandStatus(
            platform="dummy",
            state="running",
            target_id="d1",
            endpoint="local",
            snapshot=snapshot,
        )

    def stop(self) -> StandStatus:
        return StandStatus(platform="dummy", state="stopped", target_id="d1", endpoint="local")

    def status(self) -> StandStatus:
        return StandStatus(platform="dummy", state="running", target_id="d1", endpoint="local")

    def install(self, artifact_path: Path) -> InstallResult:
        return InstallResult(platform="dummy", artifact=str(artifact_path), target_id="d1", ok=True)

    def launch(self, target: str, activity: str | None = None) -> LaunchResult:
        return LaunchResult(platform="dummy", app_id=target, component=activity or target, ok=True)

    def screenshot(self, output_path: Path) -> ArtifactRecord:
        return ArtifactRecord(kind="screenshot", path=str(output_path), size_bytes=16, valid=True)

    def collect_logs(
        self,
        output_path: Path | None = None,
        clear: bool = False,
        package: str | None = None,
    ) -> LogReport:
        return LogReport(
            platform="dummy",
            raw_text="",
            output_path=str(output_path) if output_path else None,
            crashes=[],
            anrs=[],
        )


class CoreRegistryTests(unittest.TestCase):
    def test_registry_registers_and_resolves_adapter(self) -> None:
        registry = AdapterRegistry()
        registry.register("dummy", lambda **_: _DummyAdapter())

        self.assertEqual(registry.list_platforms(), ["dummy"])
        adapter = registry.create("dummy")
        self.assertEqual(adapter.platform, "dummy")
        self.assertEqual(adapter.start(snapshot="clean-base").snapshot, "clean-base")

    def test_registry_raises_for_unknown_platform(self) -> None:
        registry = AdapterRegistry()
        with self.assertRaises(UnknownPlatformError):
            registry.create("ios")

    def test_session_records_artifacts_and_serializes(self) -> None:
        session = VerificationSession(platform="dummy", session_id="s-01")
        rec = ArtifactRecord(kind="screenshot", path="shot.png", size_bytes=128, valid=True)
        session.add_artifact(rec)

        data = session.to_dict()
        self.assertEqual(data["platform"], "dummy")
        self.assertEqual(data["session_id"], "s-01")
        self.assertEqual(len(data["artifacts"]), 1)
        self.assertEqual(data["artifacts"][0]["path"], "shot.png")


class HddInvariantTests(unittest.TestCase):
    def test_accepts_default_hdd_root_and_subpaths(self) -> None:
        self.assertEqual(validate_hdd_root("/mnt/data/android"), "/mnt/data/android")
        self.assertEqual(validate_hdd_root("/mnt/data/android/"), "/mnt/data/android")
        self.assertEqual(validate_hdd_root("/mnt/data/android/staging"), "/mnt/data/android/staging")

    def test_rejects_ssd_and_traversal_paths(self) -> None:
        bad_paths = [
            "/",
            "/tmp/android",
            "/home/fus1ond/.android",
            "/mnt/data",
            "/mnt/data/android2",
            "/mnt/data/android/../docker",
            "mnt/data/android",
            "",
        ]
        for bad in bad_paths:
            with self.subTest(path=bad):
                with self.assertRaises(HddInvariantError):
                    validate_hdd_root(bad)

    def test_build_remote_env_pins_all_android_and_temp_paths_to_hdd(self) -> None:
        env = build_remote_env("/mnt/data/android", remote_adb_server_port=15037)
        expected_paths = {
            "HOME": "/mnt/data/android/home",
            "ANDROID_HOME": "/mnt/data/android/sdk",
            "ANDROID_SDK_ROOT": "/mnt/data/android/sdk",
            "ANDROID_USER_HOME": "/mnt/data/android/user-home",
            "ANDROID_EMULATOR_HOME": "/mnt/data/android/user-home",
            "ANDROID_PREFS_ROOT": "/mnt/data/android/user-home",
            "ANDROID_SDK_HOME": "/mnt/data/android/user-home",
            "ANDROID_AVD_HOME": "/mnt/data/android/avd",
            "TMPDIR": "/mnt/data/android/tmp",
            "TEMP": "/mnt/data/android/tmp",
            "TMP": "/mnt/data/android/tmp",
            "ANDROID_TMP": "/mnt/data/android/tmp",
            "XDG_CACHE_HOME": "/mnt/data/android/cache",
            "XDG_CONFIG_HOME": "/mnt/data/android/config",
            "XDG_DATA_HOME": "/mnt/data/android/user-home",
            "XDG_RUNTIME_DIR": "/mnt/data/android/run",
        }
        for key, value in expected_paths.items():
            self.assertEqual(env[key], value)
            self.assertTrue(value.startswith(ALLOWED_HDD_ROOT))
        self.assertEqual(env["ANDROID_ADB_SERVER_PORT"], "15037")
        self.assertIn("-Djava.io.tmpdir=/mnt/data/android/tmp", env["JAVA_TOOL_OPTIONS"])
        self.assertIn("-Duser.home=/mnt/data/android/home", env["JAVA_TOOL_OPTIONS"])


class FindLocalAdbTests(unittest.TestCase):
    def test_prefers_adb_bin_env_and_falls_back_to_android_sdk_locations(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            custom = root / "custom-adb"
            custom.write_text("#!/bin/sh\n", encoding="utf-8")
            custom.chmod(0o755)

            sdk_adb = root / "sdk" / "platform-tools" / "adb"
            sdk_adb.parent.mkdir(parents=True)
            sdk_adb.write_text("#!/bin/sh\n", encoding="utf-8")
            sdk_adb.chmod(0o755)

            self.assertEqual(
                find_local_adb(env={"ADB_BIN": str(custom)}, which_fn=lambda _: None, default_candidates=[]),
                str(custom),
            )
            self.assertEqual(
                find_local_adb(
                    env={"ANDROID_HOME": str(root / "sdk")},
                    which_fn=lambda _: None,
                    default_candidates=[],
                ),
                str(sdk_adb),
            )
            self.assertEqual(
                find_local_adb(env={}, which_fn=lambda _: "/usr/local/bin/adb", default_candidates=[]),
                "/usr/local/bin/adb",
            )
            with self.assertRaises(StandError):
                find_local_adb(env={}, which_fn=lambda _: None, default_candidates=[])


class LogcatAnalyzerTests(unittest.TestCase):
    def test_clean_logcat_reports_ok(self) -> None:
        sample = (
            "11-24 19:00:01.000  1000  1000 I ActivityManager: Start proc 2001:ru.rumap.android.debug\n"
            "11-24 19:00:01.120  2001  2001 D RumapApp: initialized\n"
        )
        crashes, anrs = analyze_logcat(sample)
        self.assertEqual(crashes, [])
        self.assertEqual(anrs, [])

    def test_detects_java_fatal_exception_native_crash_and_anr(self) -> None:
        sample = (
            "11-24 19:01:00.100  2001  2001 E AndroidRuntime: FATAL EXCEPTION: main\n"
            "11-24 19:01:00.101  2001  2001 E AndroidRuntime: Process: ru.rumap.android.debug, PID: 2001\n"
            "11-24 19:01:00.102  2001  2001 E AndroidRuntime: java.lang.IllegalStateException: boom\n"
            "11-24 19:01:00.103  2001  2001 E AndroidRuntime: \tat ru.rumap.android.MainActivity.onCreate(MainActivity.kt:42)\n"
            "11-24 19:01:05.000   500   520 E ActivityManager: ANR in ru.rumap.android.debug (ru.rumap.android.debug/.MainActivity)\n"
            "11-24 19:01:05.001   500   520 E ActivityManager: Reason: Input dispatching timed out\n"
            "11-24 19:01:09.000  3001  3001 F libc    : Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR) in tid 3001 (ru.rumap.android.debug)\n"
            "11-24 19:01:09.010  3010  3010 F DEBUG   : *** *** *** *** *** *** *** *** *** *** *** *** *** *** *** ***\n"
            "11-24 19:01:09.011  3010  3010 F DEBUG   : pid: 3001, tid: 3001, name: main  >>> ru.rumap.android.debug <<<\n"
        )
        crashes, anrs = analyze_logcat(sample)
        self.assertEqual(len(crashes), 2)
        self.assertEqual(len(anrs), 1)
        self.assertIn("FATAL EXCEPTION", crashes[0].summary)
        self.assertTrue(any("IllegalStateException" in ln for ln in crashes[0].lines))
        self.assertIn("Fatal signal 11", crashes[1].summary)
        self.assertIn("ANR in ru.rumap.android.debug", anrs[0].summary)
        self.assertTrue(any("Input dispatching timed out" in ln for ln in anrs[0].lines))

    def test_package_filter_ignores_other_processes(self) -> None:
        sample = (
            "11-24 19:01:00.100  9999  9999 E AndroidRuntime: FATAL EXCEPTION: main\n"
            "11-24 19:01:00.101  9999  9999 E AndroidRuntime: Process: com.other.app, PID: 9999\n"
            "11-24 19:01:05.000   500   520 E ActivityManager: ANR in com.other.app\n"
        )
        crashes, anrs = analyze_logcat(sample, package="ru.rumap.android.debug")
        self.assertEqual(crashes, [])
        self.assertEqual(anrs, [])


class _FakeRunner:
    def __init__(self, handlers) -> None:
        self.handlers = handlers
        self.calls: list[tuple[list[str], bytes | None]] = []

    def run(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
        input_bytes: bytes | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append((list(args), input_bytes))
        return self.handlers(args, input_bytes, timeout)


class AndroidKvmAdapterTests(unittest.TestCase):
    def _make_config(self, td: str) -> AndroidStandConfig:
        fake_adb = Path(td) / "adb"
        fake_adb.write_text("#!/bin/sh\n", encoding="utf-8")
        fake_adb.chmod(0o755)
        return AndroidStandConfig(
            ssh_host="home",
            remote_root="/mnt/data/android",
            avd_name="verify-api34",
            snapshot_name="clean-base",
            console_port=5564,
            local_adb_port=15565,
            adb_bin=str(fake_adb),
            ssh_control_dir=Path(td) / "ctrl",
            adb_timeout_s=5.0,
        )

    def test_start_builds_hdd_guarded_ssh_script_opens_tunnel_and_connects_adb(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = self._make_config(td)
            tunnel_open = False

            def handler(args: list[str], input_bytes: bytes | None, _timeout: float | None) -> CommandResult:
                nonlocal tunnel_open
                if args[0] == "ssh" and "bash" in args:
                    script = (input_bytes or b"").decode("utf-8")
                    self.assertIn("export ANDROID_HOME=/mnt/data/android/sdk", script)
                    self.assertIn("export ANDROID_AVD_HOME=/mnt/data/android/avd", script)
                    self.assertIn("export TMPDIR=/mnt/data/android/tmp", script)
                    self.assertIn("HDD_INVARIANT_VIOLATION", script)
                    self.assertIn("clean-base", script)
                    return CommandResult(0, b"mode=boot-snapshot\nsnapshot_used=clean-base\n")
                if args[0] == "ssh" and "-O" in args and "check" in args:
                    return CommandResult(0 if tunnel_open else 255)
                if args[0] == "ssh" and "-L" in args:
                    self.assertIn("127.0.0.1:15565:127.0.0.1:5565", args)
                    tunnel_open = True
                    return CommandResult(0)
                if args[0] == cfg.adb_bin and args[1] == "disconnect":
                    return CommandResult(0, b"disconnected\n")
                if args[0] == cfg.adb_bin and args[1] == "connect":
                    self.assertEqual(args[2], "127.0.0.1:15565")
                    return CommandResult(0, b"connected to 127.0.0.1:15565\n")
                if args[0] == cfg.adb_bin and args[1] == "devices":
                    return CommandResult(0, b"List of devices attached\n127.0.0.1:15565\tdevice\n")
                raise AssertionError(f"Unexpected call: {args}")

            runner = _FakeRunner(handler)
            adapter = AndroidKvmAdapter(cfg, runner=runner, sleep_fn=lambda _: None)
            st = adapter.start()

            self.assertEqual(st.state, "running")
            self.assertEqual(st.target_id, "127.0.0.1:15565")
            self.assertEqual(st.snapshot, "clean-base")
            self.assertEqual(st.details["mode"], "boot-snapshot")

    def test_start_raises_timeout_when_local_adb_stays_offline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = self._make_config(td)
            now = [0.0]

            def monotonic() -> float:
                now[0] += 3.0
                return now[0]

            def handler(args: list[str], _inp: bytes | None, _timeout: float | None) -> CommandResult:
                if args[0] == "ssh" and "bash" in args:
                    return CommandResult(0, b"mode=boot-snapshot\nsnapshot_used=clean-base\n")
                if args[0] == "ssh" and "-O" in args:
                    return CommandResult(0)
                if args[0] == cfg.adb_bin and args[1] == "disconnect":
                    return CommandResult(0)
                if args[0] == cfg.adb_bin and args[1] == "connect":
                    return CommandResult(0, b"connected\n")
                if args[0] == cfg.adb_bin and args[1] == "devices":
                    return CommandResult(0, b"List of devices attached\n127.0.0.1:15565\toffline\n")
                raise AssertionError(f"Unexpected call: {args}")

            adapter = AndroidKvmAdapter(
                cfg, runner=_FakeRunner(handler), sleep_fn=lambda _: None, monotonic_fn=monotonic
            )
            with self.assertRaises(StandTimeoutError):
                adapter.start()

    def test_install_launch_screenshot_logcat_and_stop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = self._make_config(td)
            apk = Path(td) / "app-debug.apk"
            apk.write_bytes(b"PK\x03\x04dummy-apk")
            shot = Path(td) / "screen.png"
            log_file = Path(td) / "logcat.txt"
            fake_png = PNG_SIGNATURE + b"\x00" * 32

            def handler(args: list[str], _inp: bytes | None, _timeout: float | None) -> CommandResult:
                if args[0] == cfg.adb_bin and "install" in args:
                    self.assertIn("-g", args)
                    return CommandResult(0, b"Performing Streamed Install\nSuccess\n")
                if args[0] == cfg.adb_bin and "resolve-activity" in args:
                    return CommandResult(
                        0,
                        b"priority=0 preferredOrder=0 match=0x108000\nru.rumap.android.debug/ru.rumap.android.MainActivity\n",
                    )
                if args[0] == cfg.adb_bin and "am" in args and "start" in args:
                    self.assertIn("ru.rumap.android.debug/ru.rumap.android.MainActivity", args)
                    return CommandResult(0, b"Starting: Intent { ... }\nStatus: ok\n")
                if args[0] == cfg.adb_bin and "screencap" in args:
                    return CommandResult(0, fake_png)
                if args[0] == cfg.adb_bin and "logcat" in args:
                    return CommandResult(
                        0,
                        b"11-24 19:00:01.000 100 100 E AndroidRuntime: FATAL EXCEPTION: main\n"
                        b"11-24 19:00:01.001 100 100 E AndroidRuntime: Process: ru.rumap.android.debug\n",
                    )
                if args[0] == cfg.adb_bin and "disconnect" in args:
                    return CommandResult(0)
                if args[0] == "ssh" and "-O" in args and "exit" in args:
                    return CommandResult(0)
                if args[0] == "ssh" and "bash" in args:
                    return CommandResult(0, b"stopped=1\n")
                raise AssertionError(f"Unexpected call: {args}")

            adapter = AndroidKvmAdapter(cfg, runner=_FakeRunner(handler), sleep_fn=lambda _: None)

            ins = adapter.install(apk)
            self.assertTrue(ins.ok)

            lnc = adapter.launch("ru.rumap.android.debug")
            self.assertEqual(lnc.component, "ru.rumap.android.debug/ru.rumap.android.MainActivity")

            art = adapter.screenshot(shot)
            self.assertTrue(art.valid)
            self.assertTrue(shot.read_bytes().startswith(PNG_SIGNATURE))

            rep = adapter.collect_logs(output_path=log_file, package="ru.rumap.android.debug")
            self.assertFalse(rep.ok)
            self.assertEqual(len(rep.crashes), 1)
            self.assertTrue(log_file.is_file())

            stopped = adapter.stop()
            self.assertEqual(stopped.state, "stopped")

    def test_screenshot_rejects_invalid_png_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = self._make_config(td)
            adapter = AndroidKvmAdapter(
                cfg,
                runner=_FakeRunner(lambda *_a: CommandResult(0, b"not-a-png")),
            )
            with self.assertRaises(StandError):
                adapter.screenshot(Path(td) / "bad.png")


class CliTests(unittest.TestCase):
    def test_default_registry_contains_android(self) -> None:
        reg = build_default_registry()
        self.assertIn("android", reg.list_platforms())

    def test_cli_routes_subcommands_via_registry_and_formats_json(self) -> None:
        reg = AdapterRegistry()
        reg.register("dummy", lambda **_: _DummyAdapter())

        out = io.StringIO()
        err = io.StringIO()
        rc = run_cli(["--platform", "dummy", "--json", "start", "--snapshot", "clean-base"], registry=reg, stdout=out, stderr=err)
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["state"], "running")
        self.assertEqual(payload["snapshot"], "clean-base")

    def test_cli_rejects_ssd_remote_root_with_nonzero_exit(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        rc = run_cli(
            ["--platform", "android", "--remote-root", "/home/fus1ond/android", "--json", "status"],
            stdout=out,
            stderr=err,
        )
        self.assertEqual(rc, 1)
        payload = json.loads(out.getvalue())
        self.assertIn("HDD", payload["error"])

    def test_cli_logcat_fail_on_error_returns_code_2_on_crash(self) -> None:
        class _CrashyAdapter(_DummyAdapter):
            def collect_logs(self, output_path=None, clear=False, package=None) -> LogReport:
                crashes, anrs = analyze_logcat(
                    "11-24 19:00:01.000 100 100 E AndroidRuntime: FATAL EXCEPTION: main\n"
                )
                return LogReport(
                    platform="dummy",
                    raw_text="FATAL",
                    output_path=None,
                    crashes=crashes,
                    anrs=anrs,
                )

        reg = AdapterRegistry()
        reg.register("dummy", lambda **_: _CrashyAdapter())
        out = io.StringIO()
        err = io.StringIO()
        rc = run_cli(
            ["--platform", "dummy", "--json", "logcat", "--fail-on-error"],
            registry=reg,
            stdout=out,
            stderr=err,
        )
        self.assertEqual(rc, 2)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["crash_count"], 1)


if __name__ == "__main__":
    unittest.main()
