from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import EmailStr, Field

from app.api.schemas import Out, Strict
from app.db.models import UserRole


class LoginRequest(Strict):
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(Strict):
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=12, max_length=256)
    full_name: str | None = Field(default=None, max_length=120)
    role: UserRole = UserRole.viewer


class TokenResponse(Out):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


class ScopeOut(Out):
    data_source_id: uuid.UUID
    scope_key: str
    scope_values: list


class PrincipalOut(Out):
    id: uuid.UUID
    email: str
    full_name: str | None
    role: UserRole
    tenant_id: uuid.UUID
    scopes: list[ScopeOut]


class UserOut(Out):
    id: uuid.UUID
    email: str
    full_name: str | None
    role: UserRole
    is_active: bool
    created_at: datetime


class MessageOut(Out):
    message: str
