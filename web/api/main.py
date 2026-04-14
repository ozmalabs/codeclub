"""
codeclub web — FastAPI application.

Entry point: uvicorn web.api.main:app --reload --port 8420
Production: serves React build from web/frontend/dist/

Lifespan:
  - init_db() on startup
  - start task_runner background loop
  - cleanup on shutdown
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .database import init_db
from .routers import dashboard, git_api, hardware, models_api, runs, settings, smash, tasks, tournament

FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    await init_db()

    # Start background task runner
    from .services.task_runner import TaskRunner
    runner = TaskRunner()
    runner_task = asyncio.create_task(runner.run_loop())
    app.state.task_runner = runner

    yield

    # Shutdown
    runner.stop()
    runner_task.cancel()
    try:
        await runner_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="codeclub",
    description="Smash code with club. Now with a control panel.",
    version="0.1.0",
    lifespan=lifespan,
    redirect_slashes=True,
)

# CORS — trusted network, allow everything
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routers
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
app.include_router(runs.router, prefix="/api/runs", tags=["runs"])
app.include_router(models_api.router, prefix="/api/models", tags=["models"])
app.include_router(hardware.router, prefix="/api/hardware", tags=["hardware"])
app.include_router(tournament.router, prefix="/api/tournament", tags=["tournament"])
app.include_router(smash.router, prefix="/api/smash", tags=["smash"])
app.include_router(git_api.router, prefix="/api/git", tags=["git"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])

# Serve pre-rendered efficiency maps
MAPS_DIR = Path(__file__).parent.parent.parent / "benchmarks" / "maps"
if MAPS_DIR.exists():
    app.mount("/maps", StaticFiles(directory=str(MAPS_DIR)), name="maps")

# Serve React SPA in production (after `npm run build`)
# Use a catch-all route so it doesn't shadow API redirects
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="frontend-assets")

    from fastapi.responses import FileResponse

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve index.html for all non-API routes (SPA client-side routing)."""
        if full_path.startswith("api/"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        file_path = FRONTEND_DIST / full_path
        if full_path and file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIST / "index.html")
