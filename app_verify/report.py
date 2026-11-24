"""Платформо-независимый отчёт верификации: находки, инварианты, рубрика и рендер."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


class _DedupMatchLike(Protocol):
    @property
    def finding(self) -> Finding: ...

    @property
    def matched_issue_number(self) -> int | None: ...


class DedupPlanProtocol(Protocol):
    @property
    def new(self) -> Sequence[_DedupMatchLike]: ...

    @property
    def duplicates(self) -> Sequence[_DedupMatchLike]: ...

    @property
    def possible_duplicates(self) -> Sequence[_DedupMatchLike]: ...

    def render_markdown(self) -> str: ...

SEVERITIES = ("Blocker", "Major", "Minor", "UX polish")
CATEGORIES = ("functional", "ui", "ux", "stability")
RUBRIC_AXES = (
    ("functional", "Функционал"),
    ("ui", "UI"),
    ("ux", "UX"),
    ("stability", "Стабильность"),
)
SEVERITY_ORDER = {severity: index for index, severity in enumerate(SEVERITIES)}


def _validate_text(value: Any, field_name: str) -> str:
    text = "" if value is None else str(value)
    if not text.strip():
        raise ValueError(f"{field_name} не должен быть пустым")
    return text


@dataclass
class Finding:
    """Находка прогона верификации с шагами воспроизведения и доказательствами."""

    id: str
    title: str
    severity: str
    screen: str = ""
    steps: list[str] = field(default_factory=list)
    expected: str = ""
    actual: str = ""
    screenshots: list[str] = field(default_factory=list)
    logcat_excerpt: str | None = None
    category: str = "functional"
    labels: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.id = _validate_text(self.id, "Finding.id")
        self.title = _validate_text(self.title, "Finding.title")
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"Недопустимая severity {self.severity!r}; ожидается одна из {', '.join(SEVERITIES)}"
            )
        if self.category not in CATEGORIES:
            raise ValueError(
                f"Недопустимая category {self.category!r}; ожидается одна из {', '.join(CATEGORIES)}"
            )
        self.steps = [str(step) for step in self.steps]
        self.screenshots = [str(path) for path in self.screenshots]
        self.labels = [str(label) for label in self.labels]
        self.screen = str(self.screen or "")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RubricAxis:
    """Оценка одной оси рубрики по шкале 1..5 с заметкой."""

    score: int
    notes: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.score, bool) or not isinstance(self.score, int):
            raise ValueError(f"Оценка рубрики должна быть целым числом 1..5, получено: {self.score!r}")
        if not 1 <= self.score <= 5:
            raise ValueError(f"Оценка рубрики должна быть в диапазоне 1..5, получено: {self.score}")
        self.notes = str(self.notes or "")

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "notes": self.notes}


@dataclass
class RubricScores:
    """Итоговая рубрика: Функционал / UI / UX / Стабильность."""

    functional: RubricAxis
    ui: RubricAxis
    ux: RubricAxis
    stability: RubricAxis

    def axis_items(self) -> list[tuple[str, str, RubricAxis]]:
        return [
            (key, label, getattr(self, key))
            for key, label in RUBRIC_AXES
        ]

    @property
    def minimum_score(self) -> int:
        return min(axis.score for _key, _label, axis in self.axis_items())

    def to_dict(self) -> dict[str, Any]:
        return {key: axis.to_dict() for key, _label, axis in self.axis_items()}


@dataclass
class Invariants:
    """Жёсткие инварианты прогона: 0 крашей, 0 ANR, 0 проваленных сценариев."""

    crash_count: int = 0
    anr_count: int = 0
    scenarios_passed: int = 0
    scenarios_failed: int = 0
    details: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for name in ("crash_count", "anr_count", "scenarios_passed", "scenarios_failed"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Invariants.{name} должен быть неотрицательным целым, получено: {value!r}")
        self.details = [str(item) for item in self.details]

    @property
    def ok(self) -> bool:
        return self.crash_count == 0 and self.anr_count == 0 and self.scenarios_failed == 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationReport:
    """Полный отчёт прогона агентской верификации приложения."""

    app: str
    invariants: Invariants
    findings: list[Finding] = field(default_factory=list)
    rubric: RubricScores | None = None
    platform: str = "android"
    build: str = ""
    commit: str = ""
    timestamp: str = ""
    stand: str = ""
    artifact_dir: str = ""
    scenarios_checked: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.app = _validate_text(self.app, "VerificationReport.app")
        if self.rubric is None:
            self.rubric = RubricScores(
                functional=RubricAxis(3, "Рубрика не заполнена"),
                ui=RubricAxis(3, "Рубрика не заполнена"),
                ux=RubricAxis(3, "Рубрика не заполнена"),
                stability=RubricAxis(3, "Рубрика не заполнена"),
            )
        self.scenarios_checked = [str(item) for item in self.scenarios_checked]

    @property
    def blocking_reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.invariants.crash_count > 0:
            reasons.append(f"крашей: {self.invariants.crash_count}")
        if self.invariants.anr_count > 0:
            reasons.append(f"ANR: {self.invariants.anr_count}")
        if self.invariants.scenarios_failed > 0:
            reasons.append(f"провалено сценариев: {self.invariants.scenarios_failed}")
        blockers = [finding.id for finding in self.findings if finding.severity == "Blocker"]
        if blockers:
            reasons.append("находки Blocker: " + ", ".join(blockers))
        return reasons

    @property
    def verdict(self) -> str:
        if (
            self.invariants.crash_count > 0
            or self.invariants.anr_count > 0
            or self.invariants.scenarios_failed > 0
        ):
            return "BLOCKED"
        if any(finding.severity == "Blocker" for finding in self.findings):
            return "BLOCKED"
        if any(finding.severity == "Major" for finding in self.findings):
            return "CONDITIONAL"
        if self.rubric is not None and self.rubric.minimum_score <= 3:
            return "CONDITIONAL"
        return "READY"

    def sorted_findings(self) -> list[Finding]:
        return sorted(
            self.findings,
            key=lambda finding: (SEVERITY_ORDER[finding.severity], finding.id),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "app": self.app,
            "platform": self.platform,
            "build": self.build,
            "commit": self.commit,
            "timestamp": self.timestamp,
            "stand": self.stand,
            "artifact_dir": self.artifact_dir,
            "scenarios_checked": list(self.scenarios_checked),
            "invariants": self.invariants.to_dict(),
            "findings": [finding.to_dict() for finding in self.findings],
            "rubric": self.rubric.to_dict() if self.rubric is not None else {},
            "verdict": self.verdict,
        }


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _strict_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Поле {field_name} должно быть целым числом, получено: {value!r}")
    return value


def _rubric_axis_from_payload(payload: Any) -> RubricAxis:
    if isinstance(payload, RubricAxis):
        return payload
    if isinstance(payload, Mapping):
        score = _strict_int(payload.get("score"), "rubric.score")
        return RubricAxis(score=score, notes=str(payload.get("notes") or ""))
    if isinstance(payload, int) and not isinstance(payload, bool):
        return RubricAxis(score=payload)
    raise ValueError(f"Некорректная ось рубрики: {payload!r}")


def _finding_from_payload(item: Any, index: int) -> Finding:
    if not isinstance(item, Mapping):
        raise ValueError(f"Элемент findings[{index}] должен быть объектом, получено: {type(item).__name__}")
    for key in ("id", "title", "severity"):
        if key not in item:
            raise ValueError(f"В findings[{index}] отсутствует обязательное поле '{key}'")
    return Finding(
        id=item["id"],
        title=item["title"],
        severity=item["severity"],
        screen=item.get("screen", ""),
        steps=_as_list(item.get("steps")),
        expected=item.get("expected", ""),
        actual=item.get("actual", ""),
        screenshots=_as_list(item.get("screenshots")),
        logcat_excerpt=item.get("logcat_excerpt"),
        category=item.get("category", "functional"),
        labels=_as_list(item.get("labels")),
    )


def report_from_dict(data: Mapping[str, Any]) -> VerificationReport:
    if not isinstance(data, Mapping):
        raise ValueError("Корневой объект отчёта должен быть JSON-объектом")
    rubric_raw = data.get("rubric") or {}
    if not isinstance(rubric_raw, Mapping):
        raise ValueError("Поле 'rubric' должно быть объектом")
    rubric = RubricScores(
        functional=_rubric_axis_from_payload(rubric_raw.get("functional", 0)),
        ui=_rubric_axis_from_payload(rubric_raw.get("ui", 0)),
        ux=_rubric_axis_from_payload(rubric_raw.get("ux", 0)),
        stability=_rubric_axis_from_payload(rubric_raw.get("stability", 0)),
    )
    invariants_raw = data.get("invariants") or {}
    if not isinstance(invariants_raw, Mapping):
        raise ValueError("Поле 'invariants' должно быть объектом")
    invariants = Invariants(
        crash_count=_strict_int(invariants_raw.get("crash_count", 0), "invariants.crash_count"),
        anr_count=_strict_int(invariants_raw.get("anr_count", 0), "invariants.anr_count"),
        scenarios_passed=_strict_int(invariants_raw.get("scenarios_passed", 0), "invariants.scenarios_passed"),
        scenarios_failed=_strict_int(invariants_raw.get("scenarios_failed", 0), "invariants.scenarios_failed"),
        details=_as_list(invariants_raw.get("details")),
    )
    findings = [
        _finding_from_payload(item, index)
        for index, item in enumerate(_as_list(data.get("findings")))
    ]
    return VerificationReport(
        app=str(data.get("app") or "unknown"),
        platform=str(data.get("platform") or "android"),
        build=str(data.get("build") or ""),
        commit=str(data.get("commit") or ""),
        timestamp=str(data.get("timestamp") or ""),
        stand=str(data.get("stand") or ""),
        artifact_dir=str(data.get("artifact_dir") or ""),
        scenarios_checked=_as_list(data.get("scenarios_checked")),
        invariants=invariants,
        findings=findings,
        rubric=rubric,
    )


def _is_existing_file(text: str) -> bool:
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return False
    try:
        return Path(text).exists()
    except OSError:
        return False


def load_verification_report(source: Mapping[str, Any] | str | Path) -> VerificationReport:
    """Загружает отчёт из словаря, JSON-файла или JSON-строки."""
    if isinstance(source, Mapping):
        return report_from_dict(source)
    if isinstance(source, Path):
        return report_from_dict(json.loads(source.read_text(encoding="utf-8")))
    text = str(source)
    if _is_existing_file(text):
        return report_from_dict(json.loads(Path(text).read_text(encoding="utf-8")))
    return report_from_dict(json.loads(text))


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown_report(
    report: VerificationReport,
    dedup_plan: DedupPlanProtocol | None = None,
) -> str:
    """Рендерит полный структурированный Markdown-отчёт прогона."""
    lines: list[str] = []
    lines.append(f"# Отчёт верификации приложения: {report.app}")
    lines.append("")
    lines.append(f"**Вердикт готовности:** `{report.verdict}`")
    if report.blocking_reasons:
        lines.append("")
        lines.append("Блокирующие причины: " + "; ".join(report.blocking_reasons) + ".")
    lines.append("")

    lines.append("## Метаданные прогона")
    lines.append("")
    lines.append("| Параметр | Значение |")
    lines.append("| --- | --- |")
    metadata = (
        ("Приложение", report.app),
        ("Платформа", report.platform),
        ("Сборка", report.build),
        ("Коммит", report.commit),
        ("Время прогона", report.timestamp),
        ("Стенд", report.stand),
        ("Каталог артефактов", report.artifact_dir),
    )
    for label, value in metadata:
        lines.append(f"| {label} | {_markdown_cell(value or '—')} |")
    lines.append("")

    if report.scenarios_checked:
        lines.append("Проверенные сценарии: " + ", ".join(f"`{item}`" for item in report.scenarios_checked) + ".")
        lines.append("")

    invariants = report.invariants
    lines.append("## Инварианты")
    lines.append("")
    lines.append("| Инвариант | Значение | Статус |")
    lines.append("| --- | --- | --- |")
    invariant_rows = (
        ("Краши", invariants.crash_count, invariants.crash_count == 0),
        ("ANR", invariants.anr_count, invariants.anr_count == 0),
        ("Сценарии пройдены", invariants.scenarios_passed, True),
        ("Сценарии провалены", invariants.scenarios_failed, invariants.scenarios_failed == 0),
    )
    for label, value, passed in invariant_rows:
        status = "OK" if passed else "ПРОВАЛ"
        lines.append(f"| {label} | {value} | {status} |")
    for detail in invariants.details:
        lines.append(f"| Детали | {_markdown_cell(detail)} | — |")
    lines.append("")

    lines.append("## Рубрика")
    lines.append("")
    lines.append("| Ось | Оценка | Заметки |")
    lines.append("| --- | --- | --- |")
    if report.rubric is not None:
        for _key, label, axis in report.rubric.axis_items():
            lines.append(f"| {label} | {axis.score}/5 | {_markdown_cell(axis.notes or '—')} |")
    lines.append("")

    lines.append(f"## Находки ({len(report.findings)})")
    lines.append("")
    if not report.findings:
        lines.append("Находок нет: инварианты и ключевые сценарии выполнены.")
        lines.append("")
    else:
        for finding in report.sorted_findings():
            lines.append(f"### {finding.severity}: {finding.id} — {finding.title}")
            lines.append("")
            lines.append(f"- **Экран:** {finding.screen or '—'}")
            lines.append(f"- **Категория:** {finding.category}")
            if finding.labels:
                lines.append("- **Метки:** " + ", ".join(finding.labels))
            lines.append(f"- **Ожидаемо:** {finding.expected or '—'}")
            lines.append(f"- **Фактически:** {finding.actual or '—'}")
            if finding.steps:
                lines.append("- **Шаги воспроизведения:**")
                for index, step in enumerate(finding.steps, 1):
                    lines.append(f"  {index}. {step}")
            if finding.screenshots:
                lines.append("- **Скриншоты:** " + ", ".join(f"`{path}`" for path in finding.screenshots))
            if finding.logcat_excerpt:
                lines.append("- **Logcat:**")
                lines.append("")
                lines.append("```text")
                lines.append(finding.logcat_excerpt.rstrip())
                lines.append("```")
            lines.append("")

    if dedup_plan is not None:
        lines.append(dedup_plan.render_markdown())
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_chat_summary(
    report: VerificationReport,
    dedup_plan: DedupPlanProtocol | None = None,
) -> str:
    """Рендерит лаконичную сводку для чата T3 со ссылками на ключевые скриншоты."""
    invariants = report.invariants
    lines = [
        f"**Вердикт:** `{report.verdict}` — краши {invariants.crash_count}, ANR {invariants.anr_count}, "
        f"сценарии {invariants.scenarios_passed} passed / {invariants.scenarios_failed} failed",
    ]
    if report.rubric is not None:
        rubric_line = " · ".join(
            f"{label} {axis.score}/5" for _key, label, axis in report.rubric.axis_items()
        )
        lines.append(f"**Рубрика:** {rubric_line}")

    counts = {severity: 0 for severity in SEVERITIES}
    for finding in report.findings:
        counts[finding.severity] += 1
    lines.append(
        "**Находки:** "
        + " · ".join(f"{severity} {counts[severity]}" for severity in SEVERITIES)
    )

    highlights = [finding for finding in report.sorted_findings() if finding.severity in ("Blocker", "Major")]
    if not highlights:
        highlights = report.sorted_findings()
    for finding in highlights[:3]:
        lines.append(f"- `{finding.id}` [{finding.severity}] {finding.title}")

    screenshots: list[str] = []
    for finding in highlights:
        for path in finding.screenshots:
            if path not in screenshots:
                screenshots.append(path)
    if screenshots:
        lines.append("**Ключевые скриншоты:** " + ", ".join(f"`{path}`" for path in screenshots[:3]))

    if dedup_plan is not None:
        new_matches = list(dedup_plan.new)
        duplicates = list(dedup_plan.duplicates)
        possible = list(dedup_plan.possible_duplicates)
        candidate_ids = ", ".join(match.finding.id for match in new_matches) or "—"
        lines.append(f"**Кандидаты в issue:** {candidate_ids}")
        if duplicates or possible:
            dup_numbers = ", ".join(
                f"#{match.matched_issue_number}" for match in duplicates + possible if match.matched_issue_number
            )
            lines.append(f"**Дубли/возможные дубли:** {dup_numbers or '—'}")

    lines.append("**Заведение issue:** только после явного подтверждения владельца.")
    return "\n".join(lines)
