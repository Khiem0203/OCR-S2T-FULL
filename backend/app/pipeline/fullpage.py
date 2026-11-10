"""Luồng OCR toàn trang."""
import asyncio
import base64
import logging
import tempfile
import time
from functools import lru_cache
from pathlib import Path

import httpx

from ..clients.chandra import ChandraClient, ChandraError, html_to_text
from ..clients.protonx import ProtonXClient
from ..config import Settings
from ..inputs.select import kind
from ..inputs.pdftext import file_problem
from ..inputs.textfile import read_text_file
from ..spell.checker import SpellChecker, load_replacements
from ..spell.compare import compare_lines
from ..spell.lexicon import Lexicon
from ..spell.unknown import build_notice, find_unknown_words
from .pages import PageSource, UnsupportedFile, jpeg_bytes, prepare_image, to_jpeg_b64

log = logging.getLogger("ocr.flow")


class AllPagesFailed(Exception):
    pass


def make_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(limits=httpx.Limits(max_connections=100))


@lru_cache(maxsize=4)
def _lexicon(directory: str, names: tuple, extra_words_file: str) -> Lexicon:
    return Lexicon.from_dictionaries(directory, names, extra_words_file)


def get_lexicon(settings: Settings) -> Lexicon | None:
    if not settings.dictionaries:
        return None
    return _lexicon(settings.dictionary_dir, settings.dictionaries, settings.extra_words_file)


def build_spell_checker(settings: Settings, http: httpx.AsyncClient, review: str | None = None) -> SpellChecker | None:
    lexicon = get_lexicon(settings)
    if lexicon is None or settings.spell_mode == "off":
        return None
    fixer = None
    if settings.spell_mode == "both":
        fixer = ProtonXClient(http, settings.spellfix_url, settings.spellfix_timeout, settings.spellfix_concurrency)
    rules = load_replacements(settings.replacements_file) if settings.replacements_file else []
    return SpellChecker(lexicon, fixer, settings.spell_mode, review or settings.spell_review, settings.spell_batch_chars, rules)


def build_chandra(settings: Settings, http: httpx.AsyncClient) -> ChandraClient:
    return ChandraClient(
        http, settings.chandra_url, settings.chandra_model, settings.chandra_api_key,
        settings.chandra_max_tokens, settings.chandra_timeout, settings.ocr_concurrency,
    )


def new_page(number: int) -> dict:
    return {"page": number, "raw_text": "", "text": "", "source": "chandra", "corrections": [], "error": None}


def pdf_text_page(number: int, text: str) -> dict:
    page = new_page(number)
    page["raw_text"] = page["text"] = text
    page["source"] = "pdf_text"
    return page


def check_pdf_text(source: PageSource, settings: Settings) -> tuple[list[str], list[bool], str]:
    """Trả (text từng trang, trang nào dùng text, lý do bỏ cả file); trang ít chữ thì OCR."""
    texts = [source.embedded_text(i) for i in range(source.count)]
    if not source.is_pdf:
        return texts, [False] * len(texts), ""
    problem = file_problem(texts, get_lexicon(settings), settings.pdf_text_min_known)
    if problem:
        log.warning("PDF text layer not used: %s", problem)
    use = settings.pdf_text_first and not problem
    return texts, [use and sum(c.isalpha() for c in t) >= _MIN_PAGE_LETTERS for t in texts], problem


_MIN_PAGE_LETTERS = 30


def pdf_report(name: str, used: list[bool], problem: str, settings: Settings) -> dict:
    report = {"file": name, "text_pages": sum(used), "ocr_pages": len(used) - sum(used)}
    if not settings.pdf_text_first:
        report["reason"] = "PDF_TEXT_FIRST=false"
    elif problem:
        report["reason"] = problem
    return report


# Bước 1: chuẩn bị


def prepare_pages(source: PageSource, out_dir: Path, settings: Settings, start: int = 0) -> tuple[list[str], list[str], dict]:
    """Lưu text trang dùng được, render trang còn lại; trang dùng text có ảnh rỗng."""
    out_dir.mkdir(parents=True, exist_ok=True)
    texts, used, problem = check_pdf_text(source, settings)
    images, text_paths = [], []
    for i in range(source.count):
        stem = out_dir / f"page_{start + i + 1:04d}"
        images.append("")
        text_paths.append("")
        # Lớp text lỗi thì không làm dự phòng
        if texts[i] and not problem:
            stem.with_suffix(".txt").write_text(texts[i], encoding="utf-8")
            text_paths[-1] = str(stem.with_suffix(".txt"))
        if used[i]:
            continue
        img = prepare_image(source.render(i), settings.max_long_side, settings.deskew)
        stem.with_suffix(".jpg").write_bytes(jpeg_bytes(img))
        images[-1] = str(stem.with_suffix(".jpg"))
    return images, text_paths, pdf_report("", used, problem, settings) if source.is_pdf else {}


# Bước 2: OCR


async def ocr_b64(chandra: ChandraClient, b64: str, number: int, fallback_text: str, settings: Settings) -> dict:
    page = new_page(number)
    try:
        page["raw_text"] = html_to_text(await chandra.ocr(b64))
    except ChandraError:
        log.warning("page %d: OCR backend failed", number)
        if settings.pdf_text_fallback and fallback_text:
            page["raw_text"], page["source"] = fallback_text, "pdf_text"
        else:
            page["source"], page["error"] = "none", "OCR backend error"
    except Exception:
        log.exception("page %d: unexpected error", number)
        page["source"], page["error"] = "none", "Internal error while processing page"
    page["text"] = page["raw_text"]
    return page


