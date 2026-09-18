from __future__ import annotations

from app.config import Settings
from app.core.auth.tokens import TokenService

_tokens: TokenService | None = None


def init_tokens(settings: Settings) -> TokenService:
    global _tokens
    _tokens = TokenService(settings)
    return _tokens


def get_token_service() -> TokenService:
    assert _tokens is not None, "token service not initialised"
    return _tokens
