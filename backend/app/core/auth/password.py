"""Argon2id password hashing with a per-password salt (handled by argon2-cffi)."""

from __future__ import annotations

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# OWASP-recommended minimums for Argon2id (m=19 MiB, t=2, p=1); costlier than the defaults' speed
# but cheap enough that a login is not a DoS vector.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2, hash_len=32, salt_len=16, type=Type.ID)

# A real hash of a throwaway password, verified against on unknown-user logins so that the
# response time does not reveal whether the email exists.
_DUMMY_HASH = _hasher.hash("lens-constant-time-dummy")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    target = password_hash or _DUMMY_HASH
    try:
        ok = _hasher.verify(target, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return ok and password_hash is not None


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
