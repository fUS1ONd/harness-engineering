#!/usr/bin/env python3
"""Unit-тесты интеграции UI-драйвера (@mobilenext/mobile-mcp) со стендом верификации."""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app_verify.android import PNG_SIGNATURE
from app_verify.cli import run_cli
from app_verify.core import StandError
from app_verify.ui_driver import (
    DEFAULT_MOBILE_MCP_PACKAGE,
    McpToolResult,
    MobileMcpClient,
    build_mcp_server_config,
    build_ui_driver_env,
    call_ui_driver_tool,
    check_ui_driver,
    derive_android_sdk_root,
    resolve_mobilecli_command,
    resolve_ui_driver_command,
    stop_mobilecli_daemon,
)


class UiDriverEnvAndConfigTests(unittest.TestCase):
    def test_build_ui_driver_env_guarantees_adb_path_and_android_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sdk_root = Path(td) / "android-sdk"
            platform_tools = sdk_root / "platform-tools"
            platform_tools.mkdir(parents=True)
            adb_bin = platform_tools / "adb"
            adb_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            adb_bin.chmod(0o755)

            env = build_ui_driver_env(
                adb_bin=str(adb_bin),
                device_id="127.0.0.1:15565",
                base_env={"PATH": "/usr/local/bin:/usr/bin", "ANDROID_HOME": "/stale/sdk"},
            )

            path_parts = env["PATH"].split(":")
            self.assertEqual(path_parts[0], str(platform_tools))
            self.assertEqual(env["ANDROID_HOME"], str(sdk_root))
            self.assertEqual(env["ANDROID_SDK_ROOT"], str(sdk_root))
            self.assertEqual(env["MOBILEMCP_DISABLE_TELEMETRY"], "1")
            self.assertEqual(env["DO_NOT_TRACK"], "1")
            self.assertEqual(env["MOBILECLI_DEVICE"], "127.0.0.1:15565")

    def test_build_ui_driver_env_moves_adb_dir_to_front_and_resolves_symlink_sdk_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sdk_root = root / "real-sdk"
            platform_tools = sdk_root / "platform-tools"
            platform_tools.mkdir(parents=True)
            adb_real = platform_tools / "adb"
            adb_real.write_text("#!/bin/sh\n", encoding="utf-8")
            adb_real.chmod(0o755)

            symlink_dir = root / "bin"
            symlink_dir.mkdir()
            adb_link = symlink_dir / "adb"
            adb_link.symlink_to(adb_real)

            self.assertEqual(derive_android_sdk_root(adb_link), str(sdk_root.resolve()))

            env = build_ui_driver_env(
                adb_bin=str(adb_real),
                device_id="127.0.0.1:15565",
                base_env={"PATH": f"/usr/local/bin:{platform_tools}:/usr/bin:{platform_tools}"},
            )
            path_parts = env["PATH"].split(":")
            self.assertEqual(path_parts[0], str(platform_tools))
            self.assertEqual(path_parts.count(str(platform_tools)), 1)
            self.assertEqual(path_parts[1:], ["/usr/local/bin", "/usr/bin"])

            env_link = build_ui_driver_env(
                adb_bin=str(adb_link),
                device_id="127.0.0.1:15565",
                base_env={"PATH": "/usr/bin"},
            )
            self.assertEqual(env_link["ANDROID_HOME"], str(sdk_root.resolve()))

    def test_resolve_ui_driver_command_prefers_direct_binary_and_falls_back_to_npx(self) -> None:
        with self.subTest("direct server binary"):
            cmd = resolve_ui_driver_command(
                which_fn=lambda name: "/usr/local/bin/mcp-server-mobile" if name == "mcp-server-mobile" else None
            )
            self.assertEqual(cmd, ["/usr/local/bin/mcp-server-mobile"])

        with self.subTest("npx fallback"):
            cmd = resolve_ui_driver_command(
                which_fn=lambda name: "/usr/local/bin/npx" if name == "npx" else None
            )
            self.assertEqual(cmd, ["/usr/local/bin/npx", "--yes", DEFAULT_MOBILE_MCP_PACKAGE])

        with self.subTest("missing node/npx raises StandError"):
            with self.assertRaises(StandError):
                resolve_ui_driver_command(which_fn=lambda _name: None)

    def test_build_mcp_server_config_returns_valid_mcp_json_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sdk_root = Path(td) / "sdk"
            pt = sdk_root / "platform-tools"
            pt.mkdir(parents=True)
            adb_bin = pt / "adb"
            adb_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            adb_bin.chmod(0o755)

            cfg = build_mcp_server_config(
                adb_bin=str(adb_bin),
                device_id="127.0.0.1:15565",
                base_env={"PATH": "/usr/bin"},
                which_fn=lambda name: "/usr/local/bin/npx" if name == "npx" else None,
            )

            self.assertIn("mcpServers", cfg)
            entry = cfg["mcpServers"]["mobile-mcp"]
            self.assertEqual(entry["command"], "/usr/local/bin/npx")
            self.assertEqual(entry["args"], ["--yes", DEFAULT_MOBILE_MCP_PACKAGE, "--stdio"])
            self.assertEqual(entry["env"]["ANDROID_HOME"], str(sdk_root))
            self.assertTrue(entry["env"]["PATH"].startswith(str(pt)))


