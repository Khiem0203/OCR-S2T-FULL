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


# Chấm text có ra tiếng Việt thật không: tỉ lệ từ là âm tiết hợp lệ

_TONES = {"̀", "́", "̃", "̉", "̣"}
_RHYMES = ("a|ă|â|e|ê|i|o|ô|ơ|u|ư|y|ai|ao|au|ay|âu|ây|eo|êu|ia|iê|iu|oa|oă|oe|oi|ôi|ơi|oo|ua|uâ|uê|ui|uô|uơ|uy"
           "|ưa|ưi|ưu|ươ|iêu|oai|oay|oeo|uây|uôi|uya|uyê|uyu|ươi|ươu|yê|yêu")
_SYLLABLE = re.compile(rf"^(ngh|ng|nh|ch|gh|gi|kh|ph|qu|th|tr|[bcdđghklmnprstvx])?({_RHYMES})(ch|ng|nh|[cmnpt])?$")
_WORD = re.compile(r"[^\W\d_]+")
VN_MIN_SCORE = 0.5
# Chữ Latin-1 không có trong tiếng Việt: còn nhiều là phông cũ (TCVN3, VNI, VPS...) chưa đổi được
_LEGACY_CHARS = ({chr(c) for c in range(0xC0, 0x100)} - set("ÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝàáâãèéêìíòóôõùúý×÷")) | TCVN3_MARKERS


def _is_syllable(word: str) -> bool:
    d = unicodedata.normalize("NFD", word.lower())
    if sum(c in _TONES for c in d) > 1:
        return False
    return bool(_SYLLABLE.match(unicodedata.normalize("NFC", "".join(c for c in d if c not in _TONES))))


def vn_score(text: str) -> float:
    words = _WORD.findall(unicodedata.normalize("NFC", text))
    return sum(map(_is_syllable, words)) / len(words) if words else 0.0


# Bảng mã VNI-Windows: chữ gốc + ký tự dấu đứng sau ('HÖÔÙNG DAÃN' = 'HƯỚNG DẪN', 'ñ' = 'đ')

_VNI_TONES = ("́", "̀", "̉", "̃", "̣")  # sắc, huyền, hỏi, ngã, nặng


def _vni_map() -> dict:
    m = {}

    # base: chữ gốc VNI; hat: ký tự mũ / trăng; marks: ký tự dấu theo thứ tự sắc, huyền, hỏi, ngã, nặng
    def add(base, char, marks, hat=""):
        for b, c, k, h in ((base, char, marks, hat), (base.upper(), char.upper(), marks.upper(), hat.upper()),
                           (base.upper(), char.upper(), marks, hat)):  # chữ hoa đầu từ, dấu viết thường
            if b + h != c:
                m[b + h] = c
            for mark, tone in zip(k, _VNI_TONES):
                m[b + mark] = unicodedata.normalize("NFC", c + tone)

    for v in "aeou":
        add(v, v, "ùøûõï")
    add("y", "y", "ùøûõ")
    add("a", "â", "áàåãä", "â")
    add("e", "ê", "áàåãä", "â")
    add("o", "ô", "áàåãä", "â")
    add("a", "ă", "éèúüë", "ê")
    add("ô", "ơ", "ùøûõï")
    add("ö", "ư", "ùøûõï")
    m.update({"ñ": "đ", "Ñ": "Đ", "æ": "ỉ", "Æ": "Ỉ", "ó": "ĩ", "Ó": "Ĩ", "ò": "ị", "Ò": "Ị", "î": "ỵ", "Î": "Ỵ"})
    return m


VNI_MAP = _vni_map()
_VNI_RE = re.compile("|".join(map(re.escape, sorted(VNI_MAP, key=len, reverse=True))))


def convert_vni(text: str) -> str:
    """Đổi VNI sang Unicode; giữ nguyên dòng đã có chữ Unicode tiếng Việt."""
    return "\n".join(
        line if _VN_UNICODE.search(line) else _VNI_RE.sub(lambda x: VNI_MAP[x.group(0)], line)
        for line in text.split("\n")
    )


