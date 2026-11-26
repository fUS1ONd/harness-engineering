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
from .debug_hooks import DEFAULT_DEBUG_PACKAGE, DebugHooksClient
from .issues import (
    IssueCreationError,
    OpenIssue,
    OwnerApprovalRequiredError,
    create_approved_issues,
    deduplicate_findings,
    fetch_open_issues,
    load_open_issues,
)
from .net_emulation import PROFILES, WIFI_ACTIONS, NetEmulationClient
from .report import load_verification_report, render_chat_summary, render_markdown_report
from .ui_driver import (
    DEFAULT_MOBILE_MCP_PACKAGE,
    build_mcp_server_config,
    build_ui_driver_serve_spec,
    call_ui_driver_tool,
    check_ui_driver,
    save_mcp_images,
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
    p_drv_call.add_argument(
        "--save-image",
        default=None,
        help="Сохранить возвращённое изображение (например, от mobile_take_screenshot) в локальный файл",
    )

    p_debug = sub.add_parser(
        "debug",
        help="White-box debug-хуки debug-сборки `ru.rumap.android.debug` через am broadcast",
    )
    debug_sub = p_debug.add_subparsers(dest="debug_command", required=True)

    p_dbg_nav = debug_sub.add_parser("navigate", help="Перейти на экран/диалог приложения")
    p_dbg_nav.add_argument("target", nargs="?", default=None, help="Целевой экран (например home/settings/update)")
    p_dbg_nav.add_argument("--target", dest="target_opt", default=None, help="Целевой экран (альтернатива позиционному)")
    _add_debug_common_args(p_dbg_nav)

    p_dbg_mock = debug_sub.add_parser("mock-state", help="Применить мок сетевого/синхронизационного состояния")
    p_dbg_mock.add_argument("--preset", required=True, help="Пресет: clear|none|offline|degraded_cellular|vpn_active|center_sync_error")
    p_dbg_mock.add_argument("--error-message", default=None, help="Кастомный текст ошибки для мока")
    _add_debug_common_args(p_dbg_mock)

    p_dbg_reset = debug_sub.add_parser("reset-state", help="Сбросить локальную БД, настройки и debug-overlay")
    _add_debug_common_args(p_dbg_reset)

    p_dbg_dump = debug_sub.add_parser("dump-diagnostics", help="Снять read-only дамп диагностики приложения")
    p_dbg_dump.add_argument(
        "--output",
        default=None,
        help="Локальный путь на VPS для сохранения JSON диагностики из ответа приёмника",
    )
    p_dbg_dump.add_argument(
        "--device-output-path",
        "--output-path",
        dest="device_output_path",
        default=None,
        help="Путь внутри cacheDir приложения на устройстве для сохранения JSON (алиас: --output-path)",
    )
    _add_debug_common_args(p_dbg_dump)

    p_net = sub.add_parser(
        "net",
        help="Программная эмуляция БС (off/soft/hard/down) и управление Wi-Fi/сотовой на стенде",
    )
    net_sub = p_net.add_subparsers(dest="net_command", required=True)

    p_net_profile = net_sub.add_parser("profile", help="Применить профиль БС к сотовому интерфейсу")
    p_net_profile.add_argument("profile", choices=PROFILES, help="Профиль: off, soft, hard или down")
    p_net_profile.add_argument(
        "--allow-ip",
        action="append",
        default=[],
        dest="allow_ip",
        metavar="IP",
        help="Дополнительный IPv4 в белый список (можно повторить или перечислить через запятую)",
    )
    p_net_profile.add_argument(
        "--wifi",
        choices=WIFI_ACTIONS,
        default=None,
        help="Включить (on) или выключить (off) Wi-Fi",
    )
    _add_net_common_args(p_net_profile)

    p_net_status = net_sub.add_parser("status", help="Показать текущий профиль БС, Wi-Fi, сотовую сеть и VPN")
    _add_net_common_args(p_net_status)

    p_net_reset = net_sub.add_parser("reset", help="Снять ограничения БС и вернуть Wi-Fi в исходное состояние")
    _add_net_common_args(p_net_reset)

    p_report = sub.add_parser("report", help="Сгенерировать Markdown/JSON отчёт верификации из run.json")
    p_report.add_argument("--input", required=True, help="Путь к run.json с результатами прогона")
    p_report.add_argument("--open-issues", default=None, help="JSON открытых issue для секции кандидатов")
    p_report.add_argument("--output", default=None, help="Сохранить Markdown-отчёт в файл")
    p_report.add_argument("--chat-summary", action="store_true", help="Вывести краткую сводку для чата")
    p_report.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        default=argparse.SUPPRESS,
        help="Вывод в формате JSON",
    )

    p_issues = sub.add_parser("issues", help="Дедупликация находок и owner-gated создание GitHub Issues")
    issues_sub = p_issues.add_subparsers(dest="issues_command", required=True)

    p_dedup = issues_sub.add_parser("dedup", help="Сверить находки с открытыми GitHub Issues")
    p_dedup.add_argument("--input", required=True, help="Путь к run.json с результатами прогона")
    dedup_source = p_dedup.add_mutually_exclusive_group(required=True)
    dedup_source.add_argument("--open-issues", default=None, help="JSON открытых issue (без обращения к GitHub)")
    dedup_source.add_argument("--repo", default=None, help="Репозиторий owner/repo для `gh issue list`")
    p_dedup.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        default=argparse.SUPPRESS,
        help="Вывод в формате JSON",
    )

    p_create = issues_sub.add_parser("create", help="Завести issue для одобренных владельцем находок")
    p_create.add_argument("--input", required=True, help="Путь к run.json с результатами прогона")
    p_create.add_argument("--repo", required=True, help="Репозиторий owner/repo для `gh issue create`")
    create_source = p_create.add_mutually_exclusive_group(required=True)
    create_source.add_argument("--open-issues", default=None, help="JSON открытых issue для дедупликации")
    create_source.add_argument("--fetch-open", action="store_true", help="Загрузить открытые issue через gh")
    p_create.add_argument("--approve", default=None, help="Список ID одобренных находок через запятую")
    p_create.add_argument("--approve-all-new", action="store_true", help="Одобрить все новые находки")
    p_create.add_argument(
        "--confirm-owner-approval",
        action="store_true",
        help="Подтверждение владельца: без него issue не создаются",
    )
    p_create.add_argument("--dry-run", action="store_true", help="Показать план без создания issue")
    p_create.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        default=argparse.SUPPRESS,
        help="Вывод в формате JSON",
    )

    return parser


