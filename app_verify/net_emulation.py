"""Программная эмуляция белых списков (БС) на Android-эмуляторе стенда.

Профили `off`/`soft`/`hard`/`down` реализуются отдельной цепочкой `iptables`
`RUMAP_WL`, подключаемой к `OUTPUT` строго через сотовый интерфейс `$CELL_IF`.
Wi-Fi, loopback и диапазон эмулятора (`10.0.2.0/24`) при этом не затрагиваются.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .android import CommandResult, CommandRunner, SubprocessRunner, find_local_adb
from .core import StandError

CHAIN_NAME = "RUMAP_WL"

DEFAULT_WHITELISTED_IPS: tuple[str, ...] = (
    "77.88.55.242",
    "87.240.132.78",
    "185.71.66.66",
    "178.248.232.209",
)

DEFAULT_CONTROL_IPS: tuple[str, ...] = (
    "1.1.1.1",
    "8.8.8.8",
    "9.9.9.9",
    "208.67.222.222",
)

PROFILES: tuple[str, ...] = ("off", "soft", "hard", "down")
WIFI_ACTIONS: tuple[str, ...] = ("on", "off")

_SAFETY_RULES: tuple[tuple[str, ...], ...] = (
    ("-d", "127.0.0.0/8", "-j", "RETURN"),
    ("-d", "10.0.2.0/24", "-j", "RETURN"),
)

_INTERFACE_RE = re.compile(r"^[a-zA-Z0-9_.:-]+$")
_FORBIDDEN_EXACT = frozenset({"lo", "wlan0"})
_FORBIDDEN_PREFIXES = ("tun", "wlan")
_NETWORK_BLOCK_RE = re.compile(r"NetworkAgentInfo\s*\{")
_TRANSPORTS_RE = re.compile(r"Transports:\s*([^\n\]]*)", re.IGNORECASE)
_INTERFACE_RE_LINE = re.compile(r"InterfaceName:\s*([^\s,}\]]+)")
_TUN_RE = re.compile(r"\b(tun\d+)\b")


@dataclass(frozen=True)
class ConnectivitySnapshot:
    """Срез сети из `dumpsys connectivity`, нужный профилям БС."""

    cell_interface: str | None = None
    cellular_connected: bool = False
    wifi_connected: bool = False
    wifi_interface: str | None = None
    vpn_active: bool = False
    vpn_interface: str | None = None


@dataclass(frozen=True)
class NetStatus:
    """Фактическое состояние профиля БС, сотовой сети, Wi-Fi и VPN."""

    profile: str
    allowed_ips: tuple[str, ...]
    cell_interface: str | None
    wifi_enabled: bool
    wifi_connected: bool
    wifi_interface: str | None
    cellular_connected: bool
    vpn_active: bool
    vpn_interface: str | None
    rules: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "profile": self.profile,
            "chain": CHAIN_NAME,
            "allowed_ips": list(self.allowed_ips),
            "cell_if": self.cell_interface,
            "wifi_enabled": self.wifi_enabled,
            "wifi_connected": self.wifi_connected,
            "wifi_interface": self.wifi_interface,
            "cellular_connected": self.cellular_connected,
            "vpn_active": self.vpn_active,
            "vpn_interface": self.vpn_interface,
            "rules": list(self.rules),
        }


def normalize_profile(profile: str | None) -> str:
    normalized = (profile or "").strip().lower()
    if normalized not in PROFILES:
        allowed = ", ".join(PROFILES)
        raise StandError(f"Неизвестный профиль БС: {profile!r}. Допустимые: {allowed}")
    return normalized


def normalize_wifi_action(wifi: str | None) -> str | None:
    if wifi is None:
        return None
    normalized = wifi.strip().lower()
    if normalized not in WIFI_ACTIONS:
        raise StandError(f"Неизвестное управление Wi-Fi: {wifi!r}. Допустимые: on, off")
    return normalized


def normalize_allow_ips(allow_ips: Iterable[str] | None) -> tuple[str, ...]:
    """Разбирает `--allow-ip`, поддерживая повторы и значения через запятую."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in allow_ips or ():
        for chunk in str(raw).split(","):
            candidate = chunk.strip()
            if not candidate:
                continue
            try:
                addr = ipaddress.IPv4Address(candidate)
            except ipaddress.AddressValueError as exc:
                raise StandError(f"Некорректный IPv4 в --allow-ip: {candidate!r}") from exc
            text = str(addr)
            if text not in seen:
                seen.add(text)
                result.append(text)
    return tuple(result)


