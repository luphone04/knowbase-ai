import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.health import router as health_router
from app.api.v1 import router as api_v1_router
from app.api.v1.sources import max_upload_bytes
from app.config import get_settings
from app.core.body_limit import BodySizeLimitMiddleware
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.origin import OriginCheckMiddleware

logger = logging.getLogger(__name__)

JSON_BODY_LIMIT = 1024 * 1024
MULTIPART_OVERHEAD = 64 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.secret_values)
    logger.info(
        "API starting: env=%s gemini_configured=%s",
        settings.app_env,
        settings.gemini_configured,
    )
    if not settings.gemini_configured:
        logger.warning(
            "GEMINI_API_KEY is not set: AI features are unavailable until it is added "
            "to the backend .env. Everything else works."
        )
    yield


def create_app() -> FastAPI:
    # Settings load here, so a misconfigured container fails at startup.
    settings = get_settings()
    app = FastAPI(title="Knowbase AI API", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(api_v1_router)

    # Starlette runs the last-added middleware first:
    # TrustedHost -> body size limit -> CORS (answers preflights) -> Origin check -> routes.
    app.add_middleware(OriginCheckMiddleware, allowed_origins=[settings.frontend_origin])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],  # exact origin; never "*" with credentials
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
        max_age=600,
    )
    # Uploads get the file limit plus room for multipart framing; JSON bodies 1 MiB.
    app.add_middleware(
        BodySizeLimitMiddleware,
        default_limit=JSON_BODY_LIMIT,
        path_limits={"/api/v1/sources/files": max_upload_bytes(settings) + MULTIPART_OVERHEAD},
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
    return app


app = create_app()
