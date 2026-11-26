#!/usr/bin/env python3
"""Unit-тесты программной эмуляции БС на Android-стенде (без реального adb/эмулятора)."""

from __future__ import annotations

import io
import json
import shlex
import unittest
from typing import Any
from unittest import mock

from app_verify.android import CommandResult
from app_verify.cli import run_cli
from app_verify.core import StandError
from app_verify.net_emulation import (
    CHAIN_NAME,
    DEFAULT_CONTROL_IPS,
    DEFAULT_WHITELISTED_IPS,
    NetEmulationClient,
    STAGING_CHAIN,
    allowed_ips,
    build_profile_rules,
    detect_profile,
    extract_allowed_ips,
    normalize_allow_ips,
    parse_connectivity,
    validate_cell_interface,
)

CELL_BLOCK = """  NetworkAgentInfo{network{100} lp{{...}} nc{[ Transports: CELLULAR Capabilities: INTERNET&NOT_RESTRICTED&TRUSTED&NOT_VPN&VALIDATED&NOT_ROAMING&FOREGROUND] LinkAddresses: [10.0.0.2/24] ]}
    InterfaceName: rmnet_data0
    LinkAddresses: [10.0.0.2/24]
  }
"""

WIFI_BLOCK = """  NetworkAgentInfo{network{101} lp{{...}} nc{[ Transports: WIFI Capabilities: INTERNET&TRUSTED&NOT_VPN&VALIDATED&FOREGROUND ]}
    InterfaceName: wlan0
    LinkAddresses: [192.168.1.20/24]
  }
"""

VPN_BLOCK = """  NetworkAgentInfo{network{102} lp{{...}} nc{[ Transports: VPN Capabilities: INTERNET&TRUSTED&VALIDATED&FOREGROUND ]}
    InterfaceName: tun0
    UnderlyingNetworks: [100, 101]
  }
"""


def _connectivity(*blocks: str) -> str:
    return "Active default network: 100\nCurrent Networks:\n" + "".join(blocks)


CELL_ONLY = _connectivity(CELL_BLOCK)
CELL_WIFI_VPN = _connectivity(CELL_BLOCK, WIFI_BLOCK, VPN_BLOCK)


def _ok(text: str = "") -> CommandResult:
    return CommandResult(returncode=0, stdout=text.encode(), stderr=b"")


def _fail(message: str = "error") -> CommandResult:
    return CommandResult(returncode=1, stdout=b"", stderr=message.encode())


def _format_iptables_s_rule(rule: list[str]) -> str:
    """Форматирует правило в каноническом виде реального `iptables -S` на Android."""
    formatted: list[str] = []
    idx = 0
    while idx < len(rule):
        token = rule[idx]
        if token == "-d" and idx + 1 < len(rule):
            dest = rule[idx + 1]
            if "/" not in dest:
                dest = f"{dest}/32"
            formatted.extend(["-d", dest])
            idx += 2
            continue
        if token == "-p" and idx + 1 < len(rule):
            proto = rule[idx + 1]
            formatted.extend(["-p", proto, "-m", proto])
            idx += 2
            continue
        if token == "--icmp-type" and idx + 1 < len(rule):
            icmp_type = "8" if rule[idx + 1] == "echo-request" else rule[idx + 1]
            formatted.extend(["--icmp-type", icmp_type])
            idx += 2
            continue
        formatted.append(token)
        idx += 1
    return " ".join(formatted)


def _rule_lines(rules: list[list[str]]) -> list[str]:
    return [f"-A {CHAIN_NAME} " + _format_iptables_s_rule(rule) for rule in rules]


