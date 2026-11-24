"""Платформенный адаптер Android: headless KVM Emulator на `ssh home` (HDD) + SSH-туннель ADB."""

from __future__ import annotations

import os
import posixpath
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .core import (
    ArtifactRecord,
    InstallResult,
    LaunchResult,
    LogReport,
    PlatformAdapter,
    StandError,
    StandStatus,
)
from .logcat import analyze_logcat

ALLOWED_HDD_ROOT = "/mnt/data/android"
DEFAULT_ADB_CANDIDATES = ("/opt/android-sdk/platform-tools/adb",)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ANIMATION_SETTINGS = (
    "window_animation_scale",
    "transition_animation_scale",
    "animator_duration_scale",
)


class HddInvariantError(StandError):
    """Нарушен инвариант хранения всех Android-артефактов строго на HDD `/mnt/data/android/`."""


class StandTimeoutError(StandError):
    """Операция стенда превысила допустимый таймаут."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


class CommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
        input_bytes: bytes | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        ...


class SubprocessRunner:
    """Стандартный исполнитель системных команд с детерминированной обработкой таймаута."""

    def run(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
        input_bytes: bytes | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        try:
            proc = subprocess.run(
                args,
                input=input_bytes,
                capture_output=True,
                timeout=timeout,
                env=dict(env) if env is not None else None,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            cmd_preview = " ".join(args[:6])
            raise StandTimeoutError(
                f"Команда превысила таймаут {timeout}s: {cmd_preview}"
            ) from exc
        except FileNotFoundError as exc:
            raise StandError(f"Исполняемый файл не найден: {args[0]}") from exc
        return CommandResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def validate_hdd_root(path: str) -> str:
    """Проверяет, что путь строго находится внутри HDD-корня `/mnt/data/android`."""
    if not path or not path.startswith("/"):
        raise HddInvariantError(
            f"Путь Android-стенда обязан быть абсолютным под {ALLOWED_HDD_ROOT}, получено: {path!r}"
        )
    if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-" for ch in path):
        raise HddInvariantError(
            f"Путь Android-стенда содержит недопустимые символы: {path!r}"
        )
    parts = [p for p in path.split("/") if p]
    if ".." in parts:
        raise HddInvariantError(
            f"Запрещён выход из {ALLOWED_HDD_ROOT} через '..': {path!r}"
        )
    normalized = posixpath.normpath(path)
    if normalized != ALLOWED_HDD_ROOT and not normalized.startswith(ALLOWED_HDD_ROOT + "/"):
        raise HddInvariantError(
            f"Запись вне HDD {ALLOWED_HDD_ROOT} запрещена (SSD защищён): {path!r}"
        )
    return normalized


def build_remote_env(
    remote_root: str = ALLOWED_HDD_ROOT,
    *,
    remote_adb_server_port: int = 15037,
) -> dict[str, str]:
    """Формирует полное окружение для `ssh home`, прибивая все SDK/AVD/кэши/tmp к HDD."""
    root = validate_hdd_root(remote_root)
    path_vars = {
        "HOME": posixpath.join(root, "home"),
        "ANDROID_HOME": posixpath.join(root, "sdk"),
        "ANDROID_SDK_ROOT": posixpath.join(root, "sdk"),
        "ANDROID_USER_HOME": posixpath.join(root, "user-home"),
        "ANDROID_EMULATOR_HOME": posixpath.join(root, "user-home"),
        "ANDROID_PREFS_ROOT": posixpath.join(root, "user-home"),
        "ANDROID_SDK_HOME": posixpath.join(root, "user-home"),
        "ANDROID_AVD_HOME": posixpath.join(root, "avd"),
        "TMPDIR": posixpath.join(root, "tmp"),
        "TEMP": posixpath.join(root, "tmp"),
        "TMP": posixpath.join(root, "tmp"),
        "ANDROID_TMP": posixpath.join(root, "tmp"),
        "XDG_CACHE_HOME": posixpath.join(root, "cache"),
        "XDG_CONFIG_HOME": posixpath.join(root, "config"),
        "XDG_DATA_HOME": posixpath.join(root, "user-home"),
        "XDG_RUNTIME_DIR": posixpath.join(root, "run"),
    }
    for key, value in path_vars.items():
        validate_hdd_root(value)

    env = dict(path_vars)
    env["ANDROID_ADB_SERVER_PORT"] = str(remote_adb_server_port)
    env["JAVA_TOOL_OPTIONS"] = (
        f"-Djava.io.tmpdir={path_vars['TMPDIR']} -Duser.home={path_vars['HOME']}"
    )
    return env


def _is_executable(path: str | Path) -> bool:
    p = Path(path)
    return p.is_file() and os.access(p, os.X_OK)


def find_local_adb(
    explicit: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    which_fn: Callable[[str], str | None] = shutil.which,
    default_candidates: tuple[str, ...] | list[str] = DEFAULT_ADB_CANDIDATES,
) -> str:
    """Находит локальный бинарник `adb` (флаг -> ADB_BIN -> PATH -> /opt/android-sdk -> ANDROID_HOME)."""
    environ = os.environ if env is None else env

    if explicit:
        if _is_executable(explicit):
            return str(explicit)
        raise StandError(f"Указанный --adb-bin недоступен или не исполняем: {explicit}")

    adb_env = environ.get("ADB_BIN")
    if adb_env and _is_executable(adb_env):
        return adb_env

    from_path = which_fn("adb")
    if from_path:
        return from_path

    candidates: list[Path] = [Path(c) for c in default_candidates]
    for sdk_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk_val = environ.get(sdk_var)
        if sdk_val:
            candidates.append(Path(sdk_val) / "platform-tools" / "adb")

    for cand in candidates:
        if _is_executable(cand):
            return str(cand)

    raise StandError(
        "Не найден локальный `adb`. Укажите ADB_BIN, добавьте `adb` в PATH "
        "или установите platform-tools в /opt/android-sdk/platform-tools/adb."
    )


def parse_adb_devices(output: str) -> dict[str, str]:
    """Парсит вывод `adb devices` в словарь `{serial: state}`."""
    devices: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices[parts[0]] = parts[1]
    return devices


@dataclass(frozen=True)
class AndroidStandConfig:
    ssh_host: str = "home"
    remote_root: str = ALLOWED_HDD_ROOT
    avd_name: str = "verify-api34"
    system_image: str = "system-images;android-34;google_apis;x86_64"
    snapshot_name: str = "clean-base"
    console_port: int = 5564
    local_adb_port: int = 15565
    remote_adb_server_port: int = 15037
    gpu_mode: str = "swiftshader_indirect"
    memory_mb: int = 4096
    cores: int = 4
    boot_timeout_s: float = 180.0
    adb_timeout_s: float = 30.0
    adb_bin: str | None = None
    ssh_control_dir: Path = field(default_factory=lambda: Path.home() / ".cache" / "app-verify")

    def __post_init__(self) -> None:
        validate_hdd_root(self.remote_root)
        if not self.ssh_host or self.ssh_host.startswith("-"):
            raise StandError(f"Некорректный ssh_host: {self.ssh_host!r}")
        if self.console_port % 2 != 0 or self.console_port < 5554:
            raise StandError(
                f"console_port эмулятора должен быть чётным >= 5554, получено: {self.console_port}"
            )
        if not (1024 <= self.local_adb_port <= 65535):
            raise StandError(f"Некорректный local_adb_port: {self.local_adb_port}")
        if not (1024 <= self.remote_adb_server_port <= 65535):
            raise StandError(f"Некорректный remote_adb_server_port: {self.remote_adb_server_port}")

    @property
    def remote_adb_port(self) -> int:
        return self.console_port + 1

    @property
    def remote_serial(self) -> str:
        return f"emulator-{self.console_port}"

    @property
    def local_serial(self) -> str:
        return f"127.0.0.1:{self.local_adb_port}"

    @property
    def control_socket(self) -> Path:
        safe_host = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in self.ssh_host)
        return self.ssh_control_dir / f"ssh-{safe_host}-{self.local_adb_port}.sock"


class AndroidKvmAdapter(PlatformAdapter):
    """Адаптер стенда Android на базе headless KVM Emulator на `ssh home` + SSH-туннеля ADB."""

    platform = "android"

    def __init__(
        self,
        config: AndroidStandConfig | None = None,
        *,
        runner: CommandRunner | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or AndroidStandConfig()
        self.runner: CommandRunner = runner or SubprocessRunner()
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._adb_path: str | None = None

    @property
    def remote_env(self) -> dict[str, str]:
        return build_remote_env(
            self.config.remote_root,
            remote_adb_server_port=self.config.remote_adb_server_port,
        )

    def _resolve_adb(self) -> str:
        if self._adb_path is None:
            self._adb_path = find_local_adb(self.config.adb_bin)
        return self._adb_path

    def build_ssh_command(self) -> list[str]:
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "--",
            self.config.ssh_host,
            "bash",
            "-s",
        ]

    def build_remote_script(self, body: str) -> str:
        env = self.remote_env
        exports = "\n".join(f"export {k}={shlex.quote(v)}" for k, v in env.items())
        root_q = shlex.quote(self.config.remote_root)
        # Оборачиваем тело в функцию и вызываем с `</dev/null`, чтобы `adb shell` не съел stdin скрипта `bash -s`.
        # Точку монтирования проверяем строго ДО `mkdir -p`, чтобы при отмонтированном HDD ничего не записалось на SSD `/`.
        guard = (
            "__app_verify_main() {\n"
            "set -euo pipefail\n"
            f"{exports}\n"
            f"CHECK_PATH={root_q}\n"
            'while [ ! -e "$CHECK_PATH" ] && [ "$CHECK_PATH" != "/" ]; do\n'
            '  CHECK_PATH=$(dirname "$CHECK_PATH")\n'
            "done\n"
            'MOUNT_TGT=$(df --output=target "$CHECK_PATH" | tail -n 1 | xargs)\n'
            'if [ "$MOUNT_TGT" = "/" ]; then\n'
            f"  printf 'HDD_INVARIANT_VIOLATION: %s resides on root SSD /\\n' {root_q} >&2\n"
            "  exit 42\n"
            "fi\n"
            f'mkdir -p "$ANDROID_HOME" "$ANDROID_USER_HOME" "$ANDROID_AVD_HOME" '
            f'"$TMPDIR" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$HOME" "$XDG_RUNTIME_DIR" '
            f"{shlex.quote(posixpath.join(self.config.remote_root, 'logs'))}\n"
            'chmod 700 "$XDG_RUNTIME_DIR"\n'
        )
        return guard + body.lstrip("\n") + "\n}\n__app_verify_main </dev/null\n"

    def _run_remote(self, body: str, *, timeout: float | None = None) -> CommandResult:
        script = self.build_remote_script(body)
        return self.runner.run(
            self.build_ssh_command(),
            timeout=timeout or self.config.adb_timeout_s,
            input_bytes=script.encode("utf-8"),
        )

    def _run_local_adb(
        self,
        args: list[str],
        *,
        with_serial: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        cmd = [self._resolve_adb()]
        if with_serial:
            cmd.extend(["-s", self.config.local_serial])
        cmd.extend(args)
        return self.runner.run(cmd, timeout=timeout or self.config.adb_timeout_s)

    def build_tunnel_start_command(self) -> list[str]:
        return [
            "ssh",
            "-f",
            "-N",
            "-M",
            "-S",
            str(self.config.control_socket),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-L",
            f"127.0.0.1:{self.config.local_adb_port}:127.0.0.1:{self.config.remote_adb_port}",
            "--",
            self.config.ssh_host,
        ]

    def build_tunnel_control_command(self, control_op: str) -> list[str]:
        return [
            "ssh",
            "-S",
            str(self.config.control_socket),
            "-O",
            control_op,
            "--",
            self.config.ssh_host,
        ]

    def _is_tunnel_alive(self) -> bool:
        res = self.runner.run(self.build_tunnel_control_command("check"), timeout=10.0)
        return res.returncode == 0

    def _ensure_tunnel(self) -> None:
        if self._is_tunnel_alive():
            return
        self.config.ssh_control_dir.mkdir(parents=True, exist_ok=True)
        self.config.control_socket.unlink(missing_ok=True)
        res = self.runner.run(self.build_tunnel_start_command(), timeout=15.0)
        if res.returncode != 0:
            raise StandError(
                f"Не удалось поднять SSH-туннель ADB ({self.config.local_adb_port} -> "
                f"{self.config.ssh_host}:{self.config.remote_adb_port}): {res.stderr_text.strip()}"
            )

    def _close_tunnel(self) -> None:
        try:
            self.runner.run(self.build_tunnel_control_command("exit"), timeout=10.0)
        finally:
            self.config.control_socket.unlink(missing_ok=True)

    def _connect_local_adb(self) -> None:
        deadline = self._monotonic() + self.config.adb_timeout_s
        last_state = "missing"
        last_err = ""
        while True:
            conn = self._run_local_adb(["connect", self.config.local_serial], with_serial=False)
            if conn.returncode != 0:
                last_err = conn.stderr_text.strip() or conn.stdout_text.strip()
            devs_res = self._run_local_adb(["devices"], with_serial=False)
            if devs_res.returncode == 0:
                devices = parse_adb_devices(devs_res.stdout_text)
                last_state = devices.get(self.config.local_serial, "missing")
                if last_state == "device":
                    return
            if self._monotonic() >= deadline:
                raise StandTimeoutError(
                    f"Локальный adb не увидел устройство {self.config.local_serial} в состоянии "
                    f"'device' (последнее состояние: {last_state}; {last_err})"
                )
            self._sleep(1.0)

    def prepare(self) -> StandStatus:
        """Проверяет состояние HDD-инварианта, SDK, AVD и базового снапшота на `ssh home`."""
        avd_q = shlex.quote(self.config.avd_name)
        snap_q = shlex.quote(self.config.snapshot_name)
        body = f"""
