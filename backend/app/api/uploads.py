"""Nhận file tải lên từ máy rồi xếp hàng như S3."""
import asyncio
import logging
import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ..config import Settings
from ..inputs.select import kind
from ..storage.files import remove_tree
from ..storage.local import SCHEME, new_upload, safe_name, save_file
from ..storage.s3 import FileTooLarge
from ..worker.speech_tasks import submit_speech
from ..worker.tasks import submit_fullpage

log = logging.getLogger("ocr.uploads")

OCR_KINDS = ("text", "pdf", "image")


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/uploads", tags=["uploads"])

    async def accept(files: list[UploadFile], kinds: tuple, max_mb: int, group: bool, spellcheck: bool, submit) -> dict:
        if len(files) > settings.max_upload_files:
            raise HTTPException(status_code=413, detail=f"too many files (limit {settings.max_upload_files})")
        usable, skipped = [], []
        for f in files:
            name = safe_name(f.filename)
            (usable if kind(name) in kinds else skipped).append(f)
        skipped = [{"file": safe_name(f.filename), "reason": "unsupported file type"} for f in skipped]
        if not usable:
            raise HTTPException(status_code=415, detail="no supported file in the upload")
        # Gộp: cả lô một việc; không gộp: mỗi file một việc
        batches = [usable] if group else [[f] for f in usable]
        jobs = []
        for batch in batches:
            upload_id, folder = new_upload(settings.uploads_root)
            try:
                names = []
                for f in batch:
                    names.append(await asyncio.to_thread(save_file, f.file, folder, f.filename, max_mb * 1024 * 1024))
            except FileTooLarge as exc:
                remove_tree(folder)
                raise HTTPException(status_code=413, detail=str(exc))
            except ValueError as exc:
                remove_tree(folder)
                raise HTTPException(status_code=422, detail=str(exc))
            path = f"{SCHEME}{upload_id}/" + ("" if group else names[0])
            request_id = uuid.uuid4().hex
            await asyncio.to_thread(submit, request_id, path, "", "", "", "", None, spellcheck)
            log.info("[%s] queued upload with %d file(s)", request_id, len(names))
            jobs.append({"request_id": request_id, "files": names})
        return {"jobs": jobs, "skipped": skipped}

    @router.post("/ocr", status_code=202)
    async def upload_ocr(
        files: list[UploadFile] = File(...),
        group: bool = Form(False),
        spellcheck: bool = Form(True),
    ):
        """OCR file tải lên; group=true thì cả lô là một tài liệu."""
        return await accept(files, OCR_KINDS, settings.max_upload_mb, group, spellcheck, submit_fullpage)

    @router.post("/speech", status_code=202)
    async def upload_speech(
        files: list[UploadFile] = File(...),
        group: bool = Form(False),
        spellcheck: bool = Form(True),
    ):
        """Trích xuất lời nói; group=true thì nối các file theo tên."""
        return await accept(files, ("audio",), settings.speech_max_upload_mb, group, spellcheck, submit_speech)

    return router