class _FakeAdb:
    """Мини-симулятор `adb` и `iptables` с состоянием цепочки и правил OUTPUT."""

    def __init__(
        self,
        *,
        connectivity: str = CELL_ONLY,
        wifi_on: bool = True,
        uid: str = "0",
        chain_rules: list[list[str]] | None = None,
        output_rules: list[list[str]] | None = None,
    ) -> None:
        self.connectivity = connectivity
        self.wifi_on = wifi_on
        self.uid = uid
        self.chains: dict[str, list[list[str]]] = {}
        if chain_rules is not None:
            self.chains[CHAIN_NAME] = [list(rule) for rule in chain_rules]
        self.output_rules: list[list[str]] = [list(rule) for rule in output_rules or []]
        self.calls: list[list[str]] = []
        self.iptables_calls: list[list[str]] = []
        self.svc_calls: list[str] = []
        self.devices: list[str | None] = []
        self.effective_history: list[list[list[list[str]]]] = []

    def run(self, args, *, timeout=None, input_bytes=None, env=None) -> CommandResult:
        self.calls.append(list(args))
        rest = list(args[1:])
        device: str | None = None
        if rest[:1] == ["-s"]:
            device = rest[1]
            rest = rest[2:]
        self.devices.append(device)

        if rest == ["root"]:
            return _ok("restarting adbd as root\n")
        if rest == ["wait-for-device"]:
            return _ok()
        if not rest or rest[0] != "shell":
            return _fail(f"unexpected adb invocation: {rest}")

        remote = shlex.split(rest[1]) if len(rest) > 1 else []
        if remote == ["dumpsys", "connectivity"]:
            return _ok(self.connectivity)
        if remote == ["settings", "get", "global", "wifi_on"]:
            return _ok("1" if self.wifi_on else "0")
        if remote == ["id", "-u"]:
            return _ok(self.uid)
        if remote[:2] == ["svc", "wifi"]:
            action = remote[2]
            self.svc_calls.append(action)
            self.wifi_on = action == "enable"
            return _ok()
        if remote and remote[0] == "iptables":
            return self._iptables(remote[1:])
        return _fail(f"unexpected shell invocation: {remote}")

    def _iptables(self, argv: list[str]) -> CommandResult:
        self.iptables_calls.append(list(argv))
        if argv[:1] == ["-w"]:
            if len(argv) < 2:
                return _fail("iptables: option -w requires an argument")
            argv = argv[2:] if argv[1].isdigit() else argv[1:]
        result = self._iptables_command(argv)
        if argv[:1] != ["-S"]:
            self.effective_history.append(self.effective_cell_rules())
        return result

    def effective_cell_rules(self) -> list[list[list[str]]]:
        """Цепочки, которые реально фильтруют трафик через OUTPUT, в порядке прыжков."""
        return [
            [list(rule) for rule in self.chains[jump[-1]]]
            for jump in self.output_rules
            if jump[-1] in self.chains
        ]

    def _iptables_command(self, argv: list[str]) -> CommandResult:

        if argv[:1] == ["-S"]:
            target = argv[1]
            if target == "OUTPUT":
                lines = ["-P OUTPUT ACCEPT"]
                lines.extend("-A OUTPUT " + " ".join(rule) for rule in self.output_rules)
                return _ok("\n".join(lines) + "\n")
            chain = self.chains.get(target)
            if chain is None:
                return _fail("iptables: No chain/target/match by that name.")
            lines = [f"-N {target}"]
            lines.extend(f"-A {target} " + _format_iptables_s_rule(rule) for rule in chain)
            return _ok("\n".join(lines) + "\n")

        if argv[:1] == ["-N"]:
            name = argv[1]
            if name in self.chains:
                return _fail("iptables: Chain already exists.")
            self.chains[name] = []
            return _ok()

        if argv[:1] == ["-A"]:
            name = argv[1]
            if name not in self.chains:
                return _fail("iptables: No chain/target/match by that name.")
            self.chains[name].append(list(argv[2:]))
            return _ok()

        if argv[:1] == ["-I"]:
            if argv[1] != "OUTPUT" or argv[2] != "1":
                return _fail("unsupported insert")
            if argv[-1] not in self.chains:
                return _fail("iptables: No chain/target/match by that name.")
            self.output_rules.insert(0, list(argv[3:]))
            return _ok()

        if argv[:1] == ["-R"]:
            if argv[1] != "OUTPUT":
                return _fail("unsupported replace")
            index = int(argv[2]) - 1
            if not 0 <= index < len(self.output_rules):
                return _fail("iptables: Index of replacement too big.")
            if argv[-1] not in self.chains:
                return _fail("iptables: No chain/target/match by that name.")
            self.output_rules[index] = list(argv[3:])
            return _ok()

        if argv[:1] == ["-D"]:
            target = argv[1]
            rule = list(argv[2:])
            if target == "OUTPUT":
                try:
                    self.output_rules.remove(rule)
                except ValueError:
                    return _fail("iptables: No chain/target/match by that name.")
                return _ok()
            chain = self.chains.get(target)
            if chain is None or rule not in chain:
                return _fail("iptables: No chain/target/match by that name.")
            chain.remove(rule)
            return _ok()

        if argv[:1] == ["-F"]:
            chain = self.chains.get(argv[1])
            if chain is None:
                return _fail("iptables: No chain/target/match by that name.")
            chain.clear()
            return _ok()

        if argv[:1] == ["-X"]:
            name = argv[1]
            if name not in self.chains:
                return _fail("iptables: No chain/target/match by that name.")
            if self.chains[name]:
                return _fail("iptables: Directory not empty.")
            if any(rule[-1] == name for rule in self.output_rules):
                return _fail("iptables: Too many links.")
            del self.chains[name]
            return _ok()

        return _fail(f"unexpected iptables invocation: {argv}")

    def jumps_to_chain(self) -> list[list[str]]:
        return [rule for rule in self.output_rules if rule[-1] == CHAIN_NAME]


