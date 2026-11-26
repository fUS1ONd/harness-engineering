import io
import json
import os
import shlex
import signal
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app_verify.android import AndroidStandConfig, CommandResult
from app_verify.bs_matrix import PACKAGE, SCENARIOS, run_bs_matrix
from app_verify.cli import run_cli
from app_verify.core import LogAnomaly, LogReport, StandError, StandStatus
from app_verify.net_emulation import NetStatus
from app_verify.remnawave import TemporarySubscription
from app_verify.vpn_clients import HappSigillError, VpnClientStatus


class MatrixCliTests(unittest.TestCase):
    def test_matrix_accepts_json_before_or_after_command_and_reports_failure(self):
        report = {"verdict": "fail", "exit_code": 2}
        for args in (["--json", "bs-matrix"], ["bs-matrix", "--json"]):
            with self.subTest(args=args), patch("app_verify.cli.run_bs_matrix", return_value=report) as run:
                out = io.StringIO()
                code = run_cli([*args, "--apk", "sample.apk", "--scenario", "happ"], stdout=out)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out.getvalue()), report)
                self.assertEqual(run.call_args.kwargs["scenarios"], ["happ"])


class FakeStand:
    def __init__(self, *, abi="x86_64"):
        self.config = AndroidStandConfig()
        self.runner = Mock()
        self.runner.run.side_effect = self.command
        self.actions = []
        self.logs = LogReport("android", "", None)
        self.entries = []
        self.profile_name = "off"
        self.wifi = "on"
        self.phase = "OFF"
        self.conflict = "NONE"
        self.sticky_conflict = False
        self.process = False
        self.deviceidle = False
        self.bg = False
        self.user = False
        self.mode = {"koala": "AcceptAll", "happ": "Off"}
        self.active = None
        self.abi = abi
        self.tab = "home"
        self.elements = {}
        self.check_result = False
        self.target = ""

    def start(self):
        self.actions.append("start")

    def install(self, apk):
        self.actions.append("install")
        return SimpleNamespace(to_dict=lambda: {"artifact": str(apk)})

    def launch(self, _package):
        self.actions.append("launch")
        self.bg = False
        self.process = True
        self.round()

    def collect_logs(self, **kwargs):
        self.actions.append(("logs", kwargs))
        return self.logs

    def stop(self):
        self.actions.append("stop")

    def status(self, client=None):
        if client is None:
            self.actions.append("status")
            return StandStatus("android", "stopped", "device", "endpoint", details={"tunnel_alive": False})
        mode = self.mode[client]
        active = self.active == client
        return VpnClientStatus(
            client, client, True, "allow", active, "tun0" if active else None, mode,
            (PACKAGE,) if mode in {"DenySelected", "Bypass"} else (),
            abi_warning="ARM-only" if client == "happ" and self.abi == "x86_64" else None,
        )

    def round(self):
        if not self.process or self.bg:
            return
        self.phase = self.profile_name.upper()
        mark = {
            "addr": "1.1.1.1", "wire_icmp": "skip", "wire_tcp": "ok",
            "modem_icmp": "skip", "modem_tcp": "timeout",
        }
        fields = {
            "phase": self.phase, "took_ms": 1211, "local_ip": "10.0.2.15", "cut": [mark], "pass": [mark],
        }
        if self.conflict != "NONE":
            fields["blocked_by"] = f"network_conflict:{self.conflict}"
        self.entries.append({
            "ts": str(len(self.entries)), "category": "phase", "msg": "раунд проверки ориентиров",
            "fields": fields,
        })

    def apply_profile(self, profile, *, allow_ips, wifi):
        self.actions.append(("profile", profile, wifi))
        previous = self.profile_name
        self.profile_name, self.wifi = profile, wifi
        if not (previous == "off" and profile == "soft"):
            self.round()

    def net_status(self):
        return NetStatus(
            self.profile_name, (), "radio0", self.wifi == "on", self.wifi == "on",
            "wlan0", True, self.active is not None, "tun0" if self.active else None,
        )

    def reset(self):
        self.actions.append("reset")
        self.profile_name, self.wifi = "off", "on"
        return self.net_status()

    def dump_diagnostics(self):
        return SimpleNamespace(diagnostics={"mock_preset": "clear", "runtime": {
            "phase": self.phase, "network_conflict": self.conflict, "runtime_present": True,
            "work_status": "RUNNING", "vpn_active": self.active is not None,
        }})

    def navigate(self, tab):
        self.tab = tab
        self.check_result = False

    def call_tool(self, name, arguments):
        if name == "mobile_list_elements_on_screen":
            options = [
                "Проверить статус сети", "77.73.236.0/24, IP или домен", "Проверить", "Настройки VPN",
                "Проверить снова",
            ]
            if self.check_result:
                options.extend([self.target, "Точечная"])
            self.elements = {i: label for i, label in enumerate(options, 1)}
            return SimpleNamespace(text="\n".join(
                f'@e{i} Button text="{label}" at={i},0 size=1x1' for i, label in self.elements.items()
            ))
        return SimpleNamespace(text="")

    def close(self):
        self.actions.append("close_ui")

    def command(self, argv, **_kwargs):
        args = shlex.split(argv[-1])
        if args[:2] == ["input", "tap"]:
            label = self.elements[int(args[2])]
            self.actions.append(("tap", label))
            if label == "Проверить статус сети":
                self.round()
            if label == "Проверить":
                self.check_result = True
            if label == "Проверить снова" and not self.sticky_conflict:
                self.conflict = "NONE"
                self.round()
        if args[:2] == ["input", "text"]:
            self.target = args[2]
        if args == ["input", "keyevent", "KEYCODE_HOME"]:
            self.bg = True
            self.actions.append("HOME")
        if args[:1] == ["run-as"]:
            return CommandResult(0, "\n".join(json.dumps(row) for row in self.entries).encode())
        if args[:2] == ["svc", "wifi"]:
            self.wifi = "on" if args[-1] == "enable" else "off"
            self.actions.append(("toggle", self.wifi))
            self.round()
        if args[:2] == ["dumpsys", "deviceidle"]:
            if args[2] == "force-idle":
                self.deviceidle = True
                self.actions.append("force-idle")
            elif args[2] == "unforce":
                self.deviceidle = False
                self.actions.append("unforce")
            elif args[2] == "get":
                return CommandResult(0, b"IDLE" if self.deviceidle else b"ACTIVE")
        if args[:2] == ["am", "force-stop"]:
            self.actions.append("force-stop")
            self.process = False
        if args[:2] == ["dumpsys", "package"]:
            return CommandResult(0, b"User 0: stopped=true" if not self.process else b"User 0: stopped=false")
        if args[:1] == ["getprop"]:
            return CommandResult(0, self.abi.encode())
        return CommandResult(0)

    def prepare(self, client):
        self.actions.append(("prepare", client))
        return self.status(client)

    def import_subscription(self, client, *, url, name):
        self.actions.append(("import", client))

    def set_exclude(self, client, *, packages=None, clear=False):
        self.mode[client] = ("AcceptAll" if clear else "DenySelected") if client == "koala" else (
            "Off" if clear else "Bypass"
        )

    def connect(self, client):
        self.actions.append(("connect", client, self.mode[client]))
        if client == "happ" and self.abi == "x86_64":
            raise HappSigillError("Happ SIGILL под libndk_translation")
        self.active = client
        self.conflict = "VPN_BLOCKED" if self.mode[client] in {"AcceptAll", "Off"} else "NONE"
        return self.status(client)

    def disconnect(self, client):
        self.actions.append(("disconnect", client))
        if self.active == client:
            self.active = None
            self.conflict = "NONE"
        return self.status(client)

    def create(self):
        self.user = True
        self.actions.append("create_user")
        return "https://example.invalid/secret"

    def delete(self):
        self.user = False
        self.actions.append("delete_user")
        return {"deleted": True}