MOUNT_SRC=$(df --output=source {shlex.quote(self.config.remote_root)} | tail -n 1 | xargs)
MOUNT_TGT=$(df --output=target {shlex.quote(self.config.remote_root)} | tail -n 1 | xargs)
HAS_EMU=$([ -x "$ANDROID_HOME/emulator/emulator" ] && echo 1 || echo 0)
HAS_ADB=$([ -x "$ANDROID_HOME/platform-tools/adb" ] && echo 1 || echo 0)
HAS_SDKMGR=$([ -x "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" ] && echo 1 || echo 0)
HAS_AVD=$([ -d "$ANDROID_AVD_HOME/"{avd_q}".avd" ] && echo 1 || echo 0)
HAS_SNAP=$([ -d "$ANDROID_AVD_HOME/"{avd_q}".avd/snapshots/"{snap_q} ] && echo 1 || echo 0)
HAS_KVM=$([ -r /dev/kvm ] && [ -w /dev/kvm ] && echo 1 || echo 0)
echo "mount_source=$MOUNT_SRC"
echo "mount_target=$MOUNT_TGT"
echo "has_emulator=$HAS_EMU"
echo "has_adb=$HAS_ADB"
echo "has_sdkmanager=$HAS_SDKMGR"
echo "has_avd=$HAS_AVD"
echo "has_snapshot=$HAS_SNAP"
echo "has_kvm=$HAS_KVM"
"""
        res = self._run_remote(body)
        if res.returncode == 42:
            raise HddInvariantError(res.stderr_text.strip() or "Нарушен HDD-инвариант на удалённом хосте")
        if res.returncode != 0:
            raise StandError(f"Проверка стенда на {self.config.ssh_host} не удалась: {res.stderr_text.strip()}")
        info = _parse_kv_lines(res.stdout_text)
        ready = all(
            info.get(k) == "1"
            for k in ("has_emulator", "has_adb", "has_sdkmanager", "has_avd", "has_snapshot", "has_kvm")
        )
        return StandStatus(
            platform=self.platform,
            state="ready" if ready else "incomplete",
            target_id=self.config.local_serial,
            endpoint=f"{self.config.ssh_host}:{self.config.remote_adb_port} -> {self.config.local_serial}",
            snapshot=self.config.snapshot_name if info.get("has_snapshot") == "1" else None,
            details=info,
        )

    def start(self, snapshot: str | None = None, reset: bool = True) -> StandStatus:
        """Поднимает или откатывает эмулятор на `ssh home` к снапшоту и пробрасывает ADB на VPS."""
        snap = self.config.snapshot_name if snapshot is None else snapshot
        avd_q = shlex.quote(self.config.avd_name)
        snap_q = shlex.quote(snap)
        gpu_q = shlex.quote(self.config.gpu_mode)
        serial_q = shlex.quote(self.config.remote_serial)
        log_path = shlex.quote(posixpath.join(self.config.remote_root, "logs", f"emulator-{self.config.console_port}.log"))
        boot_timeout = int(self.config.boot_timeout_s)
        reset_flag = "1" if reset else "0"

        # Отключаем локальное adb-соединение перед перезапуском/откатом, чтобы избежать залипшего сокета
        self._run_local_adb(["disconnect", self.config.local_serial], with_serial=False)

        body = f"""