def _client(fake: _FakeAdb, **kwargs: Any) -> NetEmulationClient:
    return NetEmulationClient(
        adb_bin="/fake/adb",
        runner=fake,
        device=kwargs.pop("device", "127.0.0.1:15565"),
        **kwargs,
    )


class ParseConnectivityTests(unittest.TestCase):
    def test_api34_detects_cellular_wifi_and_vpn(self) -> None:
        snapshot = parse_connectivity(CELL_WIFI_VPN)

        self.assertEqual(snapshot.cell_interface, "rmnet_data0")
        self.assertTrue(snapshot.cellular_connected)
        self.assertTrue(snapshot.wifi_connected)
        self.assertEqual(snapshot.wifi_interface, "wlan0")
        self.assertTrue(snapshot.vpn_active)
        self.assertEqual(snapshot.vpn_interface, "tun0")

    def test_without_wifi_and_vpn_reports_only_cellular(self) -> None:
        snapshot = parse_connectivity(CELL_ONLY)

        self.assertEqual(snapshot.cell_interface, "rmnet_data0")
        self.assertTrue(snapshot.cellular_connected)
        self.assertFalse(snapshot.wifi_connected)
        self.assertIsNone(snapshot.wifi_interface)
        self.assertFalse(snapshot.vpn_active)
        self.assertIsNone(snapshot.vpn_interface)

    def test_not_vpn_capability_does_not_trigger_vpn(self) -> None:
        snapshot = parse_connectivity(CELL_ONLY)
        self.assertFalse(snapshot.vpn_active)

    def test_vpn_without_interface_name_falls_back_to_tun_regex(self) -> None:
        block = """  NetworkAgentInfo{network{102} nc{[ Transports: VPN Capabilities: INTERNET ]}
    UnderlyingNetworks: [100]
    VpnTransportInfo iface: tun0
"""
        snapshot = parse_connectivity(_connectivity(CELL_BLOCK, block))
        self.assertTrue(snapshot.vpn_active)
        self.assertEqual(snapshot.vpn_interface, "tun0")

    def test_historical_tun0_in_event_log_does_not_trigger_vpn(self) -> None:
        text = (
            CELL_ONLY
            + "\nEvents:\n"
            + "  2026-11-26T08:00:00.000 - NetworkAgentInfo[VPN] interface tun0 disconnected\n"
        )
        snapshot = parse_connectivity(text)
        self.assertFalse(snapshot.vpn_active)
        self.assertIsNone(snapshot.vpn_interface)

    def test_mobile_type_without_transports_still_detected(self) -> None:
        block = """  NetworkAgentInfo{network{100} nc{[ type: MOBILE[LTE] Capabilities: INTERNET ]}
    InterfaceName: radio0
"""
        snapshot = parse_connectivity(_connectivity(block))
        self.assertTrue(snapshot.cellular_connected)
        self.assertEqual(snapshot.cell_interface, "radio0")

    def test_parse_connectivity_extracts_vpn_owner_uid(self) -> None:
        vpn_with_uid = (
            "  NetworkAgentInfo{network{103} lp{{InterfaceName: tun0}} "
            "nc{[ Transports: VPN Capabilities: INTERNET OwnerUid: 10192 ]}}\n"
        )
        self.assertEqual(parse_connectivity(_connectivity(CELL_BLOCK, vpn_with_uid)).vpn_owner_uid, 10192)
        self.assertIsNone(parse_connectivity(CELL_WIFI_VPN).vpn_owner_uid)
        vpn_invalid_uid = (
            "  NetworkAgentInfo{network{103} lp{{InterfaceName: tun0}} "
            "nc{[ Transports: VPN Capabilities: INTERNET OwnerUid: -1 ]}}\n"
        )
        self.assertIsNone(parse_connectivity(_connectivity(CELL_BLOCK, vpn_invalid_uid)).vpn_owner_uid)


class CellInterfaceValidationTests(unittest.TestCase):
    def test_rejects_loopback_wifi_and_vpn_interfaces(self) -> None:
        for name in ("lo", "wlan0", "tun0", "tun1", "wlan1"):
            with self.subTest(name=name), self.assertRaises(StandError):
                validate_cell_interface(name)

    def test_rejects_unsafe_interface_name(self) -> None:
        for bad in ("rmnet;reboot", "-j", "--help", "", None):
            with self.subTest(bad=bad), self.assertRaises(StandError):
                validate_cell_interface(bad)

    def test_accepts_known_cellular_interfaces(self) -> None:
        for name in ("rmnet_data0", "radio0", "eth0"):
            self.assertEqual(validate_cell_interface(name), name)


