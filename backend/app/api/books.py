"""API metadata sách: chạy ngay với file tải lên, xem kết quả đã lưu."""
import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile

from ..config import Settings
from ..pipeline.book import run_book
from ..pipeline.pages import UnsupportedFile
from ..storage import db
from ..storage.files import remove_tree
from ..storage.local import SCHEME, LocalStorage, new_upload, save_file
from ..storage.s3 import FileTooLarge


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/book-metadata", tags=["book-metadata"])

    @router.post("/sync")
    async def book_metadata_sync(request: Request, files: list[UploadFile] = File(...)):
        """Metadata một cuốn sách từ các file tải lên (PDF, Word, ảnh...), trả kết quả ngay."""
        if len(files) > settings.max_upload_files:
            raise HTTPException(status_code=413, detail=f"too many files (limit {settings.max_upload_files})")
        upload_id, folder = new_upload(settings.uploads_root)
        work = Path(settings.temp_dir) / uuid.uuid4().hex
        try:
            for f in files:
                await asyncio.to_thread(save_file, f.file, folder, f.filename, settings.max_upload_mb * 1024 * 1024)
            return await run_book(LocalStorage(settings.uploads_root), f"{SCHEME}{upload_id}/", work, settings,
                                  request.app.state.http)
        except FileTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        except UnsupportedFile as exc:
            raise HTTPException(status_code=415, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        finally:
            remove_tree(folder)
            remove_tree(work)

    @router.get("")
    async def list_books(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), document_id: str | None = None):
        """Metadata sách đã lưu (mới nhất trước)."""
        if not db.configured(settings):
            raise HTTPException(status_code=503, detail="Database is not configured")
        items, total = await asyncio.to_thread(db.list_books, settings, limit, offset, document_id)
        return {"total": total, "limit": limit, "offset": offset, "items": items}

    @router.get("/{request_id}")
    async def get_book(request_id: str):
        if not db.configured(settings):
            raise HTTPException(status_code=503, detail="Database is not configured")
        doc = await asyncio.to_thread(db.get_book, settings, request_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Book metadata not found")
        return doc

    return router