def _add_debug_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device", default=None, help="ADB serial устройства (по умолчанию 127.0.0.1:<local_adb_port>)")
    parser.add_argument(
        "--package",
        dest="debug_package",
        default=DEFAULT_DEBUG_PACKAGE,
        help=f"Пакет debug-сборки (по умолчанию: {DEFAULT_DEBUG_PACKAGE})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        default=argparse.SUPPRESS,
        help="Вывод в формате JSON",
    )


def _add_net_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device", default=None, help="ADB serial устройства (по умолчанию 127.0.0.1:<local_adb_port>)")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        default=argparse.SUPPRESS,
        help="Вывод в формате JSON",
    )


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

    if args.command == "debug":
        return _run_debug_cli(args, out=out, err=err)

    if args.command == "net":
        return _run_net_cli(args, out=out, err=err)

    if args.command == "report":
        return _run_report_cli(args, out=out, err=err)

    if args.command == "issues":
        return _run_issues_cli(args, out=out, err=err)

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
            saved_images: list[str] = []
            if args.save_image:
                saved_images = save_mcp_images(res.images, Path(args.save_image))
            payload: dict[str, Any] = {
                "ok": True,
                "tool": args.tool,
                "device": device_id,
                "text": res.text,
                "image_count": len(res.images),
            }
            if saved_images:
                payload["saved_images"] = saved_images
            if args.as_json:
                _emit(payload, as_json=True, stream=out)
            else:
                if res.text:
                    out.write(res.text + "\n")
                elif saved_images:
                    out.write("\n".join(saved_images) + "\n")
                else:
                    out.write(f"images={len(res.images)}\n")
            return 0

        raise StandError(f"Неизвестная подкоманда ui-driver: {args.ui_driver_command}")
    except StandError as exc:
        if args.as_json:
            _emit({"ok": False, "error": str(exc)}, as_json=True, stream=out)
        else:
            err.write(f"Ошибка: {exc}\n")
        return 1


