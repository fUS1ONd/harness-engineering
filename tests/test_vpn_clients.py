#!/usr/bin/env python3
"""Unit-тесты управления VPN-клиентами Koala Clash и Happ (без реального adb/эмулятора)."""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import tempfile
import unittest
from typing import Any
from unittest import mock

from app_verify.android import CommandResult, HddInvariantError, StandTimeoutError
from app_verify.cli import run_cli
from app_verify.core import StandError
from app_verify.net_emulation import parse_connectivity
from app_verify.vpn_clients import (
    DEFAULT_EXCLUDE_PACKAGE,
    HAPP_PACKAGE,
    KOALA_ACTION_START,
    KOALA_ACTION_STOP,
    KOALA_PACKAGE,
    SUBSCRIPTION_URL_ENV,
    VpnClientsManager,
    detect_happ_sigill,
    extract_vpn_uid_ranges,
    normalize_exclude_packages,
    normalize_vpn_client,
    parse_appops_activate_vpn,
    parse_koala_service_xml,
    render_koala_service_xml,
    resolve_subscription_url,
)

CELL_WIFI_NO_VPN = """Active default network: 101
Current Networks:
  NetworkAgentInfo{network{100} nc{[ Transports: CELLULAR Capabilities: INTERNET&NOT_VPN ]}
    InterfaceName: eth0
  }
  NetworkAgentInfo{network{101} nc{[ Transports: WIFI Capabilities: INTERNET&NOT_VPN ]}
    InterfaceName: wlan0
  }
"""

CELL_WIFI_KOALA_VPN_ALL = """Active default network: 101
Current Networks:
  NetworkAgentInfo{network{101} nc{[ Transports: WIFI Capabilities: INTERNET&NOT_VPN ]}
    InterfaceName: wlan0
  }
  NetworkAgentInfo{network{103} lp{{InterfaceName: tun0 LinkAddresses: [ 172.19.0.1/30 ]}} nc{[ Transports: WIFI|VPN Capabilities: INTERNET&TRUSTED&VALIDATED TransportInfo: <VpnTransportInfo{type=1, sessionId=Clash}> Uids: <{0-10191, 10193-99999}> OwnerUid: 10192 ]}
  }
"""

CELL_WIFI_KOALA_VPN_DENY = """Active default network: 101
Current Networks:
  NetworkAgentInfo{network{101} nc{[ Transports: WIFI Capabilities: INTERNET&NOT_VPN ]}
    InterfaceName: wlan0
  }
  NetworkAgentInfo{network{103} lp{{InterfaceName: tun0 LinkAddresses: [ 172.19.0.1/30 ]}} nc{[ Transports: WIFI|VPN Capabilities: INTERNET&TRUSTED&VALIDATED TransportInfo: <VpnTransportInfo{type=1, sessionId=Clash}> Uids: <{0-10191, 10193-10193, 10195-99999}> OwnerUid: 10192 ]}
  }
"""

CELL_WIFI_HAPP_VPN_ALL = """Active default network: 101
Current Networks:
  NetworkAgentInfo{network{101} nc{[ Transports: WIFI Capabilities: INTERNET&NOT_VPN ]}
    InterfaceName: wlan0
  }
  NetworkAgentInfo{network{104} lp{{InterfaceName: tun0 LinkAddresses: [ 172.19.0.1/30 ]}} nc{[ Transports: WIFI|VPN Capabilities: INTERNET&TRUSTED&VALIDATED TransportInfo: <VpnTransportInfo{type=1, sessionId=Happ}> Uids: <{0-10192, 10194-99999}> OwnerUid: 10193 ]}
  }
"""

HAPP_PER_APP_UI_INITIAL = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy>
  <node resource-id="su.happ.proxyutility:id/search_button" content-desc="Search" bounds="[848,138][975,264]" />
  <node resource-id="su.happ.proxyutility:id/ll_off" bounds="[0,380][360,542]">
    <node text="Off" resource-id="su.happ.proxyutility:id/tv_off" bounds="[150,380][209,521]" />
  </node>
  <node resource-id="su.happ.proxyutility:id/ll_bypass" bounds="[720,380][1080,521]">
    <node text="Bypass" resource-id="su.happ.proxyutility:id/tv_bypass" bounds="[831,380][969,521]" />
  </node>
</hierarchy>
"""

HAPP_PER_APP_UI_SEARCHED_UNCHECKED = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy>
  <node resource-id="su.happ.proxyutility:id/search_src_text" text="ru.rumap.android.debug" bounds="[210,152][903,247]" />
  <node resource-id="su.happ.proxyutility:id/search_close_btn" bounds="[903,138][975,264]" />
  <node resource-id="su.happ.proxyutility:id/rv_list_applications" bounds="[42,780][1080,2400]">
    <node resource-id="su.happ.proxyutility:id/cl_app" clickable="true" bounds="[42,818][1080,944]">
      <node resource-id="su.happ.proxyutility:id/names" bounds="[168,830][938,932]">
        <node text="ru.rumap.android.debug" resource-id="su.happ.proxyutility:id/package_name" bounds="[194,881][912,932]" />
      </node>
      <node resource-id="su.happ.proxyutility:id/check_box" checked="false" bounds="[938,818][1080,944]" />
    </node>
  </node>
</hierarchy>
"""


