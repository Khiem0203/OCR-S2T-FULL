"""Đọc text từ file văn bản."""
import logging
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path

log = logging.getLogger("ocr.inputs")

# Bảng mã TCVN3 (font .Vn*) sang Unicode

_TCVN3_PAIRS = [
    ("µ¸¶·¹", "àáảãạ"),
    ("¨»¾¼½Æ", "ăằắẳẵặ"),
    ("©ÇÊÈÉË", "âầấẩẫậ"),
    ("®", "đ"),
    ("ÌÐÎÏÑ", "èéẻẽẹ"),
    ("ªÒÕÓÔÖ", "êềếểễệ"),
    ("×ÝØÜÞ", "ìíỉĩị"),
    ("ßãáâä", "òóỏõọ"),
    ("«åèæçé", "ôồốổỗộ"),
    ("¬êíëìî", "ơờớởỡợ"),
    ("ïóñòô", "ùúủũụ"),
    ("\xadõøö÷ù", "ưừứửữự"),
    ("úýûüþ", "ỳýỷỹỵ"),
    ("¡¢£¤¥¦§", "ĂÂÊÔƠƯĐ"),
]
TCVN3_MAP = {t: u for ts, us in _TCVN3_PAIRS for t, u in zip(ts, us)}
_VN_LATIN1 = set("ÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝàáâãèéêìíòóôõùúý")
TCVN3_MARKERS = {c for c in TCVN3_MAP if c not in _VN_LATIN1} - {"\xad"}
_VN_UNICODE = re.compile("[ĂăĐđĨĩŨũƠơƯưẠ-ỹ]")
_TCVN3_FONT = re.compile(r"^\.vn|tcvn", re.I)


def looks_like_tcvn3(text: str) -> bool:
    if not text:
        return False
    letters = sum(c.isalpha() for c in text)
    markers = sum(c in TCVN3_MARKERS for c in text)
    return markers >= 2 and markers >= 0.02 * letters and len(_VN_UNICODE.findall(text)) * 5 <= markers


def convert_tcvn3(text: str, assume: bool | None = None) -> str:
    """Đổi TCVN3 sang Unicode."""
    if not text or not (looks_like_tcvn3(text) if assume is None else assume):
        return text
    return "\n".join(
        line if _VN_UNICODE.search(line) else "".join(TCVN3_MAP.get(c, c) for c in line) for line in text.split("\n")
    )


def quality_problem(pages: list[str]) -> str:
    """Lý do không dùng được; rỗng nếu dùng được."""
    text = "\n".join(pages)
    visible = sum(not c.isspace() for c in text)
    if not visible:
        return "không có chữ"
    letters = sum(c.isalpha() for c in text)
    if letters / visible < 0.5:
        return f"chỉ {letters / visible:.0%} ký tự là chữ cái, nghi lỗi mã hoá"
    leftover = sum(c in TCVN3_MARKERS for c in text)
    if leftover >= 3 and letters and leftover / letters >= 0.02:
        return f"còn {leftover} ký tự TCVN3 chưa đổi được"
    return ""


def split_pages(text: str, page_chars: int) -> list[str]:
    """Chia text dài thành trang ảo."""
    pages, current, size = [], [], 0
    for line in text.strip().split("\n"):
        if current and size + len(line) > page_chars:
            pages.append("\n".join(current).strip())
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        pages.append("\n".join(current).strip())
    return [p for p in pages if p]


# docx


def _tag(el) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _blocks(parent, qn):
    """Đoạn văn và bảng theo đúng thứ tự."""
    for child in parent.iterchildren():
        tag = _tag(child)
        if tag in ("p", "tbl"):
            yield tag, child
        elif tag == "sdt":
            content = child.find(qn("w:sdtContent"))
            if content is not None:
                yield from _blocks(content, qn)


def _page_break(p_el, qn) -> str | None:
    """Ngắt trang trong đoạn: before, after hoặc None."""
    found = False
    pbb = p_el.find(f"./{qn('w:pPr')}/{qn('w:pageBreakBefore')}")
    if pbb is not None and pbb.get(qn("w:val"), "1").lower() not in ("0", "false", "off"):
        found = True
    text_before = text_after = False
    for el in p_el.iter():
        if el.tag == qn("w:lastRenderedPageBreak") or (el.tag == qn("w:br") and el.get(qn("w:type")) == "page"):
            found = True
        elif el.tag == qn("w:t") and el.text and el.text.strip():
            if found:
                text_after = True
            else:
                text_before = True
    if not found:
        return None
    return "after" if text_before and not text_after else "before"