class RuleGenerationTests(unittest.TestCase):
    def test_soft_whitelists_tcp443_only_for_whitelisted_and_allow_ips(self) -> None:
        rules = build_profile_rules("soft", ["1.2.3.4", "5.6.7.8"])

        expected = [
            ["-d", "127.0.0.0/8", "-j", "RETURN"],
            ["-d", "10.0.2.0/24", "-j", "RETURN"],
        ]
        expected.extend(
            ["-p", "tcp", "--dport", "443", "-d", ip, "-j", "RETURN"]
            for ip in (*DEFAULT_WHITELISTED_IPS, "1.2.3.4", "5.6.7.8")
        )
        expected.append(["-p", "tcp", "--dport", "443", "-j", "DROP"])
        self.assertEqual(rules, expected)

    def test_soft_drops_control_ips_by_omission(self) -> None:
        rendered = " ".join(" ".join(rule) for rule in build_profile_rules("soft"))
        for control in DEFAULT_CONTROL_IPS:
            self.assertNotIn(control, rendered)

    def test_hard_drops_tcp443_and_limits_icmp_echo_to_whitelist(self) -> None:
        rules = build_profile_rules("hard", ["1.2.3.4"])

        expected = [
            ["-d", "127.0.0.0/8", "-j", "RETURN"],
            ["-d", "10.0.2.0/24", "-j", "RETURN"],
            ["-p", "tcp", "--dport", "443", "-j", "DROP"],
        ]
        expected.extend(
            ["-p", "icmp", "--icmp-type", "echo-request", "-d", ip, "-j", "RETURN"]
            for ip in (*DEFAULT_WHITELISTED_IPS, "1.2.3.4")
        )
        expected.append(["-p", "icmp", "--icmp-type", "echo-request", "-j", "DROP"])
        self.assertEqual(rules, expected)

    def test_down_drops_tcp443_and_icmp_echo(self) -> None:
        self.assertEqual(
            build_profile_rules("down"),
            [
                ["-d", "127.0.0.0/8", "-j", "RETURN"],
                ["-d", "10.0.2.0/24", "-j", "RETURN"],
                ["-p", "tcp", "--dport", "443", "-j", "DROP"],
                ["-p", "icmp", "--icmp-type", "echo-request", "-j", "DROP"],
            ],
        )

    def test_off_builds_no_rules(self) -> None:
        self.assertEqual(build_profile_rules("off", ["1.2.3.4"]), [])

    def test_allow_ip_dedupes_and_supports_comma_lists(self) -> None:
        self.assertEqual(
            normalize_allow_ips(["1.2.3.4, 5.6.7.8", "1.2.3.4", "  ", "5.6.7.8"]),
            ("1.2.3.4", "5.6.7.8"),
        )
        merged = allowed_ips(["77.88.55.242", "9.9.9.9"])
        self.assertEqual(merged[: len(DEFAULT_WHITELISTED_IPS)], DEFAULT_WHITELISTED_IPS)
        self.assertEqual(merged[-1], "9.9.9.9")
        self.assertEqual(merged.count("77.88.55.242"), 1)

    def test_invalid_ipv4_and_profile_are_rejected(self) -> None:
        for bad in ("300.1.1.1", "not-an-ip", "1.2.3"):
            with self.subTest(bad=bad), self.assertRaises(StandError):
                normalize_allow_ips([bad])
        with self.assertRaises(StandError):
            build_profile_rules("explode")


