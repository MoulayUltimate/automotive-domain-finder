"""
auth.py — bcrypt password hashing + JWT session tokens.

JWT secret comes from the JWT_SECRET env var.  If unset, we derive a stable
secret from VERCEL_URL (or a dev fallback) so tokens stay valid for the life
of a deployment without forcing the user to set anything up.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt


_TOKEN_EXPIRY_DAYS = 7
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _jwt_secret() -> str:
    explicit = os.environ.get("JWT_SECRET", "").strip()
    if explicit:
        return explicit
    # Deterministic fallback so tokens survive across serverless invocations
    seed = os.environ.get("VERCEL_URL", "adf-local-dev-secret")
    return hashlib.sha256(seed.encode()).hexdigest()


# ── Password hashing ──────────────────────────────────────────────────────────
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ── JWT ───────────────────────────────────────────────────────────────────────
def create_token(user: dict) -> str:
    payload = {
        "email": user["email"],
        "role":  user.get("role", "member"),
        "exp":   datetime.now(timezone.utc) + timedelta(days=_TOKEN_EXPIRY_DAYS),
        "iat":   datetime.now(timezone.utc),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def verify_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


# ── Validators ────────────────────────────────────────────────────────────────
def valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email or ""))


def valid_password(password: str) -> tuple[bool, str]:
    if not password:
        return False, "Password is required"
    if len(password) < 6:
        return False, "Password must be at least 6 characters"
    return True, ""


def public_user(user: dict) -> dict:
    """Strip secret fields before returning a user to a client."""
    return {k: v for k, v in user.items() if k != "password_hash"}
