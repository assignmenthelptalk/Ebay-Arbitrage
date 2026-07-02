from contextlib import asynccontextmanager

from dotenv import load_dotenv
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

load_dotenv()

from database import init_db  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="eBay Arbitrage API",
    description="Central nervous system for zero-inventory eBay-to-Amazon arbitrage",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"error": True, "message": str(exc), "code": 500},
    )


from routers import auth, competitors, listings, logs, margin, orders  # noqa: E402

app.include_router(auth.router)
app.include_router(competitors.router)
app.include_router(margin.router)
app.include_router(listings.router)
app.include_router(orders.router)
app.include_router(logs.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    return FileResponse(Path(__file__).parent / "dashboard.html", media_type="text/html")
