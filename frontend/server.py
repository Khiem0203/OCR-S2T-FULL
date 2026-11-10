"""Trang web tĩnh + chuyển tiếp /api sang backend."""
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:9090").rstrip("/")
PUBLIC = Path(__file__).resolve().parent / "public"
# Header không chuyển tiếp
HOP = {"host", "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization"}

# Không giới hạn thời gian đọc (upload lớn)
client = httpx.AsyncClient(base_url=BACKEND_URL, timeout=httpx.Timeout(None, connect=10))


async def proxy(request: Request):
    url = httpx.URL(path=request.url.path, query=request.url.query.encode())
    headers = [(k, v) for k, v in request.headers.raw if k.decode().lower() not in HOP]
    req = client.build_request(request.method, url, headers=headers, content=request.stream())
    try:
        resp = await client.send(req, stream=True)
    except httpx.TransportError:
        return JSONResponse({"detail": f"Không kết nối được backend ({BACKEND_URL})"}, status_code=502)
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP}
    return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, headers=out_headers,
                             background=BackgroundTask(resp.aclose))


@asynccontextmanager
async def lifespan(app):
    yield
    await client.aclose()


METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
app = Starlette(
    routes=[
        Route("/api/{path:path}", proxy, methods=METHODS),
        Route("/health", proxy, methods=["GET"]),
        Mount("/", StaticFiles(directory=PUBLIC, html=True)),
    ],
    lifespan=lifespan,
)