def _run_debug_cli(args: argparse.Namespace, *, out: TextIO, err: TextIO) -> int:
    device_id = args.device or f"127.0.0.1:{args.local_adb_port}"
    try:
        if args.platform.strip().lower() != "android":
            raise StandError(
                f"Debug-хуки поддерживаются только для платформы 'android', получено: {args.platform!r}"
            )
        client = DebugHooksClient(
            device=device_id,
            package=args.debug_package,
            adb_bin=args.adb_bin,
            timeout_s=args.adb_timeout,
        )
        if args.debug_command == "navigate":
            target = args.target_opt or args.target
            if not target:
                raise StandError("Укажите целевой экран: `debug navigate --target <target>`")
            res = client.navigate(target)
        elif args.debug_command == "mock-state":
            res = client.mock_state(args.preset, error_message=args.error_message)
        elif args.debug_command == "reset-state":
            res = client.reset_state()
        elif args.debug_command == "dump-diagnostics":
            res = client.dump_diagnostics(
                device_output_path=args.device_output_path,
                local_output=args.output,
            )
        else:
            raise StandError(f"Неизвестная подкоманда debug: {args.debug_command}")

        _emit(res.to_dict(), as_json=args.as_json, stream=out)
        return 0
    except StandError as exc:
        if args.as_json:
            _emit({"ok": False, "error": str(exc)}, as_json=True, stream=out)
        else:
            err.write(f"Ошибка: {exc}\n")
        return 1


def _run_net_cli(args: argparse.Namespace, *, out: TextIO, err: TextIO) -> int:
    device_id = args.device or f"127.0.0.1:{args.local_adb_port}"
    try:
        if args.platform.strip().lower() != "android":
            raise StandError(
                f"Профили БС поддерживаются только для платформы 'android', получено: {args.platform!r}"
            )
        client = NetEmulationClient(
            device=device_id,
            adb_bin=args.adb_bin,
            timeout_s=args.adb_timeout,
        )
        if args.net_command == "profile":
            status = client.apply_profile(
                args.profile,
                allow_ips=args.allow_ip,
                wifi=args.wifi,
            )
        elif args.net_command == "status":
            status = client.status()
        elif args.net_command == "reset":
            status = client.reset()
        else:
            raise StandError(f"Неизвестная подкоманда net: {args.net_command}")

        _emit(status.to_dict(), as_json=args.as_json, stream=out)
        return 0
    except StandError as exc:
        if args.as_json:
            _emit({"ok": False, "error": str(exc)}, as_json=True, stream=out)
        else:
            err.write(f"Ошибка: {exc}\n")
        return 1


