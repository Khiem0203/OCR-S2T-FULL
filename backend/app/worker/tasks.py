"""Chuỗi Celery: tải, OCR, chính tả, lưu DB, webhook."""
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

from celery import chain
from celery.utils.log import get_task_logger

from ..audio.decode import AudioTooLong, UnsupportedAudio
from ..clients.webhook import send_webhook
from ..config import load_settings
from ..inputs.collect import collect
from ..pipeline import fullpage as flow
from ..pipeline.pages import TooManyPages, UnsupportedFile
from ..storage import db
from ..storage.files import remove_tree
from ..storage.s3 import FileTooLarge, S3Storage
from .celery_app import celery_app

log = get_task_logger(__name__)


def _public_error(exc: Exception) -> tuple[int, str]:
    """Đổi lỗi thành mã và thông báo an toàn."""
    try:
        from botocore.exceptions import ClientError, EndpointConnectionError

        if isinstance(exc, ClientError):
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NoSuchBucket", "NotFound"):
                return 404, "File not found in storage"
            if code in ("403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
                return 403, "Access to storage was denied"
        if isinstance(exc, EndpointConnectionError):
            return 502, "Storage endpoint unreachable"
    except ImportError:
        pass
    if isinstance(exc, (UnsupportedFile, UnsupportedAudio)):
        return 415, str(exc)
    if isinstance(exc, (TooManyPages, FileTooLarge, AudioTooLong)):
        return 413, str(exc)
    if isinstance(exc, ValueError):
        return 422, str(exc)
    if isinstance(exc, flow.AllPagesFailed):
        return 502, "OCR backend unavailable"
    from ..pipeline.speech import AllSegmentsFailed

    if isinstance(exc, AllSegmentsFailed):
        return 502, "Speech-to-text backend unavailable"
    return 500, "Processing failed"


def _fail(stage: str, ctx: dict, exc: Exception) -> None:
    rid = ctx.get("request_id", "?")
    code, message = _public_error(exc)
    log.error("[%s] %s failed: %s", rid, stage, type(exc).__name__, exc_info=True)
    settings = load_settings()
    if ctx.get("callback_url"):
        send_webhook(
            ctx["callback_url"],
            {
                "meta": {"error_code": code, "error_message": message},
                "data": {"path": ctx.get("s3_path"), "document_id": ctx.get("document_id")},
                "request_id": rid,
            },
            settings.webhook_timeout,
            settings.webhook_retries,
        )
    if not celery_app.conf.task_always_eager:
        celery_app.backend.mark_as_failure(rid, RuntimeError(message))
    if ctx.get("temp_folder"):
        remove_tree(ctx["temp_folder"])


@celery_app.task(name="fullpage_download_task", bind=True, max_retries=0)
def fullpage_download_task(
    self,
    request_id: str,
    s3_path: str,
    access_key: str,
    secret_key: str,
    endpoint: str,
    callback_url: str = "",
    document_id=None,
    spellcheck: bool = True,
):
    settings = load_settings()
    folder = Path(settings.temp_dir) / request_id
    ctx = {
        "request_id": request_id,
        "s3_path": s3_path,
        "document_id": document_id,
        "callback_url": callback_url,
        "temp_folder": str(folder),
        "spellcheck": spellcheck,
        "started_at": time.time(),
    }
    try:
        log.info("[%s] downloading %s", request_id, s3_path)
        storage = S3Storage(
            access_key or settings.s3_access_key, secret_key or settings.s3_secret_key,
            endpoint or settings.s3_endpoint, settings.s3_region, settings.s3_ssl,
        )
        # Có file văn bản dùng được thì lấy text luôn, không thì render để OCR
        ctx.update(collect(storage, s3_path, folder, settings, flow.prepare_pages))
        info = ctx["input"]
        log.info("[%s] input %s from %s", request_id, info["mode"], ", ".join(info["files"]))
        return ctx
    except Exception as exc:
        _fail("download", ctx, exc)
        raise


@celery_app.task(name="fullpage_ocr_task", bind=True, max_retries=0)
def fullpage_ocr_task(self, ctx: dict):
    settings = load_settings()
    if ctx["input"]["mode"] == "text":
        return ctx
    try:

        async def run():
            async with flow.make_http_client() as http:
                return await flow.ocr_files(ctx["image_paths"], ctx["text_paths"], settings, http)

        pages = asyncio.run(run())
        if all(p["error"] for p in pages):
            raise flow.AllPagesFailed()
        ctx["pages"] = pages
        log.info("[%s] OCR done for %d page(s)", ctx["request_id"], len(pages))
        return ctx
    except Exception as exc:
        _fail("ocr", ctx, exc)
        raise


@celery_app.task(name="fullpage_spell_task", bind=True, max_retries=0)
def fullpage_spell_task(self, ctx: dict):
    settings = load_settings()
    try:

        async def run():
            async with flow.make_http_client() as http:
                spell = flow.build_spell_checker(settings, http) if ctx.get("spellcheck", True) else None
                await flow.postprocess(ctx["pages"], spell, settings.spell_text_files)

        asyncio.run(run())
        elapsed = int((time.time() - ctx["started_at"]) * 1000)
        result = flow.finalize(ctx["pages"], settings, elapsed, include_raw=False, input_info=ctx["input"])
        return {
            "request_id": ctx["request_id"],
            "s3_path": ctx["s3_path"],
            "document_id": ctx["document_id"],
            "callback_url": ctx["callback_url"],
            "temp_folder": ctx["temp_folder"],
            "result": result,
        }
    except Exception as exc:
        _fail("spell", ctx, exc)
        raise


@celery_app.task(name="save_fullpage_to_db_task", bind=True, max_retries=3)
def save_fullpage_to_db_task(self, ctx: dict):
    settings = load_settings()
    rid, result = ctx["request_id"], ctx["result"]
    saved = False
    try:
        saved = db.save_result(settings, rid, ctx["s3_path"], ctx["document_id"], result)
    except Exception as exc:
        log.error("[%s] database save failed: %s", rid, type(exc).__name__, exc_info=True)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30)
    if ctx.get("callback_url"):
        send_webhook(
            ctx["callback_url"],
            {
                "meta": {"error_code": 200, "error_message": "OK"},
                "data": {
                    "path": ctx["s3_path"],
                    "document_id": ctx["document_id"],
                    "creator": "ocr-fullpage",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    **result,
                },
                "request_id": rid,
            },
            settings.webhook_timeout,
            settings.webhook_retries,
        )
    remove_tree(ctx["temp_folder"])
    log.info("[%s] finished: %d page(s), %d unknown word(s)", rid, result["page_count"], result["unknown_total"])
    return {"request_id": rid, "document_id": ctx["document_id"], "saved_to_db": saved, **result}


def submit_fullpage(
    request_id: str,
    s3_path: str,
    access_key: str,
    secret_key: str,
    endpoint: str,
    callback_url: str = "",
    document_id=None,
    spellcheck: bool = True,
):
    """Đưa 4 bước vào hàng đợi; task cuối dùng request_id."""
    return chain(
        fullpage_download_task.s(
            request_id=request_id, s3_path=s3_path, access_key=access_key, secret_key=secret_key,
            endpoint=endpoint, callback_url=callback_url, document_id=document_id, spellcheck=spellcheck,
        ),
        fullpage_ocr_task.s(),
        fullpage_spell_task.s(),
        save_fullpage_to_db_task.s().set(task_id=request_id),
    ).apply_async()