def pick_legacy(text: str):
    """Cách đổi phông cũ (TCVN3 hoặc VNI) ra tiếng Việt thật nhất; None nếu text đã ổn.

    Chỉ nhận khi bản đổi đạt VN_MIN_SCORE và hơn bản gốc, để không làm hỏng text Unicode
    hay text mã khác (VPS, VietWare) bị nhận nhầm.
    """
    if not text:
        return None
    best, best_score, base = None, 0.0, vn_score(text)
    for fn in (lambda t: convert_tcvn3(t, assume=True), convert_vni):
        out = fn(text)
        if out == text:
            continue
        score = vn_score(out)
        if score >= VN_MIN_SCORE and score > base and score > best_score:
            best, best_score = fn, score
    return best


def fix_legacy(text: str) -> str:
    fn = pick_legacy(text)
    return fn(text) if fn else text


def quality_problem(pages: list[str]) -> str:
    """Lý do không dùng được; rỗng nếu dùng được."""
    text = "\n".join(pages)
    visible = sum(not c.isspace() for c in text)
    if not visible:
        return "không có chữ"
    letters = sum(c.isalpha() for c in text)
    if letters / visible < 0.5:
        return f"chỉ {letters / visible:.0%} ký tự là chữ cái, nghi lỗi mã hoá"
    leftover, score = sum(c in _LEGACY_CHARS for c in text), vn_score(text)
    if leftover >= 3 and letters and leftover / letters >= 0.02 and score < VN_MIN_SCORE:
        return (f"còn {leftover} ký tự lạ, chỉ {score:.0%} âm tiết tiếng Việt hợp lệ, "
                f"nghi phông cũ (TCVN3 / VNI / VPS...) chưa đổi được")
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
    # Phông cũ: chọn bảng mã theo cả file, đổi từng trang
    fn = pick_legacy("\n".join(pages))
    return [fn(p) for p in pages] if fn else pages


_WORD_ALIGN = {1: "center", 2: "right", 3: "justify", 4: "justify", 5: "justify", 7: "justify", 8: "justify", 9: "justify"}


def _para_align(para) -> str | None:
    """Căn lề đoạn Word (kể cả kế thừa từ kiểu đoạn); căn trái trả None."""
    try:
        value = para.paragraph_format.alignment
        style = para.style
        while value is None and style is not None:
            value = style.paragraph_format.alignment
            style = style.base_style
    except Exception:
        return None
    return _WORD_ALIGN.get(int(value)) if value is not None else None


def _style_flag(para, name: str) -> bool:
    """Đậm / nghiêng của kiểu đoạn (kể cả kiểu gốc)."""
    try:
        style = para.style
        while style is not None:
            value = getattr(style.font, name)
            if value is not None:
                return bool(value)
            style = style.base_style
    except Exception:
        pass
    return False


def _para_segments(para) -> list[tuple[str, str]]:
    """Đoạn Word -> các đoạn (chữ, kiểu): đậm, nghiêng, gạch chân, chỉ số trên / dưới; chữ "All caps" viết hoa."""
    from ..pipeline.layout import fmt_of

    try:
        fonts = [r.font.name for r in para.runs if r.font.name]
        if not fonts and para.style is not None and para.style.font is not None and para.style.font.name:
            fonts = [para.style.font.name]
    except Exception:
        fonts = []
    tcvn3 = any(_TCVN3_FONT.search(f) for f in fonts)
    bold, italic = _style_flag(para, "bold"), _style_flag(para, "italic")
    segs = []
    try:
        items = list(para.iter_inner_content())
    except Exception:
        return [(para.text, "")]
    for item in items:
        for run in getattr(item, "runs", None) or [item]:
            text = run.text
            if not text:
                continue
            font = run.font
            if font.all_caps or font.small_caps:
                text = text.upper()
            if tcvn3:
                text = convert_tcvn3(text, assume=True)
            kinds = set()
            if run.bold if run.bold is not None else bold:
                kinds.add("b")
            if run.italic if run.italic is not None else italic:
                kinds.add("i")
            if run.underline not in (None, False):
                kinds.add("u")
            if font.superscript:
                kinds.add("sup")
            if font.subscript:
                kinds.add("sub")
            segs.append((text, fmt_of(kinds)))
    return segs


