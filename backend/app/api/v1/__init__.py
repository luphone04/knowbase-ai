from fastapi import APIRouter

from app.api.v1 import ai, auth, jobs, sources, workspaces

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router)
router.include_router(workspaces.router)
router.include_router(ai.router)
router.include_router(sources.router)
router.include_router(jobs.router)
