"""Дедупликация находок по открытым GitHub Issues и owner-gated создание issue."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Mapping

from .android import CommandRunner, SubprocessRunner
from .core import StandError
from .report import Finding

DUPLICATE_THRESHOLD = 0.58
POSSIBLE_THRESHOLD = 0.35
GH_ISSUE_LIST_JSON = "number,title,body,labels,url,state"
URL_PATTERN = re.compile(r"https?://\S+?/issues/(\d+)")

_STOPWORDS = frozenset(
    {
        "а",
        "без",
        "бы",
        "в",
        "во",
        "вот",
        "для",
        "до",
        "его",
        "если",
        "есть",
        "и",
        "из",
        "или",
        "их",
        "к",
        "как",
        "ко",
        "когда",
        "ли",
        "мне",
        "на",
        "над",
        "не",
        "него",
        "нет",
        "но",
        "ну",
        "о",
        "об",
        "от",
        "по",
        "при",
        "с",
        "со",
        "так",
        "то",
        "у",
        "что",
        "это",
        "the",
        "a",
        "an",
        "and",
        "app",
        "application",
        "at",
        "был",
        "была",
        "было",
        "быть",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "приложение",
        "to",
        "when",
        "with",
    }
)

_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+")
_IDENTIFIER_SUFFIXES = ("exception", "error", "crash", "ошибка", "падение", "краш")


class OwnerApprovalRequiredError(StandError):
    """Попытка создать GitHub Issues без явного подтверждения владельца."""


@dataclass(frozen=True)
class OpenIssue:
    """Открытая (не ``closed``) issue целевого репозитория."""

    number: int
    title: str
    body: str = ""
    labels: list[str] = field(default_factory=list)
    url: str = ""
    state: str = "open"

    @property
    def is_open(self) -> bool:
        return (self.state or "").strip().lower() != "closed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "labels": list(self.labels),
            "url": self.url,
            "state": self.state,
        }

    @classmethod
    def from_gh_json(cls, data: Mapping[str, Any]) -> "OpenIssue":
        raw_labels = data.get("labels") or []
        labels: list[str] = []
        for label in raw_labels:
            if isinstance(label, Mapping):
                name = label.get("name")
                if name:
                    labels.append(str(name))
            elif label:
                labels.append(str(label))
        return cls(
            number=int(data.get("number", 0)),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            labels=labels,
            url=str(data.get("url") or ""),
            state=str(data.get("state") or "open"),
        )


@dataclass(frozen=True)
class PlannedIssue:
    finding: Finding
    title: str
    body: str
    labels: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding.id,
            "title": self.title,
            "body": self.body,
            "labels": list(self.labels),
        }


@dataclass(frozen=True)
class CreatedIssue:
    finding_id: str
    number: int | None
    url: str
    title: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "number": self.number,
            "url": self.url,
            "title": self.title,
        }


@dataclass
class IssueFilingResult:
    planned: list[PlannedIssue] = field(default_factory=list)
    created: list[CreatedIssue] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "planned": [item.to_dict() for item in self.planned],
            "created": [item.to_dict() for item in self.created],
            "skipped": list(self.skipped),
        }


@dataclass
class DedupMatch:
    """Результат сверки одной находки с открытыми issue."""

    finding: Finding
    status: str
    score: float = 0.0
    reason: str = ""
    matched_issue: OpenIssue | None = None

    @property
    def matched_issue_number(self) -> int | None:
        return self.matched_issue.number if self.matched_issue is not None else None

    @property
    def matched_issue_title(self) -> str | None:
        return self.matched_issue.title if self.matched_issue is not None else None

    @property
    def matched_issue_url(self) -> str | None:
        return self.matched_issue.url if self.matched_issue is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "finding": self.finding.to_dict(),
            "score": round(self.score, 3),
            "reason": self.reason,
            "matched_issue_number": self.matched_issue_number,
            "matched_issue_title": self.matched_issue_title,
            "matched_issue_url": self.matched_issue_url,
        }


@dataclass
class DedupPlan:
    """План дедупликации: точные дубли, возможные дубли и новые кандидаты."""

    entries: list[DedupMatch] = field(default_factory=list)

    @property
    def duplicates(self) -> list[DedupMatch]:
        return [match for match in self.entries if match.status == "duplicate"]

    @property
    def possible_duplicates(self) -> list[DedupMatch]:
        return [match for match in self.entries if match.status == "possible_duplicate"]

    @property
    def new(self) -> list[DedupMatch]:
        return [match for match in self.entries if match.status == "new"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "duplicate": len(self.duplicates),
                "possible_duplicate": len(self.possible_duplicates),
                "new": len(self.new),
            },
            "duplicates": [match.to_dict() for match in self.duplicates],
            "possible_duplicates": [match.to_dict() for match in self.possible_duplicates],
            "new": [match.to_dict() for match in self.new],
        }

    def render_markdown(self) -> str:
        return render_dedup_markdown(self)


def _normalize_tokens(text: str) -> list[str]:
    lowered = (text or "").lower().replace("ё", "е")
    return [token for token in _TOKEN_RE.findall(lowered) if len(token) > 1 and token not in _STOPWORDS]


def _normalized_text(text: str) -> str:
    return " ".join(_normalize_tokens(text))


def _identifier_tokens(tokens: Iterable[str]) -> set[str]:
    return {
        token
        for token in tokens
        if any(ch.isdigit() for ch in token) or token.endswith(_IDENTIFIER_SUFFIXES)
    }


def score_against_issue(finding: Finding, issue: OpenIssue) -> tuple[float, list[str]]:
    """Комбинированный скор токенного пересечения, SequenceMatcher и бустов."""
    finding_tokens = set(_normalize_tokens(finding.title))
    issue_tokens = set(_normalize_tokens(issue.title))
    union = finding_tokens | issue_tokens
    jaccard = len(finding_tokens & issue_tokens) / len(union) if union else 0.0
    ratio = SequenceMatcher(None, _normalized_text(finding.title), _normalized_text(issue.title)).ratio()
    score = 0.55 * jaccard + 0.35 * ratio
    reasons: list[str] = []
    if finding_tokens and issue_tokens and (finding_tokens <= issue_tokens or issue_tokens <= finding_tokens):
        score += 0.12
        reasons.append("токены заголовка вложены")

    screen_tokens = set(_normalize_tokens(finding.screen))
    issue_blob_tokens = set(_normalize_tokens(f"{issue.title} {issue.body}"))
    if screen_tokens and screen_tokens & issue_blob_tokens:
        score += 0.15
        reasons.append(f"совпадает экран '{finding.screen}'")

    finding_identifiers = _identifier_tokens(
        _normalize_tokens(f"{finding.title} {finding.actual} {finding.logcat_excerpt or ''}")
    )
    issue_identifiers = _identifier_tokens(
        _normalize_tokens(f"{issue.title} {issue.body}")
    )
    shared_identifiers = finding_identifiers & issue_identifiers
    if shared_identifiers:
        score += 0.12
        reasons.append("общие идентификаторы ошибки: " + ", ".join(sorted(shared_identifiers)))

    return min(score, 1.0), reasons


def deduplicate_findings(
    findings: Iterable[Finding],
    open_issues: Iterable[OpenIssue],
    *,
    duplicate_threshold: float = DUPLICATE_THRESHOLD,
    possible_threshold: float = POSSIBLE_THRESHOLD,
) -> DedupPlan:
    """Сверяет находки с открытыми issue и делит их на duplicate/possible/new."""
    issues = [issue for issue in open_issues if issue.is_open]
    entries: list[DedupMatch] = []

    for finding in findings:
        best_issue: OpenIssue | None = None
        best_score = 0.0
        best_reasons: list[str] = []
        for issue in issues:
            score, reasons = score_against_issue(finding, issue)
            if score > best_score:
                best_issue, best_score, best_reasons = issue, score, reasons

        if best_issue is not None and best_score >= duplicate_threshold:
            detail = "; ".join(best_reasons) or "высокое сходство заголовков"
            entries.append(
                DedupMatch(
                    finding=finding,
                    status="duplicate",
                    score=best_score,
                    reason=f"совпадение {best_score:.2f} с #{best_issue.number}: {detail}",
                    matched_issue=best_issue,
                )
            )
        elif best_issue is not None and best_score >= possible_threshold:
            detail = "; ".join(best_reasons) or "частичное сходство"
            entries.append(
                DedupMatch(
                    finding=finding,
                    status="possible_duplicate",
                    score=best_score,
                    reason=f"возможное совпадение {best_score:.2f} с #{best_issue.number}: {detail}",
                    matched_issue=best_issue,
                )
            )
        else:
            entries.append(
                DedupMatch(
                    finding=finding,
                    status="new",
                    score=best_score,
                    reason="совпадений с открытыми issue не найдено",
                )
            )

    return DedupPlan(entries=entries)


def render_dedup_markdown(plan: DedupPlan) -> str:
    """Рендерит секцию кандидатов в GitHub Issues для подтверждения владельцем."""
    lines = ["## Кандидаты в GitHub Issues (на подтверждение владельцу)", ""]

    if plan.new:
        lines.append("### Новые кандидаты (создание только после явного «ок» владельца)")
        lines.append("")
        for match in plan.new:
            finding = match.finding
            lines.append(
                f"- [ ] `{finding.id}` — {finding.title} "
                f"({finding.severity}, экран: {finding.screen or '—'})"
            )
        lines.append("")
    else:
        lines.append("Новых кандидатов нет: все находки совпали с открытыми issue.")
        lines.append("")

    if plan.possible_duplicates:
        lines.append("### Возможные дубли (не создаются автоматически)")
        lines.append("")
        for match in plan.possible_duplicates:
            lines.append(
                f"- `{match.finding.id}` — {match.finding.title} → "
                f"#{match.matched_issue_number} {match.matched_issue_title} "
                f"(score {match.score:.2f}; {match.reason})"
            )
        lines.append("")

    if plan.duplicates:
        lines.append("### Точные дубли (пропущены)")
        lines.append("")
        for match in plan.duplicates:
            lines.append(
                f"- `{match.finding.id}` — {match.finding.title} → "
                f"#{match.matched_issue_number} {match.matched_issue_title} "
                f"({match.matched_issue_url or '—'})"
            )
        lines.append("")

    return "\n".join(lines).rstrip()


def _is_existing_file(text: str) -> bool:
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return False
    try:
        return Path(text).exists()
    except OSError:
        return False


def load_open_issues(source: Iterable[Mapping[str, Any]] | Mapping[str, Any] | str | Path) -> list[OpenIssue]:
    """Загружает открытые issue из списка, gh-JSON, файла или JSON-строки."""
    if isinstance(source, Mapping):
        raw = source.get("issues", [])
    elif isinstance(source, Path):
        payload = json.loads(source.read_text(encoding="utf-8"))
        raw = payload.get("issues", []) if isinstance(payload, Mapping) else payload
    elif isinstance(source, str):
        payload = json.loads(Path(source).read_text(encoding="utf-8") if _is_existing_file(source) else source)
        raw = payload.get("issues", []) if isinstance(payload, Mapping) else payload
    else:
        raw = list(source)
    return [OpenIssue.from_gh_json(item) for item in raw]


def fetch_open_issues(
    repo: str,
    *,
    runner: CommandRunner | None = None,
    limit: int = 200,
) -> list[OpenIssue]:
    """Загружает открытые issue через `gh issue list --json ...` (только открытые)."""
    if not repo.strip():
        raise StandError("Не указан репозиторий для загрузки открытых issue")
    runner = runner or SubprocessRunner()
    argv = [
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        str(limit),
        "--json",
        GH_ISSUE_LIST_JSON,
    ]
    result = runner.run(argv, timeout=60.0)
    if result.returncode != 0:
        details = result.stderr_text.strip() or result.stdout_text.strip() or f"code={result.returncode}"
        raise StandError(f"Не удалось получить открытые issue {repo}: {details}")
    try:
        payload = json.loads(result.stdout_text or "[]")
    except json.JSONDecodeError as exc:
        raise StandError(f"Некорректный JSON от `gh issue list`: {exc}") from exc
    issues = [OpenIssue.from_gh_json(item) for item in payload]
    return [issue for issue in issues if issue.is_open]


def _meta_value(report_meta: Mapping[str, Any] | Any | None, key: str) -> str:
    if report_meta is None:
        return ""
    if isinstance(report_meta, Mapping):
        value = report_meta.get(key, "")
    else:
        value = getattr(report_meta, key, "")
    return str(value or "")


def format_issue_body(finding: Finding, report_meta: Mapping[str, Any] | Any | None) -> str:
    """Формирует тело GitHub Issue с воспроизведением и доказательствами."""
    lines: list[str] = []
    lines.append("## Что произошло")
    lines.append("")
    lines.append(finding.title)
    lines.append("")
    lines.append("## Severity и категория")
    lines.append("")
    lines.append(f"- Severity: {finding.severity}")
    lines.append(f"- Категория: {finding.category}")
    lines.append(f"- Экран: {finding.screen or '—'}")
    if finding.labels:
        lines.append("- Метки: " + ", ".join(finding.labels))
    lines.append("")

    lines.append("## Шаги воспроизведения")
    lines.append("")
    if finding.steps:
        for index, step in enumerate(finding.steps, 1):
            lines.append(f"{index}. {step}")
    else:
        lines.append("Шаги не зафиксированы.")
    lines.append("")

    lines.append("## Ожидаемое поведение")
    lines.append("")
    lines.append(finding.expected or "—")
    lines.append("")
    lines.append("## Фактическое поведение")
    lines.append("")
    lines.append(finding.actual or "—")
    lines.append("")

    lines.append("## Доказательства")
    lines.append("")
    if finding.screenshots:
        lines.append("Скриншоты:")
        for path in finding.screenshots:
            lines.append(f"- `{path}`")
    else:
        lines.append("Скриншоты не приложены.")
    lines.append("")
    if finding.logcat_excerpt:
        lines.append("Фрагмент logcat:")
        lines.append("")
        lines.append("```text")
        lines.append(finding.logcat_excerpt.rstrip())
        lines.append("```")
        lines.append("")

    lines.append("## Окружение")
    lines.append("")
    for key, label in (
        ("app", "Приложение"),
        ("platform", "Платформа"),
        ("build", "Сборка"),
        ("commit", "Коммит"),
        ("timestamp", "Время прогона"),
        ("stand", "Стенд"),
        ("artifact_dir", "Каталог артефактов"),
    ):
        value = _meta_value(report_meta, key)
        if value:
            lines.append(f"- {label}: {value}")
    lines.append("")
    lines.append("---")
    lines.append("Источник: отчёт верификации приложения (см. каталог артефактов).")

    return "\n".join(lines).rstrip() + "\n"


def create_approved_issues(
    plan: DedupPlan,
    *,
    repo: str,
    owner_approved: bool = False,
    approved_finding_ids: Iterable[str] | None = None,
    approve_all_new: bool = False,
    runner: CommandRunner | None = None,
    report_meta: Mapping[str, Any] | Any | None = None,
    dry_run: bool = False,
) -> IssueFilingResult:
    """Заводит issue только для явно одобренных владельцем новых находок.

    Без ``owner_approved=True`` и явного списка ID (или ``approve_all_new``) функция
    поднимает ``OwnerApprovalRequiredError`` и не выполняет ни одной команды.
    """
    approved = [str(item) for item in (approved_finding_ids or [])]

    if not dry_run:
        if not owner_approved:
            raise OwnerApprovalRequiredError(
                "Заведение GitHub Issues требует явного подтверждения владельца (owner_approved=True)"
            )
        if not approved and not approve_all_new:
            raise OwnerApprovalRequiredError(
                "Заведение GitHub Issues требует явного списка одобренных ID (approved_finding_ids) "
                "или approve_all_new=True"
            )
        if not repo.strip():
            raise StandError("Не указан репозиторий для создания GitHub Issues")

    known_ids = {match.finding.id for match in plan.entries}
    unknown = [item for item in approved if item not in known_ids]
    if unknown:
        raise StandError("Неизвестные ID находок для заведения issue: " + ", ".join(unknown))

    selected: list[DedupMatch] = []
    skipped: list[str] = []
    for match in plan.entries:
        explicitly_approved = match.finding.id in approved
        wants_creation = approve_all_new or explicitly_approved
        if match.status == "duplicate":
            if wants_creation:
                skipped.append(match.finding.id)
            continue
        if match.status == "possible_duplicate":
            if explicitly_approved:
                selected.append(match)
            elif approve_all_new:
                skipped.append(match.finding.id)
            continue
        if wants_creation:
            selected.append(match)

    planned: list[PlannedIssue] = []
    for match in selected:
        labels = list(match.finding.labels)
        planned.append(
            PlannedIssue(
                finding=match.finding,
                title=match.finding.title,
                body=format_issue_body(match.finding, report_meta),
                labels=labels,
            )
        )

    result = IssueFilingResult(planned=planned, created=[], skipped=skipped, dry_run=dry_run)
    if dry_run:
        return result

    runner = runner or SubprocessRunner()
    for item in planned:
        argv = ["gh", "issue", "create", "--repo", repo, "--title", item.title, "--body", item.body]
        for label in item.labels:
            argv.extend(["--label", label])
        command_result = runner.run(argv, timeout=60.0)
        if command_result.returncode != 0:
            details = (
                command_result.stderr_text.strip()
                or command_result.stdout_text.strip()
                or f"code={command_result.returncode}"
            )
            raise StandError(f"Не удалось создать issue '{item.title}': {details}")
        match = URL_PATTERN.search(command_result.stdout_text or "")
        url = match.group(0) if match else ""
        number = int(match.group(1)) if match else None
        result.created.append(
            CreatedIssue(finding_id=item.finding.id, number=number, url=url, title=item.title)
        )

    return result
