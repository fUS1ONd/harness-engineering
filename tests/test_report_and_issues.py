#!/usr/bin/env python3
"""Unit-тесты отчёта верификации, рубрики, дедупликации и owner-gated GitHub Issues."""

from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from app_verify.android import CommandResult
from app_verify.cli import run_cli
from app_verify.core import StandError
from app_verify.issues import (
    DEFAULT_ISSUE_LABELS,
    CreatedIssue,
    DedupPlan,
    IssueCreationError,
    OpenIssue,
    OwnerApprovalRequiredError,
    _identifier_tokens,
    create_approved_issues,
    deduplicate_findings,
    fetch_open_issues,
    format_issue_body,
    load_open_issues,
    score_against_issue,
)
from app_verify.report import (
    SEVERITIES,
    Finding,
    Invariants,
    RubricAxis,
    RubricScores,
    VerificationReport,
    load_verification_report,
    render_chat_summary,
    render_markdown_report,
)


class _FakeRunner:
    def __init__(self, results: list[CommandResult] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[list[str]] = []

    def run(self, args, *, timeout=None, input_bytes=None, env=None):
        self.calls.append(list(args))
        if self.results:
            return self.results.pop(0)
        return CommandResult(returncode=0, stdout=b"https://github.com/rumap/rumap-android/issues/777\n")


def _ready_findings() -> list[Finding]:
    return []


def _ready_report(**overrides: Any) -> VerificationReport:
    data: dict[str, Any] = dict(
        app="rumap-android",
        platform="android",
        build="debug-2026.11.24",
        commit="abc1234",
        timestamp="2026-11-24T20:00:00Z",
        stand="ssh home (KVM, HDD)",
        artifact_dir=".scratch/app-verify-20261124-2000",
        scenarios_checked=["Запуск приложения", "Открытие настроек", "Мок offline"],
        invariants=Invariants(crash_count=0, anr_count=0, scenarios_passed=12, scenarios_failed=0),
        findings=_ready_findings(),
        rubric=RubricScores(
            functional=RubricAxis(5, "Все базовые сценарии проходят"),
            ui=RubricAxis(4, "Мелкие отступы на узком экране"),
            ux=RubricAxis(4, "Понятные пустые состояния"),
            stability=RubricAxis(5, "0 крашей / 0 ANR"),
        ),
    )
    data.update(overrides)
    return VerificationReport(**data)


BLOCKER = Finding(
    id="B1",
    title="Краш при открытии экрана настроек",
    severity="Blocker",
    screen="settings",
    steps=["Запустить приложение", "Открыть главный экран", "Нажать «Настройки»"],
    expected="Экран настроек открывается",
    actual="Приложение падает с FATAL EXCEPTION",
    screenshots=["artifacts/settings-crash.png"],
    logcat_excerpt="FATAL EXCEPTION: main\njava.lang.NullPointerException",
    category="stability",
    labels=["bug", "crash"],
)

MAJOR = Finding(
    id="M1",
    title="После мока offline не показывается баннер отсутствия сети",
    severity="Major",
    screen="home",
    steps=["Применить мок offline", "Открыть главный экран"],
    expected="Виден баннер «Нет сети»",
    actual="Экран выглядит как при рабочей сети",
    screenshots=["artifacts/offline-home.png"],
    category="functional",
)

UX_POLISH = Finding(
    id="U1",
    title="Кнопка сохранения обрезана на узком экране",
    severity="UX polish",
    screen="settings",
    steps=["Открыть настройки на узком экране"],
    expected="Кнопка целиком видна",
    actual="Правая часть кнопки обрезана",
    screenshots=["artifacts/settings-narrow.png"],
    category="ux",
)

OPEN_ISSUES = [
    OpenIssue(
        number=101,
        title="Краш при открытии экрана настроек",
        body="Экран settings падает с NullPointerException",
        labels=["bug", "crash"],
        url="https://github.com/rumap/rumap-android/issues/101",
    ),
    OpenIssue(
        number=102,
        title="Приложение падает при открытии настроек",
        body="Падение на экране settings",
        labels=["bug"],
        url="https://github.com/rumap/rumap-android/issues/102",
    ),
    OpenIssue(
        number=103,
        title="App crashes when opening Settings screen",
        body="Crash on the settings screen",
        labels=["bug"],
        url="https://github.com/rumap/rumap-android/issues/103",
    ),
    OpenIssue(
        number=104,
        title="Ошибка отображения сети при плохом соединении",
        body="На экране network не виден баннер",
        labels=["bug"],
        url="https://github.com/rumap/rumap-android/issues/104",
    ),
    OpenIssue(
        number=105,
        title="Закрытый дубль: Краш при открытии экрана настроек",
        body="settings crash",
        labels=["bug"],
        url="https://github.com/rumap/rumap-android/issues/105",
        state="closed",
    ),
]


class ReportModelTests(unittest.TestCase):
    def test_ready_verdict_when_no_findings_and_invariants_hold(self) -> None:
        report = _ready_report()
        self.assertEqual(report.verdict, "READY")
        self.assertTrue(report.invariants.ok)

    def test_blocked_when_crash_or_anr_or_blocker_finding(self) -> None:
        crash = _ready_report(invariants=Invariants(crash_count=1, anr_count=0, scenarios_passed=11, scenarios_failed=1))
        self.assertEqual(crash.verdict, "BLOCKED")

        anr = _ready_report(invariants=Invariants(crash_count=0, anr_count=1, scenarios_passed=11, scenarios_failed=1))
        self.assertEqual(anr.verdict, "BLOCKED")

        blocker = _ready_report(findings=[BLOCKER])
        self.assertEqual(blocker.verdict, "BLOCKED")

    def test_conditional_on_major_or_low_rubric_axis(self) -> None:
        major = _ready_report(findings=[MAJOR])
        self.assertEqual(major.verdict, "CONDITIONAL")

        low_axis = _ready_report(
            rubric=RubricScores(
                functional=RubricAxis(3, "Часть сценариев недоступна"),
                ui=RubricAxis(4, ""),
                ux=RubricAxis(4, ""),
                stability=RubricAxis(5, ""),
            )
        )
        self.assertEqual(low_axis.verdict, "CONDITIONAL")

    def test_hard_invariants_flag_ok_false(self) -> None:
        self.assertFalse(Invariants(crash_count=0, anr_count=0, scenarios_passed=5, scenarios_failed=1).ok)
        self.assertTrue(Invariants(crash_count=0, anr_count=0, scenarios_passed=5, scenarios_failed=0, details=["x"]).ok)

    def test_validation_rejects_unknown_severity_category_and_rubric_score(self) -> None:
        with self.assertRaises(ValueError):
            Finding(id="X", title="t", severity="Critical")
        with self.assertRaises(ValueError):
            Finding(id="X", title="t", severity="Minor", category="beauty")
        with self.assertRaises(ValueError):
            RubricAxis(0, "too low")
        with self.assertRaises(ValueError):
            RubricAxis(6, "too high")
        self.assertEqual(SEVERITIES, ("Blocker", "Major", "Minor", "UX polish"))

    def test_round_trip_from_dict_and_file(self) -> None:
        report = _ready_report(findings=[BLOCKER, UX_POLISH])
        payload = report.to_dict()
        restored = load_verification_report(payload)
        self.assertEqual(restored.to_dict(), payload)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            from_file = load_verification_report(path)
        self.assertEqual(from_file.findings[0].id, "B1")
        self.assertEqual(from_file.verdict, "BLOCKED")

    def test_report_from_dict_allows_missing_rubric_and_rejects_duplicate_finding_ids(self) -> None:
        payload = _ready_report().to_dict()
        payload.pop("rubric")
        restored = load_verification_report(payload)
        self.assertIsNotNone(restored.rubric)
        self.assertEqual(restored.rubric.minimum_score, 3)
        self.assertEqual(restored.verdict, "CONDITIONAL")

        dup_payload = _ready_report(findings=[MAJOR]).to_dict()
        dup_payload["findings"].append(dict(dup_payload["findings"][0]))
        with self.assertRaises(ValueError) as ctx:
            load_verification_report(dup_payload)
        self.assertIn("M1", str(ctx.exception))


class ReportRenderingTests(unittest.TestCase):
    def test_markdown_contains_metadata_invariants_rubric_and_findings(self) -> None:
        report = _ready_report(findings=[BLOCKER, UX_POLISH])
        md = render_markdown_report(report)

        self.assertIn("# Отчёт верификации приложения: rumap-android", md)
        self.assertIn("BLOCKED", md)
        self.assertIn("Инварианты", md)
        self.assertIn("0", md)
        self.assertIn("Функционал", md)
        self.assertIn("UI", md)
        self.assertIn("Стабильность", md)
        self.assertIn("B1", md)
        self.assertIn("Краш при открытии экрана настроек", md)
        self.assertIn("Шаги воспроизведения", md)
        self.assertIn("artifacts/settings-crash.png", md)
        self.assertIn("NullPointerException", md)
        self.assertIn("Кнопка сохранения обрезана", md)

    def test_markdown_sorts_findings_by_severity(self) -> None:
        report = _ready_report(findings=[UX_POLISH, MAJOR, BLOCKER])
        md = render_markdown_report(report)
        self.assertLess(md.index("B1"), md.index("M1"))
        self.assertLess(md.index("M1"), md.index("U1"))

    def test_markdown_without_findings_states_clean_run(self) -> None:
        md = render_markdown_report(_ready_report())
        self.assertIn("Находок нет", md)

    def test_markdown_includes_dedup_candidates(self) -> None:
        report = _ready_report(findings=[BLOCKER, MAJOR])
        plan = deduplicate_findings(report.findings, OPEN_ISSUES)
        md = render_markdown_report(report, dedup_plan=plan)
        self.assertIn("Кандидаты в GitHub Issues", md)
        self.assertIn("#101", md)
        self.assertIn("M1", md)

    def test_chat_summary_is_compact_and_links_key_screenshots(self) -> None:
        report = _ready_report(findings=[BLOCKER, MAJOR])
        plan = deduplicate_findings(report.findings, OPEN_ISSUES)
        summary = render_chat_summary(report, dedup_plan=plan)
        self.assertIn("BLOCKED", summary)
        self.assertIn("Функционал", summary)
        self.assertIn("Стабильность", summary)
        self.assertIn("artifacts/settings-crash.png", summary)
        self.assertIn("#101", summary)
        self.assertLess(len(summary.splitlines()), 40)


class DedupTests(unittest.TestCase):
    def test_exact_duplicate_is_matched_to_open_issue(self) -> None:
        plan = deduplicate_findings([BLOCKER], OPEN_ISSUES)
        self.assertEqual(len(plan.duplicates), 1)
        match = plan.duplicates[0]
        self.assertEqual(match.finding.id, "B1")
        self.assertEqual(match.matched_issue_number, 101)
        self.assertIn("101", match.matched_issue_url)
        self.assertGreaterEqual(match.score, 0.58)
        self.assertTrue(match.reason)
        self.assertEqual(plan.new, [])

    def test_ru_and_en_near_duplicates_are_detected(self) -> None:
        ru = Finding(id="R1", title="Падение при открытии настроек", severity="Major", screen="settings")
        en = Finding(id="E1", title="Settings screen crashes on open", severity="Major", screen="settings")
        plan = deduplicate_findings([ru, en], OPEN_ISSUES)
        self.assertEqual({m.finding.id for m in plan.duplicates}, {"R1", "E1"})
        matched = {m.finding.id: m.matched_issue_number for m in plan.duplicates}
        self.assertEqual(matched["R1"], 102)
        self.assertEqual(matched["E1"], 103)

    def test_possible_duplicate_is_flagged_but_not_proposed_for_creation(self) -> None:
        possible = Finding(
            id="P1",
            title="Нет баннера ошибки сети",
            severity="Minor",
            screen="network",
            actual="Экран network без предупреждения",
        )
        plan = deduplicate_findings([possible], OPEN_ISSUES)
        self.assertEqual([m.finding.id for m in plan.possible_duplicates], ["P1"])
        self.assertEqual(plan.new, [])

    def test_new_finding_is_clean_candidate(self) -> None:
        fresh = Finding(
            id="N1",
            title="Кнопка копирования диагностики не реагирует на долгое нажатие",
            severity="Minor",
            screen="diagnostics",
        )
        plan = deduplicate_findings([fresh], OPEN_ISSUES)
        self.assertEqual([m.finding.id for m in plan.new], ["N1"])
        self.assertIsNone(plan.new[0].matched_issue_number)

    def test_closed_issues_are_ignored(self) -> None:
        only_closed = [issue for issue in OPEN_ISSUES if issue.state == "closed"]
        fresh = Finding(id="C1", title="Краш при открытии экрана настроек", severity="Blocker", screen="settings")
        plan = deduplicate_findings([fresh], only_closed)
        self.assertEqual(plan.duplicates, [])
        self.assertEqual([m.finding.id for m in plan.new], ["C1"])

    def test_plan_serializes_and_renders_candidates(self) -> None:
        plan = deduplicate_findings([BLOCKER, MAJOR], OPEN_ISSUES)
        payload = plan.to_dict()
        self.assertEqual(payload["summary"]["duplicate"], 1)
        self.assertEqual(payload["summary"]["new"], 1)
        rendered = plan.render_markdown()
        self.assertIn("M1", rendered)
        self.assertIn("#101", rendered)

    def test_fetch_open_issues_uses_gh_json_command(self) -> None:
        runner = _FakeRunner(
            [
                CommandResult(
                    returncode=0,
                    stdout=json.dumps(
                        [
                            {
                                "number": 42,
                                "title": "Crash on start",
                                "body": "boom",
                                "labels": [{"name": "bug"}, {"name": "crash"}],
                                "url": "https://github.com/rumap/rumap-android/issues/42",
                                "state": "open",
                            },
                            {
                                "number": 43,
                                "title": "Old closed issue",
                                "body": "",
                                "labels": [],
                                "url": "https://github.com/rumap/rumap-android/issues/43",
                                "state": "closed",
                            },
                        ],
                        ensure_ascii=False,
                    ).encode(),
                )
            ]
        )
        issues = fetch_open_issues("rumap/rumap-android", runner=runner)
        self.assertEqual([i.number for i in issues], [42])
        self.assertEqual(issues[0].labels, ["bug", "crash"])
        self.assertEqual(
            runner.calls[0],
            [
                "gh",
                "issue",
                "list",
                "--repo",
                "rumap/rumap-android",
                "--state",
                "open",
                "--limit",
                "200",
                "--json",
                "number,title,body,labels,url,state",
            ],
        )

    def test_fetch_failure_raises_stand_error(self) -> None:
        runner = _FakeRunner([CommandResult(returncode=1, stderr=b"gh: not logged in")])
        with self.assertRaises(StandError):
            fetch_open_issues("rumap/rumap-android", runner=runner)

    def test_load_open_issues_accepts_gh_json_and_dict_wrapper(self) -> None:
        raw = [{"number": 7, "title": "t", "body": "", "labels": ["bug"], "url": "u", "state": "open"}]
        self.assertEqual(load_open_issues(raw)[0].number, 7)
        self.assertEqual(load_open_issues({"issues": raw})[0].labels, ["bug"])
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "issues.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(load_open_issues(path)[0].number, 7)

    def test_identifier_tokens_ignore_numeric_timestamps_and_bare_suffixes(self) -> None:
        tokens = _identifier_tokens(
            [
                "19",
                "00",
                "01",
                "2026",
                "error",
                "crash",
                "exception",
                "ошибка",
                "падение",
                "краш",
                "nullpointerexception",
                "http503",
                "runtimeerror",
            ]
        )
        self.assertEqual(tokens, {"nullpointerexception", "http503", "runtimeerror"})

        finding = Finding(
            id="TS1",
            title="Кнопка экспорта не сохраняет файл",
            severity="Minor",
            screen="export",
            actual="Ошибка в 19:00:01 при нажатии",
            logcat_excerpt="11-24 19:00:01.000 E App: error",
        )
        unrelated_issue = OpenIssue(
            number=201,
            title="Проблема загрузки карты",
            body="Ошибка в 19:00:01, crash лог без стека",
            url="https://github.com/rumap/rumap-android/issues/201",
        )
        score, reasons = score_against_issue(finding, unrelated_issue)
        self.assertFalse(any("идентификаторы ошибки" in reason for reason in reasons))
        self.assertLess(score, 0.35)


class IssueBodyTests(unittest.TestCase):
    def test_body_contains_reproduction_data_and_no_ai_attribution(self) -> None:
        body = format_issue_body(
            BLOCKER,
            {
                "app": "rumap-android",
                "platform": "android",
                "build": "debug-2026.11.24",
                "commit": "abc1234",
                "timestamp": "2026-11-24T20:00:00Z",
                "stand": "ssh home (KVM, HDD)",
                "artifact_dir": ".scratch/app-verify-20261124-2000",
            },
        )
        self.assertIn("Настройки", body)
        self.assertIn("Шаги воспроизведения", body)
        self.assertIn("Экран настроек открывается", body)
        self.assertIn("FATAL EXCEPTION", body)
        self.assertIn("artifacts/settings-crash.png", body)
        self.assertIn("Blocker", body)
        lowered = body.lower()
        for forbidden in (r"\bai\b", r"\bии\b", r"\bgpt\b", r"\bgemini\b", r"\bcodex\b", r"\bopencode\b", r"\bclaude\b", r"\bdeepseek\b", r"\bагент\w*\b"):
            self.assertIsNone(re.search(forbidden, lowered), f"в теле issue найдено запрещённое: {forbidden}")


class OwnerGateTests(unittest.TestCase):
    def _plan(self) -> DedupPlan:
        return deduplicate_findings([BLOCKER, MAJOR, UX_POLISH], OPEN_ISSUES)

    def test_create_without_owner_approval_raises_and_never_calls_gh(self) -> None:
        runner = _FakeRunner()
        with self.assertRaises(OwnerApprovalRequiredError):
            create_approved_issues(
                self._plan(),
                repo="rumap/rumap-android",
                approved_finding_ids=["M1"],
                runner=runner,
            )
        self.assertEqual(runner.calls, [])

    def test_create_with_owner_approval_but_no_ids_never_calls_gh(self) -> None:
        runner = _FakeRunner()
        with self.assertRaises(OwnerApprovalRequiredError):
            create_approved_issues(
                self._plan(),
                repo="rumap/rumap-android",
                owner_approved=True,
                approved_finding_ids=[],
                runner=runner,
            )
        self.assertEqual(runner.calls, [])

    def test_create_only_for_approved_new_findings(self) -> None:
        runner = _FakeRunner()
        result = create_approved_issues(
            self._plan(),
            repo="rumap/rumap-android",
            owner_approved=True,
            approved_finding_ids=["M1"],
            runner=runner,
        )
        self.assertEqual([c.finding_id for c in result.created], ["M1"])
        self.assertEqual(len(runner.calls), 1)
        call = runner.calls[0]
        self.assertEqual(call[:6], ["gh", "issue", "create", "--repo", "rumap/rumap-android", "--title"])
        self.assertIn("--body", call)
        self.assertIn("баннер", " ".join(call))
        self.assertNotIn("B1", [c.finding_id for c in result.created])

    def test_approve_all_new_skips_duplicates_and_possible_duplicates(self) -> None:
        runner = _FakeRunner()
        result = create_approved_issues(
            self._plan(),
            repo="rumap/rumap-android",
            owner_approved=True,
            approve_all_new=True,
            runner=runner,
        )
        self.assertEqual({c.finding_id for c in result.created}, {"M1", "U1"})
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("B1", result.skipped)

    def test_dry_run_returns_plan_without_gh_calls(self) -> None:
        runner = _FakeRunner()
        result = create_approved_issues(
            self._plan(),
            repo="rumap/rumap-android",
            approved_finding_ids=["M1"],
            approve_all_new=True,
            runner=runner,
            dry_run=True,
        )
        self.assertTrue(result.dry_run)
        self.assertEqual(result.created, [])
        self.assertEqual({p.finding.id for p in result.planned}, {"M1", "U1"})
        self.assertEqual(runner.calls, [])

    def test_gh_create_failure_raises_stand_error(self) -> None:
        runner = _FakeRunner([CommandResult(returncode=1, stderr=b"label not found")])
        with self.assertRaises(StandError):
            create_approved_issues(
                self._plan(),
                repo="rumap/rumap-android",
                owner_approved=True,
                approved_finding_ids=["M1"],
                runner=runner,
            )


class ReportCliTests(unittest.TestCase):
    def _write_run(self, root: Path, report: VerificationReport) -> Path:
        path = root / "run.json"
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False), encoding="utf-8")
        return path

    def test_cli_report_json_and_markdown_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_path = self._write_run(root, _ready_report(findings=[BLOCKER]))
            out_path = root / "report.md"

            out = io.StringIO()
            err = io.StringIO()
            rc = run_cli(
                ["--json", "report", "--input", str(run_path), "--output", str(out_path)],
                stdout=out,
                stderr=err,
            )
            self.assertEqual(rc, 0, err.getvalue())
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["verdict"], "BLOCKED")
            self.assertTrue(out_path.exists())
            self.assertIn("B1", out_path.read_text(encoding="utf-8"))

    def test_cli_report_chat_summary_and_open_issues(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_path = self._write_run(root, _ready_report(findings=[BLOCKER, MAJOR]))
            issues_path = root / "issues.json"
            issues_path.write_text(
                json.dumps([issue.to_dict() for issue in OPEN_ISSUES], ensure_ascii=False),
                encoding="utf-8",
            )
            out = io.StringIO()
            err = io.StringIO()
            rc = run_cli(
                ["report", "--input", str(run_path), "--open-issues", str(issues_path), "--chat-summary"],
                stdout=out,
                stderr=err,
            )
            self.assertEqual(rc, 0, err.getvalue())
            text = out.getvalue()
            self.assertIn("BLOCKED", text)
            self.assertIn("#101", text)
            self.assertNotIn("## Метаданные прогона", text)

    def test_cli_report_rejects_bad_severity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = _ready_report().to_dict()
            payload["findings"] = [{"id": "Z", "title": "t", "severity": "Critical"}]
            run_path = root / "run.json"
            run_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            out = io.StringIO()
            err = io.StringIO()
            rc = run_cli(["--json", "report", "--input", str(run_path)], stdout=out, stderr=err)
            self.assertEqual(rc, 1)
            self.assertFalse(json.loads(out.getvalue())["ok"])


class IssuesCliTests(unittest.TestCase):
    def _write_inputs(self, root: Path) -> tuple[Path, Path]:
        run_path = root / "run.json"
        run_path.write_text(
            json.dumps(_ready_report(findings=[BLOCKER, MAJOR, UX_POLISH]).to_dict(), ensure_ascii=False),
            encoding="utf-8",
        )
        issues_path = root / "issues.json"
        issues_path.write_text(
            json.dumps([issue.to_dict() for issue in OPEN_ISSUES], ensure_ascii=False),
            encoding="utf-8",
        )
        return run_path, issues_path

    def test_cli_issues_dedup_prints_plan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_path, issues_path = self._write_inputs(root)
            out = io.StringIO()
            err = io.StringIO()
            rc = run_cli(
                ["--json", "issues", "dedup", "--input", str(run_path), "--open-issues", str(issues_path)],
                stdout=out,
                stderr=err,
            )
            self.assertEqual(rc, 0, err.getvalue())
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["summary"]["duplicate"], 1)
            self.assertEqual(payload["summary"]["new"], 2)

    def test_cli_issues_create_without_confirmation_fails_without_gh(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_path, issues_path = self._write_inputs(root)
            fake = _FakeRunner()
            with mock.patch("app_verify.issues.SubprocessRunner", return_value=fake):
                out = io.StringIO()
                err = io.StringIO()
                rc = run_cli(
                    [
                        "--json",
                        "issues",
                        "create",
                        "--input",
                        str(run_path),
                        "--repo",
                        "rumap/rumap-android",
                        "--open-issues",
                        str(issues_path),
                        "--approve",
                        "M1",
                    ],
                    stdout=out,
                    stderr=err,
                )
            self.assertEqual(rc, 1)
            self.assertFalse(json.loads(out.getvalue())["ok"])
            self.assertEqual(fake.calls, [])

    def test_cli_issues_create_dry_run_returns_plan_without_gh(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_path, issues_path = self._write_inputs(root)
            fake = _FakeRunner()
            with mock.patch("app_verify.issues.SubprocessRunner", return_value=fake):
                out = io.StringIO()
                err = io.StringIO()
                rc = run_cli(
                    [
                        "--json",
                        "issues",
                        "create",
                        "--input",
                        str(run_path),
                        "--repo",
                        "rumap/rumap-android",
                        "--open-issues",
                        str(issues_path),
                        "--approve-all-new",
                        "--dry-run",
                    ],
                    stdout=out,
                    stderr=err,
                )
            self.assertEqual(rc, 0, err.getvalue())
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["dry_run"])
            self.assertEqual(sorted(item["finding_id"] for item in payload["planned"]), ["M1", "U1"])
            self.assertEqual(fake.calls, [])

    def test_cli_issues_create_with_confirmation_files_issues(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_path, issues_path = self._write_inputs(root)
            fake = _FakeRunner()
            with mock.patch("app_verify.issues.SubprocessRunner", return_value=fake):
                out = io.StringIO()
                err = io.StringIO()
                rc = run_cli(
                    [
                        "--json",
                        "issues",
                        "create",
                        "--input",
                        str(run_path),
                        "--repo",
                        "rumap/rumap-android",
                        "--open-issues",
                        str(issues_path),
                        "--approve",
                        "M1",
                        "--confirm-owner-approval",
                    ],
                    stdout=out,
                    stderr=err,
                )
            self.assertEqual(rc, 0, err.getvalue())
            payload = json.loads(out.getvalue())
            self.assertEqual([c["finding_id"] for c in payload["created"]], ["M1"])
            self.assertEqual(len(fake.calls), 1)

    def test_scenarios_failed_blocks_verdict(self) -> None:
        report = _ready_report(invariants=Invariants(crash_count=0, anr_count=0, scenarios_passed=5, scenarios_failed=1))
        self.assertFalse(report.invariants.ok)
        self.assertEqual(report.verdict, "BLOCKED")
        self.assertTrue(any("сценариев" in reason for reason in report.blocking_reasons))

    def test_long_json_strings_load_without_enametoolong(self) -> None:
        report_json = json.dumps(_ready_report(findings=[BLOCKER, MAJOR, UX_POLISH]).to_dict(), ensure_ascii=False)
        self.assertGreater(len(report_json), 300)
        loaded_report = load_verification_report(report_json)
        self.assertEqual(len(loaded_report.findings), 3)

        issues_json = json.dumps([issue.to_dict() for issue in OPEN_ISSUES], ensure_ascii=False)
        self.assertGreater(len(issues_json), 300)
        loaded_issues = load_open_issues(issues_json)
        self.assertEqual(len(loaded_issues), len(OPEN_ISSUES))

    def test_malformed_finding_and_bool_score_return_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bad_finding = _ready_report().to_dict()
            bad_finding["findings"] = [{"id": "X1"}]
            p1 = root / "bad_finding.json"
            p1.write_text(json.dumps(bad_finding), encoding="utf-8")
            out = io.StringIO()
            rc = run_cli(["--json", "report", "--input", str(p1)], stdout=out, stderr=io.StringIO())
            self.assertEqual(rc, 1)
            self.assertIn("title", json.loads(out.getvalue())["error"])

            bad_score = _ready_report().to_dict()
            bad_score["rubric"]["ui"]["score"] = True
            p2 = root / "bad_score.json"
            p2.write_text(json.dumps(bad_score), encoding="utf-8")
            out2 = io.StringIO()
            rc2 = run_cli(["--json", "report", "--input", str(p2)], stdout=out2, stderr=io.StringIO())
            self.assertEqual(rc2, 1)
            self.assertFalse(json.loads(out2.getvalue())["ok"])

    def test_create_approved_issues_empty_repo_and_explicit_possible_duplicate(self) -> None:
        possible_finding = Finding(
            id="P1",
            title="Диалог региона зависает при смене",
            severity="Minor",
            screen="region",
        )
        open_issue = OpenIssue(
            number=77,
            title="Выбор региона не сохраняется после перезапуска",
            body="Экран region",
            url="https://github.com/rumap/rumap-android/issues/77",
        )
        plan = deduplicate_findings([possible_finding], [open_issue])
        self.assertEqual(plan.entries[0].status, "possible_duplicate")

        with self.assertRaises(StandError):
            create_approved_issues(
                plan,
                repo="   ",
                owner_approved=True,
                approved_finding_ids=["P1"],
                runner=_FakeRunner(),
            )

        # approve_all_new skips possible_duplicate
        fake_skip = _FakeRunner()
        res_skip = create_approved_issues(
            plan,
            repo="rumap/rumap-android",
            owner_approved=True,
            approve_all_new=True,
            runner=fake_skip,
        )
        self.assertEqual(res_skip.created, [])
        self.assertEqual(res_skip.skipped, ["P1"])
        self.assertEqual(fake_skip.calls, [])

        # explicit owner approval of possible_duplicate is allowed
        fake_allow = _FakeRunner()
        res_allow = create_approved_issues(
            plan,
            repo="rumap/rumap-android",
            owner_approved=True,
            approved_finding_ids=["P1"],
            runner=fake_allow,
        )
        self.assertEqual([item.finding_id for item in res_allow.created], ["P1"])
        self.assertEqual(len(fake_allow.calls), 1)

    def test_explicit_owner_approval_overrides_duplicate_and_adds_default_triage_label(self) -> None:
        plan = deduplicate_findings([BLOCKER, MAJOR], OPEN_ISSUES)
        self.assertEqual(plan.entries[0].status, "duplicate")

        # approve_all_new still skips duplicate
        fake_skip = _FakeRunner()
        res_skip = create_approved_issues(
            plan,
            repo="rumap/rumap-android",
            owner_approved=True,
            approve_all_new=True,
            runner=fake_skip,
        )
        self.assertEqual([c.finding_id for c in res_skip.created], ["M1"])
        self.assertEqual(res_skip.skipped, ["B1"])
        self.assertIn("status:needs-triage", DEFAULT_ISSUE_LABELS)
        self.assertIn("--label", fake_skip.calls[0])
        self.assertIn("status:needs-triage", fake_skip.calls[0])
        self.assertNotIn("--assignee", fake_skip.calls[0])

        # explicit owner approval overrides duplicate
        fake_override = _FakeRunner()
        res_override = create_approved_issues(
            plan,
            repo="rumap/rumap-android",
            owner_approved=True,
            approved_finding_ids=["B1"],
            runner=fake_override,
        )
        self.assertEqual([c.finding_id for c in res_override.created], ["B1"])
        self.assertEqual(res_override.skipped, [])
        self.assertEqual(fake_override.calls[0].count("status:needs-triage"), 1)

        # explicit status:* label is preserved without adding duplicate status:needs-triage
        custom_status = Finding(
            id="S1",
            title="Уникальная находка с явным статусом",
            severity="Minor",
            labels=["bug", "status:needs-owner"],
        )
        custom_plan = deduplicate_findings([custom_status], [])
        fake_custom = _FakeRunner()
        create_approved_issues(
            custom_plan,
            repo="rumap/rumap-android",
            owner_approved=True,
            approved_finding_ids=["S1"],
            runner=fake_custom,
        )
        self.assertIn("status:needs-owner", fake_custom.calls[0])
        self.assertNotIn("status:needs-triage", fake_custom.calls[0])

    def test_mid_batch_failure_preserves_created_issues_in_exception_and_cli_json(self) -> None:
        plan = deduplicate_findings([BLOCKER, MAJOR, UX_POLISH], OPEN_ISSUES)
        runner = _FakeRunner(
            [
                CommandResult(returncode=0, stdout=b"https://github.com/rumap/rumap-android/issues/801\n"),
                CommandResult(returncode=1, stderr=b"secondary rate limit"),
            ]
        )
        with self.assertRaises(IssueCreationError) as ctx:
            create_approved_issues(
                plan,
                repo="rumap/rumap-android",
                owner_approved=True,
                approve_all_new=True,
                runner=runner,
            )
        self.assertEqual([c.finding_id for c in ctx.exception.result.created], ["M1"])
        self.assertEqual(ctx.exception.result.created[0].number, 801)
        self.assertEqual([f.finding_id for f in ctx.exception.result.failed], ["U1"])
        self.assertIn("rate limit", ctx.exception.result.failed[0].error)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_path, issues_path = self._write_inputs(root)
            cli_runner = _FakeRunner(
                [
                    CommandResult(returncode=0, stdout=b"https://github.com/rumap/rumap-android/issues/802\n"),
                    CommandResult(returncode=1, stderr=b"HTTP 422 label missing"),
                ]
            )
            with mock.patch("app_verify.issues.SubprocessRunner", return_value=cli_runner):
                out = io.StringIO()
                err = io.StringIO()
                rc = run_cli(
                    [
                        "--json",
                        "issues",
                        "create",
                        "--input",
                        str(run_path),
                        "--repo",
                        "rumap/rumap-android",
                        "--open-issues",
                        str(issues_path),
                        "--approve-all-new",
                        "--confirm-owner-approval",
                    ],
                    stdout=out,
                    stderr=err,
                )
            self.assertEqual(rc, 1)
            payload = json.loads(out.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIn("HTTP 422", payload["error"])
            self.assertEqual([c["finding_id"] for c in payload["created"]], ["M1"])
            self.assertEqual(payload["created"][0]["number"], 802)
            self.assertEqual([f["finding_id"] for f in payload["failed"]], ["U1"])

    def test_cli_missing_input_or_open_issues_reports_file_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            missing_run = root / "missing-run.json"
            out = io.StringIO()
            rc = run_cli(["--json", "report", "--input", str(missing_run)], stdout=out, stderr=io.StringIO())
            self.assertEqual(rc, 1)
            err_msg = json.loads(out.getvalue())["error"]
            self.assertIn("No such file", err_msg)
            self.assertNotIn("Expecting value", err_msg)

            run_path, _ = self._write_inputs(root)
            missing_issues = root / "missing-issues.json"
            out2 = io.StringIO()
            rc2 = run_cli(
                ["--json", "issues", "dedup", "--input", str(run_path), "--open-issues", str(missing_issues)],
                stdout=out2,
                stderr=io.StringIO(),
            )
            self.assertEqual(rc2, 1)
            err_msg2 = json.loads(out2.getvalue())["error"]
            self.assertIn("No such file", err_msg2)
            self.assertNotIn("Expecting value", err_msg2)


if __name__ == "__main__":
    unittest.main()