def _ok(text: str = "") -> CommandResult:
    return CommandResult(returncode=0, stdout=text.encode("utf-8"), stderr=b"")


def _fail(message: str = "error", code: int = 1) -> CommandResult:
    return CommandResult(returncode=code, stdout=b"", stderr=message.encode("utf-8"))


class _FakeVpnEnv:
    """Фейковый `CommandRunner`, симулирующий локальный `adb` и `ssh home`."""

    def __init__(
        self,
        *,
        installed: set[str] | None = None,
        connectivity: str = CELL_WIFI_NO_VPN,
        device_abi: str = "x86_64",
        pkg_abis: dict[str, str] | None = None,
        pkg_uids: dict[str, int] | None = None,
        koala_service_xml: str = "",
        happ_sigill_on_connect: bool = True,
        koala_ignore_stop_intent: bool = False,
    ) -> None:
        self.installed: set[str] = set(installed or ())
        self.connectivity = connectivity
        self.tun0_up = "InterfaceName: tun0" in connectivity
        self.device_abi = device_abi
        self.pkg_abis: dict[str, str] = dict(
            pkg_abis
            or {
                KOALA_PACKAGE: "x86_64",
                HAPP_PACKAGE: "arm64-v8a",
            }
        )
        self.pkg_uids: dict[str, int] = dict(
            pkg_uids
            or {
                KOALA_PACKAGE: 10192,
                HAPP_PACKAGE: 10193,
            }
        )
        self.appops: dict[str, dict[str, str]] = {}
        self.granted_permissions: dict[str, set[str]] = {}
        self.koala_service_xml = koala_service_xml
        self.happ_exclude_json = ""
        self.happ_sub_ready = False
        self.happ_sigill_on_connect = happ_sigill_on_connect
        self.koala_ignore_stop_intent = koala_ignore_stop_intent
        self.logcat_text = ""
        self.ui_xml = ""
        self.taps: list[tuple[int, int]] = []
        self.typed_texts: list[str] = []
        self.started_intents: list[list[str]] = []
        self.broadcasts: list[list[str]] = []
        self.force_stops: list[str] = []
        self.devices: list[str | None] = []
        self.calls: list[list[str]] = []
        self.ssh_scripts: list[str] = []
        self.local_installs: list[str] = []

    def _activate_koala_vpn(self) -> None:
        prefs = parse_koala_service_xml(self.koala_service_xml)
        self.tun0_up = True
        if prefs.access_control_mode == "DenySelected" and prefs.access_control_packages:
            self.connectivity = CELL_WIFI_KOALA_VPN_DENY
        else:
            self.connectivity = CELL_WIFI_KOALA_VPN_ALL

    def _deactivate_vpn(self) -> None:
        self.tun0_up = False
        self.connectivity = CELL_WIFI_NO_VPN

    def run(self, args: list[str], *, timeout=None, input_bytes=None, env=None) -> CommandResult:
        self.calls.append(list(args))
        if args[0] == "ssh":
            script = (input_bytes or b"").decode("utf-8", errors="replace")
            self.ssh_scripts.append(script)
            if "koala" in script:
                self.installed.add(KOALA_PACKAGE)
            if "happ" in script.lower():
                self.installed.add(HAPP_PACKAGE)
            return _ok("Performing Streamed Install\nSuccess\n")

        rest = list(args[1:])
        device: str | None = None
        if rest[:1] == ["-s"]:
            device = rest[1]
            rest = rest[2:]
        self.devices.append(device)

        if rest[:1] == ["install"]:
            apk_path = rest[-1]
            self.local_installs.append(apk_path)
            if "koala" in apk_path.lower():
                self.installed.add(KOALA_PACKAGE)
            elif "happ" in apk_path.lower():
                self.installed.add(HAPP_PACKAGE)
            return _ok("Performing Streamed Install\nSuccess\n")

        if rest == ["root"]:
            return _ok("adbd is already running as root\n")
        if rest == ["wait-for-device"]:
            return _ok()
        if not rest or rest[0] != "shell":
            return _fail(f"unexpected adb args: {rest}")

        cmd_str = rest[1] if len(rest) > 1 else ""
        if "service.xml" in cmd_str and "printf" in cmd_str:
            if 'chown "$OWNER"' not in cmd_str or "chmod 771" not in cmd_str or "chmod 660" not in cmd_str:
                return _fail("missing chown/chmod for service.xml")
            m = re.search(r"printf '%s' (.*?) > /data/data/com\.koala\.clash/shared_prefs/service\.xml", cmd_str, re.S)
            if not m:
                return _fail("malformed printf for service.xml")
            self.koala_service_xml = shlex.split(m.group(1))[0]
            return _ok()
        if "app_verify_exclude.json" in cmd_str and "printf" in cmd_str:
            if 'chown "$OWNER"' not in cmd_str or "chmod 771" not in cmd_str or "chmod 600" not in cmd_str:
                return _fail("missing chown/chmod for happ state")
            tokens_line = next(
                line for line in cmd_str.splitlines() if "printf" in line
            )
            parts = shlex.split(tokens_line)
            self.happ_exclude_json = parts[2]
            return _ok()
        if "uiautomator dump" in cmd_str:
            return _ok(self.ui_xml)

        tokens = shlex.split(cmd_str)
        if tokens == ["id", "-u"]:
            return _ok("0\n")
        if tokens[:2] == ["pm", "path"] and len(tokens) == 3:
            pkg = tokens[2]
            return _ok(f"package:/data/app/{pkg}/base.apk\n") if pkg in self.installed else _ok("")
        if tokens[:2] == ["appops", "set"] and len(tokens) == 5:
            pkg, op, mode = tokens[2], tokens[3], tokens[4]
            self.appops.setdefault(pkg, {})[op] = mode
            return _ok()
        if tokens[:2] == ["appops", "get"] and len(tokens) == 3:
            ops = self.appops.get(tokens[2], {})
            if not ops:
                return _ok("No operations.\n")
            return _ok("\n".join(f"{op}: {mode}" for op, mode in sorted(ops.items())) + "\n")
        if tokens[:2] == ["pm", "grant"] and len(tokens) == 4:
            self.granted_permissions.setdefault(tokens[2], set()).add(tokens[3])
            return _ok()
        if tokens == ["getprop", "ro.product.cpu.abi"]:
            return _ok(f"{self.device_abi}\n")
        if tokens[:2] == ["dumpsys", "package"] and len(tokens) == 3:
            pkg = tokens[2]
            if pkg not in self.installed:
                return _fail(f"Unable to find package: {pkg}")
            abi = self.pkg_abis.get(pkg, "x86_64")
            uid = self.pkg_uids.get(pkg, 10192)
            return _ok(f"Package [{pkg}]:\n    userId={uid}\n    primaryCpuAbi={abi}\n")
        if tokens == ["dumpsys", "connectivity"]:
            return _ok(self.connectivity)
        if tokens == ["ip", "link", "show", "tun0"]:
            return _ok("18: tun0: <POINTOPOINT,UP>\n") if self.tun0_up else _fail('Device "tun0" does not exist.')
        if tokens[:1] == ["cat"] and len(tokens) == 2:
            if tokens[1].endswith("service.xml"):
                return _ok(self.koala_service_xml) if self.koala_service_xml else _fail("missing")
            if tokens[1].endswith("app_verify_exclude.json"):
                return _ok(self.happ_exclude_json) if self.happ_exclude_json else _fail("missing")
        if tokens[:2] == ["test", "-s"] and tokens[2].endswith("/mmkv/SUB"):
            return _ok() if self.happ_sub_ready else _fail("", code=1)
        if tokens[:2] == ["logcat", "-c"]:
            self.logcat_text = ""
            return _ok()
        if tokens[:2] == ["logcat", "-d"]:
            return _ok(self.logcat_text)
        if tokens[:2] == ["am", "force-stop"] and len(tokens) == 3:
            pkg = tokens[2]
            self.force_stops.append(pkg)
            owner_uid = parse_connectivity(self.connectivity).vpn_owner_uid
            if owner_uid is None or owner_uid == self.pkg_uids.get(pkg):
                self._deactivate_vpn()
            return _ok()
        if tokens[:2] == ["am", "broadcast"]:
            self.broadcasts.append(tokens)
            if "su.happ.proxyutility.action.widget.click" in tokens:
                owner_uid = parse_connectivity(self.connectivity).vpn_owner_uid
                happ_owns_tun = self.tun0_up and (
                    owner_uid is None or owner_uid == self.pkg_uids.get(HAPP_PACKAGE)
                )
                if happ_owns_tun:
                    self._deactivate_vpn()
                elif self.happ_sigill_on_connect:
                    self.logcat_text = (
                        "F/libc ( 6908): Fatal signal 4 (SIGILL) in tid 7718 (lity:XRayDaemon), "
                        "pid 6908 (su.happ.proxyutility:XRayDaemon)\n"
                    )
                else:
                    self.tun0_up = True
                    self.connectivity = CELL_WIFI_HAPP_VPN_ALL
            return _ok("Broadcast completed: result=0\n")
        if tokens[:2] == ["am", "start"]:
            self.started_intents.append(tokens)
            joined = " ".join(tokens)
            if "koala-clash://install-config" in joined:
                self.ui_xml = (
                    "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
                    "<hierarchy><node text='New Profile' bounds='[158,178][875,246]'/>"
                    "<node text='Save' bounds='[928,191][1006,234]'/></hierarchy>"
                )
            elif "happ://add/" in joined:
                self.happ_sub_ready = True
            elif KOALA_ACTION_START in tokens:
                self._activate_koala_vpn()
            elif KOALA_ACTION_STOP in tokens and not self.koala_ignore_stop_intent:
                self._deactivate_vpn()
            elif "PerAppProxyActivity" in joined:
                self.ui_xml = HAPP_PER_APP_UI_INITIAL
            return _ok("Starting: Intent\nStatus: ok\nComplete\n")
        if tokens[:2] == ["input", "tap"] and len(tokens) == 4:
            x, y = int(tokens[2]), int(tokens[3])
            self.taps.append((x, y))
            if (x, y) == (967, 212):
                self.koala_service_xml = (
                    "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n"
                    "<map><string name=\"active_profile\">imported-uuid-1</string></map>\n"
                )
                self.ui_xml = "<hierarchy><node text='DISCONNECTED' bounds='[367,1243][713,1299]'/></hierarchy>"
            elif (x, y) == (561, 881):
                self.ui_xml = HAPP_PER_APP_UI_SEARCHED_UNCHECKED.replace(
                    'checked="false"', 'checked="true"'
                )
            return _ok()
        if tokens[:2] == ["input", "text"] and len(tokens) == 3:
            self.typed_texts.append(tokens[2])
            if tokens[2] == DEFAULT_EXCLUDE_PACKAGE:
                self.ui_xml = HAPP_PER_APP_UI_SEARCHED_UNCHECKED
            return _ok()
        return _fail(f"unexpected shell command: {cmd_str}")


