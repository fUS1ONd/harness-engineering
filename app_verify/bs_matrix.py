"""Матрица живых сценариев БС для debug-сборки rumap-android."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shlex
import signal
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from uuid import uuid4

from .android import AndroidKvmAdapter, find_local_adb
from .core import StandError
from .debug_hooks import DEFAULT_DEBUG_PACKAGE, DebugHooksClient
from .net_emulation import NetEmulationClient, normalize_allow_ips
from .remnawave import TemporarySubscription
from .vpn_clients import HappSigillError, SUBSCRIPTION_URL_ENV, VpnClientsManager, resolve_subscription_url

PACKAGE = DEFAULT_DEBUG_PACKAGE
SCENARIOS = (
    "cellular-phases", "wifi-cellular", "wifi-toggle", "manual-check",
    "koala-accept-all", "koala-excluded", "doze-resume", "cold-start", "happ",
)
VPN_CONFLICTS = {"VPN_BLOCKED", "VPN_LEAK"}
RECHECK = "Проверить статус сети"
HOME_RECHECK = "Проверить снова"
ROUND_MESSAGE = "раунд проверки ориентиров"
MANUAL_CLASSES = ("Широкая", "Точечная", "Нет в белых списках")
MANUAL_ERRORS = (
    "наблюдение не запущено",
    "Не удалось запустить ручную проверку",
    "резолв доменов разрешён только через честный Wi-Fi",
)
MANUAL_ATTEMPTS = 3
START = "Запустить"
STOP_CALIBRATION = "Остановить"
NUDGE_INTERVAL_S = 20.0
DOZE_DEBOUNCE_GUARD_S = 35.0


class MatrixInterrupted(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum


class ScenarioSkipped(StandError):
    pass


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise StandError(reason)


def is_domain_target(target: str) -> bool:
    try:
        ipaddress.ip_network(target.strip(), strict=False)
        return False
    except ValueError:
        return True


class Matrix:
    def __init__(
        self,
        adapter: AndroidKvmAdapter,
        *,
        subscription_url: str | None,
        allow_ips: list[str],
        phase_timeout: float,
        manual_target: str,
    ) -> None:
        self.adapter = adapter
        self.config = adapter.config
        self.device = self.config.local_serial
        self.adb = find_local_adb(self.config.adb_bin)
        opts = dict(device=self.device, adb_bin=self.adb, runner=adapter.runner, timeout_s=self.config.adb_timeout_s)
        self.net = NetEmulationClient(**opts)
        self.debug = DebugHooksClient(**opts)
        self.vpn = VpnClientsManager(
            **opts, ssh_host=self.config.ssh_host, remote_root=self.config.remote_root,
            console_port=self.config.console_port, remote_adb_server_port=self.config.remote_adb_server_port,
        )
        candidate = subscription_url if subscription_url is not None else os.environ.get(SUBSCRIPTION_URL_ENV)
        self.url = resolve_subscription_url(candidate) if candidate else None
        self.temporary: TemporarySubscription | None = None
        self.phase_timeout = phase_timeout
        self.manual_target = manual_target
        self.allow_ips = normalize_allow_ips(allow_ips)
        self.metrics: dict[str, Any] = {}
        self.idle_forced = False
        self.koala_profile_ready = False
        self.log_anomalies: dict[tuple[str, ...], Any] = {}
        self.log_errors: list[str] = []
        self.log_line_count = None
        self.last_nudge_at: float | None = None
        self.last_round_at: float | None = None

    def shell(self, *args: str) -> str:
        command = [self.adb, "-s", self.device, "shell", shlex.join(args)]
        result = self.adapter.runner.run(command, timeout=self.config.adb_timeout_s)
        if result.returncode:
            details = result.stderr_text.strip() or result.stdout_text.strip() or f"code={result.returncode}"
            raise StandError(f"ADB {args[0]}: {details}")
        return result.stdout_text.strip()

    def wait(self, condition: Callable[[], Any], reason: str, *, timeout: float | None = None) -> Any:
        deadline = time.monotonic() + (self.phase_timeout if timeout is None else timeout)
        while True:
            value = condition()
            if value:
                return value
            if time.monotonic() >= deadline:
                raise StandError(reason)
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def type_text(self, text: str) -> None:
        require(text.isascii() and text.isprintable(), "ADB input text поддерживает только печатаемый ASCII")
        self.shell("input", "text", text.replace(" ", "%s"))

    def press(self, key: str) -> None:
        self.shell("input", "keyevent", f"KEYCODE_{key}")

    def adb_tree(self) -> str:
        path = f"/data/local/tmp/bs-matrix-{uuid4().hex}.xml"
        try:
            raw = self.shell(
                "sh", "-c", f"trap 'rm -f {path}' EXIT; uiautomator dump {path} >&2 && cat {path}",
            )
        except StandError as exc:
            if "Killed" in str(exc) or "already registered" in str(exc):
                try:
                    self.shell("sh", "-c", "pkill -f uiautomator >/dev/null 2>&1 || true")
                except StandError:
                    pass
            raise
        start = raw.find("<?xml")
        end = raw.find("</hierarchy>")
        require(start >= 0 and end >= start, "UIAutomator не вернул accessibility-дерево")
        root = ET.fromstring(raw[start:end + len("</hierarchy>")])
        lines = []
        for i, node in enumerate(root.iter("node"), start=1):
            match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
            if not match:
                continue
            x1, y1, x2, y2 = map(int, match.groups())
            if x1 >= x2 or y1 >= y2:
                continue
            ref = f"@e{i}"
            attrs = {
                "text": node.get("text"),
                "label": node.get("content-desc") or node.get("hint"),
                "id": node.get("resource-id"),
            }
            state = "" if node.get("enabled", "true") != "false" else " disabled"
            lines.append(f"{ref} " + " ".join(
                f"{name}={json.dumps(value, ensure_ascii=False)}" for name, value in attrs.items() if value
            ) + f" at={x1},{y1} size={x2 - x1}x{y2 - y1}" + state)
        return "\n".join(lines)

    def screen(self) -> str:
        self.metrics["ui_driver"] = "adb-uiautomator"
        try:
            tree = self.adb_tree()
        except (StandError, ET.ParseError) as exc:
            self.metrics["ui_dump_error"] = str(exc)
            tree = ""
        self.metrics["last_ui"] = tree[-4000:]
        return tree

    def ui_element(self, tree: str, label: str) -> tuple[int, int] | None:
        value = json.dumps(label, ensure_ascii=False)
        pattern = re.compile(rf'^@e\d+\b.*?\b(?:text|label|value|id)={re.escape(value)}(?:\s|$)')
        for line in tree.splitlines():
            if not pattern.search(line) or line.endswith(" disabled"):
                continue
            geometry = re.search(r" at=(\d+),(\d+) size=(\d+)x(\d+)", line)
            if geometry:
                x, y, width, height = map(int, geometry.groups())
                return x + width // 2, y + height // 2
        return None

    def tap_at(self, position: tuple[int, int]) -> None:
        self.shell("input", "tap", *map(str, position))

    def tap(self, label: str) -> None:
        position = self.wait(lambda: self.ui_element(self.screen(), label), f"На экране нет элемента «{label}»", timeout=20)
        self.tap_at(position)

    def tap_any(self, *labels: str) -> str:
        def find() -> tuple[str, tuple[int, int]] | None:
            tree = self.screen()
            for label in labels:
                position = self.ui_element(tree, label)
                if position is not None:
                    return label, position
            return None

        joined = " / ".join(f"«{label}»" for label in labels)
        label, position = self.wait(find, f"На экране нет элемента {joined}", timeout=20)
        self.tap_at(position)
        return label

    def diagnostics(self) -> dict:
        snapshot = self.debug.dump_diagnostics().diagnostics
        require(isinstance(snapshot, dict) and isinstance(snapshot.get("runtime"), dict), "dump-diagnostics без runtime")
        require(snapshot.get("mock_preset") == "clear", "На устройстве активен debug-мок")
        self.metrics["last_diagnostics"] = snapshot
        return snapshot["runtime"]

    def diag_rows(self) -> list[dict]:
        raw = self.shell("run-as", PACKAGE, "cat", "no_backup/diag.log")
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def rounds(self) -> list[dict]:
        return [row for row in self.diag_rows() if row.get("category") == "phase" and row.get("msg") == ROUND_MESSAGE]

    def diag_tail(self, count: int = 15) -> list[dict]:
        try:
            rows = self.diag_rows()
        except (StandError, ValueError) as exc:
            return [{"error": str(exc)}]
        return [
            {key: row.get(key) for key in ("ts", "category", "msg")}
            for row in rows if row.get("msg") != ROUND_MESSAGE
        ][-count:]

    def cursor(self) -> set[str]:
        return {row["ts"] for row in self.rounds()}

    def nudge_recheck(self) -> bool:
        """Ручной recheck обходит 30-секундный дебаунс OFF-фазы; жмём не чаще NUDGE_INTERVAL_S."""
        now = time.monotonic()
        if self.last_nudge_at is not None and now - self.last_nudge_at < NUDGE_INTERVAL_S:
            return False
        self.last_nudge_at = now
        position = self.ui_element(self.screen(), RECHECK)
        if position is None:
            self.debug.navigate("check")
            position = self.ui_element(self.screen(), RECHECK)
        if position is None:
            self.metrics["recheck_nudge_missed"] = self.metrics.get("recheck_nudge_missed", 0) + 1
            return False
        self.tap_at(position)
        self.metrics["recheck_nudges"] = self.metrics.get("recheck_nudges", 0) + 1
        return True

    def phase(
        self,
        expected: str | tuple[str, ...],
        before: set[str],
        *,
        timeout: float | None = None,
        nudge: bool = False,
        clear_cellular: bool = False,
    ) -> dict:
        allowed = (expected,) if isinstance(expected, str) else tuple(expected)
        label = "/".join(allowed)
        cellular_clears = 0

        def observed() -> dict | None:
            nonlocal cellular_clears
            runtime = self.diagnostics()
            if clear_cellular and runtime.get("network_conflict") == "CELLULAR_UNAVAILABLE" and cellular_clears < 2:
                cellular_clears += 1
                self.clear_conflict()
                self.debug.navigate("check")
                runtime = self.diagnostics()
            if (
                nudge and "OFF" not in allowed and runtime.get("phase") == "OFF"
                and runtime.get("network_conflict") == "NONE" and runtime.get("work_status") == "RUNNING"
                and self.nudge_recheck()
            ):
                runtime = self.diagnostics()
            fresh = [row for row in self.rounds() if row["ts"] not in before]
            self.metrics["last_rounds"] = fresh[-3:]
            if runtime.get("phase") not in allowed or runtime.get("network_conflict") != "NONE":
                return None
            for row in reversed(fresh):
                fields = row["fields"]
                if str(fields.get("phase")).upper() in allowed and not fields.get("blocked_by"):
                    return row
            return None

        start = time.monotonic()
        row = self.wait(observed, f"Нет свежего раунда {label} без сетевого конфликта", timeout=timeout)
        self.last_round_at = time.monotonic()
        self.metrics.setdefault("phases", []).append({
            "expected": label, "elapsed_s": round(time.monotonic() - start, 2), "round": row,
            "diagnostics": self.metrics["last_diagnostics"],
        })
        return row["fields"]

    def profile(self, profile: str, wifi: str, *, vpn: bool = False) -> None:
        self.net.apply_profile(profile, allow_ips=self.allow_ips, wifi=wifi)
        self.network(profile, wifi, vpn=vpn)

    def network(self, profile: str, wifi: str, *, vpn: bool = False) -> None:
        def ready() -> bool:
            status = self.net.status()
            self.metrics["last_network"] = status.to_dict()
            return (
                status.profile == profile and status.cellular_connected and bool(status.cell_interface)
                and status.wifi_enabled == (wifi == "on") and status.wifi_connected == (wifi == "on")
                and status.vpn_active == vpn
            )

        self.wait(ready, f"Не установилась топология {profile}, Wi-Fi {wifi}, VPN {vpn}", timeout=25)
        self.metrics.setdefault("networks", []).append(self.metrics["last_network"])

    def recheck(self) -> None:
        self.tap(RECHECK)
        self.last_nudge_at = time.monotonic()
        self.metrics["recheck_button"] = RECHECK

    def clear_conflict(self) -> None:
        runtime = self.diagnostics()
        conflict = runtime.get("network_conflict")
        if conflict in (None, "NONE"):
            return
        self.metrics.setdefault("cleared_conflicts", []).append(conflict)
        self.debug.navigate("home")
        self.tap(HOME_RECHECK)
        self.wait(
            lambda: self.diagnostics().get("network_conflict") == "NONE",
            f"«{HOME_RECHECK}» не снял конфликт {conflict}",
        )

    def baseline(self, wifi: str = "off") -> None:
        self.profile("off", wifi)
        self.clear_conflict()
        before = self.cursor()
        self.debug.navigate("check")
        runtime = self.diagnostics()
        if runtime.get("phase") == "OFF" and runtime.get("network_conflict") == "NONE":
            self.recheck()
        self.phase("OFF", before)

    def require_landmarks(self, fields: dict) -> None:
        require(type(fields.get("took_ms")) in (int, float) and fields["took_ms"] >= 0, "Нет валидного took_ms в diaglog")
        require(bool(fields.get("local_ip")), "Нет local_ip сокета в diaglog")
        for group in ("cut", "pass"):
            marks = fields.get(group)
            require(isinstance(marks, list) and bool(marks), f"Нет поориентирных ответов {group} в diaglog")
            for mark in marks:
                require(
                    all(mark.get(key) for key in ("addr", "wire_icmp", "wire_tcp", "modem_icmp", "modem_tcp")),
                    f"Неполный поориентирный ответ {group}",
                )

    def require_no_icmp_silence(self, fields: dict) -> None:
        self.require_landmarks(fields)
        for group in ("cut", "pass"):
            for mark in fields[group]:
                require(
                    mark.get("modem_icmp") == "unprobed" and mark.get("modem_tcp") == "silence",
                    f"Фаза UNKNOWN допустима только при отключённом ICMP и полном молчании TCP ({group})",
                )

    def cellular_phases(self) -> None:
        self.baseline()
        before = self.cursor()
        self.profile("soft", "off")
        self.recheck()
        soft_fields = self.phase("SOFT", before, nudge=True)
        self.require_landmarks(soft_fields)
        icmp_probed = any(
            mark.get("modem_icmp") != "unprobed"
            for group in ("cut", "pass") for mark in soft_fields[group]
        )
        self.metrics["icmp_available"] = icmp_probed
        for profile in ("hard", "down"):
            before = self.cursor()
            self.profile(profile, "off")
            expected: str | tuple[str, ...] = profile.upper() if icmp_probed else (profile.upper(), "UNKNOWN")
            fields = self.phase(expected, before, nudge=True)
            if str(fields.get("phase")).upper() == "UNKNOWN":
                self.require_no_icmp_silence(fields)
        before = self.cursor()
        self.profile("off", "off")
        self.phase("OFF", before, nudge=True)
        self.metrics.setdefault("recheck_nudges", 0)

    def wifi_cellular(self) -> None:
        self.baseline("on")
        before = self.cursor()
        self.profile("soft", "on")
        self.recheck()
        self.require_landmarks(self.phase("SOFT", before, nudge=True))

    def wifi_toggle(self) -> None:
        self.baseline()
        before = self.cursor()
        self.profile("soft", "off")
        self.recheck()
        self.phase("SOFT", before, nudge=True)
        for wifi in ("on", "off"):
            before = self.cursor()
            self.shell("svc", "wifi", "enable" if wifi == "on" else "disable")
            self.network("soft", wifi)
            self.phase("SOFT", before, nudge=True)

    def manual_allowed(self) -> bool:
        runtime = self.diagnostics()
        if not (
            runtime.get("work_status") == "RUNNING" and runtime.get("phase") in ("SOFT", "HARD")
            and runtime.get("network_conflict") == "NONE"
        ):
            return False
        tree = self.screen()
        return self.ui_element(tree, RECHECK) is None and self.ui_element(tree, "Проверить") is not None

    def manual_outcome(self) -> tuple[str, str] | None:
        tree = self.screen()
        if f'text="{self.manual_target}"' in tree:
            label = next((label for label in MANUAL_CLASSES if f'text="{label}"' in tree), None)
            if label:
                return "result", label
        error = next((line for line in tree.splitlines() if any(marker in line for marker in MANUAL_ERRORS)), None)
        if error:
            match = re.search(r'text=("(?:[^"\\]|\\.)*")', error)
            return "error", json.loads(match.group(1)) if match else error
        return None

    def stop_calibration_if_running(self) -> bool:
        """Останавливает фоновую автокалибровку, если она временно поставила orchestrator на паузу."""
        self.debug.navigate("calibration")
        try:
            position = self.ui_element(self.screen(), STOP_CALIBRATION)
            if position is None:
                return False
            self.tap_at(position)
            self.metrics["stopped_calibration"] = self.metrics.get("stopped_calibration", 0) + 1
            return True
        finally:
            self.debug.navigate("check")

    def manual_check(self) -> None:
        wifi = "on" if is_domain_target(self.manual_target) else "off"
        self.metrics["manual_wifi"] = wifi
        self.baseline(wifi)
        before = self.cursor()
        self.profile("soft", wifi)
        self.recheck()
        self.phase("SOFT", before, nudge=True)
        self.debug.navigate("check")
        self.wait(self.manual_allowed, "Ручная проверка не стала доступна в фазе SOFT")
        self.tap("77.73.236.0/24, IP или домен")
        self.type_text(self.manual_target)
        self.press("BACK")
        errors = []
        for attempt in range(1, MANUAL_ATTEMPTS + 1):
            self.wait(self.manual_allowed, "Ручная проверка не стала доступна в фазе SOFT")
            if f'text="{self.manual_target}"' not in self.screen():
                self.tap("77.73.236.0/24, IP или домен")
                self.type_text(self.manual_target)
                self.press("BACK")
            self.metrics["manual_attempts"] = attempt
            self.tap("Проверить")
            if attempt > 1:
                time.sleep(min(1.0, self.phase_timeout))
            kind, value = self.wait(self.manual_outcome, f"Нет завершённого результата ручной проверки {self.manual_target}")
            if kind == "result":
                self.metrics["manual_result"] = {"target": self.manual_target, "class": value}
                return
            errors.append({"attempt": attempt, "error": value, "diag_tail": self.diag_tail()})
            self.metrics["manual_errors"] = errors
            if attempt < MANUAL_ATTEMPTS:
                before = self.cursor()
                if "наблюдение не запущено" in value:
                    self.stop_calibration_if_running()
                self.phase("SOFT", before, nudge=True)
        raise StandError(f"Ручная проверка {self.manual_target} вернула ошибку: {errors[-1]['error']}")

    def subscription(self) -> str:
        if not self.url:
            if self.temporary is None:
                self.temporary = TemporarySubscription()
            self.url = self.temporary.create()
        return resolve_subscription_url(self.url)

    def prepare_vpn(self, client: str) -> None:
        status = self.vpn.status(client)
        if not status.installed:
            apk = "koala-clash.apk" if client == "koala" else "happ.apk"
            status = self.vpn.prepare(client, apk=f"{self.config.remote_root}/apks/{apk}")
        else:
            status = self.vpn.prepare(client)
        self.metrics["vpn_prepare"] = status.to_dict()
        self.metrics["vpn_status"] = self.vpn.status(client).to_dict()
        require(status.installed and status.activate_vpn == "allow", f"{client}: VPN не разрешён")

    def koala(self, *, excluded: bool) -> None:
        self.prepare_vpn("koala")
        if not self.koala_profile_ready:
            self.vpn.import_subscription("koala", url=self.subscription(), name="bs-matrix")
            self.koala_profile_ready = True
        self.vpn.set_exclude("koala", packages=[PACKAGE] if excluded else None, clear=not excluded)
        self.vpn.connect("koala")
        status = self.vpn.status("koala")
        self.metrics["vpn_status"] = status.to_dict()
        mode = "DenySelected" if excluded else "AcceptAll"
        require(
            status.vpn_active and bool(status.vpn_interface) and status.exclude_mode == mode
            and (PACKAGE in status.excluded_packages) == excluded,
            f"Koala Clash не подтвердил активный VPN в режиме {mode}",
        )

    def koala_accept_all(self) -> None:
        self.baseline("on")
        before = self.cursor()
        self.koala(excluded=False)
        self.profile("soft", "off", vpn=True)
        self.adapter.launch(PACKAGE)
        self.debug.navigate("check")

        def conflict() -> str | None:
            runtime = self.diagnostics()
            value = runtime.get("network_conflict")
            if value in VPN_CONFLICTS and runtime.get("vpn_active"):
                return value
            return None

        code = self.wait(conflict, "Приложение не зафиксировало VPN_BLOCKED или VPN_LEAK")
        self.metrics["conflict"] = code
        self.require_visible("Настройки VPN")
        fresh = [row for row in self.rounds() if row["ts"] not in before]
        self.metrics["rounds"] = fresh[-5:]
        blocked = [row for row in fresh if row["fields"].get("blocked_by") == f"network_conflict:{code}"]
        self.metrics["blocked_round"] = blocked[-1] if blocked else None

    def require_visible(self, label: str) -> None:
        self.wait(lambda: self.ui_element(self.screen(), label), f"На экране нет «{label}»", timeout=20)

    def koala_excluded(self) -> None:
        self.baseline("on")
        self.koala(excluded=True)
        before = self.cursor()
        self.profile("soft", "off", vpn=True)
        self.adapter.launch(PACKAGE)
        self.require_landmarks(self.phase("SOFT", before, nudge=True, clear_cellular=True))
        self.metrics["network_conflict"] = "NONE"

    def leave_idle(self) -> dict[str, str]:
        self.shell("dumpsys", "deviceidle", "unforce")
        self.idle_forced = False
        return {"deviceidle": "unforced"}

    def doze_resume(self) -> None:
        self.baseline()
        self.press("HOME")
        self.idle_forced = True
        try:
            self.shell("dumpsys", "deviceidle", "force-idle")
            idle = self.shell("dumpsys", "deviceidle", "get", "deep")
            self.metrics["deviceidle"] = idle
            require(idle == "IDLE", "Android не вошёл в глубокий Doze")
            self.profile("soft", "off")
            # onResume() делает recheck без manual: держим Doze >= 35 с, чтобы выйти за 30-секундный дебаунс.
            hold = max(0.0, float(DOZE_DEBOUNCE_GUARD_S))
            self.metrics["doze_hold_s"] = round(hold, 2)
            if hold > 0:
                time.sleep(hold)
            self.metrics["since_baseline_round_s"] = round(time.monotonic() - (self.last_round_at or 0.0), 2)
        finally:
            self.leave_idle()
        runtime = self.diagnostics()
        self.metrics["phase_before_resume"] = runtime["phase"]
        require(runtime["phase"] == "OFF", "До onResume фаза успела смениться с OFF")
        before = self.cursor()
        self.adapter.launch(PACKAGE)
        self.phase("SOFT", before)

    def cold_start(self) -> None:
        self.baseline()
        self.shell("am", "force-stop", PACKAGE)
        self.metrics["package_stopped"] = "stopped=true" in self.shell("dumpsys", "package", PACKAGE)
        require(self.metrics["package_stopped"], "Android не подтвердил force-stop")
        self.profile("soft", "off")
        before = self.cursor()
        self.adapter.launch(PACKAGE)
        self.wait(lambda: self.diagnostics().get("runtime_present"), "Сервис приложения не поднялся после cold start", timeout=30)
        try:
            self.wait(lambda: self.diagnostics().get("work_status") == "RUNNING", "", timeout=min(10.0, self.phase_timeout))
            restored = True
        except StandError:
            restored = False
        runtime = self.metrics["last_diagnostics"]["runtime"]
        self.metrics["observation_auto_restored"] = restored
        self.metrics["work_status_after_launch"] = runtime.get("work_status")
        if not restored:
            # Продукт не восстанавливает наблюдение при запуске Activity после force-stop: запускаем вручную.
            self.debug.navigate("home")
            self.tap(START)
            self.metrics["start_button"] = START
            self.wait(lambda: self.diagnostics().get("work_status") == "RUNNING", "Наблюдение не запустилось после «Запустить»", timeout=30)
        self.require_landmarks(self.phase("SOFT", before, nudge=True))

    def happ(self) -> None:
        self.profile("off", "on")
        self.prepare_vpn("happ")
        abi = self.shell("getprop", "ro.product.cpu.abi")
        self.metrics["device_abi"] = abi
        self.vpn.import_subscription("happ", url=self.subscription(), name="bs-matrix")
        self.vpn.set_exclude("happ", clear=True)
        try:
            self.vpn.connect("happ")
        except HappSigillError as exc:
            if abi == "x86_64" and self.metrics["vpn_status"].get("abi_warning"):
                raise ScenarioSkipped(f"ARM-only APK: {exc}") from None
            raise
        before = self.cursor()
        self.profile("soft", "off", vpn=True)
        self.adapter.launch(PACKAGE)
        self.debug.navigate("check")
        self.wait(
            lambda: self.diagnostics().get("network_conflict") in VPN_CONFLICTS,
            "Happ без исключения: отсутствует VPN-конфликт",
        )
        self.profile("off", "on", vpn=True)
        self.vpn.set_exclude("happ", packages=[PACKAGE])
        self.vpn.disconnect("happ")
        self.vpn.connect("happ")
        status = self.vpn.status("happ")
        self.metrics["vpn_status"] = status.to_dict()
        require(status.vpn_active and PACKAGE in status.excluded_packages, "Happ не подтвердил исключение rumap")
        before = self.cursor()
        self.profile("soft", "off", vpn=True)
        self.adapter.launch(PACKAGE)
        self.require_landmarks(self.phase("SOFT", before, nudge=True, clear_cellular=True))

    def disconnect(self, client: str) -> dict:
        status = self.vpn.status(client)
        if status.installed:
            status = self.vpn.disconnect(client)
            require(not status.vpn_active, f"{client}: VPN остался активен")
        return status.to_dict()

    def reset_network(self) -> dict:
        status = self.net.reset()
        require(status.profile == "off" and status.wifi_enabled, "net reset не снял БС или не включил Wi-Fi")
        return status.to_dict()

    def stop(self) -> dict:
        self.adapter.stop()
        status = self.adapter.status()
        require(status.state == "stopped" and not status.details.get("tunnel_alive"), "Эмулятор или ADB-туннель остался активен")
        return status.to_dict()

    def collect_stability(self) -> dict:
        try:
            log = self.adapter.collect_logs(package=PACKAGE)
            self.log_line_count = log.details.get("line_count")
            for anomaly in (*log.crashes, *log.anrs):
                self.log_anomalies[(anomaly.kind, anomaly.summary, *anomaly.lines)] = anomaly
        except Exception as exc:
            self.log_errors.append(str(exc))
        crashes = [item.to_dict() for item in self.log_anomalies.values() if item.kind == "crash"]
        anrs = [item.to_dict() for item in self.log_anomalies.values() if item.kind == "anr"]
        return {
            "status": "fail" if crashes or anrs or self.log_errors else "pass",
            "crash_count": len(crashes), "anr_count": len(anrs), "crashes": crashes, "anrs": anrs,
            "line_count": self.log_line_count, "errors": self.log_errors,
        }

    def start_app(self) -> None:
        self.adapter.launch(PACKAGE)
        self.shell("input", "keyevent", "KEYCODE_WAKEUP")
        self.shell("wm", "dismiss-keyguard")

        def ready() -> bool:
            runtime = self.diagnostics()
            return runtime.get("runtime_present", False)

        self.wait(ready, "Сервис приложения не запустился", timeout=30)
        runtime = self.diagnostics()
        if runtime.get("battery_onboarding_required"):
            self.tap_any("Продолжить", "Настроить позже")
        if self.diagnostics().get("region_onboarding_required"):
            self.tap("Поиск региона или округа")
            self.type_text("RU-MOW")
            self.press("BACK")
            self.tap("Москва")
        self.debug.navigate("check")
        roaming = self.ui_element(self.screen(), "Разрешить в роуминге")
        if roaming:
            self.tap_at(roaming)
        self.debug.navigate("home")
        if self.diagnostics().get("work_status") != "RUNNING":
            self.tap("Запустить")
        self.wait(
            lambda: self.diagnostics().get("work_status") == "RUNNING", "Наблюдение не запустилось", timeout=30,
        )
        self.wait(self.rounds, "diaglog не содержит раунды проверки ориентиров")


def run_bs_matrix(
    adapter: AndroidKvmAdapter,
    *,
    apk: Path,
    scenarios: list[str] | None = None,
    subscription_url: str | None = None,
    allow_ips: list[str] | None = None,
    phase_timeout: float = 90,
    manual_target: str = "ya.ru",
) -> dict:
    selected = [name for name in SCENARIOS if not scenarios or name in scenarios]
    if scenarios and set(scenarios) - set(SCENARIOS):
        raise StandError("Неизвестный сценарий БС")
    require(apk.is_file(), f"APK не найден: {apk}")
    require(0 < phase_timeout < 720, "Таймаут фазы должен быть больше 0 и меньше 720 секунд")
    with apk.open("rb") as artifact:
        digest = hashlib.file_digest(artifact, "sha256").hexdigest()
    matrix = Matrix(
        adapter, subscription_url=subscription_url, allow_ips=allow_ips or [],
        phase_timeout=phase_timeout, manual_target=manual_target,
    )
    results = {name: {"status": "skipped", "reason": "Сценарий не запущен"} for name in selected}
    report: dict[str, Any] = {
        "apk": str(apk), "apk_sha256": digest, "package": PACKAGE,
        "stand": adapter.config.ssh_host, "device": adapter.config.local_serial,
        "scenarios": results, "cleanup": {},
    }
    phase = "stand"
    interrupted = None
    cleaning = False
    started = False
    starting = False
    online = False

    def on_signal(signum: int, _frame: Any) -> None:
        nonlocal interrupted
        if interrupted is None:
            interrupted = signum
            if not (cleaning or starting):
                raise MatrixInterrupted(signum)

    def cleanup(name: str, action: Callable[[], Any]) -> bool:
        try:
            report["cleanup"][name] = {"status": "pass", "details": action()}
            return True
        except Exception as exc:
            report["cleanup"][name] = {"status": "fail", "reason": str(exc)}
            return False

    previous = {signum: signal.signal(signum, on_signal) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        started = starting = True
        try:
            adapter.start()
            online = True
        finally:
            starting = False
        if interrupted:
            raise MatrixInterrupted(interrupted)
        adapter.collect_logs(clear=True)
        report["install"] = adapter.install(apk).to_dict()
        matrix.start_app()
        report["setup_metrics"] = matrix.metrics
        for phase in selected:
            matrix.metrics = {}
            start = time.monotonic()
            entry = results[phase]
            try:
                getattr(matrix, phase.replace("-", "_"))()
                entry.update(status="pass")
                entry.pop("reason", None)
            except ScenarioSkipped as exc:
                entry.update(status="skipped", reason=str(exc))
            except MatrixInterrupted:
                entry.update(status="fail", reason="Прерван сигналом")
                raise
            except Exception as exc:
                entry.update(status="fail", reason=str(exc))
            finally:
                entry.update(metrics=matrix.metrics, elapsed_s=round(time.monotonic() - start, 2))
            matrix.collect_stability()
            if phase.startswith("koala-") or phase == "happ":
                client = "koala" if phase.startswith("koala-") else "happ"
                if not cleanup(f"after_{phase}", lambda: matrix.disconnect(client)):
                    break
    except MatrixInterrupted:
        if phase == "stand":
            report["error"] = "Подготовка стенда прервана сигналом"
    except Exception as exc:
        report["error"] = str(exc)
        report["setup_metrics"] = matrix.metrics
    finally:
        cleaning = True
        try:
            if online:
                if matrix.idle_forced:
                    cleanup("deviceidle_unforce", matrix.leave_idle)
                for client in ("koala", "happ"):
                    cleanup(f"vpn_disconnect_{client}", lambda client=client: matrix.disconnect(client))
                cleanup("net_reset", matrix.reset_network)
                report["stability"] = matrix.collect_stability()
            if matrix.temporary:
                cleanup("temporary_user", matrix.temporary.delete)
            if started:
                cleanup("stop", matrix.stop)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

    if matrix.url:
        def redact(value: Any) -> Any:
            if isinstance(value, str):
                return value.replace(matrix.url, "[subscription]").replace(quote(matrix.url, safe=""), "[subscription]")
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [redact(item) for item in value]
            return value

        report = redact(report)

    statuses = [row["status"] for row in results.values()]
    failed = (
        "error" in report or "fail" in statuses or report.get("stability", {}).get("status") != "pass"
        or any(row["status"] == "fail" for row in report["cleanup"].values())
    )
    report["verdict"] = "fail" if failed or interrupted else ("pass" if "pass" in statuses else "skipped")
    report["exit_code"] = 128 + interrupted if interrupted else (2 if failed else 0)
    if interrupted:
        report["signal"] = signal.Signals(interrupted).name
    return report
