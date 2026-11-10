import asyncio
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from functools import partial

import httpx
from celery.result import AsyncResult
from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..clients.webhook import webhook_allowed
from ..config import Settings, load_settings
from ..audio.decode import AudioTooLong, UnsupportedAudio
from ..pipeline import fullpage as flow
from ..pipeline import speech
from ..pipeline.pages import TooManyPages, UnsupportedFile
from ..spell.compare import compare_lines
from ..worker.celery_app import celery_app
from ..worker.speech_tasks import submit_speech
from ..worker.tasks import submit_fullpage
from . import documents, transcripts, uploads
from .schemas import (
    FullPageRequest,
    SpeechRequest,
    SpellRequest,
    SubmitResponse,
    WorkerRestartRequest,
    WorkerStartRequest,
    WorkerStopRequest,
)

log = logging.getLogger("ocr.api")


def create_app(settings: Settings | None = None, http: httpx.AsyncClient | None = None) -> FastAPI:
    settings = settings or load_settings()
    if not settings.api_keys and not settings.auth_disabled:
        raise RuntimeError("API_KEYS is empty. Set API_KEYS (comma separated) or AUTH_DISABLED=true for local use.")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = http or flow.make_http_client()
        app.state.http = client
        try:
            lexicon = flow.get_lexicon(settings)
            log.info("dictionaries loaded: %s", lexicon.sources if lexicon else "none")
        except Exception:
            log.exception("could not load dictionaries")
            raise
        yield
        await client.aclose()

    app = FastAPI(
        title="OCR full page",
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )
    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # Không trả lại input vì có thể chứa khóa
        errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["POST", "GET"], allow_headers=["*"])

    def require_key(request: Request) -> None:
        if settings.auth_disabled:
            return
        supplied = request.headers.get("x-api-key", "")
        if not supplied:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
        if not any(secrets.compare_digest(supplied.encode(), k.encode()) for k in settings.api_keys):
            raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    api = APIRouter(prefix="/api", dependencies=[Depends(require_key)])

    async def queue_job(req: FullPageRequest, submit, kind: str) -> str:
        """Kiểm tra request rồi đưa vào hàng đợi; trả request_id."""
        if req.callback_url and not webhook_allowed(req.callback_url, settings.webhook_allowed_hosts):
            raise HTTPException(status_code=400, detail="callback_url host is not allowed")
        access_key = req.access_key.get_secret_value() if req.access_key else ""
        secret_key = req.secret_key.get_secret_value() if req.secret_key else ""
        # Thiếu thì worker lấy từ .env (khóa không qua RabbitMQ)
        if not ((access_key or settings.s3_access_key) and (secret_key or settings.s3_secret_key) and (req.endpoint or settings.s3_endpoint)):
            raise HTTPException(status_code=400, detail="S3 access_key, secret_key and endpoint are required (in the request or in .env)")
        request_id = uuid.uuid4().hex
        await asyncio.to_thread(
            submit, request_id, req.s3_path, access_key, secret_key, req.endpoint, req.callback_url, req.document_id, req.spellcheck,
        )
        log.info("[%s] queued %s job", request_id, kind)
        return request_id

    @api.post("/ocr-fullpage", response_model=SubmitResponse, status_code=202)
    async def ocr_fullpage(req: FullPageRequest):
        """Nhận việc OCR toàn trang vào hàng đợi."""
        request_id = await queue_job(req, submit_fullpage, "full-page OCR")
        return SubmitResponse(error_code=202, error_message="Đã nhận yêu cầu OCR toàn trang.", request_id=request_id)

    @api.post("/speech-to-text", response_model=SubmitResponse, status_code=202)
    async def speech_to_text(req: SpeechRequest):
        """Nhận việc trích xuất lời nói vào hàng đợi."""
        request_id = await queue_job(req, submit_speech, "speech-to-text")
        return SubmitResponse(error_code=202, error_message="Đã nhận yêu cầu trích xuất lời nói.", request_id=request_id)

    @api.post("/speech-to-text/sync")
    async def speech_to_text_sync(
        request: Request,
        file: UploadFile = File(...),
        spellcheck: bool = Form(True),
        include_raw: bool = Form(False),
    ):
        """Trích xuất lời nói từ file tải lên, trả kết quả ngay."""
        data = await _read_limited(file, settings.speech_max_upload_mb * 1024 * 1024)
        try:
            return await speech.process_audio(data, file.filename or "audio", settings, request.app.state.http,
                                              spellcheck=spellcheck, include_raw=include_raw)
        except UnsupportedAudio as exc:
            raise HTTPException(status_code=415, detail=str(exc))
        except AudioTooLong as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        except speech.AllSegmentsFailed:
            raise HTTPException(status_code=502, detail="Speech-to-text backend unavailable")

    @api.post("/ocr-fullpage/sync")
    async def ocr_fullpage_sync(
        request: Request,
        file: UploadFile = File(...),
        spellcheck: bool = Form(True),
        include_raw: bool = Form(False),
    ):
        """OCR hoặc đọc text file tải lên, trả kết quả ngay."""
        data = await _read_limited(file, settings.max_upload_mb * 1024 * 1024)
        try:
            return await flow.process_document(data, settings, request.app.state.http, spellcheck=spellcheck, include_raw=include_raw, filename=file.filename or "")
        except UnsupportedFile as exc:
            raise HTTPException(status_code=415, detail=str(exc))
        except TooManyPages as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        except flow.AllPagesFailed:
            raise HTTPException(status_code=502, detail="OCR backend unavailable")

    @api.post("/spellcheck")
    async def spellcheck(body: SpellRequest, request: Request):
        """Kiểm tra chính tả một đoạn text."""
        spell = flow.build_spell_checker(settings, request.app.state.http)
        text, corrections = (body.text, []) if spell is None else await spell.check(body.text)
        changes = compare_lines(body.text, text, corrections)
        pages = [{"page": 1, "text": text}]
        lexicon = flow.get_lexicon(settings)
        unknown, total = ([], 0)
        if lexicon is not None and settings.unknown_report:
            from ..spell.unknown import build_notice, find_unknown_words

            unknown, total = find_unknown_words(pages, lexicon, settings.unknown_include, settings.unknown_min_len, settings.unknown_max_words)
            notice = build_notice(unknown, total)
        else:
            notice = None
        return {"text": text, "corrections": corrections, "changes": changes, "unknown_words": unknown, "unknown_total": total, "notice": notice}

    @api.get("/request-status/{request_id}")
    async def request_status(request_id: str):
        if not (8 <= len(request_id) <= 64) or not request_id.replace("-", "").isalnum():
            raise HTTPException(status_code=404, detail="Unknown request id")
        res = AsyncResult(request_id, app=celery_app)
        state = res.state
        if state == "SUCCESS":
            return {"request_id": request_id, "status": state, "result": res.result}
        if state == "FAILURE":
            return {"request_id": request_id, "status": state, "error": str(res.result)}
        return {"request_id": request_id, "status": state}

    # Quản lý worker

    def _run(fn, **kwargs):
        return asyncio.get_running_loop().run_in_executor(None, partial(fn, **kwargs))

    @api.get("/workers/status")
    async def workers_status(queue_name: str | None = None):
        from ..worker import manager as wm

        return await _run(wm.get_worker_status, queue_name=queue_name)

    @api.get("/workers/presets")
    async def workers_presets():
        from ..worker import manager as wm

        return {"presets": wm.get_presets()}

    @api.post("/workers/start")
    async def workers_start(req: WorkerStartRequest):
        from ..worker import manager as wm

        return await _run(wm.start_workers, queue_name=req.queue_name, num_workers=req.num_workers, concurrency=req.concurrency)

    @api.post("/workers/stop")
    async def workers_stop(req: WorkerStopRequest):
        from ..worker import manager as wm

        return await _run(wm.stop_workers, queue_name=req.queue_name)

    @api.post("/workers/restart")
    async def workers_restart(req: WorkerRestartRequest):
        from ..worker import manager as wm

        return await _run(wm.restart_workers, queue_name=req.queue_name, num_workers=req.num_workers, concurrency=req.concurrency)

    # Cho web xem kết quả
    api.include_router(documents.build_router(settings))
    api.include_router(transcripts.build_router(settings))
    # Tải file lên từ máy
    api.include_router(uploads.build_router(settings))
    app.include_router(api)
    return app


async def _read_limited(file: UploadFile, limit: int) -> bytes:
    chunks, size = [], 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail=f"File too large (limit {limit // (1024 * 1024)} MB)")
        chunks.append(chunk)
    if not size:
        raise HTTPException(status_code=422, detail="Empty file")
    return b"".join(chunks)
