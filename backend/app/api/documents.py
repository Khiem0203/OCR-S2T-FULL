"""API cho web: tài liệu OCR đã lưu."""
import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from ..config import Settings
from ..storage import db


def _counts(doc: dict) -> dict[int, dict]:
    """Số chỗ sửa và số từ lạ theo trang."""
    counts = {p["page"]: {"correction_count": len(p.get("corrections") or []), "unknown_count": 0} for p in doc["pages"]}
    for w in doc["unknown_words"]:
        for n in w["pages"] or []:
            if n in counts:
                counts[n]["unknown_count"] += 1
    return counts


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/documents", tags=["documents"])

    async def load(request_id: str) -> dict:
        if not db.configured(settings):
            raise HTTPException(status_code=503, detail="Database is not configured")
        doc = await asyncio.to_thread(db.get_document, settings, request_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return doc

    @router.get("")
    async def list_documents(
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        document_id: str | None = None,
    ):
        """Danh sách tài liệu đã xử lý (mới nhất trước)."""
        if not db.configured(settings):
            raise HTTPException(status_code=503, detail="Database is not configured")
        items, total = await asyncio.to_thread(db.list_documents, settings, limit, offset, document_id)
        return {"total": total, "limit": limit, "offset": offset, "items": items}

    @router.get("/{request_id}")
    async def get_document(request_id: str):
        """Tổng quan một tài liệu và từng trang."""
        doc = await load(request_id)
        counts = _counts(doc)
        pages = [
            {"page": p["page"], "source": p.get("source"), "error": p.get("error"), **counts[p["page"]]}
            for p in doc["pages"]
        ]
        summary = {k: v for k, v in doc.items() if k not in ("pages", "changes", "unknown_words")}
        return {**summary, "pages": pages, "unknown_words": doc["unknown_words"]}

    @router.get("/{request_id}/pages/{page}")
    async def get_page(request_id: str, page: int):
        """Mọi dòng của một trang, dòng bị sửa kèm bản trước."""
        doc = await load(request_id)
        found = next((p for p in doc["pages"] if p["page"] == page), None)
        if found is None:
            raise HTTPException(status_code=404, detail="Page not found")
        changed = {c["line"]: c for c in doc["changes"] if c["page"] == page}
        lines = []
        for n, text in enumerate((found.get("text") or "").split("\n"), start=1):
            line = {"line": n, "text": text, "changed": n in changed}
            if n in changed:
                c = changed[n]
                line.update(before=c["before"], segments=c.get("segments", []), fixes=c["fixes"])
            lines.append(line)
        return {
            "request_id": request_id,
            "page": page,
            "page_count": doc["page_count"],
            "source": found.get("source"),
            "error": found.get("error"),
            "correction_count": len(found.get("corrections") or []),
            "lines": lines,
            "unknown_words": [w for w in doc["unknown_words"] if page in (w["pages"] or [])],
        }

    @router.get("/{request_id}/changes")
    async def get_changes(
        request_id: str,
        page: int | None = None,
        by: Literal["protonx", "dict", "replace"] | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        """Các dòng đã sửa, lọc theo trang hoặc cách sửa."""
        doc = await load(request_id)
        items = [c for c in doc["changes"] if page is None or c["page"] == page]
        if by:
            items = [c for c in items if any(f["by"] == by for f in c["fixes"])]
        return {"total": len(items), "limit": limit, "offset": offset, "items": items[offset : offset + limit]}

    return router
