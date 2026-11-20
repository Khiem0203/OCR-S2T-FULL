"""API xuất kết quả OCR ra .docx / .pdf (dựng lại bảng, chữ đậm / nghiêng, căn lề)."""
import asyncio
import re
import unicodedata
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..config import Settings
from ..pipeline.export import FORMATS, export
from .schemas import ExportRequest


def file_response(data: bytes, fmt: str, title: str) -> Response:
    name = (title or "ket-qua").strip()[:150] + "." + fmt
    ascii_name = unicodedata.normalize("NFKD", name.replace("đ", "d").replace("Đ", "D")).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", ascii_name).strip() or f"ket-qua.{fmt}"
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"
    return Response(data, media_type=FORMATS[fmt], headers={"Content-Disposition": disposition})


async def build_file(settings: Settings, pages_html: list[str], fmt: str, title: str) -> Response:
    try:
        data = await asyncio.to_thread(export, pages_html, fmt, title, settings.pdf_font_files)
    except Exception:
        raise HTTPException(status_code=500, detail="Could not build the file")
    return file_response(data, fmt, title)


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/export", tags=["export"])

    @router.post("/{fmt}")
    async def export_pages(fmt: Literal["docx", "pdf"], req: ExportRequest):
        """Dựng file .docx / .pdf từ HTML từng trang (pages[].html), mỗi trang gốc một trang mới."""
        return await build_file(settings, req.pages, fmt, req.title)

    return router
