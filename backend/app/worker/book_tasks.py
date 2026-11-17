"""Task Celery metadata sách: lấy trang cuối, tách thông tin, lưu DB, webhook."""
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

from celery.utils.log import get_task_logger

from ..config import load_settings
from ..pipeline import fullpage
from ..pipeline.book import run_book
from ..storage import db
from ..storage.files import remove_tree
from . import tasks as common
from .celery_app import celery_app

log = get_task_logger(__name__)


@celery_app.task(name="book_metadata_task", bind=True, max_retries=0)
def book_metadata_task(self, request_id: str, s3_path: str, access_key: str, secret_key: str, endpoint: str,
                       callback_url: str = "", document_id=None, spellcheck: bool = True):
    settings = load_settings()
    folder = Path(settings.temp_dir) / request_id
    ctx = {"request_id": request_id, "s3_path": s3_path, "document_id": document_id,
           "callback_url": callback_url, "temp_folder": str(folder), "started_at": time.time()}
    try:
        storage = common.open_storage(s3_path, access_key, secret_key, endpoint, settings)

        async def run():
            async with fullpage.make_http_client() as http:
                return await run_book(storage, s3_path, folder, settings, http)

        result = asyncio.run(run())
    except Exception as exc:
        common._fail("book", ctx, exc)
        raise
    finally:
        common.drop_upload(s3_path, settings)
    saved = False
    try:
        saved = db.save_book(settings, request_id, s3_path, document_id, result)
    except Exception as exc:
        log.error("[%s] database save failed: %s", request_id, type(exc).__name__, exc_info=True)
    if callback_url:
        common.send_webhook(
            callback_url,
            {
                "meta": {"error_code": 200, "error_message": "OK"},
                "data": {"path": s3_path, "document_id": document_id, "creator": "book-metadata",
                         "created_at": datetime.now(timezone.utc).isoformat(), **result},
                "request_id": request_id,
            },
            settings.webhook_timeout,
            settings.webhook_retries,
        )
    remove_tree(folder)
    log.info("[%s] book metadata: %d field(s) from %s", request_id, result["field_count"], result["source"] or "-")
    return {"request_id": request_id, "document_id": document_id, "saved_to_db": saved, **result}


def submit_book(request_id: str, s3_path: str, access_key: str, secret_key: str, endpoint: str,
                callback_url: str = "", document_id=None, spellcheck: bool = True):
    """Một bước duy nhất; task dùng request_id."""
    return book_metadata_task.apply_async(
        kwargs=dict(request_id=request_id, s3_path=s3_path, access_key=access_key, secret_key=secret_key,
                    endpoint=endpoint, callback_url=callback_url, document_id=document_id, spellcheck=spellcheck),
        task_id=request_id,
    )