class ApplyProfileTests(unittest.TestCase):
    def test_soft_targets_cell_interface_and_creates_chain(self) -> None:
        fake = _FakeAdb()
        status = _client(fake).apply_profile("soft", allow_ips=["1.2.3.4"])

        self.assertEqual(status.profile, "soft")
        self.assertEqual(status.cell_interface, "rmnet_data0")
        self.assertEqual(fake.jumps_to_chain(), [["-o", "rmnet_data0", "-j", CHAIN_NAME]])
        self.assertEqual(
            fake.chains[CHAIN_NAME], build_profile_rules("soft", ["1.2.3.4"])
        )

    def test_jump_never_targets_loopback_wifi_or_adb(self) -> None:
        fake = _FakeAdb()
        _client(fake).apply_profile("hard")

        jumps = fake.jumps_to_chain()
        self.assertEqual(len(jumps), 1)
        self.assertIn("-o", jumps[0])
        interface = jumps[0][jumps[0].index("-o") + 1]
        self.assertEqual(interface, "rmnet_data0")
        for forbidden in ("lo", "wlan0", "tun0"):
            self.assertNotIn(forbidden, jumps[0])

    def test_switching_profiles_replaces_rules_without_duplicates(self) -> None:
        fake = _FakeAdb()
        client = _client(fake)

        client.apply_profile("soft", allow_ips=["1.2.3.4"])
        status = client.apply_profile("hard", allow_ips=["1.2.3.4"])

        self.assertEqual(status.profile, "hard")
        self.assertEqual(fake.jumps_to_chain(), [["-o", "rmnet_data0", "-j", CHAIN_NAME]])
        self.assertEqual(
            fake.chains[CHAIN_NAME], build_profile_rules("hard", ["1.2.3.4"])
        )

    def test_switching_profiles_never_leaves_cell_traffic_unfiltered(self) -> None:
        transitions = (("soft", "hard"), ("hard", "down"), ("down", "soft"), ("hard", "hard"))
        for old, new in transitions:
            with self.subTest(old=old, new=new):
                fake = _FakeAdb(
                    chain_rules=build_profile_rules(old),
                    output_rules=[["-j", "oem_out"], ["-o", "rmnet_data0", "-j", CHAIN_NAME]],
                )
                fake.chains["oem_out"] = []
                status = _client(fake).apply_profile(new)

                allowed = ([[], build_profile_rules(old)], [[], build_profile_rules(new)])
                for state in fake.effective_history:
                    self.assertIn(state, allowed)
                self.assertEqual(status.profile, new)
                self.assertEqual(
                    fake.output_rules,
                    [["-j", "oem_out"], ["-o", "rmnet_data0", "-j", CHAIN_NAME]],
                )
                self.assertEqual(fake.chains[CHAIN_NAME], build_profile_rules(new))
                self.assertNotIn(STAGING_CHAIN, fake.chains)

    def test_enabling_profile_from_off_applies_full_rules_in_one_step(self) -> None:
        fake = _FakeAdb()
        _client(fake).apply_profile("hard")

        states = [state for state in fake.effective_history if state]
        self.assertTrue(states)
        self.assertTrue(all(state == [build_profile_rules("hard")] for state in states))
        first = fake.effective_history.index(states[0])
        self.assertTrue(all(fake.effective_history[first:]))

    def test_leftover_staging_chain_is_replaced_and_removed(self) -> None:
        fake = _FakeAdb(
            chain_rules=build_profile_rules("soft"),
            output_rules=[
                ["-o", "rmnet_data0", "-j", STAGING_CHAIN],
                ["-o", "rmnet_data0", "-j", CHAIN_NAME],
            ],
        )
        fake.chains[STAGING_CHAIN] = [["-p", "tcp", "--dport", "80", "-j", "DROP"]]

        status = _client(fake).apply_profile("down")

        self.assertEqual(status.profile, "down")
        self.assertEqual(fake.output_rules, [["-o", "rmnet_data0", "-j", CHAIN_NAME]])
        self.assertNotIn(STAGING_CHAIN, fake.chains)

    def test_reapplying_same_profile_is_idempotent(self) -> None:
        fake = _FakeAdb()
        client = _client(fake)

        client.apply_profile("soft")
        first = [list(rule) for rule in fake.chains[CHAIN_NAME]]
        client.apply_profile("soft")

        self.assertEqual(fake.chains[CHAIN_NAME], first)
        self.assertEqual(len(fake.jumps_to_chain()), 1)

    def test_off_removes_chain_and_disables_wifi(self) -> None:
        fake = _FakeAdb(
            chain_rules=build_profile_rules("soft"),
            output_rules=[["-o", "rmnet_data0", "-j", CHAIN_NAME]],
        )

        status = _client(fake).apply_profile("off", wifi="off")

        self.assertEqual(status.profile, "off")
        self.assertNotIn(CHAIN_NAME, fake.chains)
        self.assertEqual(fake.output_rules, [])
        self.assertEqual(fake.svc_calls, ["disable"])
        self.assertFalse(status.wifi_enabled)

    def test_apply_requires_root(self) -> None:
        fake = _FakeAdb(uid="2000")

        with self.assertRaises(StandError):
            _client(fake).apply_profile("soft")
        self.assertEqual(fake.iptables_calls, [])

    def test_apply_rejects_non_cellular_detected_interface(self) -> None:
        for bad in ("lo", "wlan0", "tun0"):
            block = (
                "  NetworkAgentInfo{network{100} nc{[ Transports: CELLULAR ]}\n"
                f"    InterfaceName: {bad}\n"
            )
            fake = _FakeAdb(connectivity=_connectivity(block))
            with self.subTest(bad=bad), self.assertRaises(StandError):
                _client(fake).apply_profile("soft")
            self.assertEqual(fake.iptables_calls, [])

    def test_apply_without_cellular_network_is_rejected(self) -> None:
        fake = _FakeAdb(connectivity=_connectivity(WIFI_BLOCK))

        with self.assertRaises(StandError):
            _client(fake).apply_profile("hard")
        self.assertEqual(fake.iptables_calls, [])

    def test_invalid_allow_ip_is_rejected_before_adb(self) -> None:
        fake = _FakeAdb()

        with self.assertRaises(StandError):
            _client(fake).apply_profile("soft", allow_ips=["999.1.1.1"])
        self.assertEqual(fake.calls, [])

    def test_wifi_on_enables_wifi(self) -> None:
        fake = _FakeAdb(wifi_on=False)
        status = _client(fake).apply_profile("off", wifi="on")

        self.assertEqual(fake.svc_calls, ["enable"])
        self.assertTrue(status.wifi_enabled)