def allowed_ips(allow_ips: Iterable[str] | None = None) -> tuple[str, ...]:
    """Белый список профиля: встроенные ориентиры + `--allow-ip` без дублей."""
    merged: list[str] = []
    seen: set[str] = set()
    for ip in (*DEFAULT_WHITELISTED_IPS, *normalize_allow_ips(allow_ips)):
        if ip not in seen:
            seen.add(ip)
            merged.append(ip)
    return tuple(merged)


def validate_cell_interface(interface: str | None) -> str:
    """Проверяет, что правила БС не повесят на loopback/Wi-Fi/VPN-интерфейс."""
    name = (interface or "").strip()
    if not name:
        raise StandError("Не удалось определить сотовый интерфейс ($CELL_IF) для профиля БС")
    if not _INTERFACE_RE.match(name):
        raise StandError(f"Небезопасное имя сотового интерфейса: {interface!r}")
    lowered = name.lower()
    if lowered in _FORBIDDEN_EXACT or lowered.startswith(_FORBIDDEN_PREFIXES):
        raise StandError(
            f"Нельзя применять правила БС к интерфейсу {name!r}: "
            "он не является сотовым (lo/wlan*/tun*)"
        )
    return name


def build_profile_rules(profile: str, allow_ips: Iterable[str] | None = None) -> list[list[str]]:
    """Генерирует правила цепочки `RUMAP_WL` (без префикса `-A RUMAP_WL`)."""
    normalized = normalize_profile(profile)
    if normalized == "off":
        return []

    whitelist = allowed_ips(allow_ips)
    rules: list[list[str]] = [list(rule) for rule in _SAFETY_RULES]

    if normalized == "soft":
        for ip in whitelist:
            rules.append(["-p", "tcp", "--dport", "443", "-d", ip, "-j", "RETURN"])
        rules.append(["-p", "tcp", "--dport", "443", "-j", "DROP"])
        return rules

    rules.append(["-p", "tcp", "--dport", "443", "-j", "DROP"])

    if normalized == "hard":
        for ip in whitelist:
            rules.append(
                ["-p", "icmp", "--icmp-type", "echo-request", "-d", ip, "-j", "RETURN"]
            )

    rules.append(["-p", "icmp", "--icmp-type", "echo-request", "-j", "DROP"])
    return rules


