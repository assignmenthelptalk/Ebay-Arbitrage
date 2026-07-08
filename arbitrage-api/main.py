import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

load_dotenv()

from database import init_db  # noqa: E402
from auth import require_api_key  # noqa: E402

logger = logging.getLogger("arbitrage")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


_docs_enabled = os.getenv("ENABLE_DOCS", "false").lower() == "true"

app = FastAPI(
    title="eBay Arbitrage API",
    description="Central nervous system for zero-inventory eBay-to-Amazon arbitrage",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": True, "message": "Internal server error", "code": 500},
    )


from routers import auth as auth_router, competitors, listings, logs, margin, orders, research  # noqa: E402

protected = [Depends(require_api_key)]

app.include_router(auth_router.router, dependencies=protected)
app.include_router(competitors.router, dependencies=protected)
app.include_router(margin.router, dependencies=protected)
app.include_router(listings.router, dependencies=protected)
app.include_router(orders.router, dependencies=protected)
app.include_router(orders.fulfillment_router, dependencies=protected)
app.include_router(logs.router, dependencies=protected)
app.include_router(research.router, dependencies=protected)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    return FileResponse(Path(__file__).parent / "dashboard.html", media_type="text/html")
