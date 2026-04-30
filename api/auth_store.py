"""
auth_store.py — pluggable storage for users.

Default backend writes to /tmp/users.json (works on Vercel out of the box, but
data is lost on cold start).  If UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN
are set, the Upstash Redis backend is used instead (truly persistent, free tier:
upstash.com — no credit card, 10k commands/day).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests


# ── Abstract interface ────────────────────────────────────────────────────────
class UserStore:
    def list_users(self) -> list[dict]: ...
    def get_user(self, email: str) -> Optional[dict]: ...
    def create_user(self, email: str, password_hash: str, full_name: str, role: str) -> dict: ...
    def update_user(self, email: str, **fields) -> Optional[dict]: ...
    def delete_user(self, email: str) -> bool: ...


# ── Helper: build a user dict ─────────────────────────────────────────────────
def _new_user(email: str, password_hash: str, full_name: str, role: str) -> dict:
    return {
        "email":         email.lower().strip(),
        "password_hash": password_hash,
        "full_name":     full_name.strip(),
        "role":          role,
        "created_at":    datetime.now(timezone.utc).isoformat(),
        "last_login":    None,
    }


# ── File-backed store (default) ───────────────────────────────────────────────
class FileUserStore(UserStore):
    """JSON file at /tmp/users.json — ephemeral on Vercel cold start."""

    def __init__(self, path: str = "/tmp/users.json"):
        self._path = Path(path)
        self._lock = threading.Lock()
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text("[]")

    def _load(self) -> list[dict]:
        try:
            return json.loads(self._path.read_text() or "[]")
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, users: list[dict]) -> None:
        self._path.write_text(json.dumps(users, indent=2))

    def list_users(self) -> list[dict]:
        with self._lock:
            return self._load()

    def get_user(self, email: str) -> Optional[dict]:
        email = email.lower().strip()
        for u in self.list_users():
            if u.get("email") == email:
                return u
        return None

    def create_user(self, email, password_hash, full_name, role):
        with self._lock:
            users = self._load()
            email_l = email.lower().strip()
            if any(u["email"] == email_l for u in users):
                raise ValueError("Email already registered")
            user = _new_user(email_l, password_hash, full_name, role)
            users.append(user)
            self._save(users)
            return user

    def update_user(self, email, **fields):
        email_l = email.lower().strip()
        with self._lock:
            users = self._load()
            for i, u in enumerate(users):
                if u["email"] == email_l:
                    u.update(fields)
                    users[i] = u
                    self._save(users)
                    return u
        return None

    def delete_user(self, email):
        email_l = email.lower().strip()
        with self._lock:
            users = self._load()
            new = [u for u in users if u["email"] != email_l]
            if len(new) == len(users):
                return False
            self._save(new)
            return True


# ── Upstash Redis backend (persistent) ────────────────────────────────────────
class UpstashUserStore(UserStore):
    """Stores all users as a JSON blob under one key.  Single-key design keeps
    the implementation simple and stays well within free-tier op limits."""

    KEY = "adf:users"

    def __init__(self, url: str, token: str):
        self._url = url.rstrip("/")
        self._token = token
        self._lock = threading.Lock()

    def _req(self, *path_parts: str, body: Optional[str] = None) -> dict:
        url = f"{self._url}/" + "/".join(path_parts)
        headers = {"Authorization": f"Bearer {self._token}"}
        if body is None:
            r = requests.get(url, headers=headers, timeout=10)
        else:
            r = requests.post(url, headers=headers, data=body, timeout=10)
        r.raise_for_status()
        return r.json()

    def _load(self) -> list[dict]:
        try:
            data = self._req("get", self.KEY)
            raw = data.get("result")
            if not raw:
                return []
            return json.loads(raw)
        except (requests.RequestException, json.JSONDecodeError):
            return []

    def _save(self, users: list[dict]) -> None:
        self._req("set", self.KEY, body=json.dumps(users))

    def list_users(self) -> list[dict]:
        return self._load()

    def get_user(self, email):
        email = email.lower().strip()
        for u in self._load():
            if u.get("email") == email:
                return u
        return None

    def create_user(self, email, password_hash, full_name, role):
        with self._lock:
            users = self._load()
            email_l = email.lower().strip()
            if any(u["email"] == email_l for u in users):
                raise ValueError("Email already registered")
            user = _new_user(email_l, password_hash, full_name, role)
            users.append(user)
            self._save(users)
            return user

    def update_user(self, email, **fields):
        email_l = email.lower().strip()
        with self._lock:
            users = self._load()
            for i, u in enumerate(users):
                if u["email"] == email_l:
                    u.update(fields)
                    users[i] = u
                    self._save(users)
                    return u
        return None

    def delete_user(self, email):
        email_l = email.lower().strip()
        with self._lock:
            users = self._load()
            new = [u for u in users if u["email"] != email_l]
            if len(new) == len(users):
                return False
            self._save(new)
            return True


# ── Factory ───────────────────────────────────────────────────────────────────
_singleton: Optional[UserStore] = None

def get_user_store() -> UserStore:
    global _singleton
    if _singleton is not None:
        return _singleton

    url   = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip()
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
    if url and token:
        _singleton = UpstashUserStore(url, token)
    else:
        _singleton = FileUserStore()
    return _singleton
