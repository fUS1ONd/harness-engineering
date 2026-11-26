"""Подписка временного пользователя для живого E2E-прогона."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

from .core import StandError

CONFIG_FILES = ("/root/nodes-infrastructure/.env", "/opt/remnawave/.env", "/root/.env")
TEST_SQUADS = ("413fd3d2-fa16-4d14-8538-52746e3863b4", "7cd2e959-54bb-4935-9462-f28ddd3adce7")
CONFIG_LINE = re.compile(r"^(REMNAWAVE_API_TOKEN|REMNAWAVE_TOKEN|API_TOKEN|REMNAWAVE_PANEL_URL|SUB_PUBLIC_DOMAIN)=(.+)$", re.M)


def _config() -> tuple[str, str, str]:
    values: dict[str, str] = {}
    for path in CONFIG_FILES:
        try:
            source = Path(path).read_text()
        except FileNotFoundError:
            continue
        for key, value in CONFIG_LINE.findall(source):
            values.setdefault(key, value.strip().strip("\"'"))
    for key in ("REMNAWAVE_API_TOKEN", "REMNAWAVE_TOKEN", "REMNAWAVE_PANEL_URL", "SUB_PUBLIC_DOMAIN"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    token = values.get("REMNAWAVE_API_TOKEN") or values.get("REMNAWAVE_TOKEN") or values.get("API_TOKEN")
    panel = values.get("REMNAWAVE_PANEL_URL")
    subscription_host = values.get("SUB_PUBLIC_DOMAIN", "")
    if not (token and panel):
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "leaseweb-nl",
             "grep -hE '^(REMNAWAVE_API_TOKEN|REMNAWAVE_TOKEN|API_TOKEN|REMNAWAVE_PANEL_URL|SUB_PUBLIC_DOMAIN)=' "
             + " ".join(CONFIG_FILES) + " 2>/dev/null"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        remote = {k: v.strip().strip("\"'") for k, v in CONFIG_LINE.findall(result.stdout)}
        token = token or remote.get("REMNAWAVE_API_TOKEN") or remote.get("REMNAWAVE_TOKEN") or remote.get("API_TOKEN")
        panel = panel or remote.get("REMNAWAVE_PANEL_URL")
        subscription_host = subscription_host or remote.get("SUB_PUBLIC_DOMAIN", "")
    if not (token and panel):
        raise StandError("Нет конфигурации Remnawave. Передайте --subscription-url или APP_VERIFY_VPN_SUBSCRIPTION_URL")
    return panel.rstrip("/"), token, subscription_host


class TemporarySubscription:
    def __init__(self) -> None:
        self.username = f"bs_matrix_{uuid4().hex[:16]}"
        self.panel, self.token, self.subscription_host = _config()
        self.user_id: str | None = None
        self.url: str | None = None
        self.create_attempted = False

    @property
    def uuid(self) -> str | None:
        return self.user_id

    def _request(self, method: str, path: str, body: dict | None = None) -> dict | None:
        request = urllib.request.Request(
            self.panel + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                "User-Agent": "app-verify-bs-matrix/1.0",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and method in ("GET", "DELETE"):
                return None
            raise StandError(f"Remnawave {method}: HTTP {exc.code}") from None
        except (OSError, ValueError):
            raise StandError(f"Remnawave {method}: запрос не завершился или вернул некорректный JSON") from None

    @staticmethod
    def _user(payload: dict | None) -> dict:
        user = (payload or {}).get("response", payload or {})
        return user if isinstance(user, dict) else {}

    @staticmethod
    def _identifier(user: dict) -> str | None:
        # Remnawave 2.x отдаёт числовой `id` вместо `uuid`; DELETE /api/users/{id} принимает оба.
        value = user.get("uuid") or user.get("id")
        return str(value) if value not in (None, "") else None

    def _lookup(self) -> dict | None:
        found = self._request("GET", f"/api/users/by-username/{self.username}")
        return self._user(found) if found is not None else None

    def _subscription_url(self, user: dict) -> str:
        url = user.get("subscriptionUrl")
        if not url and self.subscription_host and user.get("shortUuid"):
            base = self.subscription_host.rstrip("/")
            if "://" not in base:
                base = f"https://{base}"
            url = f"{base}/{user['shortUuid']}"
        if not url:
            raise StandError("Remnawave не вернул URL временной подписки")
        return url

    def _remember(self, user: dict) -> str:
        self.user_id = self._identifier(user)
        if not self.user_id:
            raise StandError("Remnawave не вернул id временного пользователя")
        self.url = self._subscription_url(user)
        return self.url

    def create(self) -> str:
        if self.url:
            return self.url
        if self.create_attempted:
            existing = self._lookup()
            if existing:
                return self._remember(existing)
        expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        self.create_attempted = True
        try:
            response = self._request("POST", "/api/users", {
                "username": self.username,
                "status": "ACTIVE",
                "expireAt": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "activeInternalSquads": list(TEST_SQUADS),
                "hwidDeviceLimit": 2,
            })
        except StandError:
            existing = self._lookup()
            if not existing:
                raise
            return self._remember(existing)
        user = self._user(response)
        if not self._identifier(user):
            user = self._lookup() or user
        return self._remember(user)

    def delete(self) -> dict:
        if not self.create_attempted:
            return {"deleted": False}
        if not self.user_id:
            user = self._lookup()
            if user is None:
                return {"deleted": True, "username": self.username}
            self.user_id = self._identifier(user)
            if not self.user_id:
                raise StandError(f"Remnawave не вернул id пользователя {self.username}")
        self._request("DELETE", f"/api/users/{self.user_id}")
        if self._lookup() is not None:
            raise StandError(f"Временный пользователь {self.username} всё ещё существует")
        self.user_id = None
        self.url = None
        self.create_attempted = False
        return {"deleted": True, "username": self.username}
