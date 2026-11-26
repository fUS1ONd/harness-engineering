"""Автоматизация реальных VPN-клиентов (Koala Clash и Happ) на Android-стенде."""

from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from xml.sax.saxutils import escape as xml_escape

from .android import (
    ALLOWED_HDD_ROOT,
    CommandResult,
    CommandRunner,
    StandTimeoutError,
    SubprocessRunner,
    build_remote_env,
    find_local_adb,
    validate_hdd_root,
)
from .core import StandError
from .net_emulation import parse_connectivity

KOALA_CLIENT = "koala"
HAPP_CLIENT = "happ"
VPN_CLIENTS: tuple[str, ...] = (KOALA_CLIENT, HAPP_CLIENT)

KOALA_PACKAGE = "com.koala.clash"
HAPP_PACKAGE = "su.happ.proxyutility"

DEFAULT_EXCLUDE_PACKAGE = "ru.rumap.android.debug"
SUBSCRIPTION_URL_ENV = "APP_VERIFY_VPN_SUBSCRIPTION_URL"

KOALA_SERVICE_XML_PATH = f"/data/data/{KOALA_PACKAGE}/shared_prefs/service.xml"
KOALA_DEFAULT_REMOTE_APK = f"{ALLOWED_HDD_ROOT}/apks/koala-clash.apk"
HAPP_DEFAULT_REMOTE_APK = f"{ALLOWED_HDD_ROOT}/apks/happ.apk"

KOALA_EXTERNAL_ACTIVITY = f"{KOALA_PACKAGE}/com.github.kr328.clash.ExternalControlActivity"
KOALA_ACTION_START = f"{KOALA_PACKAGE}.action.START_CLASH"
KOALA_ACTION_STOP = f"{KOALA_PACKAGE}.action.STOP_CLASH"

HAPP_PER_APP_ACTIVITY = (
    f"{HAPP_PACKAGE}/su.happ.proxyutility.feature.proxy.PerAppProxyActivity"
)
HAPP_WIDGET_RECEIVER = f"{HAPP_PACKAGE}/.receiver.WidgetProvider"
HAPP_ACTION_WIDGET_CLICK = f"{HAPP_PACKAGE}.action.widget.click"
HAPP_MMKV_SUB_PATH = f"/data/data/{HAPP_PACKAGE}/files/mmkv/SUB"
HAPP_STATE_JSON_PATH = f"/data/data/{HAPP_PACKAGE}/files/app_verify_exclude.json"
UI_DUMP_DEVICE_PATH = "/sdcard/app_verify_vpn_ui.xml"

_CLIENT_ALIASES: dict[str, str] = {
    "koala": KOALA_CLIENT,
    "koala-clash": KOALA_CLIENT,
    "koalaclash": KOALA_CLIENT,
    "happ": HAPP_CLIENT,
}

_CLIENT_PACKAGES: dict[str, str] = {
    KOALA_CLIENT: KOALA_PACKAGE,
    HAPP_CLIENT: HAPP_PACKAGE,
}

_CLIENT_REMOTE_APKS: dict[str, str] = {
    KOALA_CLIENT: KOALA_DEFAULT_REMOTE_APK,
    HAPP_CLIENT: HAPP_DEFAULT_REMOTE_APK,
}

_PACKAGE_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$")
_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_ACTIVATE_VPN_RE = re.compile(r"\bACTIVATE_VPN:\s*([a-zA-Z0-9_-]+)")
_PRIMARY_CPU_ABI_RE = re.compile(r"\bprimaryCpuAbi=([^\s]+)")
_NETWORK_BLOCK_RE = re.compile(r"NetworkAgentInfo\s*\{")
_TRANSPORTS_RE = re.compile(r"Transports:\s*([^\n\]]*)", re.IGNORECASE)
_UIDS_RE = re.compile(r"Uids:\s*<\{([^}]*)\}>")
_SIGILL_RE = re.compile(r"\bSIGILL\b|Fatal signal 4\b", re.IGNORECASE)


@dataclass(frozen=True)
class KoalaServicePrefs:
    """Настройки `/data/data/com.koala.clash/shared_prefs/service.xml`."""

    active_profile: str | None = None
    access_control_mode: str = "AcceptAll"
    access_control_packages: tuple[str, ...] = ()
    extra_entries: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class VpnClientStatus:
    """Текущее состояние VPN-клиента, системного разрешения и `tun0`."""

    client: str
    package: str
    installed: bool
    activate_vpn: str
    vpn_active: bool
    vpn_interface: str | None
    exclude_mode: str
    excluded_packages: tuple[str, ...] = ()
    active_profile: str | None = None
    vpn_uids: str | None = None
    abi_warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": True,
            "client": self.client,
            "package": self.package,
            "installed": self.installed,
            "activate_vpn": self.activate_vpn,
            "vpn_active": self.vpn_active,
            "vpn_interface": self.vpn_interface,
            "exclude_mode": self.exclude_mode,
            "excluded_packages": list(self.excluded_packages),
            "active_profile": self.active_profile,
            "vpn_uids": self.vpn_uids,
        }
        if self.abi_warning:
            payload["abi_warning"] = self.abi_warning
        return payload