ADB="$ANDROID_HOME/platform-tools/adb"
EMU="$ANDROID_HOME/emulator/emulator"
SNAP_DIR="$ANDROID_AVD_HOME/"{avd_q}".avd/snapshots/"{snap_q}
HAS_SNAP=0
if [ -n {snap_q} ] && [ -d "$SNAP_DIR" ]; then
  HAS_SNAP=1
fi

is_running() {{
  "$ADB" -s {serial_q} get-state 2>/dev/null | grep -qx "device"
}}

wait_boot() {{
  local deadline=$(( $(date +%s) + {boot_timeout} ))
  "$ADB" -s {serial_q} wait-for-device >/dev/null 2>&1 || true
  while [ "$(date +%s)" -lt "$deadline" ]; do
    local bc
    bc=$("$ADB" -s {serial_q} shell getprop sys.boot_completed 2>/dev/null | tr -d '\\r\\n' || true)
    if [ "$bc" = "1" ]; then
      return 0
    fi
    sleep 1
  done
  echo "BOOT_TIMEOUT" >&2
  return 1
}}

RESTORED="none"
if [ "{reset_flag}" = "1" ] && [ -n {snap_q} ] && [ "$HAS_SNAP" != "1" ]; then
  printf 'SNAPSHOT_NOT_FOUND: %s\n' {snap_q} >&2
  exit 43