class MatrixRunTests(unittest.TestCase):
    def run_matrix(self, stand, **options):
        with ExitStack() as stack:
            for name, value in {
                "find_local_adb": Mock(return_value="/fake/adb"),
                "build_ui_driver_env": Mock(return_value={}),
                "resolve_ui_driver_command": Mock(return_value=["fake-mcp"]),
                "MobileMcpClient": Mock(return_value=stand),
                "NetEmulationClient": Mock(return_value=SimpleNamespace(
                    status=stand.net_status, apply_profile=stand.apply_profile, reset=stand.reset,
                )),
                "DebugHooksClient": Mock(return_value=stand),
                "VpnClientsManager": Mock(return_value=stand),
                "TemporarySubscription": Mock(return_value=stand),
            }.items():
                stack.enter_context(patch(f"app_verify.bs_matrix.{name}", value))
            stack.enter_context(patch.dict(os.environ, {"APP_VERIFY_VPN_SUBSCRIPTION_URL": ""}))
            return run_bs_matrix(stand, apk=Path(__file__), phase_timeout=0.01, **options)

    def test_full_matrix_observes_transitions_real_actions_and_x86_happ_skip(self):
        stand = FakeStand()
        report = self.run_matrix(stand)
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(list(report["scenarios"]), list(SCENARIOS))
        for name in SCENARIOS[:-1]:
            self.assertEqual(report["scenarios"][name]["status"], "pass", report["scenarios"][name])
        scenarios = report["scenarios"]
        self.assertEqual(scenarios["happ"]["status"], "skipped")
        self.assertIn("SIGILL под libndk_translation", scenarios["happ"]["reason"])
        phases = scenarios["cellular-phases"]["metrics"]["phases"]
        self.assertEqual([step["expected"] for step in phases], ["OFF", "SOFT", "HARD", "DOWN", "OFF"])
        self.assertEqual(phases[1]["round"]["fields"]["local_ip"], "10.0.2.15")
        self.assertEqual(scenarios["manual-check"]["metrics"]["manual_result"]["class"], "Точечная")
        self.assertEqual(scenarios["koala-accept-all"]["metrics"]["conflict"], "VPN_BLOCKED")
        self.assertEqual(scenarios["koala-excluded"]["metrics"]["network_conflict"], "NONE")
        self.assertEqual(scenarios["doze-resume"]["metrics"]["deviceidle"], "IDLE")
        self.assertTrue(scenarios["cold-start"]["metrics"]["package_stopped"])
        for action in ("HOME", "force-idle", "unforce", "force-stop", ("toggle", "on"), ("toggle", "off")):
            self.assertIn(action, stand.actions)
        self.assertEqual(stand.actions.count("create_user"), 1)
        self.assertFalse(stand.user)
        self.assertEqual(report["stability"]["crash_count"], 0)
        self.assertEqual(
            stand.actions[-6:],
            ["reset", ("logs", {"package": PACKAGE}), "delete_user", "close_ui", "stop", "status"],
        )
        self.assertNotIn("example.invalid", json.dumps(report))

    def test_happ_on_arm_runs_both_real_tunnel_configurations(self):
        stand = FakeStand(abi="arm64-v8a")
        report = self.run_matrix(stand, scenarios=["happ"])
        self.assertEqual(report["scenarios"]["happ"]["status"], "pass", report)
        self.assertIn(("connect", "happ", "Off"), stand.actions)
        self.assertIn(("connect", "happ", "Bypass"), stand.actions)

    def test_failed_vpn_scenario_still_deletes_user_and_checks_stability(self):
        stand = FakeStand()
        stand.import_subscription = Mock(side_effect=StandError("import: https://example.invalid/secret"))
        stand.logs.crashes.append(LogAnomaly("crash", "fatal", []))
        report = self.run_matrix(stand, scenarios=["koala-excluded"])
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["stability"]["crash_count"], 1)
        self.assertEqual(report["scenarios"]["koala-excluded"]["reason"], "import: [subscription]")
        for name in ("vpn_disconnect_koala", "vpn_disconnect_happ", "net_reset", "temporary_user", "stop"):
            self.assertEqual(report["cleanup"][name]["status"], "pass")
        self.assertFalse(stand.user)

    def test_both_signals_allow_final_cleanup_even_if_one_action_fails(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                stand = FakeStand()
                stand.import_subscription = Mock(side_effect=lambda *_a, **_kw: signal.raise_signal(signum))
                stand.reset = Mock(side_effect=StandError("net reset failed"))
                previous = signal.getsignal(signum)
                report = self.run_matrix(stand, scenarios=["koala-accept-all"])
                self.assertEqual(report["exit_code"], 128 + signum)
                self.assertEqual(report["cleanup"]["net_reset"]["status"], "fail")
                self.assertEqual(report["cleanup"]["stop"]["status"], "pass")
                self.assertFalse(stand.user)
                self.assertIs(signal.getsignal(signum), previous)

    def test_start_failure_still_stops_emulator(self):
        stand = FakeStand()
        stand.start = Mock(side_effect=StandError("boot failed"))
        report = self.run_matrix(stand)
        self.assertEqual(report["error"], "boot failed")
        self.assertEqual(stand.actions[-2:], ["stop", "status"])

    def test_baseline_clears_stale_cellular_conflict_from_home_recheck(self):
        stand = FakeStand()
        stand.conflict = "CELLULAR_UNAVAILABLE"
        report = self.run_matrix(stand, scenarios=["cold-start"])
        scenario = report["scenarios"]["cold-start"]
        self.assertEqual(scenario["status"], "pass", scenario)
        self.assertEqual(scenario["metrics"]["cleared_conflicts"], ["CELLULAR_UNAVAILABLE"])
        self.assertIn(("tap", "Проверить снова"), stand.actions)
        self.assertEqual(stand.tab, "check")

    def test_baseline_fails_when_home_recheck_cannot_clear_conflict(self):
        stand = FakeStand()
        stand.sticky_conflict = True
        stand.conflict = "VPN_BLOCKED"
        report = self.run_matrix(stand, scenarios=["cold-start"])
        scenario = report["scenarios"]["cold-start"]
        self.assertEqual(scenario["status"], "fail")
        self.assertIn("не снял конфликт VPN_BLOCKED", scenario["reason"])


class SubscriptionTests(unittest.TestCase):
    def test_cleanup_looks_up_user_when_creation_response_was_lost(self):
        with patch("app_verify.remnawave._config", return_value=("https://panel.invalid", "token", "")):
            subscription = TemporarySubscription()
        with patch.object(subscription, "_request", side_effect=[
            StandError("lost response"), {"response": {"uuid": "test-uuid"}}, {}, None,
        ]) as request:
            with self.assertRaises(StandError):
                subscription.create()
            self.assertTrue(subscription.delete()["deleted"])
        self.assertEqual(request.call_args_list[2].args, ("DELETE", "/api/users/test-uuid"))


class UiFallbackTests(unittest.TestCase):
    run_matrix = MatrixRunTests.run_matrix

    def test_accessibility_fallback_runs_after_mcp_read_failure(self):
        stand = FakeStand()
        stand.call_tool = Mock(side_effect=StandError("MCP UI timeout"))
        original = stand.runner.run.side_effect

        def command(argv, **kwargs):
            if "uiautomator dump" in argv[-1]:
                return CommandResult(0, b'<?xml version="1.0"?><hierarchy></hierarchy>')
            return original(argv, **kwargs)

        stand.runner.run.side_effect = command
        report = self.run_matrix(stand, scenarios=["happ"])
        self.assertEqual(report["verdict"], "skipped")
        self.assertEqual(report["setup_metrics"]["ui_driver"], "adb-uiautomator")
        self.assertEqual(report["setup_metrics"]["mcp_error"], "MCP UI timeout")

    def test_failed_accessibility_dump_is_retried_as_empty_screen(self):
        stand = FakeStand()
        stand.call_tool = Mock(side_effect=StandError("MCP UI timeout"))
        original = stand.runner.run.side_effect

        def command(argv, **kwargs):
            if "uiautomator dump" in argv[-1]:
                return CommandResult(1, b"", b"ERROR: could not get idle state.")
            return original(argv, **kwargs)

        stand.runner.run.side_effect = command
        report = self.run_matrix(stand, scenarios=["happ"])
        self.assertEqual(report["verdict"], "skipped")
        self.assertIn("could not get idle state", report["setup_metrics"]["ui_dump_error"])
