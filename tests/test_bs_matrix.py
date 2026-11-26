import io
import json
import os
import shlex
import signal
import subprocess
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape as xml_escape

from app_verify.android import AndroidStandConfig, CommandResult
from app_verify.bs_matrix import PACKAGE, SCENARIOS, run_bs_matrix
from app_verify.cli import run_cli
from app_verify.core import LogAnomaly, LogReport, StandError, StandStatus
from app_verify.net_emulation import NetStatus
from app_verify.remnawave import TemporarySubscription, _config as remnawave_config
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
        self.manual_error = None
        self.calibration_running = False
        self.pause_on_cold_launch = False
        self.work_status = "RUNNING"
        self.suppress_auto_round_transitions = set()
        self.target = ""
        self.battery_onboarding = False

    def start(self):
        self.actions.append("start")

    def install(self, apk):
        self.actions.append("install")
        return SimpleNamespace(to_dict=lambda: {"artifact": str(apk)})

    def launch(self, _package):
        self.actions.append("launch")
        was_stopped = not self.process
        self.bg = False
        self.process = True
        if was_stopped and self.pause_on_cold_launch:
            self.work_status = "PAUSED"
            self.phase = "UNKNOWN"
            return
        self.work_status = "RUNNING"
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
        if (previous, profile) in self.suppress_auto_round_transitions:
            return
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
            "work_status": self.work_status, "vpn_active": self.active is not None,
            "battery_onboarding_required": self.battery_onboarding,
        }})

    def navigate(self, tab):
        self.actions.append(("navigate", tab))
        self.tab = tab
        if tab != "check":
            self.check_result = False

    def screen_xml(self):
        if self.battery_onboarding:
            options = ["Продолжить"]
        elif self.tab == "calibration":
            options = ["Остановить"] if self.calibration_running else ["Подобрать скорость"]
        else:
            options = ["77.73.236.0/24, IP или домен", "Проверить", "Настройки VPN", "Проверить снова"]
            if self.phase == "OFF":
                options.insert(0, "Проверить статус сети")
            if self.work_status != "RUNNING":
                options.append("Запустить")
            if self.target:
                options.append(self.target)
            if self.manual_error:
                options.append(self.manual_error)
            if self.check_result:
                options.append("Точечная")
        self.elements = {i * 10 + 5: label for i, label in enumerate(options, 1)}
        nodes = "".join(
            f'<node text="{xml_escape(label)}" enabled="true" bounds="[{i * 10},0][{i * 10 + 10},10]"/>'
            for i, label in enumerate(options, 1)
        )
        return f'<?xml version="1.0" encoding="UTF-8"?><hierarchy>{nodes}</hierarchy>'

    def command(self, argv, **_kwargs):
        args = shlex.split(argv[-1])
        if args[:2] == ["sh", "-c"] and "uiautomator dump" in args[2]:
            return CommandResult(0, self.screen_xml().encode())
        if args[:2] == ["sh", "-c"] and "pkill -f uiautomator" in args[2]:
            self.actions.append("pkill_uiautomator")
            return CommandResult(0)
        if args[:2] == ["input", "tap"]:
            label = self.elements[int(args[2])]
            self.actions.append(("tap", label))
            if label in {"Продолжить", "Настроить позже"}:
                self.battery_onboarding = False
            if label == "Проверить статус сети":
                self.round()
            if label == "Запустить":
                self.work_status = "RUNNING"
                self.round()
            if label == "Остановить":
                self.calibration_running = False
                self.round()
            if label == "Проверить":
                if self.calibration_running:
                    self.manual_error = "orchestrator: наблюдение не запущено"
                    self.check_result = False
                else:
                    self.manual_error = None
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
    def run_matrix(self, stand, *, doze_guard=0.0, nudge_interval=0.0, **options):
        with ExitStack() as stack:
            for name, value in {
                "find_local_adb": Mock(return_value="/fake/adb"),
                "NetEmulationClient": Mock(return_value=SimpleNamespace(
                    status=stand.net_status, apply_profile=stand.apply_profile, reset=stand.reset,
                )),
                "DebugHooksClient": Mock(return_value=stand),
                "VpnClientsManager": Mock(return_value=stand),
                "TemporarySubscription": Mock(return_value=stand),
                "DOZE_DEBOUNCE_GUARD_S": doze_guard,
                "NUDGE_INTERVAL_S": nudge_interval,
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
            stand.actions[-5:],
            ["reset", ("logs", {"package": PACKAGE}), "delete_user", "stop", "status"],
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

    def test_cellular_phases_nudges_recheck_when_stuck_in_off(self):
        stand = FakeStand()
        stand.suppress_auto_round_transitions.add(("soft", "hard"))
        original_apply = stand.apply_profile

        def apply_with_off_fallback(profile, *, allow_ips, wifi):
            if (stand.profile_name, profile) == ("soft", "hard"):
                stand.phase = "OFF"
            return original_apply(profile, allow_ips=allow_ips, wifi=wifi)

        stand.apply_profile = apply_with_off_fallback
        report = self.run_matrix(stand, scenarios=["cellular-phases"], nudge_interval=0.0)
        scenario = report["scenarios"]["cellular-phases"]
        self.assertEqual(scenario["status"], "pass", scenario)
        self.assertGreaterEqual(scenario["metrics"]["recheck_nudges"], 1)

    def test_manual_check_stops_auto_calibration_and_retries_on_not_running(self):
        stand = FakeStand()
        stand.calibration_running = True
        report = self.run_matrix(stand, scenarios=["manual-check"])
        scenario = report["scenarios"]["manual-check"]
        self.assertEqual(scenario["status"], "pass", scenario)
        self.assertEqual(scenario["metrics"]["manual_attempts"], 1)
        self.assertEqual(scenario["metrics"]["stopped_calibration"], 1)
        self.assertEqual(scenario["metrics"]["manual_result"]["class"], "Точечная")
        self.assertIn(("tap", "Остановить"), stand.actions)

    def test_manual_check_retries_on_network_mode_changed_error(self):
        stand = FakeStand()
        orig_command = stand.command
        submit_taps = 0

        def network_changed_first_attempt(argv, **kwargs):
            nonlocal submit_taps
            args = shlex.split(argv[-1])
            if args[:2] == ["input", "tap"] and stand.elements.get(int(args[2])) == "Проверить":
                submit_taps += 1
                if submit_taps == 1:
                    stand.actions.append(("tap", "Проверить"))
                    stand.manual_error = "Режим сети изменился во время проверки — результат не отправлен."
                    return CommandResult(0, b"", b"")
                stand.manual_error = None
            return orig_command(argv, **kwargs)

        stand.runner.run.side_effect = network_changed_first_attempt
        report = self.run_matrix(stand, scenarios=["manual-check"])
        scenario = report["scenarios"]["manual-check"]
        self.assertEqual(scenario["status"], "pass", scenario)
        self.assertEqual(scenario["metrics"]["manual_attempts"], 2)
        self.assertEqual(scenario["metrics"]["manual_result"]["class"], "Точечная")

    def test_manual_check_retries_on_timeout_error(self):
        stand = FakeStand()
        orig_command = stand.command
        submit_taps = 0

        def timeout_first_attempt(argv, **kwargs):
            nonlocal submit_taps
            args = shlex.split(argv[-1])
            if args[:2] == ["input", "tap"] and stand.elements.get(int(args[2])) == "Проверить":
                submit_taps += 1
                if submit_taps == 1:
                    stand.actions.append(("tap", "Проверить"))
                    stand.manual_error = "Время ожидания проверки истекло — повторите попытку."
                    return CommandResult(0, b"", b"")
                stand.manual_error = None
            return orig_command(argv, **kwargs)

        stand.runner.run.side_effect = timeout_first_attempt
        report = self.run_matrix(stand, scenarios=["manual-check"])
        scenario = report["scenarios"]["manual-check"]
        self.assertEqual(scenario["status"], "pass", scenario)
        self.assertEqual(scenario["metrics"]["manual_attempts"], 2)
        self.assertEqual(scenario["metrics"]["manual_result"]["class"], "Точечная")

    def test_doze_resume_holds_doze_past_debounce_window(self):
        stand = FakeStand()
        with patch("app_verify.bs_matrix.time.sleep") as sleep_mock:
            report = self.run_matrix(stand, scenarios=["doze-resume"], doze_guard=35.0)
        scenario = report["scenarios"]["doze-resume"]
        self.assertEqual(scenario["status"], "pass", scenario)
        self.assertEqual(scenario["metrics"]["doze_hold_s"], 35.0)
        self.assertTrue(any(call.args and call.args[0] >= 35.0 for call in sleep_mock.call_args_list))

    def test_cold_start_paused_after_launch_records_metric_and_starts_observation(self):
        stand = FakeStand()
        stand.pause_on_cold_launch = True
        report = self.run_matrix(stand, scenarios=["cold-start"])
        scenario = report["scenarios"]["cold-start"]
        self.assertEqual(scenario["status"], "pass", scenario)
        self.assertFalse(scenario["metrics"]["observation_auto_restored"])
        self.assertEqual(scenario["metrics"]["work_status_after_launch"], "PAUSED")
        self.assertEqual(scenario["metrics"]["start_button"], "Запустить")

    def test_cellular_phases_accepts_unknown_when_icmp_unprobed_and_tcp_silent(self):
        stand = FakeStand()

        def no_icmp_round():
            if not stand.process or stand.bg:
                return
            if stand.profile_name in ("hard", "down"):
                stand.phase = "UNKNOWN"
                mark = {
                    "addr": "1.1.1.1", "wire_icmp": "unprobed", "wire_tcp": "answer",
                    "modem_icmp": "unprobed", "modem_tcp": "silence",
                }
            else:
                stand.phase = stand.profile_name.upper()
                mark = {
                    "addr": "1.1.1.1", "wire_icmp": "unprobed", "wire_tcp": "answer",
                    "modem_icmp": "unprobed", "modem_tcp": "answer",
                }
            stand.entries.append({
                "ts": str(len(stand.entries)), "category": "phase", "msg": "раунд проверки ориентиров",
                "fields": {"phase": stand.phase, "took_ms": 1200, "local_ip": "10.0.2.15", "cut": [mark], "pass": [mark]},
            })

        stand.round = no_icmp_round
        report = self.run_matrix(stand, scenarios=["cellular-phases"])
        scenario = report["scenarios"]["cellular-phases"]
        self.assertEqual(scenario["status"], "pass", scenario)
        self.assertFalse(scenario["metrics"]["icmp_available"])
        self.assertEqual(
            [step["round"]["fields"]["phase"] for step in scenario["metrics"]["phases"]],
            ["OFF", "SOFT", "UNKNOWN", "UNKNOWN", "OFF"],
        )

    def test_manual_check_uses_wifi_for_domain_and_cellular_only_for_ip(self):
        stand_domain = FakeStand()
        rep_domain = self.run_matrix(stand_domain, scenarios=["manual-check"], manual_target="ya.ru")
        self.assertEqual(rep_domain["scenarios"]["manual-check"]["status"], "pass", rep_domain)
        self.assertEqual(rep_domain["scenarios"]["manual-check"]["metrics"]["manual_wifi"], "on")
        self.assertIn(("profile", "soft", "on"), stand_domain.actions)

        stand_ip = FakeStand()
        rep_ip = self.run_matrix(stand_ip, scenarios=["manual-check"], manual_target="77.88.55.242")
        self.assertEqual(rep_ip["scenarios"]["manual-check"]["status"], "pass", rep_ip)
        self.assertEqual(rep_ip["scenarios"]["manual-check"]["metrics"]["manual_wifi"], "off")
        self.assertIn(("profile", "soft", "off"), stand_ip.actions)

    def test_manual_check_retaps_submit_when_first_tap_is_swallowed(self):
        stand = FakeStand()
        orig_command = stand.command
        submit_taps = 0

        def swallow_first_submit(argv, **kwargs):
            nonlocal submit_taps
            args = shlex.split(argv[-1])
            if args[:2] == ["input", "tap"] and stand.elements.get(int(args[2])) == "Проверить":
                submit_taps += 1
                if submit_taps == 1:
                    stand.actions.append(("tap", "Проверить"))
                    return CommandResult(0, b"", b"")
            return orig_command(argv, **kwargs)

        stand.runner.run.side_effect = swallow_first_submit
        report = self.run_matrix(stand, scenarios=["manual-check"])
        scenario = report["scenarios"]["manual-check"]
        self.assertEqual(scenario["status"], "pass", scenario)
        self.assertEqual(scenario["metrics"]["manual_submit_retaps"], 1)
        self.assertEqual(scenario["metrics"]["manual_result"]["class"], "Точечная")

    def test_manual_check_retries_after_temporary_wallet_limit_warning(self):
        stand = FakeStand()
        orig_command = stand.command
        submit_taps = 0

        def wallet_limit_first_attempt(argv, **kwargs):
            nonlocal submit_taps
            args = shlex.split(argv[-1])
            if args[:2] == ["input", "tap"] and stand.elements.get(int(args[2])) == "Проверить":
                submit_taps += 1
                if submit_taps == 1:
                    stand.actions.append(("tap", "Проверить"))
                    stand.manual_error = "Достигнут временный лимит проверок — повторите попытку позже."
                    return CommandResult(0, b"", b"")
                stand.manual_error = None
            return orig_command(argv, **kwargs)

        stand.runner.run.side_effect = wallet_limit_first_attempt
        report = self.run_matrix(stand, scenarios=["manual-check"])
        scenario = report["scenarios"]["manual-check"]
        self.assertEqual(scenario["status"], "pass", scenario)
        self.assertEqual(scenario["metrics"]["manual_attempts"], 2)
        self.assertEqual(scenario["metrics"]["manual_result"]["class"], "Точечная")

    def test_koala_excluded_recovers_from_transient_cellular_unavailable(self):
        stand = FakeStand()
        original_apply = stand.apply_profile

        def apply_with_transient_cellular_loss(profile, *, allow_ips, wifi):
            result = original_apply(profile, allow_ips=allow_ips, wifi=wifi)
            if profile == "soft" and wifi == "off" and stand.active == "koala":
                stand.conflict = "CELLULAR_UNAVAILABLE"
                stand.phase = "OFF"
            return result

        stand.apply_profile = apply_with_transient_cellular_loss
        report = self.run_matrix(stand, scenarios=["koala-excluded"])
        scenario = report["scenarios"]["koala-excluded"]
        self.assertEqual(scenario["status"], "pass", scenario)
        self.assertEqual(scenario["metrics"]["cleared_conflicts"], ["CELLULAR_UNAVAILABLE"])
        self.assertEqual(scenario["metrics"]["network_conflict"], "NONE")

    def test_doze_resume_leave_idle_failure_stops_remaining_scenarios(self):
        stand = FakeStand()
        orig_command = stand.command

        def fail_unforce(argv, **kwargs):
            args = shlex.split(argv[-1])
            if args == ["dumpsys", "deviceidle", "unforce"]:
                return CommandResult(1, b"", b"deviceidle unforce failed")
            return orig_command(argv, **kwargs)

        stand.runner.run.side_effect = fail_unforce
        report = self.run_matrix(stand, scenarios=["doze-resume", "cold-start", "happ"])
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["scenarios"]["doze-resume"]["status"], "fail")
        self.assertEqual(report["cleanup"]["after_doze_resume"]["status"], "fail")
        self.assertEqual(report["scenarios"]["cold-start"]["status"], "skipped")
        self.assertEqual(report["scenarios"]["cold-start"]["reason"], "Сценарий не запущен")
        self.assertEqual(report["scenarios"]["happ"]["status"], "skipped")
        self.assertEqual(report["scenarios"]["happ"]["reason"], "Сценарий не запущен")


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

    def test_remnawave_config_wraps_read_oserror_in_stand_error(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("app_verify.remnawave.Path.read_text", side_effect=PermissionError("denied")),
        ):
            with self.assertRaises(StandError) as ctx:
                remnawave_config()
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_remnawave_config_wraps_ssh_errors_in_stand_error(self):
        for exc in (
            subprocess.TimeoutExpired(["ssh", "leaseweb-nl"], 10),
            FileNotFoundError("ssh not found"),
        ):
            with (
                self.subTest(exc=type(exc).__name__),
                patch.dict(os.environ, {}, clear=True),
                patch("app_verify.remnawave.Path.read_text", side_effect=FileNotFoundError),
                patch("app_verify.remnawave.subprocess.run", side_effect=exc),
            ):
                with self.assertRaises(StandError) as ctx:
                    remnawave_config()
                self.assertIsNone(ctx.exception.__cause__)
                self.assertTrue(ctx.exception.__suppress_context__)