fi

if is_running; then
  if [ "{reset_flag}" = "1" ] && [ "$HAS_SNAP" = "1" ]; then
    SNAP_LOAD_RC=0
    SNAP_LOAD_OUT=$("$ADB" -s {serial_q} emu avd snapshot load {snap_q} 2>&1) || SNAP_LOAD_RC=$?
    SNAP_LOAD_CLEAN="${{SNAP_LOAD_OUT//$'\r'/}}"
    if grep -q "^KO:" <<< "$SNAP_LOAD_CLEAN"; then
      printf 'SNAPSHOT_LOAD_FAILED: %s\n' "$SNAP_LOAD_OUT" >&2
      exit 44
    fi
    if [ "$SNAP_LOAD_RC" -eq 0 ] && grep -qx "OK" <<< "$SNAP_LOAD_CLEAN" && wait_boot; then
      RESTORED="snapshot-load"
    else
      "$ADB" -s {serial_q} emu kill >/dev/null 2>&1 || true
      sleep 2
    fi
  else
    wait_boot
    RESTORED="reused"
  fi
fi

if [ "$RESTORED" = "none" ]; then
  pkill -f "qemu-system.*-port {self.config.console_port}([[:space:]]|$)" 2>/dev/null || true
  sleep 1
  SNAP_ARGS=("-no-snapshot-save")
  if [ "$HAS_SNAP" = "1" ]; then
    SNAP_ARGS+=("-snapshot" {snap_q})
    RESTORED="boot-snapshot"
  else
    SNAP_ARGS+=("-no-snapshot-load")
    RESTORED="cold-boot"
  fi
  nohup "$EMU" -avd {avd_q} -port {self.config.console_port} \
    -no-window -no-audio -no-boot-anim -no-metrics \
    -gpu {gpu_q} -accel on -memory {self.config.memory_mb} -cores {self.config.cores} \
    "${{SNAP_ARGS[@]}}" </dev/null >{log_path} 2>&1 &
  wait_boot
  if [ "$RESTORED" = "cold-boot" ]; then
    "$ADB" -s {serial_q} shell settings put global window_animation_scale 0 >/dev/null 2>&1 || true
    "$ADB" -s {serial_q} shell settings put global transition_animation_scale 0 >/dev/null 2>&1 || true
    "$ADB" -s {serial_q} shell settings put global animator_duration_scale 0 >/dev/null 2>&1 || true
  fi
