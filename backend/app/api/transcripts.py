"""API cho web: bản chép lời đã lưu."""
import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from ..config import Settings
from ..storage import db


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/transcripts", tags=["transcripts"])

    def need_db() -> None:
        if not db.configured(settings):
            raise HTTPException(status_code=503, detail="Database is not configured")

    async def load(request_id: str) -> dict:
        need_db()
        doc = await asyncio.to_thread(db.get_transcript, settings, request_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Transcript not found")
        return doc

    @router.get("")
    async def list_transcripts(
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        document_id: str | None = None,
    ):
        """Danh sách bản chép lời (mới nhất trước)."""
        need_db()
        items, total = await asyncio.to_thread(db.list_transcripts, settings, limit, offset, document_id)
        return {"total": total, "limit": limit, "offset": offset, "items": items}

    @router.get("/{request_id}")
    async def get_transcript(request_id: str):
        """Tổng quan: thời lượng, số đoạn, số chỗ sửa, từ lạ."""
        doc = await load(request_id)
        return {k: v for k, v in doc.items() if k not in ("segments", "changes")}

    @router.get("/{request_id}/segments")
    async def get_segments(
        request_id: str,
        file: str | None = None,
        changed: bool | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        """Các đoạn theo thời gian, đoạn bị sửa kèm bản trước."""
        doc = await load(request_id)
        fixed = {c["segment"]: c for c in doc["changes"]}
        items = []
        for s in doc["segments"]:
            if file and s["file"] != file:
                continue
            if changed is not None and (s["index"] in fixed) != changed:
                continue
            item = {k: s[k] for k in ("index", "file", "start", "end", "text", "error")}
            item["changed"] = s["index"] in fixed
            if item["changed"]:
                c = fixed[s["index"]]
                item.update(before=c["before"], segments=c.get("segments", []), fixes=c["fixes"])
            items.append(item)
        return {"total": len(items), "limit": limit, "offset": offset, "items": items[offset : offset + limit]}

    @router.get("/{request_id}/changes")
    async def get_changes(
        request_id: str,
        file: str | None = None,
        by: Literal["protonx", "dict", "replace"] | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        """Các đoạn đã sửa, lọc theo file hoặc cách sửa."""
        doc = await load(request_id)
        items = [c for c in doc["changes"] if not file or c["file"] == file]
        if by:
            items = [c for c in items if any(f["by"] == by for f in c["fixes"])]
        return {"total": len(items), "limit": limit, "offset": offset, "items": items[offset : offset + limit]}

    return router