def normalize_vpn_client(client: str | None) -> str:
    key = (client or "").strip().lower()
    resolved = _CLIENT_ALIASES.get(key)
    if resolved is None:
        allowed = ", ".join(VPN_CLIENTS)
        raise StandError(f"Неизвестный VPN-клиент: {client!r}. Допустимые: {allowed}")
    return resolved


def resolve_subscription_url(
    url: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Возвращает и валидирует URL подписки из `--url` или `APP_VERIFY_VPN_SUBSCRIPTION_URL`."""
    environ = os.environ if env is None else env
    candidate = (url if url is not None else environ.get(SUBSCRIPTION_URL_ENV, "")).strip()
    if not candidate:
        raise StandError(
            "Не указан URL подписки: передайте `--url <https://...>` или задайте "
            f"переменную окружения `{SUBSCRIPTION_URL_ENV}`"
        )
    if any(ch.isspace() for ch in candidate):
        raise StandError("Некорректный URL подписки: пробельные и управляющие символы недопустимы")
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise StandError(
            "Некорректный URL подписки: ожидается `http://` или `https://` с хостом"
        )
    return candidate


def normalize_exclude_packages(
    packages: Iterable[str] | None = None,
    *,
    clear: bool = False,
) -> tuple[str, ...]:
    """Нормализует список исключаемых пакетов (по умолчанию `ru.rumap.android.debug`)."""
    raw_items: list[str] = []
    for raw in packages or ():
        for part in str(raw).split(","):
            stripped = part.strip()
            if stripped:
                raw_items.append(stripped)

    if clear and raw_items:
        raise StandError("Нельзя одновременно указывать `--clear` и `--package`")
    if clear:
        return ()
    if not raw_items:
        return (DEFAULT_EXCLUDE_PACKAGE,)

    result: list[str] = []
    seen: set[str] = set()
    for pkg in raw_items:
        if not _PACKAGE_NAME_RE.match(pkg):
            raise StandError(f"Некорректное имя Android-пакета в --package: {pkg!r}")
        if pkg not in seen:
            seen.add(pkg)
            result.append(pkg)
    return tuple(result)


def parse_appops_activate_vpn(output: str) -> str:
    match = _ACTIVATE_VPN_RE.search(output)
    if match is None:
        return "default"
    return match.group(1).strip().lower()


def extract_vpn_uid_ranges(connectivity_text: str) -> str | None:
    """Извлекает `Uids: <{...}>` активного блока `NetworkAgentInfo` с транспортом VPN."""
    matches = list(_NETWORK_BLOCK_RE.finditer(connectivity_text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(connectivity_text)
        segment = connectivity_text[match.start() : end]
        t_match = _TRANSPORTS_RE.search(segment)
        transports = t_match.group(1).upper() if t_match else ""
        if re.search(r"\bVPN\b", transports) is None:
            continue
        uids_match = _UIDS_RE.search(segment)
        if uids_match is not None:
            return uids_match.group(1).strip()
    return None


def parse_koala_service_xml(xml_text: str) -> KoalaServicePrefs:
    """Разбирает `service.xml` Koala Clash, сохраняя прочие примитивные ключи."""
    stripped = xml_text.strip()
    if not stripped:
        return KoalaServicePrefs()
    try:
        root = ET.fromstring(stripped)
    except ET.ParseError as exc:
        raise StandError(f"Некорректный XML в {KOALA_SERVICE_XML_PATH}: {exc}") from exc

    active_profile: str | None = None
    mode = "AcceptAll"
    packages: list[str] = []
    extras: list[tuple[str, str, str]] = []

    for child in root:
        name = child.attrib.get("name", "")
        if not name:
            continue
        if child.tag == "string" and name == "active_profile":
            val = (child.text or "").strip()
            active_profile = val or None
        elif child.tag == "string" and name == "access_control_mode":
            val = (child.text or "").strip()
            if val:
                mode = val
        elif child.tag == "set" and name == "access_control_packages":
            for item in child.findall("string"):
                pkg = (item.text or "").strip()
                if pkg and pkg not in packages:
                    packages.append(pkg)
        elif child.tag == "string":
            extras.append(("string", name, child.text or ""))
        elif child.tag in {"boolean", "int", "long", "float"} and "value" in child.attrib:
            extras.append((child.tag, name, child.attrib["value"]))

    return KoalaServicePrefs(
        active_profile=active_profile,
        access_control_mode=mode,
        access_control_packages=tuple(packages),
        extra_entries=tuple(extras),
    )


def render_koala_service_xml(prefs: KoalaServicePrefs) -> str:
    """Формирует `service.xml` для Koala Clash с сохранением `active_profile` и доп. ключей."""
    lines = [
        "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>",
        "<map>",
    ]
    if prefs.active_profile:
        lines.append(
            f'    <string name="active_profile">{xml_escape(prefs.active_profile)}</string>'
        )
    lines.append(
        f'    <string name="access_control_mode">{xml_escape(prefs.access_control_mode)}</string>'
    )
    if prefs.access_control_packages:
        lines.append('    <set name="access_control_packages">')
        for pkg in prefs.access_control_packages:
            lines.append(f"        <string>{xml_escape(pkg)}</string>")
        lines.append("    </set>")
    else:
        lines.append('    <set name="access_control_packages" />')

    for tag, name, val in prefs.extra_entries:
        esc_name = xml_escape(name, {'"': "&quot;"})
        if tag == "string":
            lines.append(f'    <string name="{esc_name}">{xml_escape(val)}</string>')
        else:
            esc_val = xml_escape(val, {'"': "&quot;"})
            lines.append(f'    <{tag} name="{esc_name}" value="{esc_val}" />')
    lines.append("</map>")
    return "\n".join(lines) + "\n"


def _parse_bounds_center(bounds: str) -> tuple[int, int] | None:
    match = _BOUNDS_RE.search(bounds or "")
    if match is None:
        return None
    x1, y1, x2, y2 = (int(part) for part in match.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _parse_ui_hierarchy(xml_text: str) -> ET.Element | None:
    idx = xml_text.find("<hierarchy")
    if idx == -1:
        return None
    idx_decl = xml_text.find("<?xml")
    snippet = xml_text[idx_decl if idx_decl != -1 else idx :]
    try:
        return ET.fromstring(snippet)
    except ET.ParseError:
        return None


def find_ui_node_center(
    xml_text: str,
    *,
    texts: Sequence[str] = (),
    descs: Sequence[str] = (),
    resource_id_suffix: str | None = None,
) -> tuple[int, int] | None:
    """Находит центр узла в XML `uiautomator dump` по тексту, `content-desc` или `resource-id`."""
    root = _parse_ui_hierarchy(xml_text)
    if root is None:
        return None

    text_set = set(texts)
    desc_set = set(descs)
    for node in root.iter("node"):
        if text_set and node.attrib.get("text", "") in text_set:
            pt = _parse_bounds_center(node.attrib.get("bounds", ""))
            if pt is not None:
                return pt
        if desc_set and node.attrib.get("content-desc", "") in desc_set:
            pt = _parse_bounds_center(node.attrib.get("bounds", ""))
            if pt is not None:
                return pt
        if resource_id_suffix and node.attrib.get("resource-id", "").endswith(resource_id_suffix):
            pt = _parse_bounds_center(node.attrib.get("bounds", ""))
            if pt is not None:
                return pt
    return None


def find_happ_package_row(xml_text: str, package: str) -> tuple[tuple[int, int], bool] | None:
    """Находит строку пакета в `PerAppProxyActivity` Happ и состояние её `check_box`."""
    root = _parse_ui_hierarchy(xml_text)
    if root is None:
        return None

    # Сначала ищем строго в рамках каждого контейнера строки `:id/cl_app`,
    # чтобы чекбокс соседней строки не перехватывал состояние целевого пакета.
    for row in root.iter("node"):
        if not row.attrib.get("resource-id", "").endswith(":id/cl_app"):
            continue
        pkg_node: ET.Element | None = None
        chk_node: ET.Element | None = None
        for sub in row.iter("node"):
            srid = sub.attrib.get("resource-id", "")
            if srid.endswith(":id/package_name") and sub.attrib.get("text") == package:
                pkg_node = sub
            elif srid.endswith(":id/check_box") and chk_node is None:
                chk_node = sub
        if pkg_node is not None and chk_node is not None:
            pt = _parse_bounds_center(row.attrib.get("bounds", "")) or _parse_bounds_center(
                pkg_node.attrib.get("bounds", "")
            )
            if pt is not None:
                return pt, chk_node.attrib.get("checked", "").lower() == "true"

    # Fallback для упрощённой иерархии без обёртки `cl_app`: ищем общего родителя.
    for parent in root.iter("node"):
        direct_pkg: ET.Element | None = None
        direct_chk: ET.Element | None = None
        for child in parent:
            rid = child.attrib.get("resource-id", "")
            if rid.endswith(":id/package_name") and child.attrib.get("text") == package:
                direct_pkg = child
            elif rid.endswith(":id/check_box") and direct_chk is None:
                direct_chk = child
        if direct_pkg is not None and direct_chk is not None:
            pt = _parse_bounds_center(direct_pkg.attrib.get("bounds", "")) or _parse_bounds_center(
                direct_chk.attrib.get("bounds", "")
            )
            if pt is not None:
                return pt, direct_chk.attrib.get("checked", "").lower() == "true"
    return None


def detect_happ_sigill(logcat_text: str) -> str | None:
    """Распознаёт краш `SIGILL` нативного `XRayDaemon` (`libgojni.so`) под `libndk_translation`."""
    if _SIGILL_RE.search(logcat_text) is None:
        return None
    lowered = logcat_text.lower()
    if any(token in lowered for token in ("xraydaemon", "su.happ.proxyutility", "libgojni")):
        return (
            "Happ (`su.happ.proxyutility:XRayDaemon`) завершился с `SIGILL` на `x86_64` эмуляторе: "
            "в APK Happ отсутствуют нативные библиотеки `x86_64` (только `arm64-v8a`/`armeabi-v7a`), "
            "а `arm64-v8a/libgojni.so` исполняет неподдерживаемую `libndk_translation` инструкцию "
            "(`MRS X1, CNTFRQ_EL0`). Используйте `koala` (`com.koala.clash`) на `x86_64` эмуляторе "
            "или физическое ARM64-устройство для живого запуска туннеля Happ."
        )
    return None


class VpnClientsManager:
    """Управление установкой, подпиской, исключениями и `tun0` для Koala Clash и Happ."""

    def __init__(
        self,
        *,
        device: str | None = None,
        adb_bin: str | None = None,
        runner: CommandRunner | None = None,
        timeout_s: float = 30.0,
        ssh_host: str = "home",
        remote_root: str = ALLOWED_HDD_ROOT,
        console_port: int = 5564,
        remote_adb_server_port: int = 15037,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.device = device
        self._adb_bin = adb_bin
        self.runner: CommandRunner = runner or SubprocessRunner()
        self.timeout_s = timeout_s
        self.ssh_host = ssh_host
        self.remote_root = validate_hdd_root(remote_root)
        self.console_port = console_port
        self.remote_adb_server_port = remote_adb_server_port
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn

    @property
    def adb_bin(self) -> str:
        if self._adb_bin is None:
            self._adb_bin = find_local_adb()
        return self._adb_bin

    def _run_adb(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        args = [self.adb_bin]
        if self.device:
            args.extend(["-s", self.device])
        args.extend(argv)
        result = self.runner.run(args, timeout=timeout or self.timeout_s)
        if check and result.returncode != 0:
            details = result.stderr_text.strip() or result.stdout_text.strip()
            raise StandError(
                f"`adb {' '.join(argv)}` завершился с ошибкой: {details or result.returncode}"
            )
        return result

    def _run_shell(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        return self._run_adb(["shell", shlex.join(list(argv))], check=check, timeout=timeout)

    def _run_shell_script(
        self,
        script: str,
        *,
        check: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        return self._run_adb(["shell", script], check=check, timeout=timeout)

    def _ensure_root(self) -> None:
        root = self._run_adb(["root"], check=False)
        combined = f"{root.stdout_text}\n{root.stderr_text}".strip()
        if root.returncode != 0 and "already running as root" not in combined.lower():
            raise StandError(
                f"Не удалось получить root через `adb root` (код {root.returncode}): {combined or 'нет вывода'}"
            )
        self._run_adb(["wait-for-device"], check=False)
        uid = self._run_shell(["id", "-u"], check=False)
        if uid.returncode != 0 or uid.stdout_text.strip() != "0":
            details = uid.stderr_text.strip() or uid.stdout_text.strip() or "нет вывода"
            raise StandError(f"ADB-сессия не root: `id -u` вернул {details!r}")

    def _is_package_installed(self, package: str) -> bool:
        res = self._run_shell(["pm", "path", package], check=False)
        return res.returncode == 0 and "package:" in res.stdout_text

    def _require_installed(self, client: str) -> str:
        package = _CLIENT_PACKAGES[client]
        if not self._is_package_installed(package):
            hint_apk = _CLIENT_REMOTE_APKS[client]
            raise StandError(
                f"Пакет {package} не установлен на устройстве. "
                f"Выполните `vpn prepare {client} --apk {hint_apk}`."
            )
        return package

    def _install_apk(self, apk_path: str) -> None:
        local = Path(apk_path)
        if local.is_file():
            res = self._run_adb(
                ["install", "-r", "-g", str(local)],
                check=False,
                timeout=max(self.timeout_s, 120.0),
            )
            combined = f"{res.stdout_text}\n{res.stderr_text}".strip()
            if res.returncode != 0 or "Failure" in combined:
                raise StandError(f"Установка APK не удалась ({local}): {combined}")
            return

        if apk_path.startswith(ALLOWED_HDD_ROOT + "/"):
            remote_apk = validate_hdd_root(apk_path)
            env = build_remote_env(
                self.remote_root,
                remote_adb_server_port=self.remote_adb_server_port,
            )
            exports = "\n".join(f"export {k}={shlex.quote(v)}" for k, v in env.items())
            remote_adb = posixpath.join(self.remote_root, "sdk", "platform-tools", "adb")
            serial = f"emulator-{self.console_port}"
            script = (
                "set -euo pipefail\n"
                f"{exports}\n"
                f"{shlex.quote(remote_adb)} -s {shlex.quote(serial)} "
                f"install -r -g {shlex.quote(remote_apk)} </dev/null\n"
            )
            ssh_cmd = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "--",
                self.ssh_host,
                "bash",
                "-s",
            ]
            res = self.runner.run(
                ssh_cmd,
                timeout=max(self.timeout_s, 120.0),
                input_bytes=script.encode("utf-8"),
            )
            combined = f"{res.stdout_text}\n{res.stderr_text}".strip()
            if res.returncode != 0 or "Failure" in combined:
                raise StandError(
                    f"Удалённая установка APK на {self.ssh_host} не удалась ({remote_apk}): {combined}"
                )
            return

        raise StandError(f"APK-файл не найден: {apk_path}")

    def _detect_abi_warning(self, package: str) -> str | None:
        dev_res = self._run_shell(["getprop", "ro.product.cpu.abi"], check=False)
        pkg_res = self._run_shell(["dumpsys", "package", package], check=False)
        dev_abi = dev_res.stdout_text.strip()
        m = _PRIMARY_CPU_ABI_RE.search(pkg_res.stdout_text)
        pkg_abi = m.group(1).strip() if m else ""
        if (
            dev_abi == "x86_64"
            and pkg_abi
            and pkg_abi not in {"x86_64", "x86", "null"}
        ):
            return (
                f"Пакет {package} собран под {pkg_abi} (нет нативного x86_64 ABI): "
                f"на эмуляторе {dev_abi} нативный демон может падать с SIGILL под libndk_translation"
            )
        return None

    def _dump_ui_xml(self) -> str:
        p_q = shlex.quote(UI_DUMP_DEVICE_PATH)
        res = self._run_shell_script(
            f"uiautomator dump {p_q} >/dev/null 2>&1 && cat {p_q}; rm -f {p_q}",
            check=False,
            timeout=20.0,
        )
        return res.stdout_text

    def _read_koala_prefs(self) -> KoalaServicePrefs:
        res = self._run_shell(["cat", KOALA_SERVICE_XML_PATH], check=False)
        if res.returncode != 0:
            return KoalaServicePrefs()
        return parse_koala_service_xml(res.stdout_text)

    def _write_koala_prefs(self, prefs: KoalaServicePrefs) -> None:
        rendered = render_koala_service_xml(prefs)
        prefs_dir = posixpath.dirname(KOALA_SERVICE_XML_PATH)
        app_dir = posixpath.dirname(prefs_dir)
        script = (
            "set -e\n"
            f"OWNER=$(stat -c '%u:%g' {shlex.quote(app_dir)})\n"
            f"mkdir -p {shlex.quote(prefs_dir)}\n"
            f"chown \"$OWNER\" {shlex.quote(prefs_dir)}\n"
            f"chmod 771 {shlex.quote(prefs_dir)}\n"
            f"printf '%s' {shlex.quote(rendered)} > {shlex.quote(KOALA_SERVICE_XML_PATH)}\n"
            f"chown \"$OWNER\" {shlex.quote(KOALA_SERVICE_XML_PATH)}\n"
            f"chmod 660 {shlex.quote(KOALA_SERVICE_XML_PATH)}\n"
            f"restorecon -R {shlex.quote(prefs_dir)} 2>/dev/null || true\n"
        )
        self._run_shell_script(script)

    def _read_happ_exclude_state(self) -> tuple[str, tuple[str, ...]]:
        res = self._run_shell(["cat", HAPP_STATE_JSON_PATH], check=False)
        if res.returncode != 0 or not res.stdout_text.strip():
            return "unknown", ()
        try:
            data = json.loads(res.stdout_text)
        except json.JSONDecodeError:
            return "unknown", ()
        if not isinstance(data, dict):
            return "unknown", ()
        mode = str(data.get("mode") or "unknown")
        pkgs = tuple(str(p) for p in (data.get("packages") or ()) if str(p).strip())
        return mode, pkgs

    def _write_happ_exclude_state(self, mode: str, packages: Sequence[str]) -> None:
        payload = json.dumps({"mode": mode, "packages": list(packages)}, ensure_ascii=False)
        files_dir = posixpath.dirname(HAPP_STATE_JSON_PATH)
        app_dir = posixpath.dirname(files_dir)
        script = (
            "set -e\n"
            f"OWNER=$(stat -c '%u:%g' {shlex.quote(app_dir)})\n"
            f"mkdir -p {shlex.quote(files_dir)}\n"
            f"chown \"$OWNER\" {shlex.quote(files_dir)}\n"
            f"chmod 771 {shlex.quote(files_dir)}\n"
            f"printf '%s\\n' {shlex.quote(payload)} > {shlex.quote(HAPP_STATE_JSON_PATH)}\n"
            f"chown \"$OWNER\" {shlex.quote(HAPP_STATE_JSON_PATH)}\n"
            f"chmod 600 {shlex.quote(HAPP_STATE_JSON_PATH)}\n"
            f"restorecon -R {shlex.quote(files_dir)} 2>/dev/null || true\n"
        )
        self._run_shell_script(script)

    def _is_tun0_present(self) -> bool:
        res = self._run_shell(["ip", "link", "show", "tun0"], check=False)
        return res.returncode == 0 and "tun0" in res.stdout_text

    def _collect_status(self, client: str) -> VpnClientStatus:
        normalized = normalize_vpn_client(client)
        package = _CLIENT_PACKAGES[normalized]
        installed = self._is_package_installed(package)

        activate_vpn = "missing"
        abi_warning: str | None = None
        if installed:
            ops_res = self._run_shell(["appops", "get", package], check=False)
            activate_vpn = parse_appops_activate_vpn(ops_res.stdout_text)
            abi_warning = self._detect_abi_warning(package)

        conn_res = self._run_shell(["dumpsys", "connectivity"])
        snapshot = parse_connectivity(conn_res.stdout_text)
        vpn_uids = extract_vpn_uid_ranges(conn_res.stdout_text) if snapshot.vpn_active else None

        exclude_mode = "unknown"
        excluded_packages: tuple[str, ...] = ()
        active_profile: str | None = None

        if normalized == KOALA_CLIENT and installed:
            prefs = self._read_koala_prefs()
            exclude_mode = prefs.access_control_mode
            excluded_packages = prefs.access_control_packages
            active_profile = prefs.active_profile
        elif normalized == HAPP_CLIENT and installed:
            exclude_mode, excluded_packages = self._read_happ_exclude_state()
            sub_res = self._run_shell(["test", "-s", HAPP_MMKV_SUB_PATH], check=False)
            if sub_res.returncode == 0:
                active_profile = "mmkv:SUB"

        return VpnClientStatus(
            client=normalized,
            package=package,
            installed=installed,
            activate_vpn=activate_vpn,
            vpn_active=snapshot.vpn_active,
            vpn_interface=snapshot.vpn_interface,
            exclude_mode=exclude_mode,
            excluded_packages=excluded_packages,
            active_profile=active_profile,
            vpn_uids=vpn_uids,
            abi_warning=abi_warning,
        )

    def prepare(self, client: str, *, apk: str | None = None) -> VpnClientStatus:
        normalized = normalize_vpn_client(client)
        package = _CLIENT_PACKAGES[normalized]
        if apk:
            self._install_apk(apk)

        if not self._is_package_installed(package):
            hint_apk = _CLIENT_REMOTE_APKS[normalized]
            raise StandError(
                f"Пакет {package} не установлен на устройстве. "
                f"Передайте `--apk {hint_apk}` (или локальный путь к APK)."
            )

        self._run_shell(["appops", "set", package, "ACTIVATE_VPN", "allow"])
        self._run_shell(
            ["appops", "set", package, "ACTIVATE_PLATFORM_VPN", "allow"],
            check=False,
        )
        self._run_shell(
            ["pm", "grant", package, "android.permission.POST_NOTIFICATIONS"],
            check=False,
        )

        self._ensure_root()
        st = self._collect_status(normalized)
        if st.activate_vpn != "allow":
            raise StandError(
                f"Не удалось выдать ACTIVATE_VPN=allow для {package} (текущее: {st.activate_vpn})"
            )
        return st

    def import_subscription(
        self,
        client: str,
        *,
        url: str | None = None,
        name: str = "verify",
        timeout_s: float = 90.0,
        env: Mapping[str, str] | None = None,
    ) -> VpnClientStatus:
        normalized = normalize_vpn_client(client)
        resolved_url = resolve_subscription_url(url, env=env)
        profile_name = (name or "verify").strip() or "verify"

        self._ensure_root()
        self._require_installed(normalized)

        if normalized == KOALA_CLIENT:
            before_profile = self._read_koala_prefs().active_profile
            enc_url = urllib.parse.quote(resolved_url, safe="")
            enc_name = urllib.parse.quote(profile_name, safe="")
            deeplink = f"koala-clash://install-config?url={enc_url}&name={enc_name}"
            res = self._run_shell(
                [
                    "am",
                    "start",
                    "-W",
                    "-a",
                    "android.intent.action.VIEW",
                    "-d",
                    deeplink,
                    KOALA_PACKAGE,
                ],
                check=False,
            )
            if res.returncode != 0:
                raise StandError(
                    f"Не удалось открыть deep link импорта подписки в Koala Clash (код {res.returncode})"
                )

            deadline = self._monotonic() + timeout_s
            save_tapped = False
            while True:
                ui_xml = self._dump_ui_xml()
                save_pt = find_ui_node_center(
                    ui_xml,
                    texts=("Save", "Сохранить"),
                    descs=("Save", "Сохранить"),
                )
                if save_pt is not None and not save_tapped:
                    self._run_shell(["input", "tap", str(save_pt[0]), str(save_pt[1])])
                    save_tapped = True
                elif save_tapped:
                    prefs = self._read_koala_prefs()
                    if prefs.active_profile and (
                        prefs.active_profile != before_profile or save_pt is None
                    ):
                        return self._collect_status(normalized)

                if self._monotonic() >= deadline:
                    raise StandTimeoutError(
                        f"Таймаут импорта подписки в Koala Clash ({timeout_s}s): "
                        "`active_profile` не появился в `service.xml`"
                    )
                self._sleep(2.0)

        deeplink = f"happ://add/{resolved_url}"
        res = self._run_shell(
            [
                "am",
                "start",
                "-W",
                "-a",
                "android.intent.action.VIEW",
                "-d",
                deeplink,
                HAPP_PACKAGE,
            ],
            check=False,
        )
        if res.returncode != 0:
            raise StandError(
                f"Не удалось открыть deep link импорта подписки в Happ (код {res.returncode})"
            )
        deadline = self._monotonic() + timeout_s
        while True:
            sub_res = self._run_shell(["test", "-s", HAPP_MMKV_SUB_PATH], check=False)
            if sub_res.returncode == 0:
                return self._collect_status(normalized)
            if self._monotonic() >= deadline:
                raise StandTimeoutError(
                    f"Таймаут импорта подписки в Happ ({timeout_s}s): "
                    f"файл `{HAPP_MMKV_SUB_PATH}` не создан"
                )
            self._sleep(1.5)

    def set_exclude(
        self,
        client: str,
        *,
        packages: Iterable[str] | None = None,
        clear: bool = False,
    ) -> VpnClientStatus:
        normalized = normalize_vpn_client(client)
        target_packages = normalize_exclude_packages(packages, clear=clear)

        self._ensure_root()
        self._require_installed(normalized)

        if normalized == KOALA_CLIENT:
            conn_before = parse_connectivity(
                self._run_shell(["dumpsys", "connectivity"]).stdout_text
            )
            was_active = conn_before.vpn_active
            if was_active:
                self._disconnect_koala(timeout_s=15.0)
            self._run_shell(["am", "force-stop", KOALA_PACKAGE])

            current = self._read_koala_prefs()
            updated = KoalaServicePrefs(
                active_profile=current.active_profile,
                access_control_mode="AcceptAll" if clear else "DenySelected",
                access_control_packages=target_packages,
                extra_entries=current.extra_entries,
            )
            self._write_koala_prefs(updated)

            if was_active:
                return self.connect(KOALA_CLIENT)
            return self._collect_status(KOALA_CLIENT)

        self._run_shell(["am", "start", "-W", "-n", HAPP_PER_APP_ACTIVITY])
        self._sleep(1.0)
        ui_xml = self._dump_ui_xml()
        if clear:
            off_pt = find_ui_node_center(ui_xml, resource_id_suffix=":id/ll_off", texts=("Off",))
            if off_pt is None:
                raise StandError("Не найден переключатель `ll_off` в `PerAppProxyActivity` Happ")
            self._run_shell(["input", "tap", str(off_pt[0]), str(off_pt[1])])
            self._write_happ_exclude_state("off", ())
            return self._collect_status(HAPP_CLIENT)

        bypass_pt = find_ui_node_center(
            ui_xml, resource_id_suffix=":id/ll_bypass", texts=("Bypass",)
        )
        if bypass_pt is None:
            raise StandError("Не найден переключатель `ll_bypass` в `PerAppProxyActivity` Happ")
        self._run_shell(["input", "tap", str(bypass_pt[0]), str(bypass_pt[1])])
        self._sleep(0.5)

        for pkg in target_packages:
            ui_xml = self._dump_ui_xml()
            close_pt = find_ui_node_center(ui_xml, resource_id_suffix=":id/search_close_btn")
            if close_pt is not None:
                self._run_shell(["input", "tap", str(close_pt[0]), str(close_pt[1])])
                self._sleep(0.3)
                ui_xml = self._dump_ui_xml()
            else:
                search_pt = find_ui_node_center(
                    ui_xml, resource_id_suffix=":id/search_button", descs=("Search",)
                )
                if search_pt is None:
                    raise StandError(
                        "Не найдена кнопка поиска `search_button` в `PerAppProxyActivity` Happ"
                    )
                self._run_shell(["input", "tap", str(search_pt[0]), str(search_pt[1])])
                self._sleep(0.5)

            self._run_shell(["input", "text", pkg])
            self._sleep(0.8)
            row_ui = self._dump_ui_xml()
            row = find_happ_package_row(row_ui, pkg)
            if row is None:
                raise StandError(
                    f"Пакет {pkg} не найден в списке `PerAppProxyActivity` Happ "
                    "(убедитесь, что он установлен на эмуляторе)"
                )
            row_pt, is_checked = row
            if not is_checked:
                self._run_shell(["input", "tap", str(row_pt[0]), str(row_pt[1])])
                self._sleep(0.5)

        self._write_happ_exclude_state("bypass", target_packages)
        return self._collect_status(HAPP_CLIENT)

    def connect(self, client: str, *, timeout_s: float = 30.0) -> VpnClientStatus:
        normalized = normalize_vpn_client(client)
        self._ensure_root()
        self._require_installed(normalized)

        if normalized == KOALA_CLIENT:
            prefs = self._read_koala_prefs()
            if not prefs.active_profile:
                raise StandError(
                    "В Koala Clash нет активного профиля (`active_profile` пуст). "
                    "Сначала выполните `vpn import-subscription koala`."
                )
            self._run_shell(
                [
                    "am",
                    "start",
                    "-W",
                    "-n",
                    KOALA_EXTERNAL_ACTIVITY,
                    "-a",
                    KOALA_ACTION_START,
                ]
            )
            deadline = self._monotonic() + timeout_s
            retried = False
            while True:
                st = self._collect_status(KOALA_CLIENT)
                if st.vpn_active and st.vpn_interface:
                    return st
                if self._monotonic() >= deadline:
                    raise StandTimeoutError(
                        f"Таймаут поднятия `tun0` в Koala Clash ({timeout_s}s)"
                    )
                if not retried and deadline - self._monotonic() <= timeout_s / 2:
                    retried = True
                    self._run_shell(
                        [
                            "am",
                            "start",
                            "-W",
                            "-n",
                            KOALA_EXTERNAL_ACTIVITY,
                            "-a",
                            KOALA_ACTION_START,
                        ],
                        check=False,
                    )
                self._sleep(1.0)

        current_happ = self._collect_status(HAPP_CLIENT)
        if current_happ.vpn_active and current_happ.vpn_interface:
            return current_happ

        self._run_shell(["logcat", "-c"], check=False)
        self._run_shell(
            [
                "am",
                "broadcast",
                "-a",
                HAPP_ACTION_WIDGET_CLICK,
                "-n",
                HAPP_WIDGET_RECEIVER,
            ]
        )
        deadline = self._monotonic() + timeout_s
        while True:
            st = self._collect_status(HAPP_CLIENT)
            if st.vpn_active and st.vpn_interface:
                return st
            log_res = self._run_shell(["logcat", "-d", "-v", "brief"], check=False)
            sigill_diag = detect_happ_sigill(log_res.stdout_text)
            if sigill_diag is not None:
                self._run_shell(["am", "force-stop", HAPP_PACKAGE], check=False)
                raise StandError(sigill_diag)
            if self._monotonic() >= deadline:
                self._run_shell(["am", "force-stop", HAPP_PACKAGE], check=False)
                raise StandTimeoutError(f"Таймаут поднятия `tun0` в Happ ({timeout_s}s)")
            self._sleep(1.0)

    def _disconnect_koala(self, *, timeout_s: float) -> VpnClientStatus:
        self._run_shell(
            [
                "am",
                "start",
                "-W",
                "-n",
                KOALA_EXTERNAL_ACTIVITY,
                "-a",
                KOALA_ACTION_STOP,
            ],
            check=False,
        )
        deadline = self._monotonic() + timeout_s
        attempts = 0
        while True:
            st = self._collect_status(KOALA_CLIENT)
            if not st.vpn_active and not self._is_tun0_present():
                return st
            attempts += 1
            if attempts == 2:
                self._run_shell(["am", "force-stop", KOALA_PACKAGE], check=False)
            if self._monotonic() >= deadline:
                raise StandTimeoutError(
                    f"Таймаут отключения `tun0` в Koala Clash ({timeout_s}s)"
                )
            self._sleep(1.0)

    def disconnect(self, client: str, *, timeout_s: float = 20.0) -> VpnClientStatus:
        normalized = normalize_vpn_client(client)
        self._ensure_root()
        self._require_installed(normalized)

        if normalized == KOALA_CLIENT:
            return self._disconnect_koala(timeout_s=timeout_s)

        st = self._collect_status(HAPP_CLIENT)
        if st.vpn_active:
            self._run_shell(
                [
                    "am",
                    "broadcast",
                    "-a",
                    HAPP_ACTION_WIDGET_CLICK,
                    "-n",
                    HAPP_WIDGET_RECEIVER,
                ],
                check=False,
            )
            self._sleep(1.0)
        self._run_shell(["am", "force-stop", HAPP_PACKAGE], check=False)

        deadline = self._monotonic() + timeout_s
        while True:
            st = self._collect_status(HAPP_CLIENT)
            if not st.vpn_active and not self._is_tun0_present():
                return st
            if self._monotonic() >= deadline:
                raise StandTimeoutError(f"Таймаут отключения `tun0` в Happ ({timeout_s}s)")
            self._sleep(1.0)

    def status(self, client: str) -> VpnClientStatus:
        normalized = normalize_vpn_client(client)
        self._ensure_root()
        return self._collect_status(normalized)
