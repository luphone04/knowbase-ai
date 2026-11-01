import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import get_settings
from app.core.logging import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Loading settings here makes a misconfigured container fail at startup, not on first request.
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info(
        "API starting: env=%s gemini_configured=%s",
        settings.app_env,
        settings.gemini_configured,
    )
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Knowbase AI API", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    return app


app = create_app()
