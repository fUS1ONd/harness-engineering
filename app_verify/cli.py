"""Платформо-независимый CLI для управления стендами агентской верификации."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from .android import ALLOWED_HDD_ROOT, AndroidKvmAdapter, AndroidStandConfig
from .core import AdapterRegistry, PlatformAdapter, StandError
from .ui_driver import (
    DEFAULT_MOBILE_MCP_PACKAGE,
    build_mcp_server_config,
    build_ui_driver_serve_spec,
    call_ui_driver_tool,
    check_ui_driver,
)


def build_default_registry() -> AdapterRegistry:
    """Создаёт реестр адаптеров по умолчанию (первый адаптер — Android KVM на ssh home)."""
    registry = AdapterRegistry()

    def _android_factory(**kwargs: Any) -> PlatformAdapter:
        cfg = AndroidStandConfig(
            ssh_host=kwargs.get("ssh_host", "home"),
            remote_root=kwargs.get("remote_root", ALLOWED_HDD_ROOT),
            avd_name=kwargs.get("avd", "verify-api34"),
            snapshot_name=kwargs.get("snapshot", "clean-base"),
            console_port=kwargs.get("console_port", 5564),
            local_adb_port=kwargs.get("local_adb_port", 15565),
            remote_adb_server_port=kwargs.get("remote_adb_server_port", 15037),
            adb_bin=kwargs.get("adb_bin"),
            gpu_mode=kwargs.get("gpu_mode", "swiftshader_indirect"),
            boot_timeout_s=kwargs.get("boot_timeout", 180.0),
            adb_timeout_s=kwargs.get("adb_timeout", 30.0),
        )
        return AndroidKvmAdapter(cfg)

    registry.register("android", _android_factory)
    return registry


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app-verify",
        description="Тонкий CLI стенда агентской верификации приложений (Android KVM и будущие платформы).",
    )
    parser.add_argument("--platform", default="android", help="Целевая платформа (по умолчанию: android)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Вывод в формате JSON")

    android_group = parser.add_argument_group("Параметры Android KVM-адаптера")
    android_group.add_argument("--ssh-host", default="home", help="SSH-хост KVM-стенда (по умолчанию: home)")
    android_group.add_argument(
        "--remote-root",
        default=ALLOWED_HDD_ROOT,
        help=f"Корневая директория на HDD (строго под {ALLOWED_HDD_ROOT})",
    )
    android_group.add_argument("--avd", default="verify-api34", help="Имя AVD на удалённом стенде")
    android_group.add_argument("--snapshot", default="clean-base", help="Имя базового снапшота")
    android_group.add_argument("--console-port", type=int, default=5564, help="Порт консоли эмулятора на ssh home")
    android_group.add_argument("--local-adb-port", type=int, default=15565, help="Локальный порт SSH-туннеля ADB на VPS")
    android_group.add_argument("--remote-adb-server-port", type=int, default=15037, help="Изолированный порт ADB-сервера на ssh home")
    android_group.add_argument("--adb-bin", default=None, help="Явный путь к локальному бинарнику adb")
    android_group.add_argument("--gpu-mode", default="swiftshader_indirect", help="Режим GPU эмулятора")
    android_group.add_argument("--boot-timeout", type=float, default=180.0, help="Таймаут загрузки эмулятора (сек)")
    android_group.add_argument("--adb-timeout", type=float, default=30.0, help="Таймаут операций ADB (сек)")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("platforms", help="Показать список зарегистрированных платформенных адаптеров")
    sub.add_parser("prepare", help="Проверить готовность стенда и инвариант HDD")

    p_start = sub.add_parser("start", aliases=["up"], help="Поднять/откатить стенд к чистому снапшоту и открыть туннель")
    p_start.add_argument("--snapshot", dest="start_snapshot", default=None, help="Переопределить имя снапшота")
    p_start.add_argument("--no-reset", action="store_true", help="Не откатывать уже запущенный эмулятор к снапшоту")

    sub.add_parser("stop", aliases=["down"], help="Остановить стенд и закрыть SSH-туннель")
    sub.add_parser("status", help="Показать статус стенда, туннеля и устройства")

    p_install = sub.add_parser("install", help="Установить debug APK / артефакт приложения")
    p_install.add_argument("artifact", help="Путь к локальному APK/артефакту")
    p_install.add_argument(
        "--no-grant-permissions",
        action="store_true",
        help="Не выдавать runtime-разрешения автоматически при установке",
    )

    p_launch = sub.add_parser("launch", help="Запустить приложение на стенде")
    p_launch.add_argument("target", help="Имя пакета / идентификатор приложения")
    p_launch.add_argument("--activity", default=None, help="Явное имя Activity (опционально)")

    p_shot = sub.add_parser("screenshot", help="Снять PNG-скриншот экрана")
    p_shot.add_argument("output", help="Путь для сохранения PNG")

    p_log = sub.add_parser("logcat", help="Выгрузить logcat и проверить отсутствие crash/ANR")
    p_log.add_argument("--output", default=None, help="Сохранить сырой logcat в файл")
    p_log.add_argument("--package", default=None, help="Фильтровать crash/ANR по имени пакета")
    p_log.add_argument("--clear", action="store_true", help="Очистить буфер logcat вместо выгрузки")
    p_log.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Вернуть код 2 при обнаружении хотя бы одного crash или ANR",
    )

    p_snap = sub.add_parser("snapshot-save", help="Сохранить чистый базовый снапшот (Android)")
    p_snap.add_argument("name", nargs="?", default=None, help="Имя снапшота (по умолчанию clean-base)")

    p_drv = sub.add_parser("ui-driver", help="Управление и проверка UI-драйвера (@mobilenext/mobile-mcp)")
    drv_sub = p_drv.add_subparsers(dest="ui_driver_command", required=True)

    p_drv_cfg = drv_sub.add_parser("mcp-config", help="Вывести MCP-конфигурацию для агента (JSON)")
    p_drv_cfg.add_argument("--device", default=None, help="ADB serial устройства (по умолчанию 127.0.0.1:<local_adb_port>)")
    p_drv_cfg.add_argument("--package", default=DEFAULT_MOBILE_MCP_PACKAGE, help="npm-пакет драйвера")

    p_drv_doc = drv_sub.add_parser("doctor", help="Проверить готовность UI-драйвера и ADB-окружения")
    p_drv_doc.add_argument("--device", default=None, help="ADB serial устройства (по умолчанию 127.0.0.1:<local_adb_port>)")
    p_drv_doc.add_argument("--package", default=DEFAULT_MOBILE_MCP_PACKAGE, help="npm-пакет драйвера")
    p_drv_doc.add_argument("--probe-mcp", action="store_true", help="Выполнить реальное MCP-рукопожатие и проверку устройств")
    p_drv_doc.add_argument("--require-device", action="store_true", help="Считать ready=false, если устройство не online")

    p_drv_srv = drv_sub.add_parser("serve", help="Запустить @mobilenext/mobile-mcp с гарантированным PATH/ANDROID_HOME")
    p_drv_srv.add_argument("--device", default=None, help="ADB serial устройства (по умолчанию 127.0.0.1:<local_adb_port>)")
    p_drv_srv.add_argument("--package", default=DEFAULT_MOBILE_MCP_PACKAGE, help="npm-пакет драйвера")
    p_drv_srv.add_argument("--listen", default=None, help="Поднять Streamable HTTP сервер на [host:]port вместо stdio")

    p_drv_call = drv_sub.add_parser("call", help="Выполнить один инструмент mobile_* через @mobilenext/mobile-mcp")
    p_drv_call.add_argument("tool", help="Имя MCP-инструмента (например, mobile_list_elements_on_screen)")
    p_drv_call.add_argument("--args-json", default="{}", help="JSON-объект аргументов инструмента")
    p_drv_call.add_argument("--device", default=None, help="ADB serial устройства (по умолчанию 127.0.0.1:<local_adb_port>)")
    p_drv_call.add_argument("--package", default=DEFAULT_MOBILE_MCP_PACKAGE, help="npm-пакет драйвера")
    p_drv_call.add_argument("--timeout", type=float, default=30.0, help="Таймаут MCP-вызова (сек)")

    return parser


def _emit(payload: dict[str, Any], *, as_json: bool, stream: TextIO) -> None:
    if as_json:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            stream.write(f"{key}: {json.dumps(value, ensure_ascii=False)}\n")
        else:
            stream.write(f"{key}: {value}\n")


def run_cli(
    argv: list[str] | None = None,
    *,
    registry: AdapterRegistry | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    reg = build_default_registry() if registry is None else registry

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "platforms":
        _emit({"platforms": reg.list_platforms()}, as_json=args.as_json, stream=out)
        return 0

    if args.command == "ui-driver":
        return _run_ui_driver_cli(args, out=out, err=err)

    try:
        adapter_kwargs: dict[str, Any] = {}
        if args.platform.strip().lower() == "android":
            adapter_kwargs = {
                "ssh_host": args.ssh_host,
                "remote_root": args.remote_root,
                "avd": args.avd,
                "snapshot": args.snapshot,
                "console_port": args.console_port,
                "local_adb_port": args.local_adb_port,
                "remote_adb_server_port": args.remote_adb_server_port,
                "adb_bin": args.adb_bin,
                "gpu_mode": args.gpu_mode,
                "boot_timeout": args.boot_timeout,
                "adb_timeout": args.adb_timeout,
            }
        adapter = reg.create(args.platform, **adapter_kwargs)

        if args.command == "prepare":
            _emit(adapter.prepare().to_dict(), as_json=args.as_json, stream=out)
            return 0

        if args.command in ("start", "up"):
            snap = args.start_snapshot if args.start_snapshot is not None else args.snapshot
            res = adapter.start(snapshot=snap, reset=not args.no_reset)
            _emit(res.to_dict(), as_json=args.as_json, stream=out)
            return 0

        if args.command in ("stop", "down"):
            _emit(adapter.stop().to_dict(), as_json=args.as_json, stream=out)
            return 0

        if args.command == "status":
            _emit(adapter.status().to_dict(), as_json=args.as_json, stream=out)
            return 0

        if args.command == "install":
            install_opts: dict[str, Any] = {}
            if args.no_grant_permissions:
                install_opts["grant_permissions"] = False
            res = adapter.install(Path(args.artifact), **install_opts)
            _emit(res.to_dict(), as_json=args.as_json, stream=out)
            return 0

        if args.command == "launch":
            res = adapter.launch(args.target, activity=args.activity)
            _emit(res.to_dict(), as_json=args.as_json, stream=out)
            return 0

        if args.command == "screenshot":
            res = adapter.screenshot(Path(args.output))
            _emit(res.to_dict(), as_json=args.as_json, stream=out)
            return 0

        if args.command == "logcat":
            out_path = Path(args.output) if args.output else None
            report = adapter.collect_logs(output_path=out_path, clear=args.clear, package=args.package)
            _emit(report.to_dict(), as_json=args.as_json, stream=out)
            if args.fail_on_error and not report.ok:
                return 2
            return 0

        if args.command == "snapshot-save":
            res = adapter.save_snapshot(args.name)
            _emit(res.to_dict(), as_json=args.as_json, stream=out)
            return 0

        raise StandError(f"Неизвестная команда: {args.command}")
    except StandError as exc:
        if args.as_json:
            _emit({"ok": False, "error": str(exc)}, as_json=True, stream=out)
        else:
            err.write(f"Ошибка: {exc}\n")
        return 1


def _run_ui_driver_cli(args: argparse.Namespace, *, out: TextIO, err: TextIO) -> int:
    device_id = args.device or f"127.0.0.1:{args.local_adb_port}"
    try:
        if args.platform.strip().lower() != "android":
            raise StandError(
                f"UI-драйвер поддерживается только для платформы 'android', получено: {args.platform!r}"
            )

        if args.ui_driver_command == "mcp-config":
            cfg = build_mcp_server_config(
                adb_bin=args.adb_bin,
                device_id=device_id,
                package=args.package,
            )
            out.write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
            return 0

        if args.ui_driver_command == "doctor":
            st = check_ui_driver(
                adb_bin=args.adb_bin,
                device_id=device_id,
                package=args.package,
                probe_mcp=args.probe_mcp,
                require_device=args.require_device,
            )
            _emit(st.to_dict(), as_json=args.as_json, stream=out)
            return 0 if st.ready else 1

        if args.ui_driver_command == "serve":
            cmd, env = build_ui_driver_serve_spec(
                adb_bin=args.adb_bin,
                device_id=device_id,
                package=args.package,
                listen=args.listen,
            )
            try:
                os.execvpe(cmd[0], cmd, env)
            except OSError as exc:
                raise StandError(f"Не удалось запустить UI-драйвер ({cmd[0]}): {exc}") from exc
            return 0

        if args.ui_driver_command == "call":
            if args.timeout <= 0:
                raise StandError(f"--timeout должен быть больше 0, получено: {args.timeout}")
            try:
                parsed_args = json.loads(args.args_json)
            except json.JSONDecodeError as exc:
                raise StandError(f"Некорректный JSON в --args-json: {exc}") from exc
            if not isinstance(parsed_args, dict):
                raise StandError("--args-json должен быть JSON-объектом")
            res = call_ui_driver_tool(
                args.tool,
                parsed_args,
                adb_bin=args.adb_bin,
                device_id=device_id,
                package=args.package,
                timeout_s=args.timeout,
            )
            payload = {
                "ok": True,
                "tool": args.tool,
                "device": device_id,
                "text": res.text,
                "image_count": len(res.images),
            }
            if args.as_json:
                _emit(payload, as_json=True, stream=out)
            else:
                out.write((res.text or f"images={len(res.images)}") + "\n")
            return 0

        raise StandError(f"Неизвестная подкоманда ui-driver: {args.ui_driver_command}")
    except StandError as exc:
        if args.as_json:
            _emit({"ok": False, "error": str(exc)}, as_json=True, stream=out)
        else:
            err.write(f"Ошибка: {exc}\n")
        return 1


def main() -> None:
    raise SystemExit(run_cli())