class PrepareAndStatusTests(unittest.TestCase):
    def test_normalize_client_aliases_and_rejects_unknown(self) -> None:
        self.assertEqual(normalize_vpn_client("koala"), "koala")
        self.assertEqual(normalize_vpn_client("Koala-Clash"), "koala")
        self.assertEqual(normalize_vpn_client("happ"), "happ")
        with self.assertRaises(StandError):
            normalize_vpn_client("wireguard")

    def test_parse_and_render_koala_service_xml_preserves_extra_entries(self) -> None:
        xml_text = """<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="active_profile">eb86e44a-3faa-4703-b786-79881ca4268c</string>
    <string name="access_control_mode">DenySelected</string>
    <boolean name="bypass_private_network" value="true" />
    <set name="access_control_packages">
        <string>ru.rumap.android.debug</string>
    </set>
</map>
"""
        parsed = parse_koala_service_xml(xml_text)
        self.assertEqual(parsed.active_profile, "eb86e44a-3faa-4703-b786-79881ca4268c")
        self.assertEqual(parsed.access_control_mode, "DenySelected")
        self.assertEqual(parsed.access_control_packages, (DEFAULT_EXCLUDE_PACKAGE,))
        rerendered = parse_koala_service_xml(render_koala_service_xml(parsed))
        self.assertEqual(rerendered, parsed)

    def test_parse_appops_and_vpn_uids(self) -> None:
        self.assertEqual(
            parse_appops_activate_vpn("Uid mode: CAMERA: foreground\nACTIVATE_VPN: allow\n"),
            "allow",
        )
        self.assertEqual(
            extract_vpn_uid_ranges(CELL_WIFI_KOALA_VPN_DENY),
            "0-10191, 10193-10193, 10195-99999",
        )

    def test_prepare_koala_from_local_apk_sets_appops_and_permissions(self) -> None:
        fake = _FakeVpnEnv()
        mgr = VpnClientsManager(adb_bin="/fake/adb", runner=fake, device="127.0.0.1:15565")
        with tempfile.NamedTemporaryFile(prefix="koala-", suffix=".apk") as tmp:
            status = mgr.prepare("koala", apk=tmp.name)

        self.assertTrue(status.installed)
        self.assertEqual(status.package, KOALA_PACKAGE)
        self.assertEqual(status.activate_vpn, "allow")
        self.assertEqual(fake.local_installs, [tmp.name])
        self.assertIsNone(status.abi_warning)
        self.assertIn(
            "android.permission.POST_NOTIFICATIONS",
            fake.granted_permissions[KOALA_PACKAGE],
        )

    def test_prepare_happ_from_remote_hdd_cache_reports_abi_warning_on_x86_64(self) -> None:
        fake = _FakeVpnEnv()
        mgr = VpnClientsManager(
            adb_bin="/fake/adb",
            runner=fake,
            device="127.0.0.1:15565",
            ssh_host="home",
        )
        status = mgr.prepare("happ", apk="/mnt/data/android/apks/happ.apk")

        self.assertTrue(status.installed)
        self.assertEqual(status.package, HAPP_PACKAGE)
        self.assertEqual(status.activate_vpn, "allow")
        self.assertEqual(len(fake.ssh_scripts), 1)
        self.assertIn("arm64-v8a", status.abi_warning or "")

    def test_prepare_rejects_remote_apk_outside_hdd_root(self) -> None:
        fake = _FakeVpnEnv()
        mgr = VpnClientsManager(adb_bin="/fake/adb", runner=fake, device="127.0.0.1:15565")
        with self.assertRaises(HddInvariantError):
            mgr.prepare("koala", apk="/mnt/data/android/../escape.apk")

    def test_prepare_without_apk_fails_when_package_missing(self) -> None:
        fake = _FakeVpnEnv()
        mgr = VpnClientsManager(adb_bin="/fake/adb", runner=fake, device="127.0.0.1:15565")
        with self.assertRaises(StandError):
            mgr.prepare("koala")


