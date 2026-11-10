"""Lấy đầu vào từ S3: đọc file văn bản trước, không dùng được mới chuẩn bị OCR."""
import logging
import posixpath
from pathlib import Path

from ..config import Settings
from ..pipeline.pages import PageSource, TooManyPages, UnsupportedFile
from .select import kind, natural_key, plan
from .textfile import read_text_file

log = logging.getLogger("ocr.inputs")


def text_page(number: int, text: str) -> dict:
    return {"page": number, "raw_text": text, "text": text, "source": "text_file", "corrections": [], "error": None}


def read_texts(fetch, keys: list[str], settings: Settings, info: dict) -> list[str] | None:
    """Tải và thử lần lượt từng file văn bản; trả các trang của file đầu tiên dùng được."""
    for key in keys:
        path = fetch(key)
        pages, problem = read_text_file(path, settings.text_page_chars)
        path.unlink(missing_ok=True)
        if not problem:
            info["files"].append(path.name)
            return pages
        log.warning("%s: not usable (%s)", path.name, problem)
        info["skipped"].append({"file": path.name, "reason": problem})
        if pages and "fallback" not in info:
            info["fallback"] = (path.name, pages)
    return None


def collect(storage, s3_path: str, folder: Path, settings: Settings, prepare) -> dict:
    """Trả {"input": ..., "pages": [...]} khi có text, hoặc {"input": ..., "image_paths", "text_paths"} để OCR.

    prepare(source, out_dir, settings, start) trả (ảnh, text PDF, báo cáo lớp text); ảnh rỗng là trang dùng text.
    """
    max_bytes = settings.max_upload_mb * 1024 * 1024
    bucket, keys, requested = storage.candidates(s3_path, settings.input_siblings)
    texts, ocr = plan(keys, requested)
    if not texts and not ocr:
        raise UnsupportedFile("no supported file (PDF, image, docx, doc, odt, rtf, txt, md)")
    info = {"mode": "", "files": [], "skipped": []}

    # TEXT_FIRST=false thì chỉ đọc file văn bản khi không có gì để OCR
    if texts and (settings.text_first or not ocr):
        pages = read_texts(lambda k: storage.download(bucket, k, folder / "text", max_bytes), texts, settings, info)
        if pages is not None:
            info["mode"] = "text"
            return {"input": info, "pages": [text_page(i + 1, t) for i, t in enumerate(pages)]}

    if not ocr:
        # Không có gì để OCR: dùng tạm text kém chất lượng nếu có
        name, pages = info.pop("fallback", (None, None))
        if pages is None:
            raise ValueError("text files could not be read and there is no PDF or image to OCR")
        info.update(mode="text", files=[name])
        info["skipped"] = [s for s in info["skipped"] if s["file"] != name]
        info["warning"] = "text file has quality problems and there is nothing to OCR"
        return {"input": info, "pages": [text_page(i + 1, t) for i, t in enumerate(pages)]}

    info.pop("fallback", None)
    if kind(ocr[0]) == "image" and len(ocr) > settings.max_pages:
        raise TooManyPages(f"folder has {len(ocr)} images; limit is {settings.max_pages}")
    images, page_texts = [], []
    for key in ocr:
        local = storage.download(bucket, key, folder / "src", max_bytes)
        source = PageSource(local.read_bytes(), settings.render_dpi, settings.max_pages)
        try:
            if len(images) + source.count > settings.max_pages:
                raise TooManyPages(f"files have more than {settings.max_pages} pages in total")
            a, b, report = prepare(source, folder / "pages", settings, len(images))
        finally:
            source.close()
        local.unlink(missing_ok=True)
        images += a
        page_texts += b
        info["files"].append(posixpath.basename(key))
        if report:
            info.setdefault("pdf_text", []).append({**report, "file": posixpath.basename(key)})
    info["mode"] = "ocr"
    return {"input": info, "image_paths": images, "text_paths": page_texts}


def collect_audio(storage, s3_path: str, folder: Path, settings: Settings, prepare) -> dict:
    """Tải file âm thanh (một file hoặc cả thư mục, theo tên) và giải mã từng file.

    prepare(src, out_dir, settings, index, used_seconds) trả thông tin file và các đoạn.
    """
    bucket, keys, _ = storage.candidates(s3_path, siblings=False)
    keys = sorted((k for k in keys if kind(k) == "audio"), key=natural_key)
    if not keys:
        raise UnsupportedFile("no supported audio file (mp3, m4a, wav, flac, ogg, opus, aac, wma, mp4...)")
    files, used = [], 0.0
    for i, key in enumerate(keys, start=1):
        local = storage.download(bucket, key, folder / "src", settings.speech_max_upload_mb * 1024 * 1024)
        try:
            f = prepare(local, folder / "pcm", settings, i, used)
        finally:
            local.unlink(missing_ok=True)
        used += f["duration"]
        files.append(f)
    return {"files": files}