def _section_break(p_el, qn) -> bool:
    sect = p_el.find(f"./{qn('w:pPr')}/{qn('w:sectPr')}")
    if sect is None:
        return False
    typ = sect.find(qn("w:type"))
    return (typ.get(qn("w:val")) if typ is not None else "nextPage") in ("nextPage", "oddPage", "evenPage")


def _para_text(para) -> str:
    text = para.text.strip()
    if not text:
        return text
    try:
        fonts = [r.font.name for r in para.runs if r.font.name]
        if not fonts and para.style is not None and para.style.font is not None and para.style.font.name:
            fonts = [para.style.font.name]
    except Exception:
        fonts = []
    return convert_tcvn3(text, assume=True) if any(_TCVN3_FONT.search(f) for f in fonts) else text


def read_docx(path: str | Path) -> list[str]:
    """Đọc docx theo ngắt trang, gồm bảng và text box."""
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    pages, current = [], []

    def flush():
        if any(current):
            pages.append("\n".join(current))
        current.clear()

    for kind, el in _blocks(doc.element.body, qn):
        if kind == "tbl":
            for row in Table(el, doc).rows:
                seen, cells = set(), []
                for cell in row.cells:
                    if cell._tc in seen:
                        continue
                    seen.add(cell._tc)
                    t = " ".join(x for x in (_para_text(p) for p in cell.paragraphs) if x)
                    if t:
                        cells.append(t)
                if cells:
                    current.append(" | ".join(cells))
            continue
        mode = _page_break(el, qn)
        if mode == "before":
            flush()
        boxes = el.xpath('.//w:txbxContent[not(ancestor::*[local-name()="Fallback"])]//w:p')
        current.extend(t for t in [_para_text(Paragraph(el, doc))] + [_para_text(Paragraph(b, doc)) for b in boxes] if t)
        if mode == "after" or _section_break(el, qn):
            flush()
    flush()
    assume = looks_like_tcvn3("\n".join(pages))
    return [convert_tcvn3(p, assume=assume) for p in pages]


# doc, odt, rtf


def _soffice_text(path: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["soffice", "--headless", "--convert-to", "txt:Text (encoded):UTF8", "--outdir", tmp, str(path)],
            capture_output=True, timeout=120,
        )
        out = Path(tmp) / (path.stem + ".txt")
        return out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""


def read_with_tools(path: str | Path) -> str:
    """.doc dùng antiword/catdoc, còn lại dùng LibreOffice."""
    path = Path(path)
    if path.suffix.lower() == ".doc":
        for cmd in (["antiword", "-m", "UTF-8.txt", str(path)], ["catdoc", "-w", "-d", "utf-8", str(path)]):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=60)
                text = r.stdout.decode("utf-8", errors="replace")
                if r.returncode == 0 and text.strip():
                    return text
            except (OSError, subprocess.SubprocessError):
                pass
    try:
        return _soffice_text(path)
    except (OSError, subprocess.SubprocessError):
        return ""


# txt


def decode_text(data: bytes) -> str:
    """Thử UTF-8, UTF-16, rồi cp1258."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp1258"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1")


# Chung


def clean(text: str) -> str:
    """Bỏ khoảng trắng và dòng trống thừa."""
    text = re.sub(r"[ \t\xa0]+", " ", unicodedata.normalize("NFC", text))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.strip() for line in text.split("\n"))).strip()


def read_text_file(path: str | Path, page_chars: int = 3000) -> tuple[list[str], str]:
    """Trả (các trang, lý do không dùng được)."""
    path = Path(path)
    ext = path.suffix.lower()
    try:
        if ext == ".docx":
            pages = read_docx(path)
        else:
            if ext in (".txt", ".md"):
                text = decode_text(path.read_bytes())
            else:
                text = read_with_tools(path)
                if not text.strip():
                    return [], "không đọc được (cần antiword, catdoc hoặc LibreOffice trên server)"
            text = unicodedata.normalize("NFC", convert_tcvn3(text.replace("\r\n", "\n").replace("\r", "\n")))
            # \f là ngắt trang
            pages = [p.strip() for p in text.split("\f")]
        pages = [clean(p) for p in pages if p.strip()]
    except Exception as exc:
        log.warning("%s: read failed: %s", path.name, type(exc).__name__)
        return [], f"không mở được file ({type(exc).__name__})"
    # Không có ngắt trang thì chia trang ảo
    pages = [q for p in pages for q in (split_pages(p, page_chars) if len(p) > page_chars * 2 else [p])]
    return pages, quality_problem(pages)