def _docx_table(tbl, doc, qn, Paragraph) -> dict | None:
    """Bảng Word -> khối bảng: gridSpan thành colspan, vMerge thành rowspan, hàng tiêu đề lặp thành th."""
    from ..pipeline.layout import segments_to_lines

    rows, open_cells = [], {}  # cột lưới -> ô đang gộp dọc
    for tr in tbl.iterchildren(qn("w:tr")):
        tr_pr = tr.find(qn("w:trPr"))
        header = tr_pr is not None and tr_pr.find(qn("w:tblHeader")) is not None
        col = 0
        before = tr_pr.find(qn("w:gridBefore")) if tr_pr is not None else None
        if before is not None:
            col += int(before.get(qn("w:val"), "0") or 0)
        row, grown = [], []
        cells = [c for c in tr.iterchildren() if _tag(c) == "tc"]
        cells += [c for sdt in tr.iterchildren(qn("w:sdt")) for c in sdt.iter(qn("w:tc"))]
        for tc in cells:
            tc_pr = tc.find(qn("w:tcPr"))
            grid = tc_pr.find(qn("w:gridSpan")) if tc_pr is not None else None
            span = max(1, int(grid.get(qn("w:val"), "1") or 1)) if grid is not None else 1
            vmerge = tc_pr.find(qn("w:vMerge")) if tc_pr is not None else None
            if vmerge is not None and vmerge.get(qn("w:val"), "continue") == "continue" and col in open_cells:
                open_cells[col]["rowspan"] += 1
                grown.append(open_cells[col])
                col += span
                continue
            segs, align = [], None
            for p in tc.iter(qn("w:p")):
                para = Paragraph(p, doc)
                part = _para_segments(para)
                if not "".join(t for t, _ in part).strip():
                    continue
                if segs:
                    segs.append((" ", ""))
                else:
                    align = _para_align(para)
                segs.extend(part)
            lines = segments_to_lines(segs, split=False)
            text, marks = lines[0] if lines else ("", [])
            cell = {"text": text, "th": header, "colspan": span, "rowspan": 1}
            if marks:
                cell["marks"] = marks
            if align:
                cell["align"] = align
            row.append(cell)
            if vmerge is not None:
                open_cells[col] = cell
            else:
                open_cells.pop(col, None)
            col += span
        if row:
            rows.append(row)
        else:
            # Cả hàng chỉ là phần gộp dọc: bỏ hàng, trả lại rowspan
            for cell in grown:
                cell["rowspan"] -= 1
    return {"type": "table", "caption": "", "rows": rows} if rows else None


def _docx_paragraph(el, doc, qn, Paragraph, page: list) -> None:
    """Thêm đoạn văn vào trang: tiêu đề (Heading / Title), mục danh sách hoặc đoạn thường."""
    from ..pipeline.layout import segments_to_lines

    para = Paragraph(el, doc)
    lines = segments_to_lines(_para_segments(para))
    if not lines:
        return
    try:
        style = (para.style.name or "").lower() if para.style is not None else ""
    except Exception:
        style = ""
    num = el.find(f"./{qn('w:pPr')}/{qn('w:numPr')}")
    if num is not None or style.startswith("list"):
        lvl = num.find(qn("w:ilvl")) if num is not None else None
        level = int(lvl.get(qn("w:val"), "0") or 0) if lvl is not None else 0
        last = page[-1] if page else None
        if not (last and last["type"] == "list"):
            last = {"type": "list", "ordered": "number" in style, "items": []}
            page.append(last)
        for text, marks in lines:
            item = {"text": text, "level": level}
            if marks:
                item["marks"] = marks
            last["items"].append(item)
        return
    heading = re.match(r"heading (\d)", style)
    tag = f"h{min(int(heading.group(1)), 6)}" if heading else ("h1" if style == "title" else "p")
    if tag != "p":
        from ..pipeline.layout import drop_kind

        lines = [(t, drop_kind(m, "b")) for t, m in lines]
    block = {"type": "text", "tag": tag, "lines": [t for t, _ in lines]}
    if any(m for _, m in lines):
        block["marks"] = [m for _, m in lines]
    align = _para_align(para)
    if align:
        block["align"] = align
    page.append(block)


