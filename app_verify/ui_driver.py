"""Интеграция готового UI-драйвера (@mobilenext/mobile-mcp) поверх проброшенного ADB."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .android import find_local_adb, parse_adb_devices
from .core import StandError

DEFAULT_MOBILE_MCP_PACKAGE = "@mobilenext/mobile-mcp@1.0.20"
DEFAULT_DEVICE_ID = "127.0.0.1:15565"
MCP_PROTOCOL_VERSION = "2024-11-05"


@dataclass(frozen=True)
class McpToolResult:
    content: list[dict[str, Any]]
    text: str
    images: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class UiDriverStatus:
    driver: str
    ready: bool
    adb_path: str | None
    android_home: str | None
    command: list[str]
    device_id: str
    device_online: bool | None = None
    devices: list[dict[str, Any]] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def derive_android_sdk_root(adb_path: str | Path) -> str | None:
    """Выводит корень Android SDK из пути вида `<sdk>/platform-tools/adb`."""
    resolved = Path(adb_path)
    parent = resolved.parent
    if parent.name == "platform-tools":
        return str(parent.parent)
    return None


def build_ui_driver_env(
    *,
    adb_bin: str | None = None,
    device_id: str | None = DEFAULT_DEVICE_ID,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Собирает окружение для `@mobilenext/mobile-mcp` с гарантированным `adb` и `ANDROID_HOME`."""
    environ = dict(os.environ if base_env is None else base_env)
    adb_path = Path(find_local_adb(adb_bin, env=environ))
    adb_dir = str(adb_path.parent)

    current_path = environ.get("PATH", "")
    path_parts = [p for p in current_path.split(":") if p]
    if adb_dir not in path_parts:
        environ["PATH"] = f"{adb_dir}:{current_path}" if current_path else adb_dir

    sdk_root = derive_android_sdk_root(adb_path)
    if sdk_root:
        environ.setdefault("ANDROID_HOME", sdk_root)
        environ.setdefault("ANDROID_SDK_ROOT", sdk_root)

    environ.setdefault("MOBILEMCP_DISABLE_TELEMETRY", "1")
    environ.setdefault("DO_NOT_TRACK", "1")
    if device_id:
        environ["MOBILECLI_DEVICE"] = device_id

    return environ