class StatusTests(unittest.TestCase):
    def test_detects_every_profile_from_actual_rules(self) -> None:
        cases = {
            "soft": build_profile_rules("soft"),
            "hard": build_profile_rules("hard"),
            "down": build_profile_rules("down"),
            "custom": [["-p", "tcp", "--dport", "80", "-j", "DROP"]],
        }
        for expected, rules in cases.items():
            fake = _FakeAdb(chain_rules=rules)
            with self.subTest(expected=expected):
                self.assertEqual(_client(fake).status().profile, expected)

        off_fake = _FakeAdb()
        self.assertEqual(_client(off_fake).status().profile, "off")

    def test_reports_wifi_cellular_vpn_and_allowed_ips(self) -> None:
        rules = build_profile_rules("soft", ["1.2.3.4"])
        fake = _FakeAdb(connectivity=CELL_WIFI_VPN, chain_rules=rules, wifi_on=True)

        status = _client(fake).status()

        self.assertEqual(status.profile, "soft")
        self.assertEqual(status.cell_interface, "rmnet_data0")
        self.assertTrue(status.wifi_enabled)
        self.assertTrue(status.wifi_connected)
        self.assertEqual(status.wifi_interface, "wlan0")
        self.assertTrue(status.cellular_connected)
        self.assertTrue(status.vpn_active)
        self.assertEqual(status.vpn_interface, "tun0")
        self.assertEqual(status.allowed_ips, (*DEFAULT_WHITELISTED_IPS, "1.2.3.4"))
        self.assertEqual(status.rules, tuple(_rule_lines(rules)))

    def test_reports_vpn_without_wifi(self) -> None:
        fake = _FakeAdb(
            connectivity=_connectivity(CELL_BLOCK, VPN_BLOCK),
            chain_rules=build_profile_rules("hard"),
            wifi_on=False,
        )
        status = _client(fake).status()

        self.assertEqual(status.profile, "hard")
        self.assertFalse(status.wifi_enabled)
        self.assertFalse(status.wifi_connected)
        self.assertIsNone(status.wifi_interface)
        self.assertTrue(status.vpn_active)
        self.assertEqual(status.vpn_interface, "tun0")

    def test_allowed_ips_ignore_safety_networks(self) -> None:
        rules = build_profile_rules("soft", ["1.2.3.4"])
        self.assertEqual(
            extract_allowed_ips(_rule_lines(rules)),
            (*DEFAULT_WHITELISTED_IPS, "1.2.3.4"),
        )

    def test_parses_real_android_iptables_s_output_for_all_profiles(self) -> None:
        real_soft = [
            "-N RUMAP_WL",
            "-A RUMAP_WL -d 127.0.0.0/8 -j RETURN",
            "-A RUMAP_WL -d 10.0.2.0/24 -j RETURN",
            "-A RUMAP_WL -d 77.88.55.242/32 -p tcp -m tcp --dport 443 -j RETURN",
            "-A RUMAP_WL -d 1.2.3.4/32 -p tcp -m tcp --dport 443 -j RETURN",
            "-A RUMAP_WL -p tcp -m tcp --dport 443 -j DROP",
        ]
        real_hard = [
            "-N RUMAP_WL",
            "-A RUMAP_WL -d 127.0.0.0/8 -j RETURN",
            "-A RUMAP_WL -d 10.0.2.0/24 -j RETURN",
            "-A RUMAP_WL -p tcp -m tcp --dport 443 -j DROP",
            "-A RUMAP_WL -d 77.88.55.242/32 -p icmp -m icmp --icmp-type 8 -j RETURN",
            "-A RUMAP_WL -p icmp -m icmp --icmp-type 8 -j DROP",
        ]
        real_down = [
            "-N RUMAP_WL",
            "-A RUMAP_WL -d 127.0.0.0/8 -j RETURN",
            "-A RUMAP_WL -d 10.0.2.0/24 -j RETURN",
            "-A RUMAP_WL -p tcp -m tcp --dport 443 -j DROP",
            "-A RUMAP_WL -p icmp -m icmp --icmp-type 8/0 -j DROP",
        ]
        self.assertEqual(detect_profile(real_soft), "soft")
        self.assertEqual(extract_allowed_ips(real_soft), ("77.88.55.242", "1.2.3.4"))
        self.assertEqual(detect_profile(real_hard), "hard")
        self.assertEqual(extract_allowed_ips(real_hard), ("77.88.55.242",))
        self.assertEqual(detect_profile(real_down), "down")
        self.assertEqual(extract_allowed_ips(real_down), ())

    def test_status_to_dict_contains_expected_keys(self) -> None:
        fake = _FakeAdb(connectivity=CELL_WIFI_VPN, chain_rules=build_profile_rules("soft"))
        payload = _client(fake).status().to_dict()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["profile"], "soft")
        self.assertEqual(payload["chain"], CHAIN_NAME)
        for key in (
            "allowed_ips",
            "cell_if",
            "wifi_enabled",
            "wifi_connected",
            "wifi_interface",
            "cellular_connected",
            "vpn_active",
            "vpn_interface",
            "rules",
        ):
            self.assertIn(key, payload)

    def test_status_reports_custom_for_leftover_staging_chain_or_output_jump(self) -> None:
        fake_staging = _FakeAdb()
        fake_staging.chains[STAGING_CHAIN] = [["-p", "tcp", "--dport", "443", "-j", "DROP"]]
        st_staging = _client(fake_staging).status()
        self.assertEqual(st_staging.profile, "custom")
        self.assertTrue(st_staging.rules)

        fake_jump = _FakeAdb(output_rules=[["-o", "rmnet_data0", "-j", CHAIN_NAME]])
        self.assertEqual(_client(fake_jump).status().profile, "custom")

        fake_mixed = _FakeAdb(
            chain_rules=build_profile_rules("soft"),
            output_rules=[
                ["-o", "rmnet_data0", "-j", CHAIN_NAME],
                ["-o", "rmnet_data0", "-j", STAGING_CHAIN],
            ],
        )
        self.assertEqual(_client(fake_mixed).status().profile, "custom")

    def test_status_raises_stand_error_on_xtables_lock_code_4(self) -> None:
        fake = _FakeAdb()
        orig_iptables = fake._iptables_command

        def lock_error(argv: list[str]) -> CommandResult:
            if argv == ["-S", CHAIN_NAME]:
                return CommandResult(
                    returncode=4,
                    stdout=b"",
                    stderr=b"Another app is currently holding the xtables lock.",
                )
            return orig_iptables(argv)

        fake._iptables_command = lock_error  # type: ignore[method-assign]
        with self.assertRaisesRegex(StandError, "код 4"):
            _client(fake).status()


