from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from wa_scheduler.db import init_db
from wa_scheduler.routes.web import router as web_router


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(title="wa-scheduler")
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(web_router)
    return app


app = create_app()
