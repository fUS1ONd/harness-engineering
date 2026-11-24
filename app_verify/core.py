"""Общий платформо-независимый слой верификации приложений.

Определяет единый контракт платформенного адаптера стенда (Android, в будущем Web/Bot),
реестр адаптеров, сессию с артефактами и структурированные результаты операций.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


class StandError(RuntimeError):
    """Базовая ошибка операций стенда верификации."""


class UnknownPlatformError(StandError):
    """Запрошен незарегистрированный платформенный адаптер."""


def is_existing_file(text: str) -> bool:
    """Проверяет, указывает ли строка на существующий файл, избегая OSError на длинном JSON."""
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return False
    try:
        return Path(text).is_file()
    except OSError:
        return False


@dataclass(frozen=True)
class StandStatus:
    platform: str
    state: str
    target_id: str
    endpoint: str
    snapshot: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ArtifactRecord:
    kind: str
    path: str
    size_bytes: int
    valid: bool
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InstallResult:
    platform: str
    artifact: str
    target_id: str
    ok: bool
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LaunchResult:
    platform: str
    app_id: str
    component: str
    ok: bool
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LogAnomaly:
    kind: str
    summary: str
    lines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LogReport:
    platform: str
    raw_text: str
    output_path: str | None
    crashes: list[LogAnomaly] = field(default_factory=list)
    anrs: list[LogAnomaly] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.crashes and not self.anrs

    def to_dict(self, include_raw: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "platform": self.platform,
            "ok": self.ok,
            "output_path": self.output_path,
            "crash_count": len(self.crashes),
            "anr_count": len(self.anrs),
            "crashes": [c.to_dict() for c in self.crashes],
            "anrs": [a.to_dict() for a in self.anrs],
            "details": dict(self.details),
        }
        if include_raw:
            data["raw_text"] = self.raw_text
        return data


@dataclass
class VerificationSession:
    platform: str
    session_id: str
    artifacts: list[ArtifactRecord] = field(default_factory=list)

    def add_artifact(self, record: ArtifactRecord) -> None:
        self.artifacts.append(record)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "session_id": self.session_id,
            "artifacts": [item.to_dict() for item in self.artifacts],
        }


class PlatformAdapter(ABC):
    """Единый контракт стенда верификации для любой целевой платформы."""

    platform: str

    @abstractmethod
    def prepare(self) -> StandStatus:
        """Идемпотентно проверяет и подготавливает стенд."""

    @abstractmethod
    def start(self, snapshot: str | None = None, reset: bool = True) -> StandStatus:
        """Поднимает или откатывает стенд к чистому снапшоту и открывает доступ."""

    @abstractmethod
    def stop(self) -> StandStatus:
        """Останавливает стенд и закрывает транспортные туннели."""

    @abstractmethod
    def status(self) -> StandStatus:
        """Возвращает текущее состояние стенда и доступность цели."""

    @abstractmethod
    def install(self, artifact_path: Path, **options: Any) -> InstallResult:
        """Устанавливает проверяемый артефакт (например, debug APK)."""

    @abstractmethod
    def launch(self, target: str, activity: str | None = None) -> LaunchResult:
        """Запускает проверяемое приложение."""

    @abstractmethod
    def screenshot(self, output_path: Path) -> ArtifactRecord:
        """Снимает и валидирует скриншот текущего экрана."""

    @abstractmethod
    def collect_logs(
        self,
        output_path: Path | None = None,
        clear: bool = False,
        package: str | None = None,
    ) -> LogReport:
        """Собирает логи и выделяет критические сбои (crash/ANR)."""

    def save_snapshot(self, name: str | None = None) -> StandStatus:
        """Сохраняет базовый снапшот стенда (опционально для платформ с поддержкой снапшотов)."""
        raise StandError(f"Платформа '{self.platform}' не поддерживает snapshot-save")


AdapterFactory = Callable[..., PlatformAdapter]


class AdapterRegistry:
    """Реестр платформенных адаптеров верификации."""

    def __init__(self) -> None:
        self._factories: dict[str, AdapterFactory] = {}

    def register(self, name: str, factory: AdapterFactory) -> None:
        key = name.strip().lower()
        if not key:
            raise ValueError("Platform name must not be empty")
        self._factories[key] = factory

    def list_platforms(self) -> list[str]:
        return sorted(self._factories.keys())

    def create(self, name: str, **kwargs: Any) -> PlatformAdapter:
        key = name.strip().lower()
        if key not in self._factories:
            available = ", ".join(self.list_platforms()) or "<none>"
            raise UnknownPlatformError(
                f"Неизвестная платформа '{name}'. Доступные адаптеры: {available}"
            )
        return self._factories[key](**kwargs)
