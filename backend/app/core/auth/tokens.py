"""Access tokens (short-lived JWT) and refresh tokens (opaque, random, stored hashed).

The verifier pins the expected algorithm from configuration — the token header's
``alg`` is never trusted, so ``alg=none`` and HS256-with-the-public-key attacks
are rejected before any claim is read.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt

from app.config import Settings


@dataclass(frozen=True, slots=True)
class AccessClaims:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str
    email: str
    jti: str
    expires_at: datetime


class TokenError(Exception):
    pass


class TokenService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.algorithm = settings.jwt_algorithm
        if self.algorithm == "HS256":
            secret = settings.jwt_secret.get_secret_value()
            if len(secret.encode()) < 32:
                raise ValueError("JWT_SECRET must be at least 32 bytes for HS256")
            self._sign_key: str | bytes = secret
            self._verify_key: str | bytes = secret
        else:
            if not settings.jwt_private_key_path or not settings.jwt_public_key_path:
                raise ValueError("RS256 requires JWT_PRIVATE_KEY_PATH and JWT_PUBLIC_KEY_PATH")
            self._sign_key = Path(settings.jwt_private_key_path).read_bytes()
            self._verify_key = Path(settings.jwt_public_key_path).read_bytes()

    # ── access tokens ────────────────────────────────────────────────────────
    def issue_access(self, *, user_id: uuid.UUID, tenant_id: uuid.UUID, role: str, email: str) -> tuple[str, datetime]:
        now = datetime.now(UTC)
        exp = now + timedelta(minutes=self.settings.access_token_ttl_minutes)
        payload = {
            "iss": self.settings.jwt_issuer,
            "aud": self.settings.jwt_audience,
            "sub": str(user_id),
            "tid": str(tenant_id),
            "role": role,
            "email": email,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "jti": secrets.token_hex(8),
        }
        return jwt.encode(payload, self._sign_key, algorithm=self.algorithm), exp

    def verify_access(self, token: str) -> AccessClaims:
        try:
            payload = jwt.decode(
                token,
                self._verify_key,
                algorithms=[self.algorithm],  # pinned; the header's alg is not consulted
                issuer=self.settings.jwt_issuer,
                audience=self.settings.jwt_audience,
                options={"require": ["exp", "iat", "nbf", "sub", "jti"], "verify_signature": True},
                leeway=5,
            )
        except jwt.PyJWTError as exc:
            raise TokenError(type(exc).__name__) from None
        try:
            return AccessClaims(
                user_id=uuid.UUID(payload["sub"]),
                tenant_id=uuid.UUID(payload["tid"]),
                role=str(payload["role"]),
                email=str(payload.get("email", "")),
                jti=str(payload["jti"]),
                expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
            )
        except (KeyError, ValueError) as exc:
            raise TokenError("malformed claims") from exc

    # ── refresh tokens ───────────────────────────────────────────────────────
    @staticmethod
    def new_refresh_token() -> str:
        return secrets.token_urlsafe(48)

    @staticmethod
    def hash_refresh_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def refresh_expiry(self) -> datetime:
        return datetime.now(UTC) + timedelta(days=self.settings.refresh_token_ttl_days)