class ImportSubscriptionTests(unittest.TestCase):
    def test_resolve_subscription_url_from_arg_or_env_and_rejects_missing(self) -> None:
        self.assertEqual(
            resolve_subscription_url("https://sub.example.test/abc", env={}),
            "https://sub.example.test/abc",
        )
        self.assertEqual(
            resolve_subscription_url(
                None, env={SUBSCRIPTION_URL_ENV: "https://sub.example.test/from-env"}
            ),
            "https://sub.example.test/from-env",
        )
        for bad in (None, "", "ftp://example.test/sub", "not-a-url"):
            with self.subTest(bad=bad), self.assertRaises(StandError):
                resolve_subscription_url(bad, env={})

    def test_import_koala_opens_deeplink_taps_save_and_waits_for_active_profile(self) -> None:
        fake = _FakeVpnEnv(installed={KOALA_PACKAGE})
        fake.appops[KOALA_PACKAGE] = {"ACTIVATE_VPN": "allow"}
        mgr = VpnClientsManager(
            adb_bin="/fake/adb",
            runner=fake,
            device="127.0.0.1:15565",
            sleep_fn=lambda _s: None,
        )

        st = mgr.import_subscription("koala", url="https://sub.example.test/token-1", name="verify")

        self.assertEqual(st.active_profile, "imported-uuid-1")
        self.assertEqual(fake.taps, [(967, 212)])
        self.assertTrue(
            any(
                "koala-clash://install-config?url=https%3A%2F%2Fsub.example.test%2Ftoken-1&name=verify"
                in " ".join(intent)
                for intent in fake.started_intents
            )
        )

    def test_import_happ_uses_env_url_and_waits_for_mmkv_sub(self) -> None:
        fake = _FakeVpnEnv(installed={HAPP_PACKAGE})
        fake.appops[HAPP_PACKAGE] = {"ACTIVATE_VPN": "allow"}
        mgr = VpnClientsManager(
            adb_bin="/fake/adb",
            runner=fake,
            device="127.0.0.1:15565",
            sleep_fn=lambda _s: None,
        )

        st = mgr.import_subscription(
            "happ",
            env={SUBSCRIPTION_URL_ENV: "https://sub.example.test/happ-tok"},
        )

        self.assertEqual(st.active_profile, "mmkv:SUB")
        self.assertTrue(
            any(
                "happ://add/https://sub.example.test/happ-tok" in " ".join(intent)
                for intent in fake.started_intents
            )
        )

    def test_import_rejects_missing_url_before_touching_adb(self) -> None:
        fake = _FakeVpnEnv(installed={KOALA_PACKAGE})
        mgr = VpnClientsManager(adb_bin="/fake/adb", runner=fake, device="127.0.0.1:15565")
        with self.assertRaises(StandError):
            mgr.import_subscription("koala", url=None, env={})
        self.assertEqual(fake.calls, [])

    def test_import_subscription_redacts_secret_url_on_am_start_timeout(self) -> None:
        secret_url = "https://sub.example.test/very-secret-token?a=1&b=2"
        encoded_url = "https%3A%2F%2Fsub.example.test%2Fvery-secret-token%3Fa%3D1%26b%3D2"
        for client, pkg in (("koala", KOALA_PACKAGE), ("happ", HAPP_PACKAGE)):
            with self.subTest(client=client):
                fake = _FakeVpnEnv(installed={pkg})
                orig_run = fake.run

                def run_with_timeout(args: list[str], **kwargs: Any) -> CommandResult:
                    if "shell" in args and "am start" in args[-1]:
                        raise StandTimeoutError(f"Команда превысила таймаут 30s: {' '.join(args)}")
                    return orig_run(args, **kwargs)

                fake.run = run_with_timeout  # type: ignore[method-assign]
                mgr = VpnClientsManager(
                    adb_bin="/fake/adb",
                    runner=fake,
                    device="127.0.0.1:15565",
                    sleep_fn=lambda _s: None,
                )
                with self.assertRaises(StandTimeoutError) as ctx:
                    mgr.import_subscription(client, url=secret_url)
                message = str(ctx.exception)
                self.assertIn("[subscription]", message)
                self.assertNotIn(secret_url, message)
                self.assertNotIn(encoded_url, message)
                self.assertIsNone(ctx.exception.__cause__)

    def test_import_koala_ignores_empty_ui_xml_after_save_tap(self) -> None:
        existing_xml = (
            "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n"
            "<map><string name=\"active_profile\">existing-uuid</string></map>\n"
        )
        fake = _FakeVpnEnv(installed={KOALA_PACKAGE}, koala_service_xml=existing_xml)
        save_ui = (
            "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
            "<hierarchy><node text='Save' bounds='[928,191][1006,234]'/></hierarchy>"
        )
        done_ui = "<hierarchy><node text='DISCONNECTED' bounds='[0,0][100,100]'/></hierarchy>"
        ui_sequence = iter([save_ui, "", "not-xml", done_ui])
        dumps = 0
        orig_run = fake.run

        def run_scripted(args: list[str], **kwargs: Any) -> CommandResult:
            nonlocal dumps
            if "shell" in args and "uiautomator dump" in args[-1]:
                dumps += 1
                return _ok(next(ui_sequence, done_ui))
            if "shell" in args and "input tap" in args[-1]:
                fake.taps.append((967, 212))
                return _ok()
            return orig_run(args, **kwargs)

        fake.run = run_scripted  # type: ignore[method-assign]
        now = [0.0]
        mgr = VpnClientsManager(
            adb_bin="/fake/adb",
            runner=fake,
            device="127.0.0.1:15565",
            sleep_fn=lambda s: now.__setitem__(0, now[0] + s),
            monotonic_fn=lambda: now[0],
        )

        st = mgr.import_subscription("koala", url="https://sub.example.test/reimport")
        self.assertEqual(st.active_profile, "existing-uuid")
        self.assertEqual(dumps, 4)
        self.assertEqual(fake.taps, [(967, 212)])

    def test_import_koala_retaps_save_while_button_remains_in_tree(self) -> None:
        fake = _FakeVpnEnv(installed={KOALA_PACKAGE})
        orig_run = fake.run
        tap_count = 0

        def swallow_first_save_tap(args: list[str], **kwargs: Any) -> CommandResult:
            nonlocal tap_count
            if "shell" in args and "input tap" in args[-1]:
                tap_count += 1
                if tap_count == 1:
                    fake.taps.append((967, 212))
                    return _ok()
            return orig_run(args, **kwargs)

        fake.run = swallow_first_save_tap  # type: ignore[method-assign]
        now = [0.0]
        mgr = VpnClientsManager(
            adb_bin="/fake/adb",
            runner=fake,
            device="127.0.0.1:15565",
            sleep_fn=lambda s: now.__setitem__(0, now[0] + s),
            monotonic_fn=lambda: now[0],
        )

        st = mgr.import_subscription("koala", url="https://sub.example.test/retap")
        self.assertEqual(st.active_profile, "imported-uuid-1")
        self.assertEqual(len(fake.taps), 2)
        self.assertGreaterEqual(now[0], 6.0)


