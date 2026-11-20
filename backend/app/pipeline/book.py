"""Luồng metadata sách: lấy text trang cuối (Word, text PDF hoặc OCR) rồi tách thông tin xuất bản."""
import asyncio
import logging
import time
from pathlib import Path

import httpx

from ..book.fields import FIELDS, NUMBER_FIELDS, count, extract, info_block, is_info_page
from ..clients.chandra import ChandraError, html_to_text
from ..config import Settings
from ..inputs.pdftext import file_problem
from ..inputs.select import kind, plan
from ..inputs.textfile import read_text_file
from .fullpage import build_chandra, get_lexicon
from .pages import PageSource, UnsupportedFile, to_jpeg_b64

log = logging.getLogger("ocr.book")

_MIN_LETTERS = 30
_FILL = ("dia_chi_nxb", "dien_thoai_nxb", "fax_nxb", "sdt_quan_ly_xuat_ban", "sdt_tong_bien_tap")


def empty_fields() -> dict:
    return {k: (None if k in NUMBER_FIELDS else "") for k in FIELDS}


def pick(pages: list[tuple[int | None, str]]) -> tuple[int | None, str, dict]:
    """Chọn trang thông tin xuất bản (đã xếp từ cuối lên); trường thiếu lấy thêm từ trang khác."""
    main = next(((no, t) for no, t in pages if is_info_page(t)), None)
    if main is None:
        main = max(pages, key=lambda p: count(extract(p[1])))
    no, text = main
    text = info_block(text)
    fields = extract(text)
    # Trang khác (vd bìa sau) chỉ bổ sung địa chỉ, điện thoại NXB
    for other_no, other in pages:
        if other_no != no:
            for k, v in extract(other).items():
                if k in _FILL and v and not fields.get(k):
                    fields[k] = v
    return no, text, fields


async def _ocr_pages(images: list[tuple[int, object]], settings: Settings, http: httpx.AsyncClient) -> list[tuple[int, str]]:
    """OCR từng trang từ cuối lên, dừng ở trang thông tin xuất bản."""
    chandra = build_chandra(settings, http)
    out = []
    for no, render in images:
        b64 = await asyncio.to_thread(lambda: to_jpeg_b64(render(), settings.max_long_side, settings.deskew))
        try:
            text = html_to_text(await chandra.ocr(b64, f"page {no}"))
        except ChandraError:
            log.warning("page %s: OCR backend failed", no)
            continue
        out.append((no, text))
        if is_info_page(text):
            break
    return out


async def run_book(storage, path: str, folder: Path, settings: Settings, http: httpx.AsyncClient) -> dict:
    """Metadata một cuốn sách từ file chỉ định (kèm file cùng tên) hoặc cả thư mục."""
    started = time.perf_counter()
    n = settings.book_last_pages
    max_bytes = settings.max_upload_mb * 1024 * 1024
    bucket, keys, requested = await asyncio.to_thread(storage.candidates, path, settings.input_siblings)
    texts, ocr = plan(keys, requested)
    if not texts and not ocr:
        raise UnsupportedFile("no supported file (PDF, image, docx, doc, odt, rtf, txt, md)")
    info = {"files": [], "skipped": []}

    def done(source: str, file: str, pages: list, page_count: int | None, error: str = "") -> dict:
        no, text, fields = pick(pages) if pages else (None, "", empty_fields())
        info["files"].append(file)
        return {
            "thong_tin": fields, "field_count": count(fields), "text": text, "source": source,
            "file": file, "page": no, "page_count": page_count, "error": error, "input": info,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    # 1. File văn bản: N trang cuối
    for key in texts:
        local = await asyncio.to_thread(storage.download, bucket, key, folder / "text", max_bytes)
        pages, problem = await asyncio.to_thread(read_text_file, local, settings.text_page_chars)
        if not problem:
            last = list(enumerate(pages, start=1))[-n:][::-1]
            block = info_block("\n".join(t for _, t in last[::-1]))
            if is_info_page(block) or count(extract(block)) >= settings.book_min_fields:
                return done("text_file", local.name, last, len(pages))
            problem = f"không thấy thông tin xuất bản trong {n} trang cuối"
        info["skipped"].append({"file": local.name, "reason": problem})

    if not ocr:
        return done("", "", [], None, "không tìm thấy thông tin xuất bản")

    # 2. PDF: file cuối cùng (theo tên) chứa trang cuối sách; 3. ảnh: N ảnh cuối
    if kind(ocr[0]) == "pdf":
        local = await asyncio.to_thread(storage.download, bucket, ocr[-1], folder / "src", max_bytes)
        source = await asyncio.to_thread(PageSource, local.read_bytes(), settings.render_dpi, 100_000)
        try:
            total = source.count
            last = list(range(total, max(0, total - n), -1))
            layer = await asyncio.to_thread(lambda: [source.embedded_text(i) for i in range(total)])
            problem = file_problem(layer, get_lexicon(settings), settings.pdf_text_min_known)
            usable = [(no, layer[no - 1]) for no in last if sum(c.isalpha() for c in layer[no - 1]) >= _MIN_LETTERS]
            if settings.pdf_text_first and not problem and usable:
                return done("pdf_text", local.name, usable, total)
            info["pdf_text"] = problem or "không có lớp text ở các trang cuối"
            pages = await _ocr_pages([(no, lambda i=no - 1: source.render(i)) for no in last], settings, http)
        finally:
            source.close()
        return done("ocr", local.name, pages, total, "" if pages else "OCR backend unavailable")

    images = ocr[-n:][::-1]
    pages = []
    for i, key in enumerate(images):
        local = await asyncio.to_thread(storage.download, bucket, key, folder / "src", max_bytes)
        src = await asyncio.to_thread(PageSource, local.read_bytes(), settings.render_dpi, 100_000)
        try:
            found = await _ocr_pages([(len(ocr) - i, lambda s=src: s.render(0))], settings, http)
        finally:
            src.close()
        pages += found
        if found and is_info_page(found[0][1]):
            break
    return done("ocr", Path(images[0]).name, pages, len(ocr), "" if pages else "OCR backend unavailable")