fi

echo "mode=$RESTORED"
if [ "$RESTORED" = "snapshot-load" ] || [ "$RESTORED" = "boot-snapshot" ]; then
  echo "snapshot_used="{snap_q}
else
  echo "snapshot_used=none"
fi
"""
        res = self._run_remote(body, timeout=self.config.boot_timeout_s + 30.0)
        if res.returncode == 42:
            raise HddInvariantError(res.stderr_text.strip() or "Нарушен HDD-инвариант на удалённом хосте")
        if res.returncode != 0:
            raise StandError(
                f"Не удалось запустить эмулятор на {self.config.ssh_host}: "
                f"{res.stderr_text.strip() or res.stdout_text.strip()}"
            )
        remote_info = _parse_kv_lines(res.stdout_text)

        self._ensure_tunnel()
        self._connect_local_adb()

        used_snap = remote_info.get("snapshot_used")
        return StandStatus(
            platform=self.platform,
            state="running",
            target_id=self.config.local_serial,
            endpoint=f"{self.config.ssh_host}:{self.config.remote_adb_port} -> {self.config.local_serial}",
            snapshot=None if used_snap in (None, "", "none") else used_snap,
            details={
                "mode": remote_info.get("mode", "unknown"),
                "remote_root": self.config.remote_root,
                "avd": self.config.avd_name,
            },
        )

    def stop(self) -> StandStatus:
        """Отключает локальный ADB, закрывает SSH-туннель и останавливает эмулятор на `ssh home`."""
        local_warnings: list[str] = []
        try:
            self._run_local_adb(["disconnect", self.config.local_serial], with_serial=False)
        except StandError as exc:
            local_warnings.append(f"adb disconnect: {exc}")
        try:
            self._close_tunnel()
        except StandError as exc:
            local_warnings.append(f"ssh tunnel exit: {exc}")

        serial_q = shlex.quote(self.config.remote_serial)
        body = f"""
