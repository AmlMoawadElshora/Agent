"""FastAPI application factory.

Run locally::

    uvicorn app.main:app --reload

Endpoints
---------
POST /webhooks/twilio/whatsapp   Twilio inbound message webhook (TwiML)
GET  /health, /ready             liveness / readiness
GET  /docs                       interactive OpenAPI docs
     /api/...                    admin API (X-API-Key when configured)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import health, leads, webhook
from app.config import get_settings
from app.db.base import init_db
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    if settings.auto_create_tables:
        init_db()

    if settings.dry_run:
        logger.warning(
            "DRY-RUN MODE: no WhatsApp messages will be transmitted. "
            "Set DRY_RUN=false and configure Twilio credentials to go live."
        )
    if not settings.admin_api_key:
        logger.warning("ADMIN_API_KEY is not set: the admin API is unauthenticated")
    if not settings.validate_twilio_signature:
        logger.warning("Twilio signature validation is disabled")

    logger.info(
        "application ready",
        extra={
            "environment": settings.environment,
            "admin_number": settings.admin_whatsapp_number,
            "llm_enabled": settings.llm_enabled,
        },
    )
    yield
    logger.info("application shutdown")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Real Estate WhatsApp Outreach & Qualification Agent",
        description=(
            "Automated WhatsApp outreach, multi-turn qualification and admin handoff "
            "for the US real estate market."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(webhook.router)
    app.include_router(leads.router)

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "docs": "/docs",
            "webhook": "/webhooks/twilio/whatsapp",
            "health": "/health",
        }

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    return app


app = create_app()
