#!/usr/bin/env python3
"""Unit-тесты интеграции UI-драйвера (@mobilenext/mobile-mcp) со стендом верификации."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from app_verify.cli import run_cli
from app_verify.core import StandError
from app_verify.ui_driver import (
    DEFAULT_MOBILE_MCP_PACKAGE,
    MobileMcpClient,
    build_mcp_server_config,
    build_ui_driver_env,
    call_ui_driver_tool,
    check_ui_driver,
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
                base_env={"PATH": "/usr/local/bin:/usr/bin"},
            )

            path_parts = env["PATH"].split(":")
            self.assertEqual(path_parts[0], str(platform_tools))
            self.assertEqual(env["ANDROID_HOME"], str(sdk_root))
            self.assertEqual(env["ANDROID_SDK_ROOT"], str(sdk_root))
            self.assertEqual(env["MOBILEMCP_DISABLE_TELEMETRY"], "1")
            self.assertEqual(env["DO_NOT_TRACK"], "1")
            self.assertEqual(env["MOBILECLI_DEVICE"], "127.0.0.1:15565")

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
                command_override=[sys.executable, str(srv)],
                which_fn=lambda name: "/usr/local/bin/node" if name == "node" else None,
            )

            self.assertTrue(status.ready)
            self.assertTrue(status.device_online)
            self.assertEqual(status.android_home, str(root / "sdk"))
            self.assertIn("mobile_list_elements_on_screen", status.tools)
            self.assertEqual(status.devices[0]["id"], "127.0.0.1:15565")

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


if __name__ == "__main__":
    unittest.main()