ADB="$ANDROID_HOME/platform-tools/adb"
if [ -x "$ADB" ]; then
  "$ADB" -s {serial_q} emu kill >/dev/null 2>&1 || true
fi
for _ in $(seq 1 15); do
  if ! pgrep -f "qemu-system.*-port {self.config.console_port}([[:space:]]|$)" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
pkill -f "qemu-system.*-port {self.config.console_port}([[:space:]]|$)" 2>/dev/null || true
if [ -x "$ADB" ]; then
  "$ADB" kill-server >/dev/null 2>&1 || true
fi
echo "stopped=1"
"""
        res = self._run_remote(body, timeout=40.0)
        if res.returncode == 42:
            raise HddInvariantError(res.stderr_text.strip() or "Нарушен HDD-инвариант на удалённом хосте")
        if res.returncode != 0:
            raise StandError(f"Ошибка остановки эмулятора на {self.config.ssh_host}: {res.stderr_text.strip()}")

        details: dict[str, Any] = {"remote_root": self.config.remote_root}
        if local_warnings:
            details["local_cleanup_warnings"] = local_warnings
        return StandStatus(
            platform=self.platform,
            state="stopped",
            target_id=self.config.local_serial,
            endpoint=f"{self.config.ssh_host}:{self.config.remote_adb_port} -> {self.config.local_serial}",
            snapshot=None,
            details=details,
        )

    def status(self) -> StandStatus:
        """Возвращает объединённый статус эмулятора, SSH-туннеля и локального `adb`."""
        serial_q = shlex.quote(self.config.remote_serial)
        body = f"""
ADB="$ANDROID_HOME/platform-tools/adb"
EMU_ALIVE=$(pgrep -f "qemu-system.*-port {self.config.console_port}([[:space:]]|$)" >/dev/null 2>&1 && echo 1 || echo 0)
REMOTE_STATE="stopped"
BOOT_COMPLETED="0"
if [ "$EMU_ALIVE" = "1" ] && [ -x "$ADB" ]; then
  REMOTE_STATE=$("$ADB" -s {serial_q} get-state 2>/dev/null | tr -d '\\r\\n' || echo "offline")
  if [ "$REMOTE_STATE" = "device" ]; then
    BOOT_COMPLETED=$("$ADB" -s {serial_q} shell getprop sys.boot_completed 2>/dev/null | tr -d '\\r\\n' || echo "0")
  fi
