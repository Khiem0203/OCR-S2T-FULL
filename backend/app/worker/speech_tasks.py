"""Chuỗi Celery speech-to-text: tải + giải mã, PhoWhisper, chính tả, lưu DB + webhook."""
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

from celery import chain
from celery.utils.log import get_task_logger

from ..config import load_settings
from ..inputs.collect import collect_audio
from ..pipeline import fullpage
from ..pipeline import speech as flow
from ..storage import db
from ..storage.files import remove_tree
from . import tasks as common
from .celery_app import celery_app

log = get_task_logger(__name__)


@celery_app.task(name="speech_download_task", bind=True, max_retries=0)
def speech_download_task(
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
        storage = common.open_storage(s3_path, access_key, secret_key, endpoint, settings)
        ctx.update(collect_audio(storage, s3_path, folder, settings, flow.prepare_audio))
        log.info("[%s] prepared %d file(s), %d segment(s)", request_id, len(ctx["files"]),
                 sum(len(f["segments"]) for f in ctx["files"]))
        return ctx
    except Exception as exc:
        common._fail("download", ctx, exc)
        raise
    finally:
        common.drop_upload(s3_path, settings)


@celery_app.task(name="speech_asr_task", bind=True, max_retries=0)
def speech_asr_task(self, ctx: dict):
    settings = load_settings()
    try:

        async def run():
            async with fullpage.make_http_client() as http:
                return await flow.transcribe_files(ctx["files"], settings, http)

        ctx["segments"] = asyncio.run(run())
        log.info("[%s] transcribed %d segment(s)", ctx["request_id"], len(ctx["segments"]))
        return ctx
    except Exception as exc:
        common._fail("asr", ctx, exc)
        raise


@celery_app.task(name="speech_spell_task", bind=True, max_retries=0)
def speech_spell_task(self, ctx: dict):
    settings = load_settings()
    try:

        async def run():
            async with fullpage.make_http_client() as http:
                spell = flow.build_speech_spell_checker(settings, http) if ctx.get("spellcheck", True) else None
                return await flow.postprocess(ctx["segments"], spell)

        changes = asyncio.run(run())
        elapsed = int((time.time() - ctx["started_at"]) * 1000)
        result = flow.finalize(ctx["files"], ctx["segments"], changes, settings, elapsed, include_raw=False)
        return {k: ctx[k] for k in ("request_id", "s3_path", "document_id", "callback_url", "temp_folder")} | {"result": result}
    except Exception as exc:
        common._fail("spell", ctx, exc)
        raise


@celery_app.task(name="save_speech_to_db_task", bind=True, max_retries=3)
def save_speech_to_db_task(self, ctx: dict):
    settings = load_settings()
    rid, result = ctx["request_id"], ctx["result"]
    saved = False
    try:
        saved = db.save_transcript(settings, rid, ctx["s3_path"], ctx["document_id"], result)
    except Exception as exc:
        log.error("[%s] database save failed: %s", rid, type(exc).__name__, exc_info=True)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30)
    if ctx.get("callback_url"):
        common.send_webhook(
            ctx["callback_url"],
            {
                "meta": {"error_code": 200, "error_message": "OK"},
                "data": {
                    "path": ctx["s3_path"],
                    "document_id": ctx["document_id"],
                    "creator": "speech-to-text",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    **result,
                },
                "request_id": rid,
            },
            settings.webhook_timeout,
            settings.webhook_retries,
        )
    remove_tree(ctx["temp_folder"])
    log.info("[%s] finished: %.0fs audio, %d segment(s)", rid, result["duration"], result["segment_count"])
    return {"request_id": rid, "document_id": ctx["document_id"], "saved_to_db": saved, **result}


def submit_speech(
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
        speech_download_task.s(
            request_id=request_id, s3_path=s3_path, access_key=access_key, secret_key=secret_key,
            endpoint=endpoint, callback_url=callback_url, document_id=document_id, spellcheck=spellcheck,
        ),
        speech_asr_task.s(),
        speech_spell_task.s(),
        save_speech_to_db_task.s().set(task_id=request_id),
    ).apply_async()