class ResetTests(unittest.TestCase):
    def test_reset_removes_chain_and_enables_wifi(self) -> None:
        fake = _FakeAdb(
            chain_rules=build_profile_rules("hard"),
            output_rules=[["-o", "rmnet_data0", "-j", CHAIN_NAME]],
            wifi_on=False,
        )

        status = _client(fake).reset()

        self.assertEqual(status.profile, "off")
        self.assertNotIn(CHAIN_NAME, fake.chains)
        self.assertEqual(fake.output_rules, [])
        self.assertEqual(fake.svc_calls, ["enable"])
        self.assertTrue(status.wifi_enabled)

    def test_reset_is_idempotent_when_chain_is_absent(self) -> None:
        fake = _FakeAdb(wifi_on=False)

        status = _client(fake).reset()

        self.assertEqual(status.profile, "off")
        self.assertEqual(fake.svc_calls, ["enable"])

    def test_cleanup_removes_duplicate_jumps_and_preserves_other_output_rules(self) -> None:
        fake = _FakeAdb(
            chain_rules=build_profile_rules("soft"),
            output_rules=[
                ["-o", "rmnet_data0", "-j", CHAIN_NAME],
                ["-j", "oem_out"],
                ["-o", "radio0", "-j", CHAIN_NAME],
                ["-o", "rmnet_data0", "-j", f"{CHAIN_NAME}_OTHER"],
            ],
            wifi_on=False,
        )

        status = _client(fake).reset()

        self.assertEqual(status.profile, "off")
        self.assertEqual(
            fake.output_rules,
            [
                ["-j", "oem_out"],
                ["-o", "rmnet_data0", "-j", f"{CHAIN_NAME}_OTHER"],
            ],
        )
        self.assertTrue(all(call[:2] == ["-w", "5"] for call in fake.iptables_calls))

    def test_reset_raises_stand_error_when_chain_or_jump_survives_cleanup(self) -> None:
        fake = _FakeAdb(
            chain_rules=build_profile_rules("soft"),
            output_rules=[["-o", "rmnet_data0", "-j", CHAIN_NAME]],
            wifi_on=False,
        )
        orig_iptables = fake._iptables_command

        def block_delete(argv: list[str]) -> CommandResult:
            if argv[:2] == ["-D", "OUTPUT"] or argv[:1] == ["-X"]:
                return CommandResult(returncode=4, stdout=b"", stderr=b"xtables lock")
            return orig_iptables(argv)

        fake._iptables_command = block_delete  # type: ignore[method-assign]
        with self.assertRaisesRegex(StandError, "остались"):
            _client(fake).reset()

    def test_reset_raises_stand_error_when_post_reset_status_is_not_off(self) -> None:
        fake = _FakeAdb(wifi_on=False)
        client = _client(fake)
        reappeared_status = _client(
            _FakeAdb(chain_rules=build_profile_rules("soft"))
        ).status()
        with mock.patch.object(client, "_collect_status", return_value=reappeared_status):
            with self.assertRaisesRegex(StandError, "ожидался профиль 'off'"):
                client.reset()