fi
echo "emu_alive=$EMU_ALIVE"
echo "remote_adb_state=$REMOTE_STATE"
echo "boot_completed=$BOOT_COMPLETED"
"""
        remote_res = self._run_remote(body)
        if remote_res.returncode == 42:
            raise HddInvariantError(
                remote_res.stderr_text.strip() or "Нарушен HDD-инвариант на удалённом хосте"
            )
        tunnel_alive = self._is_tunnel_alive()

        local_state = "disconnected"
        devs_res = self._run_local_adb(["devices"], with_serial=False)
        if devs_res.returncode == 0:
            local_state = parse_adb_devices(devs_res.stdout_text).get(
                self.config.local_serial, "disconnected"
            )

        if remote_res.returncode != 0:
            remote_error = remote_res.stderr_text.strip() or remote_res.stdout_text.strip()
            return StandStatus(
                platform=self.platform,
                state="error",
                target_id=self.config.local_serial,
                endpoint=f"{self.config.ssh_host}:{self.config.remote_adb_port} -> {self.config.local_serial}",
                snapshot=self.config.snapshot_name,
                details={
                    "remote_emu_alive": False,
                    "remote_adb_state": "error",
                    "boot_completed": False,
                    "tunnel_alive": tunnel_alive,
                    "local_adb_state": local_state,
                    "remote_root": self.config.remote_root,
                    "remote_returncode": remote_res.returncode,
                    "remote_error": remote_error,
                },
            )

        remote_info = _parse_kv_lines(remote_res.stdout_text)
        emu_alive = remote_info.get("emu_alive") == "1"
        booted = remote_info.get("boot_completed") == "1"
        if emu_alive and booted and tunnel_alive and local_state == "device":
            state = "running"
        elif not emu_alive and not tunnel_alive and local_state == "disconnected":
            state = "stopped"
        else:
            state = "degraded"

        return StandStatus(
            platform=self.platform,
            state=state,
            target_id=self.config.local_serial,
            endpoint=f"{self.config.ssh_host}:{self.config.remote_adb_port} -> {self.config.local_serial}",
            snapshot=self.config.snapshot_name,
            details={
                "remote_emu_alive": emu_alive,
                "remote_adb_state": remote_info.get("remote_adb_state", "unknown"),
                "boot_completed": booted,
                "tunnel_alive": tunnel_alive,
                "local_adb_state": local_state,
                "remote_root": self.config.remote_root,
            },
        )

    def install(self, artifact_path: Path, *, grant_permissions: bool = True) -> InstallResult:
        """Устанавливает debug APK через локальный проброшенный `adb`."""
        apk = Path(artifact_path)
        if not apk.is_file():
            raise StandError(f"APK-файл не найден: {apk}")
        cmd = ["install", "-r", "-d"]
        if grant_permissions:
            cmd.append("-g")
        cmd.append(str(apk))
        res = self._run_local_adb(cmd, timeout=max(self.config.adb_timeout_s, 120.0))
        combined = (res.stdout_text + "\n" + res.stderr_text).strip()
        if res.returncode != 0 or "Failure" in combined:
            raise StandError(f"Установка APK не удалась ({apk}): {combined}")
        return InstallResult(
            platform=self.platform,
            artifact=str(apk),
            target_id=self.config.local_serial,
            ok=True,
            details={"output": combined},
        )

    def launch(self, target: str, activity: str | None = None) -> LaunchResult:
        """Запускает установленное Android-приложение через `am start -W`."""
        if activity:
            component = activity if "/" in activity else f"{target}/{activity}"
        else:
            resolve_res = self._run_local_adb(
                [
                    "shell",
                    "cmd",
                    "package",
                    "resolve-activity",
                    "--brief",
                    "-c",
                    "android.intent.category.LAUNCHER",
                    shlex.quote(target),
                ]
            )
            if resolve_res.returncode != 0:
                raise StandError(
                    f"Не удалось определить launcher activity для {target}: "
                    f"{resolve_res.stderr_text.strip() or resolve_res.stdout_text.strip()}"
                )
            candidates = [
                ln.strip()
                for ln in resolve_res.stdout_text.splitlines()
                if "/" in ln.strip() and not ln.strip().startswith("priority=")
            ]
            if not candidates:
                raise StandError(
                    f"У пакета {target} не найдена LAUNCHER activity: {resolve_res.stdout_text.strip()}"
                )
            component = candidates[-1]

        res = self._run_local_adb(["shell", "am", "start", "-W", "-n", shlex.quote(component)])
        combined = (res.stdout_text + "\n" + res.stderr_text).strip()
        if res.returncode != 0 or "Error:" in combined or "Exception" in combined:
            raise StandError(f"Не удалось запустить {component}: {combined}")
        return LaunchResult(
            platform=self.platform,
            app_id=target,
            component=component,
            ok=True,
            details={"output": combined},
        )

    def screenshot(self, output_path: Path) -> ArtifactRecord:
        """Снимает PNG-скриншот через `adb exec-out screencap -p` и проверяет PNG-сигнатуру."""
        res = self._run_local_adb(["exec-out", "screencap", "-p"])
        if res.returncode != 0:
            raise StandError(f"Снятие скриншота не удалось: {res.stderr_text.strip()}")
        data = res.stdout
        if len(data) <= 24 or not data.startswith(PNG_SIGNATURE):
            raise StandError(
                f"Получен некорректный PNG от screencap (размер={len(data)} байт)"
            )
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return ArtifactRecord(
            kind="screenshot",
            path=str(out),
            size_bytes=len(data),
            valid=True,
            details={"format": "png", "target_id": self.config.local_serial},
        )

    def collect_logs(
        self,
        output_path: Path | None = None,
        clear: bool = False,
        package: str | None = None,
    ) -> LogReport:
        """Выгружает (или очищает) `logcat` и выделяет Java/Native crash и ANR."""
        if clear:
            res = self._run_local_adb(["logcat", "-c"])
            if res.returncode != 0:
                raise StandError(f"Не удалось очистить logcat: {res.stderr_text.strip()}")
            return LogReport(
                platform=self.platform,
                raw_text="",
                output_path=None,
                crashes=[],
                anrs=[],
                details={"cleared": True, "target_id": self.config.local_serial},
            )

        res = self._run_local_adb(["logcat", "-d", "-v", "threadtime"])
        if res.returncode != 0:
            raise StandError(f"Не удалось выгрузить logcat: {res.stderr_text.strip()}")
        raw_text = res.stdout_text
        saved_path: str | None = None
        if output_path is not None:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(raw_text, encoding="utf-8")
            saved_path = str(out)

        crashes, anrs = analyze_logcat(raw_text, package=package)
        return LogReport(
            platform=self.platform,
            raw_text=raw_text,
            output_path=saved_path,
            crashes=crashes,
            anrs=anrs,
            details={
                "line_count": len(raw_text.splitlines()),
                "package": package,
                "target_id": self.config.local_serial,
            },
        )

    def save_snapshot(self, name: str | None = None) -> StandStatus:
        """Отключает анимации и сохраняет чистый снапшот запущенного эмулятора на HDD."""
        snap = name or self.config.snapshot_name
        snap_q = shlex.quote(snap)
        serial_q = shlex.quote(self.config.remote_serial)
        anim_cmds = "\n".join(
            f'"$ADB" -s {serial_q} shell settings put global {key} 0'
            for key in ANIMATION_SETTINGS
        )
        body = f"""