def parse_connectivity(text: str) -> ConnectivitySnapshot:
    """Разбирает блоки `NetworkAgentInfo` из `dumpsys connectivity`."""
    segments: list[str] = []
    matches = list(_NETWORK_BLOCK_RE.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segments.append(text[match.start() : end])

    cell_interface: str | None = None
    cellular_connected = False
    wifi_connected = False
    wifi_interface: str | None = None
    vpn_active = False
    vpn_interface: str | None = None

    for segment in segments:
        transports = _transport_tokens(segment)
        interfaces = _segment_interfaces(segment)

        if _has_transport(transports, "CELLULAR") or _has_mobile_type(segment):
            cellular_connected = True
            if cell_interface is None and interfaces:
                cell_interface = interfaces[0]

        if _has_transport(transports, "WIFI"):
            wifi_connected = True
            if wifi_interface is None and interfaces:
                wifi_interface = interfaces[0]

        if _has_transport(transports, "VPN"):
            vpn_active = True
            if vpn_interface is None and interfaces:
                vpn_interface = interfaces[0]

    if vpn_interface is None:
        tun_match = _TUN_RE.search(text)
        if tun_match is not None:
            vpn_active = True
            vpn_interface = tun_match.group(1)

    return ConnectivitySnapshot(
        cell_interface=cell_interface,
        cellular_connected=cellular_connected,
        wifi_connected=wifi_connected,
        wifi_interface=wifi_interface,
        vpn_active=vpn_active,
        vpn_interface=vpn_interface,
    )


def _transport_tokens(segment: str) -> str:
    match = _TRANSPORTS_RE.search(segment)
    return match.group(1).upper() if match else ""


def _has_transport(transports: str, name: str) -> bool:
    return re.search(rf"\b{name}\b", transports) is not None


def _has_mobile_type(segment: str) -> bool:
    return re.search(r"type:\s*MOBILE\b", segment, re.IGNORECASE) is not None


def _segment_interfaces(segment: str) -> list[str]:
    result: list[str] = []
    for match in _INTERFACE_RE_LINE.finditer(segment):
        candidate = match.group(1)
        if candidate.lower() in {"null", "none", "unknown"}:
            continue
        if candidate not in result:
            result.append(candidate)
    return result


def _rule_tokens(line: str) -> list[str]:
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


def _option_value(tokens: Sequence[str], option: str) -> str | None:
    try:
        index = tokens.index(option)
    except ValueError:
        return None
    if index + 1 >= len(tokens):
        return None
    return tokens[index + 1]


def _jump(tokens: Sequence[str]) -> str | None:
    return _option_value(tokens, "-j") or _option_value(tokens, "--jump")


def _matches(
    tokens: Sequence[str],
    *,
    proto: str | None = None,
    dport: str | None = None,
    icmp_type: str | None = None,
    jump: str | None = None,
) -> bool:
    if proto is not None and proto not in tokens:
        return False
    if dport is not None and _option_value(tokens, "--dport") != dport:
        return False
    if icmp_type is not None and _option_value(tokens, "--icmp-type") != icmp_type:
        return False
    if jump is not None and _jump(tokens) != jump:
        return False
    return True


def detect_profile(rule_lines: Sequence[str]) -> str:
    """Определяет профиль по фактическим правилам `iptables -S RUMAP_WL`."""
    rules = [
        _rule_tokens(line)
        for line in rule_lines
        if line.strip().startswith("-A ")
    ]
    if not rules:
        return "off"

    tcp_return = any(
        _matches(tokens, proto="tcp", dport="443", jump="RETURN") for tokens in rules
    )
    tcp_drop = any(_matches(tokens, proto="tcp", dport="443", jump="DROP") for tokens in rules)
    icmp_rules = [tokens for tokens in rules if "icmp" in tokens]
    icmp_return = any(
        _matches(tokens, proto="icmp", icmp_type="echo-request", jump="RETURN")
        for tokens in icmp_rules
    )
    icmp_drop = any(
        _matches(tokens, proto="icmp", icmp_type="echo-request", jump="DROP")
        for tokens in icmp_rules
    )

    if tcp_drop and icmp_return and icmp_drop and not tcp_return:
        return "hard"
    if tcp_drop and icmp_drop and not icmp_return and not tcp_return:
        return "down"
    if tcp_return and tcp_drop and not icmp_rules:
        return "soft"
    return "custom"


def extract_allowed_ips(rule_lines: Sequence[str]) -> tuple[str, ...]:
    """Возвращает IP из `-j RETURN`-правил цепочки, исключая страховочные сети."""
    result: list[str] = []
    for line in rule_lines:
        tokens = _rule_tokens(line)
        if _jump(tokens) != "RETURN":
            continue
        destination = _option_value(tokens, "-d") or _option_value(tokens, "--destination")
        if not destination or "/" in destination:
            continue
        try:
            ipaddress.IPv4Address(destination)
        except ipaddress.AddressValueError:
            continue
        if destination not in result:
            result.append(destination)
    return tuple(result)


class NetEmulationClient:
    """Клиент `adb`/`iptables` для программной эмуляции БС на сотовом интерфейсе."""

    def __init__(
        self,
        *,
        device: str | None = None,
        adb_bin: str | None = None,
        runner: CommandRunner | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.device = device
        self._adb_bin = adb_bin
        self.runner: CommandRunner = runner or SubprocessRunner()
        self.timeout_s = timeout_s

    @property
    def adb_bin(self) -> str:
        if self._adb_bin is None:
            self._adb_bin = find_local_adb()
        return self._adb_bin

    def _run_adb(self, argv: Sequence[str], *, check: bool = True) -> CommandResult:
        args = [self.adb_bin]
        if self.device:
            args.extend(["-s", self.device])
        args.extend(argv)
        result = self.runner.run(args, timeout=self.timeout_s)
        if check and result.returncode != 0:
            details = result.stderr_text.strip() or result.stdout_text.strip()
            raise StandError(f"`adb {' '.join(argv)}` завершился с ошибкой: {details or result.returncode}")
        return result

    def _run_shell(self, argv: Sequence[str], *, check: bool = True) -> CommandResult:
        # `adb shell` исполняет строку через `/system/bin/sh -c`, поэтому аргументы экранируем.
        return self._run_adb(["shell", shlex.join(list(argv))], check=check)

    def _iptables(self, *argv: str, check: bool = True) -> CommandResult:
        return self._run_shell(["iptables", *argv], check=check)

    def _ensure_root(self) -> None:
        root = self._run_adb(["root"], check=False)
        combined = f"{root.stdout_text}\n{root.stderr_text}".strip()
        lowered = combined.lower()
        if root.returncode != 0 and "already running as root" not in lowered:
            raise StandError(
                "Не удалось получить root через `adb root` "
                f"(код {root.returncode}): {combined or 'нет вывода'}"
            )
        self._run_adb(["wait-for-device"], check=False)
        uid = self._run_shell(["id", "-u"], check=False)
        if uid.returncode != 0 or uid.stdout_text.strip() != "0":
            details = uid.stderr_text.strip() or uid.stdout_text.strip() or "нет вывода"
            raise StandError(
                f"ADB-сессия не root: `id -u` вернул {details!r}; правила БС не применены"
            )

    def _read_connectivity(self) -> ConnectivitySnapshot:
        result = self._run_shell(["dumpsys", "connectivity"])
        return parse_connectivity(result.stdout_text)

    def _require_cell_interface(self) -> str:
        snapshot = self._read_connectivity()
        return validate_cell_interface(snapshot.cell_interface)

    def _read_chain_rules(self) -> tuple[str, ...]:
        result = self._iptables("-S", CHAIN_NAME, check=False)
        if result.returncode != 0:
            return ()
        return tuple(
            line.strip()
            for line in result.stdout_text.splitlines()
            if line.strip().startswith("-A ")
        )

    def _cleanup_chain(self) -> None:
        """Идемпотентно снимает все ссылки `OUTPUT -> RUMAP_WL`, чистит и удаляет цепочку."""
        output = self._iptables("-S", "OUTPUT", check=False)
        if output.returncode == 0:
            for line in output.stdout_text.splitlines():
                stripped = line.strip()
                if not stripped.startswith("-A OUTPUT") or CHAIN_NAME not in stripped:
                    continue
                tokens = _rule_tokens(stripped)
                if tokens[:2] != ["-A", "OUTPUT"]:
                    continue
                self._iptables("-D", "OUTPUT", *tokens[2:], check=False)
        self._iptables("-F", CHAIN_NAME, check=False)
        self._iptables("-X", CHAIN_NAME, check=False)

    def _apply_wifi(self, action: str | None) -> None:
        if action is None:
            return
        verb = "enable" if action == "on" else "disable"
        self._run_shell(["svc", "wifi", verb])

    def _read_wifi_enabled(self, snapshot: ConnectivitySnapshot) -> bool:
        result = self._run_shell(["settings", "get", "global", "wifi_on"], check=False)
        if result.returncode == 0:
            value = result.stdout_text.strip().lower()
            if value in {"1", "enabled"}:
                return True
            if value in {"0", "disabled"}:
                return False
        return snapshot.wifi_connected

    def _collect_status(self) -> NetStatus:
        rules = self._read_chain_rules()
        snapshot = self._read_connectivity()
        return NetStatus(
            profile=detect_profile(rules),
            allowed_ips=extract_allowed_ips(rules),
            cell_interface=snapshot.cell_interface,
            wifi_enabled=self._read_wifi_enabled(snapshot),
            wifi_connected=snapshot.wifi_connected,
            wifi_interface=snapshot.wifi_interface,
            cellular_connected=snapshot.cellular_connected,
            vpn_active=snapshot.vpn_active,
            vpn_interface=snapshot.vpn_interface,
            rules=rules,
        )

    def apply_profile(
        self,
        profile: str,
        *,
        allow_ips: Iterable[str] | None = None,
        wifi: str | None = None,
    ) -> NetStatus:
        normalized = normalize_profile(profile)
        whitelist = normalize_allow_ips(allow_ips)
        wifi_action = normalize_wifi_action(wifi)

        self._ensure_root()
        cell_interface: str | None = None
        if normalized != "off":
            cell_interface = self._require_cell_interface()

        self._cleanup_chain()

        if normalized != "off":
            self._iptables("-N", CHAIN_NAME)
            for rule in build_profile_rules(normalized, whitelist):
                self._iptables("-A", CHAIN_NAME, *rule)
            # Прыжок строго по сотовому интерфейсу: adb/lo/wlan/10.0.2.0/24 не затрагиваются.
            self._iptables("-I", "OUTPUT", "1", "-o", cell_interface, "-j", CHAIN_NAME)

        self._apply_wifi(wifi_action)
        return self._collect_status()

    def status(self) -> NetStatus:
        self._ensure_root()
        return self._collect_status()

    def reset(self) -> NetStatus:
        self._ensure_root()
        self._cleanup_chain()
        self._apply_wifi("on")
        return self._collect_status()