_FAKE_MCP_SERVER_SCRIPT = """
import json
import sys

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    method = msg.get("method")
    req_id = msg.get("id")
    if method == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "mobile-mcp", "version": "1.0.20"},
                "capabilities": {"tools": {}},
            },
        }
        sys.stdout.write(json.dumps(resp) + "\\n")
        sys.stdout.flush()
    elif method == "tools/list":
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {"name": "mobile_list_available_devices"},
                    {"name": "mobile_list_elements_on_screen"},
                    {"name": "mobile_click_on_screen_at_coordinates"},
                ]
            },
        }
        sys.stdout.write(json.dumps(resp) + "\\n")
        sys.stdout.flush()
    elif method == "tools/call":
        name = msg["params"]["name"]
        if name == "mobile_list_available_devices":
            payload = {"devices": [{"id": "127.0.0.1:15565", "platform": "android", "state": "online"}]}
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
            }
        elif name == "failing_tool":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"isError": True, "content": [{"type": "text", "text": "Device offline"}]},
            }
        else:
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": f"ok:{name}"}]},
            }
        sys.stdout.write(json.dumps(resp) + "\\n")
        sys.stdout.flush()
"""


class MobileMcpClientAndDoctorTests(unittest.TestCase):
    def test_mcp_client_handshake_lists_tools_calls_tool_and_raises_on_tool_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            srv = Path(td) / "fake_mcp.py"
            srv.write_text(_FAKE_MCP_SERVER_SCRIPT, encoding="utf-8")

            with MobileMcpClient([sys.executable, str(srv)], timeout_s=5.0) as client:
                init = client.initialize()
                self.assertEqual(init["serverInfo"]["name"], "mobile-mcp")
                tools = client.list_tools()
                self.assertIn("mobile_list_elements_on_screen", tools)

                res = client.call_tool("mobile_list_available_devices", {})
                data = json.loads(res.text)
                self.assertEqual(data["devices"][0]["id"], "127.0.0.1:15565")

                with self.assertRaises(StandError) as ctx:
                    client.call_tool("failing_tool", {})
                self.assertIn("Device offline", str(ctx.exception))

    def test_check_ui_driver_probes_mcp_and_detects_target_device(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pt = root / "sdk" / "platform-tools"
            pt.mkdir(parents=True)
            adb_bin = pt / "adb"
            adb_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            adb_bin.chmod(0o755)

            srv = root / "fake_mcp.py"
            srv.write_text(_FAKE_MCP_SERVER_SCRIPT, encoding="utf-8")

            status = check_ui_driver(
                adb_bin=str(adb_bin),
                device_id="127.0.0.1:15565",
                probe_mcp=True,
                require_device=True,
                base_env={"PATH": "/usr/bin"},
                command_override=[sys.executable, str(srv)],
                which_fn=lambda name: "/usr/local/bin/node" if name == "node" else None,
            )

            self.assertTrue(status.ready)
            self.assertTrue(status.device_online)
            self.assertEqual(status.android_home, str(root / "sdk"))
            self.assertIn("mobile_list_elements_on_screen", status.tools)
            self.assertEqual(status.devices[0]["id"], "127.0.0.1:15565")

    def test_mcp_client_handles_burst_non_dict_json_and_large_stderr(self) -> None:
        burst_script = """
import json
import sys

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    req_id = msg.get("id")
    if req_id is None:
        continue
    sys.stderr.write("E" * 90000 + "\\n")
    sys.stderr.flush()
    burst = (
        "plain log line\\n"
        + "123\\n"
        + '["not-a-dict"]\\n'
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}) + "\\n"
        + json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}}) + "\\n"
    )
    sys.stdout.write(burst)
    sys.stdout.flush()
"""
        with tempfile.TemporaryDirectory() as td:
            srv = Path(td) / "burst_mcp.py"
            srv.write_text(burst_script, encoding="utf-8")

            with MobileMcpClient([sys.executable, str(srv)], timeout_s=5.0) as client:
                res = client.request("ping", {})
                self.assertEqual(res, {"ok": True})
                res2 = client.request("ping", {})
                self.assertEqual(res2, {"ok": True})

    def test_mcp_client_times_out_on_partial_line_and_handles_unexpected_eof(self) -> None:
        partial_script = """
import sys
import time

sys.stdin.readline()
sys.stdout.write('{"jsonrpc": "2.0", "id": 1')
sys.stdout.flush()
time.sleep(5.0)
"""
        eof_script = """
import sys

sys.stdin.readline()
sys.stderr.write("fatal startup error\\n")
sys.stderr.flush()
sys.exit(7)
"""
        with tempfile.TemporaryDirectory() as td:
            partial_srv = Path(td) / "partial_mcp.py"
            partial_srv.write_text(partial_script, encoding="utf-8")
            with MobileMcpClient([sys.executable, str(partial_srv)], timeout_s=0.3) as client:
                with self.assertRaises(StandError) as ctx:
                    client.request("ping", {})
                self.assertIn("Таймаут", str(ctx.exception))

            eof_srv = Path(td) / "eof_mcp.py"
            eof_srv.write_text(eof_script, encoding="utf-8")
            with MobileMcpClient([sys.executable, str(eof_srv)], timeout_s=2.0) as client:
                with self.assertRaises(StandError) as ctx2:
                    client.request("ping", {})
                self.assertIn("неожиданно завершил работу", str(ctx2.exception))
                self.assertIn("fatal startup error", str(ctx2.exception))

    def test_check_ui_driver_require_device_without_probe_checks_adb_online(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pt = Path(td) / "sdk" / "platform-tools"
            pt.mkdir(parents=True)
            adb_bin = pt / "adb"
            adb_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            adb_bin.chmod(0o755)

            offline_st = check_ui_driver(
                adb_bin=str(adb_bin),
                device_id="127.0.0.1:15565",
                probe_mcp=False,
                require_device=True,
                base_env={"PATH": "/usr/bin"},
                which_fn=lambda name: "/usr/local/bin/" + name if name in ("node", "npx") else None,
                adb_online_fn=lambda *_args: False,
            )
            self.assertFalse(offline_st.ready)
            self.assertFalse(offline_st.device_online)
            self.assertIsNotNone(offline_st.error)

            online_st = check_ui_driver(
                adb_bin=str(adb_bin),
                device_id="127.0.0.1:15565",
                probe_mcp=False,
                require_device=True,
                base_env={"PATH": "/usr/bin"},
                which_fn=lambda name: "/usr/local/bin/" + name if name in ("node", "npx") else None,
                adb_online_fn=lambda *_args: True,
            )
            self.assertTrue(online_st.ready)
            self.assertTrue(online_st.device_online)

    def test_stale_mobilecli_daemon_recovery_in_doctor_and_call(self) -> None:
        stale_server_script = """
import json
import pathlib
import sys

state_file = pathlib.Path(sys.argv[1])
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    method = msg.get("method")
    req_id = msg.get("id")
    if method == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "mobile-mcp", "version": "1.0.20"},
                "capabilities": {"tools": {}},
            },
        }
    elif method == "tools/list":
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": [{"name": "mobile_list_available_devices"}, {"name": "mobile_list_elements_on_screen"}]},
        }
    elif method == "tools/call":
        name = msg["params"]["name"]
        restarted = state_file.exists()
        if name == "mobile_list_available_devices":
            devices = (
                [{"id": "127.0.0.1:15565", "platform": "android", "state": "online"}]
                if restarted
                else []
            )
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps({"devices": devices})}]},
            }
        elif name == "mobile_list_elements_on_screen" and not restarted:
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"isError": True, "content": [{"type": "text", "text": 'Device "127.0.0.1:15565" not found.'}]},
            }
        else:
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": "@e1 TextView text=\\"rumap\\""}]},
            }
    else:
        continue
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pt = root / "sdk" / "platform-tools"
            pt.mkdir(parents=True)
            adb_bin = pt / "adb"
            adb_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            adb_bin.chmod(0o755)

            srv = root / "stale_mcp.py"
            srv.write_text(stale_server_script, encoding="utf-8")
            marker = root / "daemon_restarted"

            def _stop_daemon(_pkg: str, _env: object) -> bool:
                marker.write_text("1", encoding="utf-8")
                return True

            status = check_ui_driver(
                adb_bin=str(adb_bin),
                device_id="127.0.0.1:15565",
                probe_mcp=True,
                require_device=True,
                command_override=[sys.executable, str(srv), str(marker)],
                which_fn=lambda name: "/usr/local/bin/node" if name == "node" else None,
                adb_online_fn=lambda *_args: True,
                stop_daemon_fn=_stop_daemon,
            )
            self.assertTrue(status.ready)
            self.assertTrue(status.device_online)
            self.assertEqual(status.devices[0]["id"], "127.0.0.1:15565")

            marker.unlink()
            res = call_ui_driver_tool(
                "mobile_list_elements_on_screen",
                {},
                adb_bin=str(adb_bin),
                device_id="127.0.0.1:15565",
                command_override=[sys.executable, str(srv), str(marker)],
                adb_online_fn=lambda *_args: True,
                stop_daemon_fn=_stop_daemon,
            )
            self.assertIn("@e1 TextView", res.text)

    def test_resolve_and_stop_mobilecli_daemon(self) -> None:
        self.assertEqual(
            resolve_mobilecli_command(which_fn=lambda n: "/usr/bin/npx" if n == "npx" else None),
            ["/usr/bin/npx", "--yes", "-p", DEFAULT_MOBILE_MCP_PACKAGE, "mobilecli"],
        )
        seen: list[list[str]] = []

        class _Proc:
            returncode = 0

        ok = stop_mobilecli_daemon(
            which_fn=lambda n: "/usr/bin/mobilecli" if n == "mobilecli" else None,
            run_fn=lambda cmd, **_kw: seen.append(cmd) or _Proc(),
        )
        self.assertTrue(ok)
        self.assertEqual(seen, [["/usr/bin/mobilecli", "daemon", "stop"]])


class UiDriverCliTests(unittest.TestCase):
    def test_cli_ui_driver_mcp_config_and_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pt = root / "sdk" / "platform-tools"
            pt.mkdir(parents=True)
            adb_bin = pt / "adb"
            adb_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            adb_bin.chmod(0o755)

            fake_which = lambda name: f"/usr/local/bin/{name}" if name in ("node", "npx") else None

            with (
                mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True),
                mock.patch("shutil.which", side_effect=fake_which),
            ):
                out = io.StringIO()
                err = io.StringIO()
                rc = run_cli(
                    ["--adb-bin", str(adb_bin), "--json", "ui-driver", "mcp-config"],
                    stdout=out,
                    stderr=err,
                )
                self.assertEqual(rc, 0)
                cfg = json.loads(out.getvalue())
                self.assertIn("mcpServers", cfg)
                self.assertEqual(cfg["mcpServers"]["mobile-mcp"]["env"]["ANDROID_HOME"], str(root / "sdk"))
                self.assertEqual(cfg["mcpServers"]["mobile-mcp"]["env"]["MOBILECLI_DEVICE"], "127.0.0.1:15565")

                out2 = io.StringIO()
                err2 = io.StringIO()
                rc2 = run_cli(
                    ["--adb-bin", str(adb_bin), "--json", "ui-driver", "doctor"],
                    stdout=out2,
                    stderr=err2,
                )
                self.assertEqual(rc2, 0)
                doc = json.loads(out2.getvalue())
                self.assertTrue(doc["ready"])
                self.assertEqual(doc["driver"], "mobile-mcp")
                self.assertEqual(doc["adb_path"], str(adb_bin))

    def test_cli_ui_driver_rejects_unsupported_platform_and_invalid_timeout(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        rc = run_cli(["--platform", "web", "--json", "ui-driver", "doctor"], stdout=out, stderr=err)
        self.assertEqual(rc, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("android", payload["error"])

        out2 = io.StringIO()
        err2 = io.StringIO()
        rc2 = run_cli(
            ["--json", "ui-driver", "call", "mobile_list_available_devices", "--timeout", "0"],
            stdout=out2,
            stderr=err2,
        )
        self.assertEqual(rc2, 1)
        payload2 = json.loads(out2.getvalue())
        self.assertFalse(payload2["ok"])
        self.assertIn("--timeout", payload2["error"])

    def test_cli_ui_driver_call_save_image_writes_png_and_errors_when_no_image(self) -> None:
        fake_png = PNG_SIGNATURE + b"\x00\x01\x02\x03" * 8
        b64_png = base64.b64encode(fake_png).decode("ascii")
        img_res = McpToolResult(
            content=[{"type": "image", "data": b64_png, "mimeType": "image/png"}],
            text="",
            images=[{"type": "image", "data": b64_png, "mimeType": "image/png"}],
        )
        with tempfile.TemporaryDirectory() as td:
            out_png = Path(td) / "shots" / "screen.png"
            with mock.patch("app_verify.cli.call_ui_driver_tool", return_value=img_res):
                out = io.StringIO()
                err = io.StringIO()
                rc = run_cli(
                    [
                        "--json",
                        "ui-driver",
                        "call",
                        "mobile_take_screenshot",
                        "--save-image",
                        str(out_png),
                    ],
                    stdout=out,
                    stderr=err,
                )
            self.assertEqual(rc, 0, err.getvalue())
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["image_count"], 1)
            self.assertEqual(payload["saved_images"], [str(out_png)])
            self.assertEqual(out_png.read_bytes(), fake_png)

            empty_res = McpToolResult(content=[{"type": "text", "text": "ok"}], text="ok", images=[])
            with mock.patch("app_verify.cli.call_ui_driver_tool", return_value=empty_res):
                out2 = io.StringIO()
                rc2 = run_cli(
                    [
                        "--json",
                        "ui-driver",
                        "call",
                        "mobile_list_elements_on_screen",
                        "--save-image",
                        str(Path(td) / "nope.png"),
                    ],
                    stdout=out2,
                    stderr=io.StringIO(),
                )
            self.assertEqual(rc2, 1)
            self.assertFalse(json.loads(out2.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