def resolve_ui_driver_command(
    *,
    package: str = DEFAULT_MOBILE_MCP_PACKAGE,
    which_fn: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    """Возвращает базовую команду запуска `@mobilenext/mobile-mcp`."""
    direct = which_fn("mcp-server-mobile")
    if direct:
        return [direct]

    npx_bin = which_fn("npx")
    if npx_bin:
        return [npx_bin, "--yes", package]

    raise StandError(
        "Не найден `mcp-server-mobile` или `npx` для запуска @mobilenext/mobile-mcp. "
        "Установите Node.js/npm или пакет `@mobilenext/mobile-mcp`."
    )


def resolve_mobilecli_command(
    *,
    package: str = DEFAULT_MOBILE_MCP_PACKAGE,
    which_fn: Callable[[str], str | None] = shutil.which,
) -> list[str] | None:
    """Возвращает команду запуска `mobilecli` (напрямую или через `npx -p <package>`)."""
    direct = which_fn("mobilecli")
    if direct:
        return [direct]

    npx_bin = which_fn("npx")
    if npx_bin:
        return [npx_bin, "--yes", "-p", package, "mobilecli"]

    return None


def stop_mobilecli_daemon(
    *,
    package: str = DEFAULT_MOBILE_MCP_PACKAGE,
    env: Mapping[str, str] | None = None,
    which_fn: Callable[[str], str | None] = shutil.which,
    run_fn: Callable[..., Any] = subprocess.run,
) -> bool:
    """Останавливает фоновый демон `mobilecli`, если он закэшировал пустой список устройств."""
    cmd = resolve_mobilecli_command(package=package, which_fn=which_fn)
    if not cmd:
        return False
    try:
        res = run_fn(
            [*cmd, "daemon", "stop"],
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _is_adb_device_online(
    adb_path: str,
    device_id: str,
    *,
    env: Mapping[str, str] | None = None,
    run_fn: Callable[..., Any] = subprocess.run,
) -> bool:
    """Проверяет, видит ли `adb devices` целевой serial в состоянии `device`."""
    try:
        res = run_fn(
            [adb_path, "devices"],
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if res.returncode != 0:
        return False
    return parse_adb_devices(res.stdout).get(device_id) == "device"


def build_mcp_server_config(
    *,
    adb_bin: str | None = None,
    device_id: str | None = DEFAULT_DEVICE_ID,
    package: str = DEFAULT_MOBILE_MCP_PACKAGE,
    base_env: Mapping[str, str] | None = None,
    which_fn: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    """Формирует фрагмент MCP-конфигурации (`mcpServers`) для подключения агента к стенду."""
    cmd = resolve_ui_driver_command(package=package, which_fn=which_fn)
    env = build_ui_driver_env(adb_bin=adb_bin, device_id=device_id, base_env=base_env)

    config_env = {
        key: env[key]
        for key in (
            "PATH",
            "ANDROID_HOME",
            "ANDROID_SDK_ROOT",
            "MOBILEMCP_DISABLE_TELEMETRY",
            "DO_NOT_TRACK",
            "MOBILECLI_DEVICE",
        )
        if key in env
    }

    return {
        "mcpServers": {
            "mobile-mcp": {
                "command": cmd[0],
                "args": [*cmd[1:], "--stdio"],
                "env": config_env,
            }
        }
    }


class MobileMcpClient:
    """Минимальный JSON-RPC 2.0 stdio-клиент для `@mobilenext/mobile-mcp`."""

    def __init__(
        self,
        command: list[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._command = list(command)
        self._env = dict(env) if env is not None else None
        self._timeout_s = timeout_s
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._initialized = False

    def __enter__(self) -> MobileMcpClient:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def start(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self._env,
            )
        except FileNotFoundError as exc:
            raise StandError(f"Не удалось запустить UI-драйвер: {self._command[0]} не найден") from exc

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe and not pipe.closed:
                    pipe.close()
            except OSError:
                pass

    def _send(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise StandError("MCP-процесс не запущен")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()

    def _read_message(self, deadline: float) -> dict[str, Any]:
        if self._proc is None or self._proc.stdout is None:
            raise StandError("MCP-процесс не запущен")
        sel = selectors.DefaultSelector()
        sel.register(self._proc.stdout, selectors.EVENT_READ)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StandError(f"Таймаут ожидания ответа от UI-драйвера ({self._timeout_s}s)")
                events = sel.select(timeout=remaining)
                if not events:
                    raise StandError(f"Таймаут ожидания ответа от UI-драйвера ({self._timeout_s}s)")
                line = self._proc.stdout.readline()
                if line == "":
                    stderr_tail = ""
                    if self._proc.stderr is not None:
                        try:
                            stderr_tail = self._proc.stderr.read().strip()
                        except OSError:
                            pass
                    raise StandError(
                        f"UI-драйвер неожиданно завершил работу (rc={self._proc.poll()}): {stderr_tail}"
                    )
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    continue
        finally:
            sel.close()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._proc is None:
            self.start()
        req_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

        deadline = time.monotonic() + self._timeout_s
        while True:
            msg = self._read_message(deadline)
            if msg.get("id") != req_id:
                continue
            if "error" in msg and msg["error"] is not None:
                err = msg["error"]
                raise StandError(f"MCP error ({method}): {err.get('message', err)}")
            return msg.get("result", {})

    def initialize(self) -> dict[str, Any]:
        if self._initialized:
            return {}
        res = self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "app-verify", "version": "1.0.0"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True
        return res

    def list_tools(self) -> list[str]:
        self.initialize()
        res = self.request("tools/list", {})
        return [t["name"] for t in res.get("tools", []) if isinstance(t, dict) and "name" in t]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> McpToolResult:
        self.initialize()
        res = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        content = res.get("content", [])
        texts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        images = [
            block
            for block in content
            if isinstance(block, dict) and block.get("type") == "image"
        ]
        joined_text = "\n".join(t for t in texts if t)
        if res.get("isError"):
            raise StandError(joined_text or f"Инструмент {name} вернул ошибку")
        return McpToolResult(content=content, text=joined_text, images=images)


def _extract_online_devices(dev_res: McpToolResult, device_id: str) -> tuple[list[dict[str, Any]], bool]:
    parsed = json.loads(dev_res.text) if dev_res.text else {}
    devices = parsed.get("devices", []) if isinstance(parsed, dict) else []
    online = any(
        isinstance(d, dict) and d.get("id") == device_id and d.get("state") == "online"
        for d in devices
    )
    return devices, online


def check_ui_driver(
    *,
    adb_bin: str | None = None,
    device_id: str = DEFAULT_DEVICE_ID,
    package: str = DEFAULT_MOBILE_MCP_PACKAGE,
    probe_mcp: bool = False,
    require_device: bool = False,
    base_env: Mapping[str, str] | None = None,
    command_override: list[str] | None = None,
    which_fn: Callable[[str], str | None] = shutil.which,
    timeout_s: float = 30.0,
    adb_online_fn: Callable[[str, str, Mapping[str, str] | None], bool] | None = None,
    stop_daemon_fn: Callable[[str, Mapping[str, str] | None], bool] | None = None,
) -> UiDriverStatus:
    """Проверяет доступность UI-драйвера (`adb`, Node/npx, при `probe_mcp=True` — MCP-рукопожатие)."""
    try:
        env = build_ui_driver_env(adb_bin=adb_bin, device_id=device_id, base_env=base_env)
        resolved_adb = find_local_adb(adb_bin, env=env, which_fn=which_fn)
        cmd = (
            list(command_override)
            if command_override is not None
            else [*resolve_ui_driver_command(package=package, which_fn=which_fn), "--stdio"]
        )
        node_bin = which_fn("node")
        if command_override is None and not node_bin:
            raise StandError("Не найден `node` в PATH")
    except StandError as exc:
        return UiDriverStatus(
            driver="mobile-mcp",
            ready=False,
            adb_path=adb_bin,
            android_home=None,
            command=list(command_override or []),
            device_id=device_id,
            error=str(exc),
        )

    android_home = env.get("ANDROID_HOME")
    if not probe_mcp:
        return UiDriverStatus(
            driver="mobile-mcp",
            ready=True,
            adb_path=resolved_adb,
            android_home=android_home,
            command=cmd,
            device_id=device_id,
        )

    is_adb_online = adb_online_fn or (
        lambda adb_p, dev_id, e: _is_adb_device_online(adb_p, dev_id, env=e)
    )
    stop_daemon = stop_daemon_fn or (
        lambda pkg, e: stop_mobilecli_daemon(package=pkg, env=e, which_fn=which_fn)
    )

    try:
        with MobileMcpClient(cmd, env=env, timeout_s=timeout_s) as client:
            client.initialize()
            tools = client.list_tools()
            dev_res = client.call_tool("mobile_list_available_devices", {})
            devices, online = _extract_online_devices(dev_res, device_id)
            if not online and (command_override is None or adb_online_fn is not None):
                if is_adb_online(resolved_adb, device_id, env) and stop_daemon(package, env):
                    dev_res = client.call_tool("mobile_list_available_devices", {})
                    devices, online = _extract_online_devices(dev_res, device_id)
            ready = online if require_device else True
            err_msg = (
                None
                if ready
                else f"Устройство {device_id} не найдено среди online-устройств mobile-mcp"
            )
            return UiDriverStatus(
                driver="mobile-mcp",
                ready=ready,
                adb_path=resolved_adb,
                android_home=android_home,
                command=cmd,
                device_id=device_id,
                device_online=online,
                devices=devices,
                tools=tools,
                error=err_msg,
            )
    except (StandError, json.JSONDecodeError, OSError) as exc:
        return UiDriverStatus(
            driver="mobile-mcp",
            ready=False,
            adb_path=resolved_adb,
            android_home=android_home,
            command=cmd,
            device_id=device_id,
            error=str(exc),
        )


def build_ui_driver_serve_spec(
    *,
    adb_bin: str | None = None,
    device_id: str | None = DEFAULT_DEVICE_ID,
    package: str = DEFAULT_MOBILE_MCP_PACKAGE,
    listen: str | None = None,
    base_env: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Возвращает `(argv, env)` для запуска `@mobilenext/mobile-mcp` в режиме stdio или HTTP."""
    env = build_ui_driver_env(adb_bin=adb_bin, device_id=device_id, base_env=base_env)
    cmd = resolve_ui_driver_command(package=package)
    if listen:
        cmd.extend(["--listen", listen])
    else:
        cmd.append("--stdio")
    return cmd, env


def call_ui_driver_tool(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    adb_bin: str | None = None,
    device_id: str | None = DEFAULT_DEVICE_ID,
    package: str = DEFAULT_MOBILE_MCP_PACKAGE,
    auto_device: bool = True,
    command_override: list[str] | None = None,
    timeout_s: float = 30.0,
    adb_online_fn: Callable[[str, str, Mapping[str, str] | None], bool] | None = None,
    stop_daemon_fn: Callable[[str, Mapping[str, str] | None], bool] | None = None,
) -> McpToolResult:
    """Выполняет одиночный MCP tool call через `@mobilenext/mobile-mcp`."""
    env = build_ui_driver_env(adb_bin=adb_bin, device_id=device_id)
    resolved_adb = find_local_adb(adb_bin, env=env)
    cmd = (
        list(command_override)
        if command_override is not None
        else [*resolve_ui_driver_command(package=package), "--stdio"]
    )
    args = dict(arguments or {})
    no_device_tools = {
        "mobile_list_available_devices",
        "mobile_list_remote_devices",
        "mobile_allocate_remote_device",
        "mobile_login_to_cloud_provider",
    }
    if auto_device and device_id and "device" not in args and tool_name not in no_device_tools:
        args["device"] = device_id

    is_adb_online = adb_online_fn or (
        lambda adb_p, dev_id, e: _is_adb_device_online(adb_p, dev_id, env=e)
    )
    stop_daemon = stop_daemon_fn or (
        lambda pkg, e: stop_mobilecli_daemon(package=pkg, env=e)
    )

    with MobileMcpClient(cmd, env=env, timeout_s=timeout_s) as client:
        try:
            return client.call_tool(tool_name, args)
        except StandError as exc:
            if (
                device_id
                and "not found" in str(exc).lower()
                and (command_override is None or adb_online_fn is not None)
                and is_adb_online(resolved_adb, device_id, env)
                and stop_daemon(package, env)
            ):
                return client.call_tool(tool_name, args)
            raise
