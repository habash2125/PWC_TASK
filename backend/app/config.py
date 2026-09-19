"""Application settings.

Every knob comes from the environment (see `.env.example`).  Nothing here is a
secret by default; DSNs are *assembled* from parts so a password never has to
be written into a URL in a config file.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["dev", "prod", "test"] = "dev"
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"

    # ── app-db ──────────────────────────────────────────────────────────────
    app_db_host: str = "localhost"
    app_db_port: int = 5432
    app_db_name: str = "lens_app"
    app_db_user: str = "lens_app"
    app_db_password: SecretStr = SecretStr("lens_app_dev_pw")
    app_db_pool_size: int = 10

    # ── analytics-db (SQLite file; the seed writes it, the API opens it read-only) ──
    analytics_sqlite_path: str = "./data/analytics/northwind.db"
    analytics_dsn_secret_ref: str = "ANALYTICS_SQLITE_PATH"
    analytics_db_pool_size: int = 4

    # ── cache ───────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── auth ────────────────────────────────────────────────────────────────
    jwt_algorithm: Literal["HS256", "RS256"] = "HS256"
    jwt_secret: SecretStr = SecretStr("change-me-dev-only-hs256-secret-at-least-32-bytes-long")
    jwt_private_key_path: str = ""
    jwt_public_key_path: str = ""
    jwt_issuer: str = "lens"
    jwt_audience: str = "lens-api"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 14
    cookie_secure: bool = False
    login_max_failures: int = 5
    login_lockout_base_seconds: int = 30

    # ── seed ────────────────────────────────────────────────────────────────
    seed_tenant_name: str = "Lens Demo"
    seed_admin_password: SecretStr = SecretStr("Admin!Lens2024")
    seed_analyst_password: SecretStr = SecretStr("Analyst!Lens2024")
    seed_analyst2_password: SecretStr = SecretStr("Analyst2!Lens2024")
    seed_partner_password: SecretStr = SecretStr("Partner!Lens2024")

    # ── LLM ─────────────────────────────────────────────────────────────────
    # LLM_API_KEY wins when set; otherwise the conventional OPENAI_API_KEY is used
    llm_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    llm_base_url: str = ""
    llm_timeout_seconds: float = 60
    llm_max_retries: int = 2
    llm_circuit_failure_threshold: int = 3
    llm_circuit_cooldown_seconds: int = 60
    llm_model_screen: str = "gpt-4o-mini"
    llm_model_agent: str = "gpt-4o"
    llm_model_guard: str = "gpt-4o-mini"
    llm_model_narrative: str = "gpt-4o-mini"
    llm_model_grouping: str = "gpt-4o-mini"
    llm_model_table_select: str = "gpt-4o-mini"
    llm_fallback_models: str = "gpt-4o-mini"
    llm_temperature_code: float = 0.0
    llm_temperature_narrative: float = 0.2
    llm_pricing_json: str = '{"gpt-4o":[2.5,10.0],"gpt-4o-mini":[0.15,0.6]}'
    llm_extra_body_json: str = "{}"

    # ── guard ───────────────────────────────────────────────────────────────
    guard_enforcer: Literal["llm", "parser"] = "llm"
    sql_max_rows: int = 5000
    sql_statement_timeout_ms: int = 8000

    # ── budgets ─────────────────────────────────────────────────────────────
    turn_max_steps: int = 12
    turn_max_sql_retries: int = 4
    turn_timeout_seconds: int = 90
    turn_max_tokens: int = 60_000
    daily_turn_ceiling: int = 200
    daily_token_ceiling: int = 2_000_000
    daily_cost_ceiling_usd: float = 20.0
    py_exec_timeout_seconds: int = 20
    py_exec_memory_mb: int = 768
    py_exec_cpu_seconds: int = 15
    py_exec_scratch_dir: str = "/tmp/lens-scratch"

    # ── dashboards ──────────────────────────────────────────────────────────
    refresh_concurrency: int = 4
    query_cache_ttl_seconds: int = 30

    # ── HTTP hardening ──────────────────────────────────────────────────────
    cors_origin: str = "http://localhost:3000"
    max_request_bytes: int = 262_144
    rate_limit_per_minute: int = 120
    rate_limit_chat_per_minute: int = 10
    rate_limit_ip_per_minute: int = 300

    # ── observability ───────────────────────────────────────────────────────
    trace_retention_days: int = 30
    metrics_enabled: bool = True
    otel_batch_delay_ms: int = 500

    # ── LangSmith (optional third-party LLM tracing; blank key = disabled) ────
    langsmith_api_key: SecretStr = SecretStr("")
    langsmith_project: str = "lens"
    langsmith_endpoint: str = ""  # blank = https://api.smith.langchain.com

    @field_validator("llm_pricing_json")
    @classmethod
    def _pricing_is_json(cls, v: str) -> str:
        json.loads(v)
        return v

    # ── derived ─────────────────────────────────────────────────────────────
    @property
    def app_db_url(self) -> str:
        pw = self.app_db_password.get_secret_value()
        return f"postgresql+asyncpg://{self.app_db_user}:{pw}@{self.app_db_host}:{self.app_db_port}/{self.app_db_name}"

    @property
    def analytics_sqlite_file(self) -> Path:
        return Path(self.analytics_sqlite_path).expanduser().resolve()

    @property
    def analytics_readonly_url(self) -> str:
        # SQLite URI form so the file can be opened ``mode=ro``: the OS refuses writes before any guard runs
        return f"sqlite+aiosqlite:///file:{self.analytics_sqlite_file}?mode=ro&uri=true"

    @property
    def fallback_models(self) -> list[str]:
        return [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]

    @model_validator(mode="after")
    def _default_llm_key_from_openai(self) -> Settings:
        if not self.llm_api_key.get_secret_value() and self.openai_api_key.get_secret_value():
            self.llm_api_key = self.openai_api_key
        return self

    @property
    def pricing(self) -> dict[str, tuple[float, float]]:
        raw = json.loads(self.llm_pricing_json)
        return {k: (float(v[0]), float(v[1])) for k, v in raw.items()}

    @property
    def llm_configured(self) -> bool:
        key = self.llm_api_key.get_secret_value()
        return bool(key) and not key.startswith("sk-REPLACE")

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def langsmith_enabled(self) -> bool:
        return bool(self.langsmith_api_key.get_secret_value())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
