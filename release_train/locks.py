"""Идемпотентные замки и журнал выкатки: единственная локальная граница состояния."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Protocol

SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


class LockStore(Protocol):
    def acquire(self, repo: str, key: str, sha: str) -> bool: ...

    def release(self, repo: str, key: str, sha: str) -> None: ...

    def contains(self, repo: str, key: str, sha: str) -> bool: ...

    def paused(self) -> bool: ...

    def deployed_sha(self, repo: str) -> str | None: ...

    def mark_deployed(self, repo: str, sha: str) -> None: ...


def _safe(value: str) -> str:
    return SAFE_RE.sub("-", value).strip("-")


class FileLockStore:
    """Замки — файлы в каталоге состояния; повтор того же `(repo, key, sha)` их видит.

    Замки не истекают: идемпотентность webhook'ов держится именно на том, что
    отработанный переход остаётся отмеченным. Снимает замок только явный `release`
    при упавшей мутации, чтобы шаг можно было повторить.
    """

    def __init__(self, directory: Path | str, pause_file: Path | str | None = None) -> None:
        self.directory = Path(directory)
        self.pause_file = Path(pause_file) if pause_file else None

    def _lock_path(self, repo: str, key: str, sha: str) -> Path:
        return self.directory / f"{_safe(repo)}__{_safe(key)}__{_safe(sha)[:12]}.lock"

    def _ledger_path(self, repo: str) -> Path:
        return self.directory / f"deployed__{_safe(repo)}.json"

    def acquire(self, repo: str, key: str, sha: str) -> bool:
        path = self._lock_path(repo, key, sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()} {int(time.time())}\n")
        return True

    def release(self, repo: str, key: str, sha: str) -> None:
        try:
            self._lock_path(repo, key, sha).unlink()
        except OSError:
            pass

    def contains(self, repo: str, key: str, sha: str) -> bool:
        return self._lock_path(repo, key, sha).exists()

    def paused(self) -> bool:
        return self.pause_file is not None and self.pause_file.exists()

    def deployed_sha(self, repo: str) -> str | None:
        try:
            data = json.loads(self._ledger_path(repo).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        sha = data.get("deployed_sha") if isinstance(data, dict) else None
        return sha if isinstance(sha, str) and sha else None

    def mark_deployed(self, repo: str, sha: str) -> None:
        path = self._ledger_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"repo": repo, "deployed_sha": sha, "recorded_at": int(time.time())}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
