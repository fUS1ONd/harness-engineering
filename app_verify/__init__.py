"""Платформо-независимый каркас верификации приложений и адаптеры стендов."""

from .core import (
    AdapterFactory,
    AdapterRegistry,
    ArtifactRecord,
    InstallResult,
    LaunchResult,
    LogAnomaly,
    LogReport,
    PlatformAdapter,
    StandError,
    StandStatus,
    UnknownPlatformError,
    VerificationSession,
)

__all__ = [
    "AdapterFactory",
    "AdapterRegistry",
    "ArtifactRecord",
    "InstallResult",
    "LaunchResult",
    "LogAnomaly",
    "LogReport",
    "PlatformAdapter",
    "StandError",
    "StandStatus",
    "UnknownPlatformError",
    "VerificationSession",
]