class RemnawaveUserFormatTests(unittest.TestCase):
    USER = {
        "id": 192, "shortUuid": "short", "username": "bs_matrix_x", "status": "ACTIVE",
        "subscriptionUrl": "https://sub.invalid/short",
    }

    def subscription(self, host=""):
        with patch("app_verify.remnawave._config", return_value=("https://panel.invalid", "token", host)):
            return TemporarySubscription()

    def test_create_parses_remnawave_numeric_id_and_is_idempotent(self):
        subscription = self.subscription()
        with patch.object(subscription, "_request", return_value={"response": dict(self.USER)}) as request:
            self.assertEqual(subscription.create(), "https://sub.invalid/short")
            self.assertEqual(subscription.create(), "https://sub.invalid/short")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[:2], ("POST", "/api/users"))
        self.assertEqual(subscription.user_id, "192")

    def test_create_builds_url_from_short_uuid_and_looks_up_missing_id(self):
        subscription = self.subscription("sub.invalid")
        user = {key: value for key, value in self.USER.items() if key != "subscriptionUrl"}
        with patch.object(subscription, "_request", side_effect=[
            {"response": {"username": subscription.username}}, {"response": user},
        ]) as request:
            self.assertEqual(subscription.create(), "https://sub.invalid/short")
        self.assertEqual(request.call_args_list[1].args, ("GET", f"/api/users/by-username/{subscription.username}"))
        self.assertEqual(subscription.user_id, "192")

    def test_repeated_create_after_conflict_reuses_existing_user(self):
        subscription = self.subscription()
        with patch.object(subscription, "_request", side_effect=[
            StandError("Remnawave POST: HTTP 400"), {"response": dict(self.USER)},
        ]) as request:
            self.assertEqual(subscription.create(), "https://sub.invalid/short")
            self.assertEqual(subscription.create(), "https://sub.invalid/short")
        self.assertEqual([call.args[0] for call in request.call_args_list], ["POST", "GET"])

    def test_retry_after_failed_create_does_not_post_twice_when_user_exists(self):
        subscription = self.subscription()
        with patch.object(subscription, "_request", side_effect=[
            {"response": {"id": 192}}, {"response": dict(self.USER)},
        ]) as request:
            with self.assertRaises(StandError):
                subscription.create()
            self.assertEqual(subscription.create(), "https://sub.invalid/short")
        self.assertEqual([call.args[0] for call in request.call_args_list], ["POST", "GET"])

    def test_create_failure_without_user_is_reported(self):
        subscription = self.subscription()
        with patch.object(subscription, "_request", side_effect=[StandError("Remnawave POST: HTTP 500"), None]):
            with self.assertRaisesRegex(StandError, "HTTP 500"):
                subscription.create()

    def test_delete_uses_numeric_id_and_verifies_absence(self):
        subscription = self.subscription()
        with patch.object(subscription, "_request", side_effect=[
            {"response": dict(self.USER)}, {}, None,
        ]) as request:
            subscription.create()
            self.assertEqual(subscription.delete(), {"deleted": True, "username": subscription.username})
        self.assertEqual(request.call_args_list[1].args, ("DELETE", "/api/users/192"))
        self.assertEqual(request.call_args_list[2].args, ("GET", f"/api/users/by-username/{subscription.username}"))

    def test_delete_looks_up_numeric_id_when_create_response_was_lost(self):
        subscription = self.subscription()
        subscription.create_attempted = True
        with patch.object(subscription, "_request", side_effect=[{"response": dict(self.USER)}, {}, None]) as request:
            self.assertTrue(subscription.delete()["deleted"])
        self.assertEqual(request.call_args_list[1].args, ("DELETE", "/api/users/192"))

    def test_delete_fails_when_user_survives(self):
        subscription = self.subscription()
        with patch.object(subscription, "_request", side_effect=[
            {"response": dict(self.USER)}, {}, {"response": dict(self.USER)},
        ]):
            subscription.create()
            with self.assertRaisesRegex(StandError, "всё ещё существует"):
                subscription.delete()