class SetExcludeAndConnectTests(unittest.TestCase):
    def test_normalize_exclude_packages_defaults_dedupes_and_validates(self) -> None:
        self.assertEqual(normalize_exclude_packages(None), (DEFAULT_EXCLUDE_PACKAGE,))
        self.assertEqual(normalize_exclude_packages(None, clear=True), ())
        self.assertEqual(
            normalize_exclude_packages(["ru.rumap.android.debug, com.example.app", "com.example.app"]),
            ("ru.rumap.android.debug", "com.example.app"),
        )
        with self.assertRaises(StandError):
            normalize_exclude_packages(["ru.rumap.android.debug"], clear=True)
        with self.assertRaises(StandError):
            normalize_exclude_packages(["bad;pkg"])

    def test_koala_connect_set_exclude_while_connected_and_disconnect(self) -> None:
        initial_xml = (
            "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n"
            "<map><string name=\"active_profile\">prof-1</string></map>\n"
        )
        fake = _FakeVpnEnv(installed={KOALA_PACKAGE}, koala_service_xml=initial_xml)
        fake.appops[KOALA_PACKAGE] = {"ACTIVATE_VPN": "allow"}
        mgr = VpnClientsManager(
            adb_bin="/fake/adb",
            runner=fake,
            device="127.0.0.1:15565",
            sleep_fn=lambda _s: None,
        )

        connected_all = mgr.connect("koala")
        self.assertTrue(connected_all.vpn_active)
        self.assertEqual(connected_all.vpn_interface, "tun0")
        self.assertEqual(connected_all.exclude_mode, "AcceptAll")
        self.assertEqual(connected_all.vpn_uids, "0-10191, 10193-99999")

        excluded = mgr.set_exclude("koala")
        self.assertTrue(excluded.vpn_active)
        self.assertEqual(excluded.exclude_mode, "DenySelected")
        self.assertEqual(excluded.excluded_packages, (DEFAULT_EXCLUDE_PACKAGE,))
        self.assertEqual(excluded.active_profile, "prof-1")
        self.assertEqual(excluded.vpn_uids, "0-10191, 10193-10193, 10195-99999")

        cleared = mgr.set_exclude("koala", clear=True)
        self.assertTrue(cleared.vpn_active)
        self.assertEqual(cleared.exclude_mode, "AcceptAll")
        self.assertEqual(cleared.excluded_packages, ())

        disconnected = mgr.disconnect("koala")
        self.assertFalse(disconnected.vpn_active)
        self.assertIsNone(disconnected.vpn_interface)

    def test_koala_disconnect_falls_back_to_force_stop_when_stop_intent_ignored(self) -> None:
        initial_xml = (
            "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n"
            "<map><string name=\"active_profile\">prof-1</string></map>\n"
        )
        fake = _FakeVpnEnv(
            installed={KOALA_PACKAGE},
            connectivity=CELL_WIFI_KOALA_VPN_ALL,
            koala_service_xml=initial_xml,
            koala_ignore_stop_intent=True,
        )
        mgr = VpnClientsManager(
            adb_bin="/fake/adb",
            runner=fake,
            device="127.0.0.1:15565",
            sleep_fn=lambda _s: None,
        )
        st = mgr.disconnect("koala")
        self.assertFalse(st.vpn_active)
        self.assertIn(KOALA_PACKAGE, fake.force_stops)

    def test_happ_set_exclude_bypass_and_clear_via_per_app_proxy_ui(self) -> None:
        fake = _FakeVpnEnv(installed={HAPP_PACKAGE})
        fake.appops[HAPP_PACKAGE] = {"ACTIVATE_VPN": "allow"}
        mgr = VpnClientsManager(
            adb_bin="/fake/adb",
            runner=fake,
            device="127.0.0.1:15565",
            sleep_fn=lambda _s: None,
        )

        bypass = mgr.set_exclude("happ", packages=[DEFAULT_EXCLUDE_PACKAGE])
        self.assertEqual(bypass.exclude_mode, "bypass")
        self.assertEqual(bypass.excluded_packages, (DEFAULT_EXCLUDE_PACKAGE,))
        self.assertIn(DEFAULT_EXCLUDE_PACKAGE, fake.typed_texts)

        cleared = mgr.set_exclude("happ", clear=True)
        self.assertEqual(cleared.exclude_mode, "off")
        self.assertEqual(cleared.excluded_packages, ())

    def test_happ_connect_detects_sigill_stops_crash_loop_and_raises_clear_error(self) -> None:
        fake = _FakeVpnEnv(installed={HAPP_PACKAGE}, happ_sigill_on_connect=True)
        mgr = VpnClientsManager(
            adb_bin="/fake/adb",
            runner=fake,
            device="127.0.0.1:15565",
            sleep_fn=lambda _s: None,
        )

        with self.assertRaises(StandError) as ctx:
            mgr.connect("happ", timeout_s=5.0)
        self.assertIn("SIGILL", str(ctx.exception))
        self.assertIn("libndk_translation", str(ctx.exception))
        self.assertIn(HAPP_PACKAGE, fake.force_stops)

    def test_detect_happ_sigill_helper(self) -> None:
        self.assertIsNone(detect_happ_sigill("I/ActivityManager: Displayed MainActivity"))
        diag = detect_happ_sigill("F/libc: Fatal signal 4 (SIGILL) in su.happ.proxyutility:XRayDaemon")
        self.assertIsNotNone(diag)