def _split_layout(blocks: list[dict], page_chars: int) -> list[list[dict]]:
    """Trang Word quá dài (không có ngắt trang): chia thành trang ảo ở ranh giới khối."""
    from ..pipeline.layout import layout_text

    if len(layout_text(blocks)) <= page_chars * 2:
        return [blocks]
    pages, current, size = [], [], 0
    for b in blocks:
        n = len(layout_text([b]))
        if current and size + n > page_chars:
            pages.append(current)
            current, size = [], 0
        current.append(b)
        size += n + 2
    if current:
        pages.append(current)
    return pages


def read_docx_layout(path: str | Path, page_chars: int = 3000) -> list[list[dict]]:
    """Đọc docx thành bố cục từng trang (đoạn, tiêu đề, danh sách, bảng giữ ô gộp), chia trang như read_docx."""
    from docx import Document
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    pages, current = [], []

    def flush():
        if current:
            pages.append(list(current))
        current.clear()

    for kind, el in _blocks(doc.element.body, qn):
        if kind == "tbl":
            table = _docx_table(el, doc, qn, Paragraph)
            if table:
                current.append(table)
            continue
        mode = _page_break(el, qn)
        if mode == "before":
            flush()
        _docx_paragraph(el, doc, qn, Paragraph, current)
        for box in el.xpath('.//w:txbxContent[not(ancestor::*[local-name()="Fallback"])]//w:p'):
            _docx_paragraph(box, doc, qn, Paragraph, current)
        if mode == "after" or _section_break(el, qn):
            flush()
    flush()
    from ..pipeline.layout import layout_text

    fn = pick_legacy("\n".join(layout_text(p) for p in pages))
    if fn:
        from ..pipeline.layout import map_layout

        pages = [map_layout(p, fn) for p in pages]
    return [q for p in pages for q in _split_layout(p, page_chars)]


def read_text_pages(path: str | Path, page_chars: int = 3000) -> tuple[list[dict], str]:
    """Như read_text_file nhưng mỗi trang kèm bố cục: [{"text", "layout"}], lý do không dùng được.
    docx có bố cục (bảng giữ ô gộp); định dạng khác layout = None."""
    from ..pipeline.layout import layout_text

    path = Path(path)
    if path.suffix.lower() != ".docx":
        texts, problem = read_text_file(path, page_chars)
        return [{"text": t, "layout": None} for t in texts], problem
    try:
        layouts = [p for p in read_docx_layout(path, page_chars) if p]
    except Exception as exc:
        log.warning("%s: read failed: %s", path.name, type(exc).__name__)
        return [], f"không mở được file ({type(exc).__name__})"
    pages = [{"text": layout_text(b), "layout": b} for b in layouts]
    pages = [p for p in pages if p["text"].strip()]
    return pages, quality_problem([p["text"] for p in pages])


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
            text = unicodedata.normalize("NFC", fix_legacy(text.replace("\r\n", "\n").replace("\r", "\n")))
            # \f là ngắt trang
            pages = [p.strip() for p in text.split("\f")]
        pages = [clean(p) for p in pages if p.strip()]
    except Exception as exc:
        log.warning("%s: read failed: %s", path.name, type(exc).__name__)
        return [], f"không mở được file ({type(exc).__name__})"
    # Không có ngắt trang thì chia trang ảo
    pages = [q for p in pages for q in (split_pages(p, page_chars) if len(p) > page_chars * 2 else [p])]
    return pages, quality_problem(pages)
