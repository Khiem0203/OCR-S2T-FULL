"""Chọn file đầu vào: ưu tiên file văn bản, không có mới OCR."""
import posixpath
import re

from ..audio.decode import AUDIO_EXT

# Thứ tự ưu tiên file văn bản
TEXT_EXT = (".docx", ".doc", ".odt", ".rtf", ".txt", ".md")
PDF_EXT = (".pdf",)
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp")


def kind(name: str) -> str | None:
    """text, pdf, image, audio hoặc None nếu không hỗ trợ."""
    base = posixpath.basename(name)
    if not base or base.startswith(("~$", ".")):
        return None
    ext = posixpath.splitext(base)[1].lower()
    if ext in TEXT_EXT:
        return "text"
    if ext in PDF_EXT:
        return "pdf"
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    return None


def stem(name: str) -> str:
    return posixpath.splitext(posixpath.basename(name))[0].lower()


def natural_key(name: str) -> list:
    """Sắp tên theo số: trang2 trước trang10."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", posixpath.basename(name))]


def plan(names: list[str], requested: str = "") -> tuple[list[str], list[str]]:
    """Trả (file văn bản theo thứ tự thử, file cần OCR).

    requested: file người dùng chỉ định; rỗng là cả thư mục.
    """
    names = [n for n in dict.fromkeys(names) if kind(n)]
    texts = [n for n in names if kind(n) == "text"]
    texts.sort(key=lambda n: (stem(n) != stem(requested) if requested else False,
                              TEXT_EXT.index(posixpath.splitext(n)[1].lower()), natural_key(n)))
    if requested and kind(requested) in ("pdf", "image"):
        return texts, [requested]
    pdfs = sorted((n for n in names if kind(n) == "pdf"), key=natural_key)
    # Có PDF thì chỉ OCR PDF, không thì OCR ảnh (mỗi ảnh một trang)
    return texts, pdfs or sorted((n for n in names if kind(n) == "image"), key=natural_key)