async def ocr_files(image_paths: list[str], text_paths: list[str], settings: Settings, http: httpx.AsyncClient) -> list[dict]:
    chandra = build_chandra(settings, http)
    sem = asyncio.Semaphore(settings.ocr_concurrency * 2)

    async def one(i: int) -> dict:
        async with sem:
            fallback = ""
            if text_paths[i]:
                fallback = await asyncio.to_thread(Path(text_paths[i]).read_text, "utf-8")
            if not image_paths[i]:
                return pdf_text_page(i + 1, fallback)
            data = await asyncio.to_thread(Path(image_paths[i]).read_bytes)
            return await ocr_b64(chandra, base64.b64encode(data).decode("ascii"), i + 1, fallback, settings)

    return list(await asyncio.gather(*(one(i) for i in range(len(image_paths)))))


# Bước 3: hậu xử lý


# Text gõ máy: mặc định không sửa chính tả
TYPED_SOURCES = ("text_file", "pdf_text")


async def postprocess(pages: list[dict], spell: SpellChecker | None, typed_too: bool = False) -> None:
    """Sửa chính tả từng trang."""
    if spell is None:
        return

    async def one(page: dict) -> None:
        if page["raw_text"] and (typed_too or page.get("source") not in TYPED_SOURCES):
            page["text"], page["corrections"] = await spell.check(page["raw_text"])
            page["changes"] = compare_lines(page["raw_text"], page["text"], page["corrections"])

    await asyncio.gather(*(one(p) for p in pages))


def finalize(pages: list[dict], settings: Settings, elapsed_ms: int, include_raw: bool = True, input_info: dict | None = None) -> dict:
    """Tạo kết quả cuối kèm từ lạ."""
    unknown, unknown_total, notice = [], 0, None
    lexicon = get_lexicon(settings)
    if settings.unknown_report and lexicon is not None:
        unknown, unknown_total = find_unknown_words(
            pages, lexicon, settings.unknown_include, settings.unknown_min_len, settings.unknown_max_words
        )
        notice = build_notice(unknown, unknown_total)
        if notice:
            log.warning(notice)
    out_pages, changes = [], []
    for p in pages:
        q = dict(p)
        # Gom các dòng bị sửa
        changes.extend({"page": p["page"], **c} for c in q.pop("changes", []))
        if not include_raw:
            q.pop("raw_text", None)
        out_pages.append(q)
    return {
        "page_count": len(pages),
        "text": "\n\n".join(p["text"] for p in pages if p["text"]),
        "pages": out_pages,
        "correction_total": sum(len(p["corrections"]) for p in pages),
        "changes": changes,
        "unknown_words": unknown,
        "unknown_total": unknown_total,
        "notice": notice,
        "input": input_info or {"mode": "ocr", "files": [], "skipped": []},
        "elapsed_ms": elapsed_ms,
    }


# Xử lý cả file một lần (endpoint sync)


async def process_document(
    data: bytes,
    settings: Settings,
    http: httpx.AsyncClient,
    *,
    spellcheck: bool = True,
    include_raw: bool = False,
    filename: str = "",
) -> dict:
    started = time.perf_counter()
    name = Path(filename).name or "document"
    if kind(name) == "text":
        # File văn bản: không OCR
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            await asyncio.to_thread(path.write_bytes, data)
            texts, problem = await asyncio.to_thread(read_text_file, path, settings.text_page_chars)
        if not texts:
            raise UnsupportedFile(f"could not read {name}: {problem}")
        info = {"mode": "text", "files": [name], "skipped": []}
        if problem:
            info["warning"] = problem
        pages = [{**new_page(i + 1), "raw_text": t, "text": t, "source": "text_file"} for i, t in enumerate(texts)]
        await postprocess(pages, build_spell_checker(settings, http) if spellcheck else None, settings.spell_text_files)
        return finalize(pages, settings, int((time.perf_counter() - started) * 1000), include_raw, info)

    source = await asyncio.to_thread(PageSource, data, settings.render_dpi, settings.max_pages)
    chandra = build_chandra(settings, http)
    sem = asyncio.Semaphore(settings.ocr_concurrency * 2)  # Giới hạn số trang giữ trong RAM
    try:
        texts, used, problem = await asyncio.to_thread(check_pdf_text, source, settings)
    except Exception:
        source.close()
        raise

    async def one(i: int) -> dict:
        async with sem:
            if used[i]:
                return pdf_text_page(i + 1, texts[i])
            fallback = "" if problem else texts[i]
            image = await asyncio.to_thread(source.render, i)
            b64 = await asyncio.to_thread(to_jpeg_b64, image, settings.max_long_side, settings.deskew)
            return await ocr_b64(chandra, b64, i + 1, fallback, settings)

    try:
        pages = list(await asyncio.gather(*(one(i) for i in range(source.count))))
    finally:
        source.close()
    if all(p["error"] for p in pages):
        raise AllPagesFailed()
    await postprocess(pages, build_spell_checker(settings, http) if spellcheck else None, settings.spell_text_files)
    info = {"mode": "ocr", "files": [name], "skipped": []}
    if source.is_pdf:
        info["pdf_text"] = [pdf_report(name, used, problem, settings)]
    return finalize(pages, settings, int((time.perf_counter() - started) * 1000), include_raw, info)