ADB="$ANDROID_HOME/platform-tools/adb"
{anim_cmds}
SNAP_SAVE_RC=0
SNAP_SAVE_OUT=$("$ADB" -s {serial_q} emu avd snapshot save {snap_q} 2>&1) || SNAP_SAVE_RC=$?
SNAP_SAVE_CLEAN="${{SNAP_SAVE_OUT//$'\r'/}}"
printf '%s\n' "$SNAP_SAVE_OUT"
if [ "$SNAP_SAVE_RC" -ne 0 ] || grep -q "^KO:" <<< "$SNAP_SAVE_CLEAN" || ! grep -qx "OK" <<< "$SNAP_SAVE_CLEAN"; then
  printf 'SNAPSHOT_SAVE_FAILED: %s\n' "$SNAP_SAVE_OUT" >&2
  exit 45
fi
"""
        res = self._run_remote(body, timeout=60.0)
        if res.returncode == 42:
            raise HddInvariantError(res.stderr_text.strip() or "Нарушен HDD-инвариант на удалённом хосте")
        if res.returncode != 0:
            raise StandError(
                f"Не удалось сохранить снапшот {snap}: {res.stderr_text.strip() or res.stdout_text.strip()}"
            )
        if not _is_adb_emu_ok(res.stdout_text):
            raise StandError(
                f"Эмулятор отклонил сохранение снапшота {snap}: "
                f"{res.stdout_text.strip() or res.stderr_text.strip() or 'отсутствует OK в ответе adb emu'}"
            )
        return StandStatus(
            platform=self.platform,
            state="snapshot-saved",
            target_id=self.config.local_serial,
            endpoint=f"{self.config.ssh_host}:{self.config.remote_adb_port}",
            snapshot=snap,
            details={"output": res.stdout_text.strip()},
        )


def _is_adb_emu_ok(output: str) -> bool:
    """Проверяет stdout `adb emu`: должна присутствовать строка `OK` и отсутствовать `KO:`."""
    lines = [line.strip() for line in output.replace("\r", "").splitlines() if line.strip()]
    has_ok = any(line == "OK" for line in lines)
    has_ko = any(line.startswith("KO:") for line in lines)
    return has_ok and not has_ko


def _parse_kv_lines(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out
