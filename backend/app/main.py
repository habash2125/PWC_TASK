"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_error_handlers
from app.api.middleware import BodySizeLimitMiddleware, IpRateLimitMiddleware, RequestContextMiddleware
from app.api.routers import auth, charts, chat, dashboards, health, ops, sessions, sources
from app.config import Settings, get_settings
from app.core.cache import close_cache, get_redis, init_cache
from app.db.analytics_pool import dispose_analytics_db, init_analytics_db
from app.db.session import dispose_app_db, init_app_db
from app.observability.logging_setup import setup_logging

log = logging.getLogger("lens.request")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_app_db(settings)
        init_analytics_db(settings)
        init_cache(settings)
        from app.core.auth import init_tokens

        init_tokens(settings)
        os.makedirs(settings.py_exec_scratch_dir, exist_ok=True)
        from app.observability.tracing import setup_tracing, shutdown_tracing

        setup_tracing(settings)
        from app.observability.langsmith_tracing import setup_langsmith, shutdown_langsmith

        setup_langsmith(settings)
        from app.core.llm.client import init_llm

        init_llm(settings)
        from app.prompts import load_prompts

        load_prompts()
        from app.core.runtime.python_exec import warm_up

        warm_up()
        from app.observability.tracing import retention_loop

        sweeper = asyncio.create_task(retention_loop(settings.trace_retention_days))
        log.info("lens api started", extra={"env": settings.app_env, "llm_configured": settings.llm_configured})
        try:
            yield
        finally:
            sweeper.cancel()
            await shutdown_tracing()
            shutdown_langsmith()
            await close_cache()
            await dispose_analytics_db()
            await dispose_app_db()

    app = FastAPI(
        title="Lens API",
        version="0.1.0",
        description="Conversational analytics & dashboard composer. Generate once, execute forever.",
        lifespan=lifespan,
        docs_url=f"{settings.api_prefix}/docs",
        openapi_url=f"{settings.api_prefix}/openapi.json",
        redoc_url=None,
    )
    app.state.settings = settings

    # order matters: outermost first
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(IpRateLimitMiddleware, settings=settings, redis_getter=get_redis)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.cors_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-Id"],
        expose_headers=["X-Request-Id", "X-Trace-Id"],
        max_age=600,
    )

    register_error_handlers(app)

    for router in (
        health.router,
        auth.router,
        sources.router,
        sessions.router,
        chat.router,
        charts.router,
        dashboards.router,
        ops.router,
    ):
        app.include_router(router, prefix=settings.api_prefix)
    return app


app = create_app()
