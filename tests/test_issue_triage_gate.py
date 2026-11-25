#!/usr/bin/env python3
"""Unit-тесты детерминированного гейта триажа issue (`scripts/issue-triage-gate.py`)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "issue-triage-gate.py"


def load_gate():
    spec = importlib.util.spec_from_file_location("issue_triage_gate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = load_gate()


def make_issue(
    number: int,
    *,
    title: str = "Bug report",
    url: str | None = None,
    author: str = "reporter",
    body: str = "",
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    comments: list[dict] | None = None,
    state: str = "OPEN",
) -> dict:
    return {
        "number": number,
        "title": title,
        "url": url or f"https://github.com/rumap/rumap/issues/{number}",
        "state": state,
        "author": {"login": author},
        "body": body,
        "labels": [{"name": label} for label in labels or []],
        "assignees": [{"login": login} for login in assignees or []],
        "comments": comments or [],
    }


def make_comment(body: str, createdAt: str, author: str = "someone") -> dict:
    return {
        "author": {"login": author},
        "createdAt": createdAt,
        "body": body,
    }


class FakeGitHub:
    """Минимальный двойник слоя `gh`: отдаёт JSON и записывает мутации."""

    def __init__(self) -> None:
        self.issues: dict[tuple[str, int], dict] = {}
        self.events: dict[tuple[str, int], list[dict]] = {}
        self.event_pages: dict[tuple[str, int], list[list[dict]]] = {}
        self.mutations: list[dict] = []
        self.calls: list[list[str]] = []
        self.comment_error = False
        self.edit_error = False
        self.fail_on_call = None

    def add(self, repo: str, raw_issue: dict) -> dict:
        self.issues[(repo, raw_issue["number"])] = raw_issue
        return raw_issue

    def add_event(self, repo: str, number: int, label: str, created_at: str) -> None:
        self.events.setdefault((repo, number), []).append({
            "event": "labeled",
            "label": {"name": label},
            "created_at": created_at,
        })

    def __call__(
        self,
        cmd: list[str],
        check: bool = True,
        *,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        if cmd[0] != "gh":
            raise AssertionError(f"unexpected command: {cmd}")
        if self.fail_on_call is not None and self.fail_on_call(cmd):
            result = self._fail()
            if check:
                raise subprocess.CalledProcessError(
                    result.returncode, cmd, result.stdout, result.stderr
                )
            return result
        args = cmd[1:]
        if args[:2] == ["issue", "list"]:
            result = self._ok(self._list(args))
        elif args[:2] == ["issue", "view"]:
            result = self._view(args)
        elif args[:2] == ["issue", "comment"]:
            result = self._comment(args, input_text=input_text)
        elif args[:2] == ["issue", "edit"]:
            result = self._edit(args)
        elif args[:1] == ["api"]:
            result = self._api(args)
        elif args[:2] == ["pr", "view"]:
            result = self._pr_view(args)
        else:
            raise AssertionError(f"unexpected gh args: {args}")
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )
        return result

    @staticmethod
    def _ok(payload) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], 0, json.dumps(payload), "")

    @staticmethod
    def _fail() -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], 1, "", "not found")

    @staticmethod
    def _flag(args: list[str], name: str) -> str | None:
        if name in args:
            return args[args.index(name) + 1]
        return None

    @staticmethod
    def _flag_all(args: list[str], name: str) -> list[str]:
        values: list[str] = []
        index = 0
        while index < len(args):
            if args[index] == name:
                values.append(args[index + 1])
                index += 2
            else:
                index += 1
        return values

    def _list(self, args: list[str]) -> list[dict]:
        repo = self._flag(args, "--repo") or ""
        label = self._flag(args, "--label")
        items = [
            raw
            for (candidate_repo, _), raw in sorted(self.issues.items())
            if candidate_repo == repo
        ]
        if label:
            items = [
                raw for raw in items
                if label in [entry["name"] for entry in raw["labels"]]
            ]
            return [{"number": raw["number"]} for raw in items]
        return items

    def _view(self, args: list[str]) -> subprocess.CompletedProcess:
        repo = self._flag(args, "--repo") or ""
        number = int(args[2])
        raw = self.issues.get((repo, number))
        if raw is None:
            return self._fail()
        return self._ok(raw)

    def _pr_view(self, args: list[str]) -> subprocess.CompletedProcess:
        return self._fail()

    def _comment(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess:
        if self.comment_error:
            return self._fail()
        body_file = self._flag(args, "--body-file")
        used_stdin = body_file == "-"
        body = (input_text or "") if used_stdin else (self._flag(args, "--body") or "")
        self.mutations.append({
            "kind": "comment",
            "repo": self._flag(args, "--repo"),
            "issue": int(args[2]),
            "body": body,
            "body_file": body_file,
            "used_stdin": used_stdin,
            "args": list(args),
        })
        return self._ok({"ok": True})

    def _edit(self, args: list[str]) -> subprocess.CompletedProcess:
        if self.edit_error:
            return self._fail()
        self.mutations.append({
            "kind": "edit",
            "repo": self._flag(args, "--repo"),
            "issue": int(args[2]),
            "add_labels": self._flag_all(args, "--add-label"),
            "remove_labels": self._flag_all(args, "--remove-label"),
            "add_assignees": self._flag_all(args, "--add-assignee"),
        })
        return self._ok({"ok": True})

    def _api(self, args: list[str]) -> subprocess.CompletedProcess:
        slurp = "--slurp" in args
        endpoint = next((a for a in args[1:] if not a.startswith("-")), "")
        parts = endpoint.split("?")[0].split("/")
        if (
            len(parts) >= 6
            and parts[0] == "repos"
            and parts[3] == "issues"
            and parts[5] == "events"
        ):
            repo = f"{parts[1]}/{parts[2]}"
            number = int(parts[4])
            if slurp and (repo, number) in self.event_pages:
                return self._ok(self.event_pages[(repo, number)])
            payload = self.events.get((repo, number), [])
            return self._ok([payload] if slurp else payload)
        return self._fail()


class GateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.lock_tmp = tempfile.TemporaryDirectory(prefix="issue-triage-gate-tests-")
        self.addCleanup(self.lock_tmp.cleanup)
        self.lock_dir = Path(self.lock_tmp.name) / "locks"

        self.gh = FakeGitHub()
        run_patcher = mock.patch.object(gate, "run", self.gh)
        run_patcher.start()
        self.addCleanup(run_patcher.stop)

        env_patcher = mock.patch.dict(
            os.environ,
            {"ISSUE_TRIAGE_LOCK_DIR": "", "ISSUE_TRIAGE_MAX_FIXERS": ""},
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def run_cli(self, *argv: str) -> tuple[int, dict | None]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = gate.main([str(arg) for arg in argv])
        output = buffer.getvalue().strip()
        return code, json.loads(output) if output else None

    def scan(self, *extra: str, repos: tuple[str, ...] = ("rumap/rumap",)) -> tuple[int, dict]:
        argv = ["scan", "--lock", "--lock-dir", str(self.lock_dir), *extra, "--repos", *repos]
        code, payload = self.run_cli(*argv)
        self.assertEqual(code, 0)
        self.assertIsNotNone(payload)
        return code, payload

    def lock_files(self) -> list[Path]:
        if not self.lock_dir.exists():
            return []
        return sorted(self.lock_dir.glob("*.lock"))


class ScanFilterTest(GateTestCase):
    def test_bug_label_is_eligible(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["bug"]))
        _, report = self.scan()
        self.assertEqual([item["issue"] for item in report["items"]], [7])
        self.assertIsNone(report["items"][0]["retrigger_reason"])

    def test_needs_triage_label_is_eligible(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["status:needs-triage"]))
        _, report = self.scan()
        self.assertEqual([item["issue"] for item in report["items"]], [7])

    def test_issue_without_labels_is_eligible(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7))
        _, report = self.scan()
        self.assertEqual([item["issue"] for item in report["items"]], [7])

    def test_every_stop_label_skips_issue(self) -> None:
        for index, label in enumerate(sorted(gate.STOP_LABELS), start=10):
            with self.subTest(label=label):
                self.gh.issues.clear()
                self.gh.add("rumap/rumap", make_issue(index, labels=[label]))
                _, report = self.scan()
                self.assertEqual(report["items"], [])
                self.assertEqual(report["skipped"][0]["reason"], f"stop_label:{label}")

    def test_assignee_skips_issue(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, assignees=["developer"]))
        _, report = self.scan()
        self.assertEqual(report["items"], [])
        self.assertEqual(report["skipped"][0]["reason"], "has_assignee")

    def test_owner_and_external_authors_are_both_eligible(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, author="fus1ond"))
        self.gh.add("rumap/rumap", make_issue(8, author="outside-contributor"))
        _, report = self.scan()
        self.assertEqual(sorted(item["issue"] for item in report["items"]), [7, 8])
        self.assertEqual(
            {item["author"] for item in report["items"]},
            {"fus1ond", "outside-contributor"},
        )

    def test_enhancement_and_question_are_skipped(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["enhancement"]))
        self.gh.add("rumap/rumap", make_issue(8, labels=["question"]))
        _, report = self.scan()
        self.assertEqual(report["items"], [])
        self.assertEqual(
            {entry["issue"]: entry["reason"] for entry in report["skipped"]},
            {7: "stop_label:enhancement", 8: "stop_label:question"},
        )

    def test_non_bug_labels_skip_issue(self) -> None:
        non_bug = (
            "type:epic",
            "documentation",
            "duplicate",
            "invalid",
            "wontfix",
            "area:roadmap",
        )
        for index, label in enumerate(non_bug, start=40):
            with self.subTest(label=label):
                self.gh.issues.clear()
                self.gh.add("rumap/rumap", make_issue(index, labels=[label]))
                _, report = self.scan()
                self.assertEqual(report["items"], [])
                self.assertEqual(report["skipped"][0]["reason"], f"stop_label:{label}")

    def test_wayfinder_prefix_label_skips_issue(self) -> None:
        self.gh.add("rumap/rumap", make_issue(51, labels=["wayfinder:decision"]))
        self.gh.add("rumap/rumap", make_issue(52, labels=["bug", "wayfinder:map"]))
        _, report = self.scan()
        self.assertEqual(report["items"], [])
        self.assertEqual(
            {entry["issue"]: entry["reason"] for entry in report["skipped"]},
            {51: "stop_label:wayfinder:decision", 52: "stop_label:wayfinder:map"},
        )

    def test_spec_and_map_titles_skipped_without_bug_or_needs_triage(self) -> None:
        self.gh.add("rumap/rumap", make_issue(61, title="Spec: onboarding flow"))
        self.gh.add("rumap/rumap", make_issue(62, title="Map: Android v2 roadmap"))
        self.gh.add("rumap/rumap", make_issue(63, title="  spec: lowercase prefix"))
        _, report = self.scan()
        self.assertEqual(report["items"], [])
        self.assertEqual(
            {entry["issue"]: entry["reason"] for entry in report["skipped"]},
            {
                61: "non_bug_title:spec",
                62: "non_bug_title:map",
                63: "non_bug_title:spec",
            },
        )

    def test_spec_and_map_titles_allowed_with_bug_or_needs_triage_label(self) -> None:
        self.gh.add("rumap/rumap", make_issue(64, title="Map: crash on zoom", labels=["bug"]))
        self.gh.add(
            "rumap/rumap",
            make_issue(65, title="Spec: broken parser", labels=["status:needs-triage"]),
        )
        _, report = self.scan()
        self.assertEqual(sorted(item["issue"] for item in report["items"]), [64, 65])

    def test_closed_issue_is_skipped(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, state="CLOSED"))
        _, report = self.scan()
        self.assertEqual(report["items"], [])
        self.assertEqual(report["skipped"][0]["reason"], "state=CLOSED")

    def test_pull_request_entries_are_ignored(self) -> None:
        raw_pr = make_issue(
            9,
            url="https://github.com/rumap/rumap/pull/9",
        )
        raw_pr["pull_request"] = {"url": raw_pr["url"]}
        self.gh.add("rumap/rumap", raw_pr)
        _, report = self.scan()
        self.assertEqual(report["items"], [])
        self.assertEqual(report["skipped"], [])

    def test_scan_restricts_to_requested_repos(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7))
        self.gh.add("rumap/rumap-android", make_issue(8))
        _, report = self.scan(repos=("rumap/rumap-android",))
        self.assertEqual([item["issue"] for item in report["items"]], [8])
        self.assertEqual(report["items"][0]["local_repo_path"], "/root/rumap-android")

    def test_scan_rejects_unknown_repo(self) -> None:
        code, report = self.run_cli(
            "scan",
            "--lock-dir",
            str(self.lock_dir),
            "--repos",
            "rumap/unknown",
        )
        self.assertEqual(code, 2)
        self.assertEqual(report["error"], "unknown_repo:rumap/unknown")
        self.assertEqual(self.gh.calls, [])


class IdempotencyTest(GateTestCase):
    def test_existing_marker_without_retrigger_is_skipped(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                7,
                labels=["bug"],
                comments=[make_comment(gate.MARKER, "2026-02-01T10:00:00Z")],
            ),
        )
        _, report = self.scan()
        self.assertEqual(report["items"], [])
        self.assertEqual(report["skipped"][0]["reason"], "already_triaged")

    def test_marker_with_whitespace_still_counts(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                7,
                comments=[make_comment("<!--   benny-triage   -->", "2026-02-01T10:00:00Z")],
            ),
        )
        _, report = self.scan()
        self.assertEqual(report["skipped"][0]["reason"], "already_triaged")

    def test_benny_comment_newer_than_marker_retriggers(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                7,
                labels=["bug"],
                comments=[
                    make_comment(gate.MARKER, "2026-02-01T10:00:00Z"),
                    make_comment("/benny", "2026-02-02T10:00:00Z"),
                ],
            ),
        )
        _, report = self.scan()
        self.assertEqual([item["issue"] for item in report["items"]], [7])
        self.assertEqual(
            report["items"][0]["retrigger_reason"],
            "comment:/benny@2026-02-02T10:00:00Z",
        )

    def test_triage_comment_newer_than_marker_retriggers(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                7,
                comments=[
                    make_comment(gate.MARKER, "2026-02-01T10:00:00Z"),
                    make_comment("/triage", "2026-02-03T10:00:00Z"),
                ],
            ),
        )
        _, report = self.scan()
        self.assertEqual(report["items"][0]["retrigger_reason"], "comment:/triage@2026-02-03T10:00:00Z")

    def test_retrigger_command_is_case_insensitive_and_allows_indent(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                7,
                comments=[
                    make_comment(gate.MARKER, "2026-02-01T10:00:00Z"),
                    make_comment("   /BeNnY", "2026-02-02T10:00:00Z"),
                ],
            ),
        )
        _, report = self.scan()
        self.assertEqual([item["issue"] for item in report["items"]], [7])
        self.assertEqual(report["items"][0]["retrigger_reason"], "comment:/benny@2026-02-02T10:00:00Z")

    def test_inline_command_mention_does_not_retrigger(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                7,
                comments=[
                    make_comment(gate.MARKER, "2026-02-01T10:00:00Z"),
                    make_comment("please run /benny later", "2026-02-02T10:00:00Z"),
                ],
            ),
        )
        _, report = self.scan()
        self.assertEqual(report["items"], [])
        self.assertEqual(report["skipped"][0]["reason"], "already_triaged")

    def test_command_older_than_marker_does_not_retrigger(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                7,
                comments=[
                    make_comment("/benny", "2026-01-01T10:00:00Z"),
                    make_comment(gate.MARKER, "2026-02-01T10:00:00Z"),
                ],
            ),
        )
        _, report = self.scan()
        self.assertEqual(report["skipped"][0]["reason"], "already_triaged")

    def test_command_at_same_timestamp_as_marker_does_not_retrigger(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                7,
                comments=[
                    make_comment(gate.MARKER, "2026-02-01T10:00:00Z"),
                    make_comment("/benny", "2026-02-01T10:00:00Z"),
                ],
            ),
        )
        _, report = self.scan()
        self.assertEqual(report["skipped"][0]["reason"], "already_triaged")

    def test_relabel_of_needs_triage_after_marker_retriggers(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                7,
                labels=["status:needs-triage"],
                comments=[make_comment(gate.MARKER, "2026-02-01T10:00:00Z")],
            ),
        )
        self.gh.add_event("rumap/rumap", 7, "status:needs-triage", "2026-02-04T10:00:00Z")
        _, report = self.scan()
        self.assertEqual([item["issue"] for item in report["items"]], [7])
        self.assertEqual(
            report["items"][0]["retrigger_reason"],
            "label:status:needs-triage@2026-02-04T10:00:00Z",
        )

    def test_relabel_before_marker_does_not_retrigger(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                7,
                labels=["status:needs-triage"],
                comments=[make_comment(gate.MARKER, "2026-02-01T10:00:00Z")],
            ),
        )
        self.gh.add_event("rumap/rumap", 7, "status:needs-triage", "2026-01-01T10:00:00Z")
        _, report = self.scan()
        self.assertEqual(report["skipped"][0]["reason"], "already_triaged")

    def test_unlabeled_and_other_label_events_do_not_retrigger(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                7,
                labels=["status:needs-triage"],
                comments=[make_comment(gate.MARKER, "2026-02-01T10:00:00Z")],
            ),
        )
        self.gh.events[("rumap/rumap", 7)] = [
            {
                "event": "labeled",
                "label": {"name": "bug"},
                "created_at": "2026-02-05T10:00:00Z",
            },
            {
                "event": "unlabeled",
                "label": {"name": "status:needs-triage"},
                "created_at": "2026-02-06T10:00:00Z",
            },
        ]
        _, report = self.scan()
        self.assertEqual(report["skipped"][0]["reason"], "already_triaged")

    def test_events_are_not_requested_without_needs_triage_label(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                7,
                labels=["bug"],
                comments=[make_comment(gate.MARKER, "2026-02-01T10:00:00Z")],
            ),
        )
        self.scan()
        self.assertFalse(any(call[1:2] == ["api"] for call in self.gh.calls))

    def test_fresh_issue_does_not_query_events(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["status:needs-triage"]))
        _, report = self.scan()
        self.assertEqual([item["issue"] for item in report["items"]], [7])
        self.assertFalse(any(call[1:2] == ["api"] for call in self.gh.calls))

    def test_paginated_events_detect_relabel_on_later_page(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                7,
                labels=["bug", "status:needs-triage"],
                comments=[make_comment(gate.MARKER, "2026-02-01T10:00:00Z")],
            ),
        )
        self.gh.event_pages[("rumap/rumap", 7)] = [
            [
                {
                    "event": "labeled",
                    "label": {"name": "bug"},
                    "created_at": "2026-01-15T10:00:00Z",
                }
            ],
            [
                {
                    "event": "labeled",
                    "label": {"name": "status:needs-triage"},
                    "created_at": "2026-02-05T12:00:00Z",
                }
            ],
        ]
        _, report = self.scan()
        self.assertEqual([item["issue"] for item in report["items"]], [7])
        self.assertEqual(
            report["items"][0]["retrigger_reason"],
            "label:status:needs-triage@2026-02-05T12:00:00Z",
        )
        api_calls = [call for call in self.gh.calls if call[1:2] == ["api"]]
        self.assertEqual(len(api_calls), 1)
        self.assertIn("--paginate", api_calls[0])
        self.assertIn("--slurp", api_calls[0])

    def test_benny_retriggers_over_needs_owner(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                7,
                labels=["bug", "status:needs-owner"],
                comments=[
                    make_comment(gate.MARKER, "2026-02-01T10:00:00Z"),
                    make_comment("Логи:\npanic: nil\n/benny", "2026-02-02T10:00:00Z"),
                ],
            ),
        )
        _, report = self.scan()
        self.assertEqual([item["issue"] for item in report["items"]], [7])
        self.assertEqual(
            report["items"][0]["retrigger_reason"],
            "comment:/benny@2026-02-02T10:00:00Z",
        )

    def test_triage_retriggers_over_ready_for_agent(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                7,
                labels=["bug", "status:ready-for-agent"],
                comments=[
                    make_comment(gate.MARKER, "2026-02-01T10:00:00Z"),
                    make_comment("/triage", "2026-02-02T10:00:00Z"),
                ],
            ),
        )
        _, report = self.scan()
        self.assertEqual([item["issue"] for item in report["items"]], [7])
        self.assertEqual(
            report["items"][0]["retrigger_reason"],
            "comment:/triage@2026-02-02T10:00:00Z",
        )

    def test_relabel_needs_triage_retriggers_over_post_triage_statuses(self) -> None:
        for index, post_label in enumerate(("status:needs-owner", "status:ready-for-agent"), start=70):
            with self.subTest(post_label=post_label):
                self.gh.issues.clear()
                self.gh.events.clear()
                self.gh.add(
                    "rumap/rumap",
                    make_issue(
                        index,
                        labels=["bug", post_label, "status:needs-triage"],
                        comments=[make_comment(gate.MARKER, "2026-02-01T10:00:00Z")],
                    ),
                )
                self.gh.add_event("rumap/rumap", index, "status:needs-triage", "2026-02-04T10:00:00Z")
                _, report = self.scan()
                self.assertEqual([item["issue"] for item in report["items"]], [index])
                self.assertEqual(
                    report["items"][0]["retrigger_reason"],
                    "label:status:needs-triage@2026-02-04T10:00:00Z",
                )

    def test_retrigger_does_not_override_hard_stop_labels_or_assignee(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                71,
                labels=["bug", "status:in-progress"],
                comments=[
                    make_comment(gate.MARKER, "2026-02-01T10:00:00Z"),
                    make_comment("/benny", "2026-02-02T10:00:00Z"),
                ],
            ),
        )
        self.gh.add(
            "rumap/rumap",
            make_issue(
                72,
                labels=["bug", "status:needs-owner"],
                assignees=["dev"],
                comments=[
                    make_comment(gate.MARKER, "2026-02-01T10:00:00Z"),
                    make_comment("/benny", "2026-02-02T10:00:00Z"),
                ],
            ),
        )
        _, report = self.scan()
        self.assertEqual(report["items"], [])
        self.assertEqual(
            {entry["issue"]: entry["reason"] for entry in report["skipped"]},
            {71: "stop_label:status:in-progress", 72: "has_assignee"},
        )

    def test_stale_retrigger_does_not_override_post_triage_status(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                73,
                labels=["bug", "status:needs-owner"],
                comments=[
                    make_comment("/benny", "2026-01-10T10:00:00Z"),
                    make_comment(gate.MARKER, "2026-02-01T10:00:00Z"),
                ],
            ),
        )
        _, report = self.scan()
        self.assertEqual(report["items"], [])
        self.assertEqual(report["skipped"][0]["reason"], "stop_label:status:needs-owner")


class LockTest(GateTestCase):
    def eligible_issue(self, number: int = 7) -> None:
        self.gh.add("rumap/rumap", make_issue(number, labels=["bug"]))

    def test_scan_with_lock_creates_lock_file(self) -> None:
        self.eligible_issue()
        self.scan()
        files = self.lock_files()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "rumap__rumap__issue7.lock")

    def test_scan_without_lock_writes_no_files(self) -> None:
        self.eligible_issue()
        argv = ["scan", "--lock-dir", str(self.lock_dir), "--repos", "rumap/rumap"]
        code, report = self.run_cli(*argv)
        self.assertEqual(code, 0)
        self.assertEqual([item["issue"] for item in report["items"]], [7])
        self.assertEqual(self.lock_files(), [])

    def test_second_scan_with_lock_skips_locked_issue(self) -> None:
        self.eligible_issue()
        self.scan()
        _, report = self.scan()
        self.assertEqual(report["items"], [])
        self.assertEqual(report["skipped"][0]["reason"], "triage_in_progress_locked")

    def test_expired_lock_is_reacquired(self) -> None:
        self.eligible_issue()
        self.scan()
        lock_file = self.lock_files()[0]
        expired = time.time() - gate.LOCK_TTL_SECONDS - 60
        os.utime(lock_file, (expired, expired))
        _, report = self.scan()
        self.assertEqual([item["issue"] for item in report["items"]], [7])
        self.assertEqual(len(self.lock_files()), 1)

    def test_lock_is_scoped_per_issue(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["bug"]))
        self.gh.add("rumap/rumap", make_issue(8, labels=["bug"]))
        gate.acquire_lock(self.lock_dir, "rumap/rumap", 7)
        _, report = self.scan()
        self.assertEqual([item["issue"] for item in report["items"]], [8])
        self.assertEqual(report["skipped"][0]["issue"], 7)

    def test_skipped_issues_are_not_locked(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["enhancement"]))
        self.gh.add(
            "rumap/rumap",
            make_issue(
                8,
                comments=[make_comment(gate.MARKER, "2026-02-01T10:00:00Z")],
            ),
        )
        self.eligible_issue(9)
        self.scan()
        self.assertEqual([path.name for path in self.lock_files()], ["rumap__rumap__issue9.lock"])

    def test_env_var_overrides_lock_dir(self) -> None:
        self.eligible_issue()
        env_dir = Path(self.lock_tmp.name) / "env-locks"
        with mock.patch.dict(os.environ, {"ISSUE_TRIAGE_LOCK_DIR": str(env_dir)}):
            code, report = self.run_cli("scan", "--lock", "--repos", "rumap/rumap")
        self.assertEqual(code, 0)
        self.assertEqual([item["issue"] for item in report["items"]], [7])
        self.assertEqual(len(list(env_dir.glob("*.lock"))), 1)

    def test_concurrent_acquire_lock_allows_only_one_winner(self) -> None:
        workers = 8
        barrier = threading.Barrier(workers)

        def attempt() -> bool:
            barrier.wait()
            return gate.acquire_lock(self.lock_dir, "rumap/rumap", 7)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda _: attempt(), range(workers)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), workers - 1)

    def test_concurrent_expired_lock_takeover_allows_only_one_winner(self) -> None:
        self.assertTrue(gate.acquire_lock(self.lock_dir, "rumap/rumap", 7))
        lock_file = self.lock_files()[0]
        expired = time.time() - gate.LOCK_TTL_SECONDS - 60
        os.utime(lock_file, (expired, expired))

        workers = 8
        barrier = threading.Barrier(workers)

        def attempt() -> bool:
            barrier.wait()
            return gate.acquire_lock(self.lock_dir, "rumap/rumap", 7)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda _: attempt(), range(workers)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), workers - 1)

    def test_is_locked_dead_helper_removed(self) -> None:
        self.assertFalse(hasattr(gate, "is_locked"))

    def test_evaluate_issue_releases_lock_on_error(self) -> None:
        raw = make_issue(7, labels=["bug"])
        with mock.patch.object(gate, "collect_referenced_items", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                gate.evaluate_issue("rumap/rumap", raw, take_lock=True, lock_dir=self.lock_dir)
        self.assertEqual(self.lock_files(), [])

    def test_cmd_scan_releases_acquired_locks_when_later_issue_fails(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["bug"]))
        self.gh.add("rumap/rumap", make_issue(8, labels=["bug"]))

        calls = 0

        def flaky_refs(repo: str, issue: dict) -> list[dict]:
            nonlocal calls
            calls += 1
            if issue["number"] == 8:
                raise RuntimeError("gh failed mid-scan")
            return []

        with mock.patch.object(gate, "collect_referenced_items", side_effect=flaky_refs):
            with self.assertRaises(RuntimeError):
                self.scan()
        self.assertEqual(calls, 2)
        self.assertEqual(self.lock_files(), [])

    def test_cmd_inspect_releases_lock_when_count_active_fixers_fails(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["bug"]))
        self.gh.fail_on_call = lambda cmd: cmd[1:3] == ["issue", "list"]
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_cli("inspect", "rumap/rumap", "7", "--lock", "--lock-dir", str(self.lock_dir))
        self.assertEqual(self.lock_files(), [])


class FixerSlotTest(GateTestCase):
    def add_fixer(self, repo: str, number: int) -> None:
        self.gh.add(
            repo,
            make_issue(number, labels=["bug", "status:in-progress"], title="Fix in flight"),
        )

    def test_active_fixers_are_counted_across_repos(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["bug"]))
        self.add_fixer("rumap/rumap", 20)
        self.add_fixer("rumap/rumap-core", 21)
        _, report = self.scan(
            repos=("rumap/rumap", "rumap/rumap-core", "rumap/rumap-android"),
        )
        self.assertEqual(report["active_fixers"], 2)
        self.assertEqual(report["max_fixers"], 2)
        self.assertFalse(report["auto_fix_slot_available"])
        item = report["items"][0]
        self.assertEqual(item["active_fixers"], 2)
        self.assertEqual(item["max_fixers"], 2)
        self.assertFalse(item["auto_fix_slot_available"])

    def test_slot_available_below_cap(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["bug"]))
        self.add_fixer("rumap/rumap", 20)
        _, report = self.scan()
        self.assertEqual(report["active_fixers"], 1)
        self.assertTrue(report["auto_fix_slot_available"])

    def test_active_fixers_counted_globally_when_scanning_single_repo(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["bug"]))
        self.add_fixer("rumap/rumap-android", 30)
        self.add_fixer("rumap/rumap-core", 31)
        _, report = self.scan(repos=("rumap/rumap",))
        self.assertEqual(report["active_fixers"], 2)
        self.assertEqual(report["max_fixers"], 2)
        self.assertFalse(report["auto_fix_slot_available"])
        self.assertEqual(report["items"][0]["active_fixers"], 2)
        self.assertFalse(report["items"][0]["auto_fix_slot_available"])

    def test_cap_from_env(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["bug"]))
        self.add_fixer("rumap/rumap", 20)
        with mock.patch.dict(os.environ, {"ISSUE_TRIAGE_MAX_FIXERS": "1"}):
            _, report = self.scan()
        self.assertEqual(report["max_fixers"], 1)
        self.assertFalse(report["auto_fix_slot_available"])

    def test_cap_from_cli(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["bug"]))
        _, report = self.scan("--max-fixers", "1")
        self.assertEqual(report["max_fixers"], 1)
        self.assertTrue(report["auto_fix_slot_available"])

    def test_invalid_env_cap_falls_back_to_default(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["bug"]))
        with mock.patch.dict(os.environ, {"ISSUE_TRIAGE_MAX_FIXERS": "5"}):
            _, report = self.scan()
        self.assertEqual(report["max_fixers"], gate.DEFAULT_MAX_FIXERS)
        self.assertEqual(report["max_fixers"], 2)

    def test_non_numeric_env_cap_falls_back_to_default(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["bug"]))
        with mock.patch.dict(os.environ, {"ISSUE_TRIAGE_MAX_FIXERS": "many"}):
            _, report = self.scan()
        self.assertEqual(report["max_fixers"], 2)

    def test_cli_cap_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                gate.main(["scan", "--max-fixers", "3", "--repos", "rumap/rumap"])
        self.assertEqual(self.gh.calls, [])

    def test_scan_reuses_open_issues_for_active_fixers_and_queries_only_remaining_repos(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["bug"]))
        self.add_fixer("rumap/rumap", 20)
        self.add_fixer("rumap/rumap-core", 21)

        self.scan(repos=("rumap/rumap", "rumap/rumap-android", "rumap/rumap-core"))
        in_progress_calls = [
            call for call in self.gh.calls
            if call[1:3] == ["issue", "list"] and "--label" in call
        ]
        self.assertEqual(in_progress_calls, [])

        self.gh.calls.clear()
        self.scan(repos=("rumap/rumap",))
        in_progress_repos = [
            call[call.index("--repo") + 1]
            for call in self.gh.calls
            if call[1:3] == ["issue", "list"] and "--label" in call
        ]
        self.assertEqual(sorted(in_progress_repos), ["rumap/rumap-android", "rumap/rumap-core"])


class ItemContextTest(GateTestCase):
    def test_item_contains_structured_context(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(
                7,
                title="Crash on start",
                url="https://github.com/rumap/rumap/issues/7",
                author="fus1ond",
                body="App crashes when opening",
                labels=["bug", "status:needs-triage"],
                comments=[make_comment("reproduced on Pixel", "2026-02-01T10:00:00Z")],
            ),
        )
        _, report = self.scan()
        item = report["items"][0]
        expected_keys = {
            "action",
            "repo",
            "issue",
            "url",
            "title",
            "author",
            "body",
            "labels",
            "comments",
            "referenced_items",
            "local_repo_path",
            "retrigger_reason",
            "active_fixers",
            "max_fixers",
            "auto_fix_slot_available",
        }
        self.assertTrue(expected_keys.issubset(item.keys()))
        self.assertEqual(item["action"], "triage")
        self.assertEqual(item["labels"], ["bug", "status:needs-triage"])
        self.assertEqual(item["comments"][0]["author"], "someone")
        self.assertEqual(item["local_repo_path"], "/root/rumap")

    def test_cross_referenced_issue_is_collected(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, body="Duplicate of #99"))
        self.gh.add("rumap/rumap", make_issue(99, title="Original crash", labels=["bug"]))
        _, report = self.scan()
        refs = report["items"][0]["referenced_items"]
        self.assertEqual([ref["number"] for ref in refs], [99])
        self.assertEqual(refs[0]["title"], "Original crash")
        self.assertEqual(refs[0]["type"], "issue")

    def test_self_reference_is_not_collected(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, body="Updates #7"))
        _, report = self.scan()
        self.assertEqual(report["items"][0]["referenced_items"], [])


class ActTest(GateTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.gh.add(
            "rumap/rumap",
            make_issue(7, labels=["bug", "status:needs-triage"]),
        )
        self.body_file = Path(self.lock_tmp.name) / "body.md"
        self.body_file.write_text("## Root cause\nCrash in startup path.", encoding="utf-8")

    def act(self, *extra: str) -> tuple[int, dict | None]:
        return self.run_cli(
            "act",
            "rumap/rumap",
            "7",
            "--body-file",
            str(self.body_file),
            "--lock-dir",
            str(self.lock_dir),
            *extra,
        )

    def test_act_posts_comment_with_marker_area_and_status(self) -> None:
        code, report = self.act("--area", "area:ui", "--status", "status:ready-for-agent")
        self.assertEqual(code, 0)
        self.assertEqual(report["result"], "acted")
        edit = self.gh.mutations[0]
        self.assertEqual(edit["kind"], "edit")
        self.assertEqual(edit["add_labels"], ["area:ui", "status:ready-for-agent"])
        self.assertEqual(edit["remove_labels"], ["status:needs-triage"])
        comment = self.gh.mutations[1]
        self.assertEqual(comment["kind"], "comment")
        self.assertTrue(comment["used_stdin"])
        self.assertEqual(comment["body_file"], "-")
        self.assertNotIn("--body", comment["args"])
        body = comment["body"]
        self.assertIn(gate.MARKER, body)
        self.assertTrue(body.endswith(gate.MARKER))
        self.assertEqual(report["add_labels"], ["area:ui", "status:ready-for-agent"])

    def test_act_supports_multiple_areas_and_needs_owner(self) -> None:
        code, report = self.act(
            "--area",
            "area:android",
            "--area",
            "area:vpn",
            "--status",
            "status:needs-owner",
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            self.gh.mutations[0]["add_labels"],
            ["area:android", "area:vpn", "status:needs-owner"],
        )
        self.assertIn("area:android", report["add_labels"])

    def test_act_does_not_duplicate_existing_marker(self) -> None:
        self.body_file.write_text(f"Report\n\n{gate.MARKER}", encoding="utf-8")
        code, _ = self.act("--area", "area:core", "--status", "status:ready-for-agent")
        self.assertEqual(code, 0)
        self.assertEqual(self.gh.mutations[1]["body"].count(gate.MARKER), 1)

    def test_act_skips_removal_when_needs_triage_absent(self) -> None:
        self.gh.issues[("rumap/rumap", 7)] = make_issue(7, labels=["bug"])
        code, report = self.act("--area", "area:backend", "--status", "status:ready-for-agent")
        self.assertEqual(code, 0)
        self.assertEqual(report["remove_labels"], [])
        self.assertEqual(self.gh.mutations[0]["remove_labels"], [])

    def test_act_removes_opposite_status_label_on_transition(self) -> None:
        self.gh.issues[("rumap/rumap", 7)] = make_issue(
            7, labels=["bug", "status:needs-owner", "status:needs-triage"]
        )
        code, report = self.act("--area", "area:backend", "--status", "status:ready-for-agent")
        self.assertEqual(code, 0)
        self.assertEqual(report["add_labels"], ["area:backend", "status:ready-for-agent"])
        self.assertEqual(report["remove_labels"], ["status:needs-triage", "status:needs-owner"])

        self.gh.mutations.clear()
        self.gh.issues[("rumap/rumap", 7)] = make_issue(
            7, labels=["bug", "status:ready-for-agent"]
        )
        code, report = self.act("--area", "area:backend", "--status", "status:needs-owner")
        self.assertEqual(code, 0)
        self.assertEqual(report["add_labels"], ["area:backend", "status:needs-owner"])
        self.assertEqual(report["remove_labels"], ["status:ready-for-agent"])

    def test_act_does_not_post_comment_and_releases_lock_when_edit_fails(self) -> None:
        gate.acquire_lock(self.lock_dir, "rumap/rumap", 7)
        self.gh.edit_error = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.act("--area", "area:ui", "--status", "status:ready-for-agent")
        self.assertEqual(self.gh.mutations, [])
        self.assertEqual(self.lock_files(), [])

    def test_act_claim_autofix_sets_in_progress_and_assignee_when_slot_available(self) -> None:
        self.gh.issues[("rumap/rumap", 7)] = make_issue(
            7, labels=["bug", "status:needs-triage", "status:needs-owner"]
        )
        code, report = self.act(
            "--area",
            "area:backend",
            "--status",
            "status:ready-for-agent",
            "--claim-autofix",
        )
        self.assertEqual(code, 0)
        self.assertTrue(report["autofix_claimed"])
        self.assertEqual(report["add_labels"], ["area:backend", "status:in-progress"])
        self.assertEqual(report["remove_labels"], ["status:needs-triage", "status:needs-owner"])
        self.assertEqual(report["add_assignees"], ["@me"])
        edit = self.gh.mutations[0]
        self.assertEqual(edit["kind"], "edit")
        self.assertEqual(edit["add_labels"], ["area:backend", "status:in-progress"])
        self.assertEqual(edit["add_assignees"], ["@me"])
        self.assertEqual(self.gh.mutations[1]["kind"], "comment")

    def test_act_claim_autofix_falls_back_when_slots_full(self) -> None:
        self.gh.add("rumap/rumap-core", make_issue(20, labels=["bug", "status:in-progress"]))
        self.gh.add("rumap/rumap-android", make_issue(21, labels=["bug", "status:in-progress"]))
        code, report = self.act(
            "--area",
            "area:backend",
            "--status",
            "status:ready-for-agent",
            "--claim-autofix",
        )
        self.assertEqual(code, 0)
        self.assertFalse(report["autofix_claimed"])
        self.assertEqual(report["claim_skipped_reason"], "max_fixers_reached")
        self.assertEqual(report["add_labels"], ["area:backend", "status:ready-for-agent"])
        self.assertEqual(report["add_assignees"], [])
        self.assertEqual(self.gh.mutations[0]["add_assignees"], [])

    def test_act_claim_autofix_rejects_needs_owner(self) -> None:
        code, report = self.act(
            "--area",
            "area:backend",
            "--status",
            "status:needs-owner",
            "--claim-autofix",
        )
        self.assertEqual(code, 2)
        self.assertEqual(report["error"], "claim_autofix_requires_ready_for_agent")
        self.assertEqual(self.gh.mutations, [])

    def test_act_claim_autofix_dry_run_plans_without_mutating(self) -> None:
        code, report = self.act(
            "--area",
            "area:backend",
            "--status",
            "status:ready-for-agent",
            "--claim-autofix",
            "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["result"], "dry_run")
        self.assertTrue(report["autofix_claimed"])
        self.assertEqual(report["add_labels"], ["area:backend", "status:in-progress"])
        self.assertEqual(report["add_assignees"], ["@me"])
        self.assertEqual(self.gh.mutations, [])

    def test_act_supports_extra_remove_label_present_on_issue(self) -> None:
        self.gh.issues[("rumap/rumap", 7)] = make_issue(
            7, labels=["bug", "status:needs-triage", "triage-me"]
        )
        code, report = self.act(
            "--area",
            "area:backend",
            "--status",
            "status:ready-for-agent",
            "--remove-label",
            "triage-me",
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["remove_labels"], ["status:needs-triage", "triage-me"])

    def test_act_releases_lock(self) -> None:
        gate.acquire_lock(self.lock_dir, "rumap/rumap", 7)
        self.assertEqual(len(self.lock_files()), 1)
        code, report = self.act("--area", "area:ui", "--status", "status:ready-for-agent")
        self.assertEqual(code, 0)
        self.assertTrue(report["released_lock"])
        self.assertEqual(self.lock_files(), [])

    def test_act_releases_lock_when_comment_fails(self) -> None:
        gate.acquire_lock(self.lock_dir, "rumap/rumap", 7)
        self.gh.comment_error = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.act("--area", "area:ui", "--status", "status:ready-for-agent")
        self.assertEqual(self.lock_files(), [])

    def test_act_dry_run_returns_plan_without_mutations(self) -> None:
        gate.acquire_lock(self.lock_dir, "rumap/rumap", 7)
        code, report = self.act(
            "--area",
            "area:ui",
            "--status",
            "status:ready-for-agent",
            "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["result"], "dry_run")
        self.assertEqual(report["repo"], "rumap/rumap")
        self.assertEqual(report["issue"], 7)
        self.assertEqual(report["add_labels"], ["area:ui", "status:ready-for-agent"])
        self.assertEqual(report["remove_labels"], ["status:needs-triage"])
        self.assertIn(gate.MARKER, report["comment_body"])
        self.assertTrue(report["would_release_lock"])
        self.assertEqual(self.gh.mutations, [])
        self.assertEqual(len(self.lock_files()), 1)

    def test_act_dry_run_reports_empty_removals_when_no_needs_triage(self) -> None:
        self.gh.issues[("rumap/rumap", 7)] = make_issue(7, labels=["bug"])
        code, report = self.act(
            "--area",
            "area:ui",
            "--status",
            "status:ready-for-agent",
            "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["remove_labels"], [])

    def test_act_rejects_unknown_repo(self) -> None:
        code, report = self.run_cli(
            "act",
            "rumap/other",
            "7",
            "--body-file",
            str(self.body_file),
            "--area",
            "area:ui",
            "--status",
            "status:ready-for-agent",
        )
        self.assertEqual(code, 2)
        self.assertEqual(report["error"], "unknown_repo:rumap/other")
        self.assertEqual(self.gh.mutations, [])

    def test_act_rejects_unknown_area_label(self) -> None:
        code, report = self.act("--area", "area:web", "--status", "status:ready-for-agent")
        self.assertEqual(code, 2)
        self.assertEqual(report["error"], "unknown_area_label:area:web")
        self.assertEqual(self.gh.mutations, [])

    def test_act_requires_area_arguments(self) -> None:
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self.act("--status", "status:ready-for-agent")

    def test_act_rejects_unknown_status(self) -> None:
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self.act("--area", "area:ui", "--status", "status:done")

    def test_act_requires_existing_body_file(self) -> None:
        code, report = self.run_cli(
            "act",
            "rumap/rumap",
            "7",
            "--area",
            "area:ui",
            "--status",
            "status:ready-for-agent",
            "--body-file",
            str(Path(self.lock_tmp.name) / "missing.md"),
        )
        self.assertEqual(code, 2)
        self.assertTrue(report["error"].startswith("body_file_not_found:"))


class InspectTest(GateTestCase):
    def test_inspect_returns_issue_context(self) -> None:
        self.gh.add(
            "rumap/rumap",
            make_issue(7, title="Crash", labels=["bug"], body="steps"),
        )
        code, report = self.run_cli(
            "inspect",
            "rumap/rumap",
            7,
            "--lock-dir",
            str(self.lock_dir),
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["action"], "triage")
        self.assertEqual(report["issue"], 7)
        self.assertEqual(report["title"], "Crash")
        self.assertIn("active_fixers", report)
        self.assertIn("auto_fix_slot_available", report)

    def test_inspect_reports_skip_reason(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["enhancement"]))
        code, report = self.run_cli(
            "inspect",
            "rumap/rumap",
            7,
            "--lock-dir",
            str(self.lock_dir),
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["action"], "skip")
        self.assertEqual(report["reason"], "stop_label:enhancement")

    def test_inspect_takes_lock(self) -> None:
        self.gh.add("rumap/rumap", make_issue(7, labels=["bug"]))
        code, report = self.run_cli(
            "inspect",
            "rumap/rumap",
            7,
            "--lock",
            "--lock-dir",
            str(self.lock_dir),
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["action"], "triage")
        self.assertEqual(len(self.lock_files()), 1)

    def test_inspect_rejects_unknown_repo(self) -> None:
        code, report = self.run_cli(
            "inspect",
            "rumap/other",
            7,
            "--lock-dir",
            str(self.lock_dir),
        )
        self.assertEqual(code, 2)
        self.assertEqual(report["error"], "unknown_repo:rumap/other")


if __name__ == "__main__":
    unittest.main()
