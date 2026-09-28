import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import PurePosixPath

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response

from app import storage
from app.config import get_settings
from app.queue import close_queue
from app.routers import assets, auth, batches, events, health, runs, sessions

settings = get_settings()


class SPAStaticFiles(StaticFiles):
    """Serve index.html for client routes while preserving real API/asset 404s."""

    async def get_response(self, path: str, scope: dict) -> Response:
        first = path.strip("/").partition("/")[0]
        fallback = (
            scope.get("method") in {"GET", "HEAD"}
            and first not in {"api", "events", "assets"}
            and not PurePosixPath(path).suffix
        )
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404 and fallback:
                return await super().get_response("index.html", scope)
            raise
        if response.status_code == 404 and fallback:
            return await super().get_response("index.html", scope)
        return response


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await asyncio.to_thread(storage.ensure_bucket)
    yield
    await close_queue()


app = FastAPI(
    title="智绘台",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

api = APIRouter(prefix="/api")
api.include_router(health.router)
api.include_router(auth.router)
api.include_router(assets.router)
api.include_router(runs.router)
api.include_router(sessions.router)
api.include_router(batches.router)
app.include_router(api)

# SSE 不挂在 /api 下，便于反向代理单独关闭缓冲
app.include_router(events.router)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def missing_api(path: str) -> JSONResponse:
    return JSONResponse({"detail": "Not Found"}, status_code=404)


@app.api_route("/events/{path:path}", methods=["GET"])
async def missing_event(path: str) -> JSONResponse:
    return JSONResponse({"detail": "Not Found"}, status_code=404)

# 生产环境下前端与 API 同源，静态产物由本服务托管；开发环境走 Vite dev proxy。
if settings.frontend_dist.is_dir():
    app.mount("/", SPAStaticFiles(directory=settings.frontend_dist, html=True), name="frontend")
