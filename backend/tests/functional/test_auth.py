"""Phase 4 gate: login/refresh/logout, rotation, reuse detection, lockout, RBAC, alg=none rejection."""

from __future__ import annotations

import base64
import json

import jwt
import pytest

from app.db.models import UserRole
from tests.conftest import password_for

REFRESH_COOKIE = "lens_refresh"


async def test_login_returns_access_token_and_httponly_refresh_cookie(api, settings, seeded):
    r = await api.login("analyst@lens.demo", password_for(settings, "analyst@lens.demo"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer" and body["access_token"]
    cookie_header = r.headers.get("set-cookie", "")
    assert REFRESH_COOKIE in cookie_header
    assert "HttpOnly" in cookie_header and "SameSite=strict" in cookie_header and "Path=/api/v1/auth" in cookie_header
    assert r.headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert r.headers["Cache-Control"] == "no-store"


async def test_me_returns_principal_with_scopes(login):
    a = await login("analyst2@lens.demo")
    r = await a.get("/auth/me")
    assert r.status_code == 200
    me = r.json()
    assert me["role"] == "analyst" and me["email"] == "analyst2@lens.demo"
    assert me["scopes"][0]["scope_values"] == [1, 2, 3]


async def test_bad_password_and_unknown_user_are_indistinguishable(api, seeded):
    r1 = await api.login("analyst@lens.demo", "definitely-wrong")
    r2 = await api.login("nobody@lens.demo", "definitely-wrong")
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["detail"] == r2.json()["detail"]
    assert r1.json()["type"].endswith("unauthenticated")
    assert "request_id" in r1.json()


async def test_lockout_after_repeated_failures(api, settings, seeded, make_user):
    _, email, password = await make_user()
    for _ in range(settings.login_max_failures):
        assert (await api.login(email, "wrong")).status_code == 401
    # even the right password is rejected while locked
    assert (await api.login(email, password)).status_code == 401


async def test_refresh_rotates_and_reuse_revokes_family(client, api, settings, seeded):
    await api.login("analyst@lens.demo", password_for(settings, "analyst@lens.demo"))
    first_cookie = client.cookies.get(REFRESH_COOKIE)
    assert first_cookie

    r2 = await client.post("/api/v1/auth/refresh")
    assert r2.status_code == 200, r2.text
    second_cookie = client.cookies.get(REFRESH_COOKIE)
    assert second_cookie and second_cookie != first_cookie

    # replay the consumed token → reuse detected → whole family revoked
    client.cookies.set(REFRESH_COOKIE, first_cookie, path="/api/v1/auth")
    r3 = await client.post("/api/v1/auth/refresh")
    assert r3.status_code == 401
    assert "reuse" in r3.json()["detail"].lower()

    # the newest token in the family is dead too
    client.cookies.set(REFRESH_COOKIE, second_cookie, path="/api/v1/auth")
    r4 = await client.post("/api/v1/auth/refresh")
    assert r4.status_code == 401


async def test_logout_revokes_family(client, api, settings, seeded):
    await api.login("analyst@lens.demo", password_for(settings, "analyst@lens.demo"))
    cookie = client.cookies.get(REFRESH_COOKIE)
    r = await client.post("/api/v1/auth/logout")
    assert r.status_code == 200
    client.cookies.set(REFRESH_COOKIE, cookie, path="/api/v1/auth")
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401


async def test_alg_none_token_is_rejected(api, settings, seeded, login):
    a = await login("analyst@lens.demo")
    real = a.token
    payload = jwt.decode(real, options={"verify_signature": False})
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    forged = f"{header}.{body}."
    api.token = forged
    assert (await api.get("/auth/me")).status_code == 401
    api.token = f"{header}.{body}"
    assert (await api.get("/auth/me")).status_code == 401


async def test_token_signed_with_wrong_key_is_rejected(api, settings, seeded):
    payload = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(seeded["users"]["admin@lens.demo"]),
        "tid": str(seeded["tenant_id"]),
        "role": "admin",
        "iat": 1,
        "nbf": 1,
        "exp": 4102444800,
        "jti": "x",
    }
    api.token = jwt.encode(payload, "some-other-secret-that-is-long-enough-1234", algorithm="HS256")
    assert (await api.get("/auth/me")).status_code == 401


async def test_role_claim_in_token_cannot_escalate(api, settings, seeded):
    """The role is read from the user row, not from the token."""
    payload = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(seeded["users"]["partner@lens.demo"]),
        "tid": str(seeded["tenant_id"]),
        "role": "admin",
        "email": "partner@lens.demo",
        "iat": 1,
        "nbf": 1,
        "exp": 4102444800,
        "jti": "x",
    }
    api.token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm="HS256")
    r = await api.get("/auth/me")
    assert r.status_code == 200 and r.json()["role"] == "viewer"


async def test_register_requires_admin(login):
    import uuid

    email = f"new-{uuid.uuid4().hex[:8]}@lens.demo"
    analyst = await login("analyst@lens.demo")
    r = await analyst.post("/auth/register", json={"email": email, "password": "LongEnoughPassw0rd!", "role": "viewer"})
    assert r.status_code == 403
    admin = await login("admin@lens.demo")
    r = await admin.post("/auth/register", json={"email": email, "password": "LongEnoughPassw0rd!", "role": "analyst"})
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "analyst"
    # extra fields are forbidden
    r = await admin.post(
        "/auth/register", json={"email": "x@lens.demo", "password": "LongEnoughPassw0rd!", "is_admin": True}
    )
    assert r.status_code == 422


@pytest.mark.parametrize("path", ["/auth/me", "/charts", "/dashboards"])
async def test_missing_token_is_401(api, path):
    assert (await api.get(path)).status_code == 401


async def test_problem_details_never_leak_internals(api):
    r = await api.client.post("/api/v1/auth/login", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/problem+json")
    assert "Traceback" not in r.text and "postgresql" not in r.text


async def test_password_hashes_are_argon2id(seeded, app):
    from sqlalchemy import select

    from app.db.models import AppUser
    from app.db.session import session_factory

    async with session_factory()() as s:
        user = (await s.execute(select(AppUser).where(AppUser.email == "admin@lens.demo"))).scalar_one()
    assert user.password_hash.startswith("$argon2id$")
    assert user.role is UserRole.admin