class CliIntegrationTests(unittest.TestCase):
    def _run(self, fake: _FakeAdb, argv: list[str]):
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch(
            "app_verify.net_emulation.SubprocessRunner", return_value=fake
        ):
            rc = run_cli(
                ["--adb-bin", "/fake/adb", *argv],
                stdout=out,
                stderr=err,
            )
        return rc, out.getvalue(), err.getvalue()

    def test_profile_json_with_allow_ip_and_device(self) -> None:
        fake = _FakeAdb()
        rc, out, _err = self._run(
            fake,
            [
                "--json",
                "net",
                "profile",
                "soft",
                "--allow-ip",
                "1.2.3.4",
                "--allow-ip",
                "5.6.7.8",
                "--device",
                "127.0.0.1:9999",
            ],
        )

        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["profile"], "soft")
        self.assertEqual(
            payload["allowed_ips"], [*DEFAULT_WHITELISTED_IPS, "1.2.3.4", "5.6.7.8"]
        )
        self.assertIn("127.0.0.1:9999", fake.devices)
        self.assertEqual(fake.jumps_to_chain(), [["-o", "rmnet_data0", "-j", CHAIN_NAME]])

    def test_profile_subcommand_level_json_flag(self) -> None:
        fake = _FakeAdb()
        rc, out, _err = self._run(
            fake, ["net", "profile", "down", "--json"]
        )

        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["profile"], "down")

    def test_status_text_mode(self) -> None:
        fake = _FakeAdb(chain_rules=build_profile_rules("soft"))
        rc, out, err = self._run(fake, ["net", "status"])

        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertIn("profile: soft", out)
        self.assertIn("cell_if: rmnet_data0", out)

    def test_status_json_mode(self) -> None:
        fake = _FakeAdb(connectivity=CELL_WIFI_VPN, chain_rules=build_profile_rules("hard"))
        rc, out, _err = self._run(fake, ["net", "status", "--json"])

        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["profile"], "hard")
        self.assertTrue(payload["wifi_connected"])
        self.assertTrue(payload["vpn_active"])
        self.assertEqual(payload["vpn_interface"], "tun0")

    def test_reset_json_mode(self) -> None:
        fake = _FakeAdb(
            chain_rules=build_profile_rules("down"),
            output_rules=[["-o", "rmnet_data0", "-j", CHAIN_NAME]],
            wifi_on=False,
        )
        rc, out, _err = self._run(fake, ["--json", "net", "reset"])

        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["profile"], "off")
        self.assertTrue(payload["wifi_enabled"])
        self.assertEqual(fake.svc_calls, ["enable"])

    def test_invalid_allow_ip_returns_json_error(self) -> None:
        fake = _FakeAdb()
        rc, out, _err = self._run(
            fake, ["--json", "net", "profile", "soft", "--allow-ip", "300.1.1.1"]
        )

        self.assertEqual(rc, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("300.1.1.1", payload["error"])
        self.assertEqual(fake.calls, [])

    def test_non_android_platform_is_rejected(self) -> None:
        fake = _FakeAdb()
        rc, out, _err = self._run(
            fake, ["--platform", "web", "--json", "net", "status"]
        )

        self.assertEqual(rc, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("android", payload["error"])


if __name__ == "__main__":
    unittest.main()
