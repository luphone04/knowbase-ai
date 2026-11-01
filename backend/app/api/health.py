"""Liveness and readiness probes. Unversioned, and they never call Gemini."""

import logging
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import DbSession
from app.db.migrations_support import REQUIRED_EXTENSIONS, installed_extensions

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    status: Literal["ready", "unavailable"]
    database: Literal["ok", "unreachable"]
    extensions: dict[str, str] = {}
    missing_extensions: list[str] = []


@router.get("/health")
def health() -> HealthResponse:
    """The process is alive. Checks no dependencies."""
    return HealthResponse()


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
def ready(db: DbSession) -> ReadinessResponse | JSONResponse:
    """The database is reachable and the required extensions are installed."""
    try:
        extensions = installed_extensions(db.connection())
    except SQLAlchemyError as exc:
        # Log the error class only: driver messages can include host and user details.
        logger.warning("Readiness check failed: database unreachable (%s)", type(exc).__name__)
        return _unavailable(ReadinessResponse(status="unavailable", database="unreachable"))

    missing = [name for name in REQUIRED_EXTENSIONS if name not in extensions]
    if missing:
        logger.warning("Readiness check failed: missing extensions %s", missing)
        return _unavailable(
            ReadinessResponse(
                status="unavailable",
                database="ok",
                extensions=extensions,
                missing_extensions=missing,
            )
        )
    return ReadinessResponse(status="ready", database="ok", extensions=extensions)


def _unavailable(body: ReadinessResponse) -> JSONResponse:
    return JSONResponse(status_code=503, content=body.model_dump())