class VpnCliIntegrationTests(unittest.TestCase):
    def _run(self, fake: _FakeVpnEnv, argv: list[str], *, extra_env: dict[str, str] | None = None):
        out = io.StringIO()
        err = io.StringIO()
        env_patch = dict(os.environ)
        env_patch.pop(SUBSCRIPTION_URL_ENV, None)
        if extra_env:
            env_patch.update(extra_env)
        with (
            mock.patch("app_verify.vpn_clients.SubprocessRunner", return_value=fake),
            mock.patch.dict(os.environ, env_patch, clear=True),
        ):
            rc = run_cli(["--adb-bin", "/fake/adb", *argv], stdout=out, stderr=err)
        return rc, out.getvalue(), err.getvalue()

    def test_cli_full_koala_flow_json_and_device(self) -> None:
        fake = _FakeVpnEnv()
        rc, out, _ = self._run(
            fake,
            [
                "--json",
                "vpn",
                "prepare",
                "koala",
                "--apk",
                "/mnt/data/android/apks/koala-clash.apk",
                "--device",
                "127.0.0.1:19999",
            ],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["activate_vpn"], "allow")
        self.assertIn("127.0.0.1:19999", fake.devices)

        with mock.patch("app_verify.vpn_clients.time.sleep", return_value=None):
            rc, out, _ = self._run(
                fake,
                ["vpn", "import-subscription", "koala", "--json"],
                extra_env={SUBSCRIPTION_URL_ENV: "https://sub.example.test/cli-sub"},
            )
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["active_profile"], "imported-uuid-1")

            rc, out, _ = self._run(
                fake,
                ["--json", "vpn", "set-exclude", "koala", "--package", DEFAULT_EXCLUDE_PACKAGE],
            )
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["exclude_mode"], "DenySelected")

            rc, out, _ = self._run(fake, ["vpn", "connect", "koala", "--json"])
            self.assertEqual(rc, 0)
            payload = json.loads(out)
            self.assertTrue(payload["vpn_active"])
            self.assertEqual(payload["vpn_interface"], "tun0")

            rc, out, _ = self._run(fake, ["vpn", "status", "koala"])
            self.assertEqual(rc, 0)
            self.assertIn("vpn_active: True", out)

            rc, out, _ = self._run(fake, ["--json", "vpn", "disconnect", "koala"])
            self.assertEqual(rc, 0)
            self.assertFalse(json.loads(out)["vpn_active"])

    def test_cli_rejects_non_android_and_missing_subscription_url(self) -> None:
        fake = _FakeVpnEnv(installed={KOALA_PACKAGE})
        rc, out, _ = self._run(fake, ["--platform", "web", "--json", "vpn", "status", "koala"])
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out)["ok"])

        rc, out, _ = self._run(fake, ["--json", "vpn", "import-subscription", "koala"])
        self.assertEqual(rc, 1)
        self.assertIn(SUBSCRIPTION_URL_ENV, json.loads(out)["error"])

    def test_happ_package_row_ignores_checkbox_from_sibling_row(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node resource-id="su.happ.proxyutility:id/rv_list_applications" bounds="[0,0][1080,1920]">
    <node resource-id="su.happ.proxyutility:id/cl_app" bounds="[0,100][1080,200]">
      <node resource-id="su.happ.proxyutility:id/package_name" text="ru.rumap.android" bounds="[20,120][700,180]" />
      <node resource-id="su.happ.proxyutility:id/check_box" checked="true" bounds="[900,120][1040,180]" />
    </node>
    <node resource-id="su.happ.proxyutility:id/cl_app" bounds="[0,200][1080,300]">
      <node resource-id="su.happ.proxyutility:id/package_name" text="ru.rumap.android.debug" bounds="[20,220][700,280]" />
      <node resource-id="su.happ.proxyutility:id/check_box" checked="false" bounds="[900,220][1040,280]" />
    </node>
  </node>
</hierarchy>"""
        from app_verify.vpn_clients import find_happ_package_row, resolve_subscription_url

        row = find_happ_package_row(xml, "ru.rumap.android.debug")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row[0], (540, 250))
        self.assertFalse(row[1])
        with self.assertRaises(StandError):
            resolve_subscription_url("https://sub.example.test/with space")

    def test_happ_connect_is_idempotent_when_vpn_already_active(self) -> None:
        fake = _FakeVpnEnv(
            installed={HAPP_PACKAGE},
            connectivity=(
                "NetworkAgentInfo{network{102} nc{[ Transports: VPN Capabilities: INTERNET ]} "
                "InterfaceName: tun0 Uids: <{0-99999}>}\n"
            ),
        )
        fake.tun0_up = True
        st = VpnClientsManager(adb_bin="/fake/adb", runner=fake, sleep_fn=lambda _: None).connect("happ")
        self.assertTrue(st.vpn_active)
        self.assertEqual(st.vpn_interface, "tun0")
        self.assertFalse(any("widget.click" in " ".join(c) for c in fake.calls))

    def test_happ_disconnect_ignores_foreign_koala_tun0_and_skips_widget_broadcast(self) -> None:
        fake = _FakeVpnEnv(
            installed={KOALA_PACKAGE, HAPP_PACKAGE},
            connectivity=CELL_WIFI_KOALA_VPN_ALL,
        )
        mgr = VpnClientsManager(adb_bin="/fake/adb", runner=fake, sleep_fn=lambda _: None)

        st = mgr.disconnect("happ", timeout_s=2.0)

        self.assertFalse(st.vpn_active)
        self.assertIsNone(st.vpn_interface)
        self.assertEqual(fake.broadcasts, [])
        self.assertTrue(fake.tun0_up)
        self.assertEqual(parse_connectivity(fake.connectivity).vpn_owner_uid, 10192)

    def test_happ_connect_does_not_treat_koala_tun0_as_already_connected(self) -> None:
        fake = _FakeVpnEnv(
            installed={KOALA_PACKAGE, HAPP_PACKAGE},
            connectivity=CELL_WIFI_KOALA_VPN_ALL,
            happ_sigill_on_connect=False,
        )
        mgr = VpnClientsManager(adb_bin="/fake/adb", runner=fake, sleep_fn=lambda _: None)

        st = mgr.connect("happ", timeout_s=5.0)

        self.assertTrue(st.vpn_active)
        self.assertEqual(st.vpn_interface, "tun0")
        self.assertEqual(len(fake.broadcasts), 1)
        self.assertEqual(parse_connectivity(fake.connectivity).vpn_owner_uid, 10193)

    def test_koala_disconnect_ignores_foreign_happ_tun0(self) -> None:
        fake = _FakeVpnEnv(
            installed={KOALA_PACKAGE, HAPP_PACKAGE},
            connectivity=CELL_WIFI_HAPP_VPN_ALL,
        )
        mgr = VpnClientsManager(adb_bin="/fake/adb", runner=fake, sleep_fn=lambda _: None)

        st = mgr.disconnect("koala", timeout_s=2.0)

        self.assertFalse(st.vpn_active)
        self.assertIsNone(st.vpn_interface)
        self.assertEqual(fake.started_intents, [])
        self.assertTrue(fake.tun0_up)
        self.assertEqual(parse_connectivity(fake.connectivity).vpn_owner_uid, 10193)


if __name__ == "__main__":
    unittest.main()