def _run_report_cli(args: argparse.Namespace, *, out: TextIO, err: TextIO) -> int:
    try:
        report = load_verification_report(Path(args.input))
        plan = None
        if args.open_issues:
            plan = deduplicate_findings(report.findings, load_open_issues(Path(args.open_issues)))

        markdown = render_markdown_report(report, dedup_plan=plan)
        summary = render_chat_summary(report, dedup_plan=plan)

        output_path: str | None = None
        if args.output:
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown, encoding="utf-8")
            output_path = str(path)

        if args.as_json:
            _emit(
                {
                    "ok": True,
                    "verdict": report.verdict,
                    "output": output_path,
                    "chat_summary": summary,
                    "markdown": markdown,
                },
                as_json=True,
                stream=out,
            )
        elif args.chat_summary:
            out.write(summary + "\n")
        elif output_path is None:
            out.write(markdown)
        else:
            out.write(f"Отчёт сохранён: {output_path}\n")
        return 0
    except (StandError, ValueError, OSError, json.JSONDecodeError) as exc:
        if args.as_json:
            _emit({"ok": False, "error": str(exc)}, as_json=True, stream=out)
        else:
            err.write(f"Ошибка: {exc}\n")
        return 1


def _resolve_cli_open_issues(args: argparse.Namespace, *, allow_fetch: bool) -> list[OpenIssue]:
    if args.open_issues:
        return load_open_issues(Path(args.open_issues))
    if allow_fetch and getattr(args, "fetch_open", False):
        return fetch_open_issues(args.repo)
    if allow_fetch and getattr(args, "repo", None):
        return fetch_open_issues(args.repo)
    raise StandError("Укажите --open-issues <file.json> или --repo <owner/repo>")


def _run_issues_cli(args: argparse.Namespace, *, out: TextIO, err: TextIO) -> int:
    try:
        report = load_verification_report(Path(args.input))

        if args.issues_command == "dedup":
            issues = _resolve_cli_open_issues(args, allow_fetch=True)
            plan = deduplicate_findings(report.findings, issues)
            if args.as_json:
                _emit(plan.to_dict(), as_json=True, stream=out)
            else:
                out.write(plan.render_markdown() + "\n")
            return 0

        if args.issues_command == "create":
            approved_ids = [
                item.strip() for item in (args.approve or "").split(",") if item.strip()
            ]
            if not args.confirm_owner_approval and not args.dry_run:
                raise OwnerApprovalRequiredError(
                    "Создание GitHub Issues требует явного подтверждения владельца: "
                    "добавьте --confirm-owner-approval (или используйте --dry-run для плана)"
                )

            issues = _resolve_cli_open_issues(args, allow_fetch=True)
            plan = deduplicate_findings(report.findings, issues)
            result = create_approved_issues(
                plan,
                repo=args.repo,
                owner_approved=args.confirm_owner_approval,
                approved_finding_ids=approved_ids,
                approve_all_new=args.approve_all_new,
                report_meta=report,
                dry_run=args.dry_run,
            )
            if args.as_json:
                _emit(result.to_dict(), as_json=True, stream=out)
            elif args.dry_run:
                out.write("План создания GitHub Issues (dry-run, ничего не создано):\n")
                for item in result.planned:
                    out.write(f"- {item.finding.id}: {item.title}\n")
            else:
                out.write("Созданы GitHub Issues:\n")
                for created in result.created:
                    out.write(f"- {created.finding_id}: {created.url or created.title}\n")
            return 0

        raise StandError(f"Неизвестная подкоманда issues: {args.issues_command}")
    except IssueCreationError as exc:
        if args.as_json:
            payload = exc.result.to_dict()
            payload["ok"] = False
            payload["error"] = str(exc)
            _emit(payload, as_json=True, stream=out)
        else:
            if exc.result.created:
                out.write("Частично созданы GitHub Issues до ошибки:\n")
                for created in exc.result.created:
                    out.write(f"- {created.finding_id}: {created.url or created.title}\n")
            err.write(f"Ошибка: {exc}\n")
        return 1
    except (StandError, ValueError, OSError, json.JSONDecodeError) as exc:
        if args.as_json:
            _emit({"ok": False, "error": str(exc)}, as_json=True, stream=out)
        else:
            err.write(f"Ошибка: {exc}\n")
        return 1


def main() -> None:
    raise SystemExit(run_cli())