class UiDumpTests(unittest.TestCase):
    run_matrix = MatrixRunTests.run_matrix

    def test_adb_uiautomator_is_used_directly_without_mobile_mcp(self):
        stand = FakeStand()
        report = self.run_matrix(stand, scenarios=["happ"])
        self.assertEqual(report["verdict"], "skipped")
        self.assertEqual(report["setup_metrics"]["ui_driver"], "adb-uiautomator")
        self.assertNotIn("mcp_error", report["setup_metrics"])

    def test_failed_accessibility_dump_is_retried_as_empty_screen(self):
        stand = FakeStand()
        original = stand.runner.run.side_effect

        def command(argv, **kwargs):
            if "uiautomator dump" in argv[-1]:
                return CommandResult(1, b"", b"ERROR: could not get idle state.")
            return original(argv, **kwargs)

        stand.runner.run.side_effect = command
        report = self.run_matrix(stand, scenarios=["happ"])
        self.assertEqual(report["verdict"], "skipped")
        self.assertIn("could not get idle state", report["setup_metrics"]["ui_dump_error"])

    def test_killed_dump_cleans_up_uiautomator_and_battery_onboarding_retries(self):
        stand = FakeStand()
        stand.battery_onboarding = True
        original = stand.runner.run.side_effect
        attempts = {"count": 0}

        def command(argv, **kwargs):
            if "uiautomator dump" in argv[-1]:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    return CommandResult(137, b"", b"Killed")
            return original(argv, **kwargs)

        stand.runner.run.side_effect = command
        with patch("app_verify.bs_matrix.time.sleep"):
            report = self.run_matrix(stand, scenarios=["happ"])
        self.assertEqual(report["verdict"], "skipped", report)
        self.assertIn("pkill_uiautomator", stand.actions)
        self.assertIn(("tap", "Продолжить"), stand.actions)
        self.assertFalse(stand.battery_onboarding)
