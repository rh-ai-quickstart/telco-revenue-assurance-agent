#!/usr/bin/env python3
"""
FastAPI backend for the Telco Revenue Assurance Agent.

Responsibilities:
  - Create and stream OGX agent sessions (chat API)
  - Serve aggregated dashboard metrics (direct PostgreSQL queries)
  - Serve the React frontend as a static SPA (when built artefact is present)
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .db import SessionLocal, engine
from .routes.chat import router as chat_router
from .routes.health import router as health_router
from .routes.metrics import router as metrics_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Verify database connectivity
    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))
    log.info("Database connection verified")

    # Initialise the agent runner (creates LlamaStack client, logs OGX URL)
    from agent.runner import AgentRunner
    app.state.runner = AgentRunner()

    yield

    await engine.dispose()


app = FastAPI(
    title="Telco Revenue Assurance Backend",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to the Route hostname in production
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(chat_router)
app.include_router(metrics_router)

# Serve the compiled React SPA — mount last so API routes take precedence.
# Uncomment once the frontend build artefact is available in /app/frontend/dist.
#
# from fastapi.staticfiles import StaticFiles
# app.mount("/", StaticFiles(directory="/app/frontend/dist", html=True), name="frontend")
