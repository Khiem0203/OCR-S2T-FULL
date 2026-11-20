"""Xuất kết quả ra .docx / .pdf, dựng lại gần nhất với văn bản gốc.

Đầu vào là HTML từng trang (pages[].html). HTML được đọc lại thành bố cục (parse_layout) nên chỉ giữ
những gì backend hiểu: tiêu đề, đoạn văn (căn lề, xuống dòng), danh sách, bảng (ô gộp, hàng tiêu đề),
chữ đậm / nghiêng / gạch chân / chỉ số trên, dưới. Mỗi trang gốc bắt đầu một trang mới.

Trình bày theo thể thức văn bản hành chính: khổ A4, lề trên / dưới 20 mm, trái 30 mm, phải 15 mm,
Times New Roman 13.
"""
import io
import logging
import os
import re
from pathlib import Path

from .layout import blocks_html, parse_layout

log = logging.getLogger("ocr.export")

FORMATS = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}
MARGINS_MM = {"top": 20, "bottom": 20, "left": 30, "right": 15}
FONT_SIZE = 13
HEADING_SIZES = {"h1": 16, "h2": 14}

# Phông cho PDF: (thường, đậm, nghiêng, đậm nghiêng). Lấy bộ đầu tiên có đủ file.
_FONT_SETS = [
    ("C:/Windows/Fonts", ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf")),
    ("/usr/share/fonts/truetype/msttcorefonts", ("Times_New_Roman.ttf", "Times_New_Roman_Bold.ttf",
                                                 "Times_New_Roman_Italic.ttf", "Times_New_Roman_Bold_Italic.ttf")),
    ("/usr/share/fonts/truetype/liberation", ("LiberationSerif-Regular.ttf", "LiberationSerif-Bold.ttf",
                                              "LiberationSerif-Italic.ttf", "LiberationSerif-BoldItalic.ttf")),
    ("/usr/share/fonts/truetype/liberation2", ("LiberationSerif-Regular.ttf", "LiberationSerif-Bold.ttf",
                                               "LiberationSerif-Italic.ttf", "LiberationSerif-BoldItalic.ttf")),
    ("/usr/share/fonts/truetype/dejavu", ("DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf",
                                          "DejaVuSerif-Italic.ttf", "DejaVuSerif-BoldItalic.ttf")),
    ("/usr/share/fonts/truetype/noto", ("NotoSerif-Regular.ttf", "NotoSerif-Bold.ttf",
                                        "NotoSerif-Italic.ttf", "NotoSerif-BoldItalic.ttf")),
]


def page_layouts(pages_html: list[str]) -> list[list[dict]]:
    return [parse_layout(h or "") for h in pages_html]


# Word


def _add_runs(para, text: str, marks: list | None, bold: bool = False, size: float | None = None, mono: bool = False) -> None:
    from docx.shared import Pt

    fmts = [""] * len(text)
    for start, end, kind in marks or []:
        for k in range(max(0, start), min(end, len(text))):
            fmts[k] = kind
    start = 0
    for i in range(1, len(text) + 1):
        if i < len(text) and fmts[i] == fmts[start]:
            continue
        kinds = set(fmts[start].split("+")) if fmts[start] else set()
        run = para.add_run(text[start:i])
        run.bold = bold or "b" in kinds or None
        run.italic = "i" in kinds or None
        run.underline = "u" in kinds or None
        if "sup" in kinds:
            run.font.superscript = True
        if "sub" in kinds:
            run.font.subscript = True
        if size:
            run.font.size = Pt(size)
        if mono:
            run.font.name = "Courier New"
        start = i


def _align(para, align: str | None) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    value = {"center": WD_ALIGN_PARAGRAPH.CENTER, "right": WD_ALIGN_PARAGRAPH.RIGHT,
             "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}.get(align or "")
    if value is not None:
        para.alignment = value


def _grid(rows: list[list[dict]]) -> tuple[list[tuple[int, int, int, int, dict]], int, int]:
    """Đặt ô HTML vào lưới (thuật toán bảng HTML): [(hàng, cột, số hàng, số cột, ô)], số hàng, số cột."""
    taken, placed = set(), []
    n_rows = len(rows)
    for r, row in enumerate(rows):
        c = 0
        for cell in row:
            while (r, c) in taken:
                c += 1
            rs = max(1, min(cell.get("rowspan", 1), n_rows - r))
            cs = max(1, cell.get("colspan", 1))
            # Ô gộp đè lên ô đã đặt (HTML sai): thu nhỏ lại
            while cs > 1 and any((r + dr, c + cs - 1) in taken for dr in range(rs)):
                cs -= 1
            while rs > 1 and any((r + rs - 1, c + dc) in taken for dc in range(cs)):
                rs -= 1
            for dr in range(rs):
                for dc in range(cs):
                    taken.add((r + dr, c + dc))
            placed.append((r, c, rs, cs, cell))
            c += cs
    n_cols = max((c + cs for _, c, _, cs, _ in placed), default=0)
    return placed, n_rows, n_cols


def _column_widths(table, placed: list, n_cols: int) -> None:
    """Chia bề rộng trang cho các cột theo độ dài chữ (Word mặc định chia đều, cột tên bị xuống dòng)."""
    from docx.shared import Mm

    need = [3.0] * n_cols
    for _, c, _, cs, cell in placed:
        longest = max((len(w) for w in cell["text"].split()), default=0)
        want = min(max(len(cell["text"]) * 0.6, longest, 3), 40) / cs
        for dc in range(cs):
            need[c + dc] = max(need[c + dc], want)
    total = 210 - MARGINS_MM["left"] - MARGINS_MM["right"]
    widths = [Mm(total * n / sum(need)) for n in need]
    for row in table.rows:
        for i, cell in enumerate(row.cells[:n_cols]):
            cell.width = widths[i]
    for i, col in enumerate(table.columns):
        col.width = widths[i]


def _docx_table(doc, block: dict) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    placed, n_rows, n_cols = _grid(block["rows"])
    if not n_cols:
        return
    if block.get("caption"):
        cap = doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _add_runs(cap, block["caption"], block.get("caption_marks"), bold=True)
    table = doc.add_table(rows=n_rows, cols=n_cols)
    table.style = "Table Grid"
    _column_widths(table, placed, n_cols)
    for r, c, rs, cs, cell in placed:
        target = table.cell(r, c)
        if rs > 1 or cs > 1:
            target = target.merge(table.cell(r + rs - 1, c + cs - 1))
        para = target.paragraphs[0]
        para.paragraph_format.space_after = Pt(0)
        _add_runs(para, cell["text"], cell.get("marks"), bold=cell.get("th", False))
        _align(para, cell.get("align") or ("center" if cell.get("th") else None))
    # Các hàng tiêu đề ở đầu bảng (toàn ô th): lặp lại khi bảng sang trang
    from docx.oxml import OxmlElement

    for r, row in enumerate(table.rows):
        starts = [c for rr, _, _, _, c in placed if rr == r]
        if not starts or not all(c.get("th") for c in starts):
            break
        row._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
    doc.add_paragraph().paragraph_format.space_after = Pt(0)


def to_docx(pages: list[list[dict]], title: str = "") -> bytes:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt

    doc = Document()
    doc.core_properties.title = title
    section = doc.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    section.top_margin, section.bottom_margin = Mm(MARGINS_MM["top"]), Mm(MARGINS_MM["bottom"])
    section.left_margin, section.right_margin = Mm(MARGINS_MM["left"]), Mm(MARGINS_MM["right"])
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(FONT_SIZE)
    rfonts = normal.element.get_or_add_rPr().get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(attr), "Times New Roman")
    normal.paragraph_format.space_after = Pt(6)

    for n, blocks in enumerate(pages):
        if n:
            doc.add_page_break()
        for b in blocks:
            if b["type"] == "table":
                _docx_table(doc, b)
            elif b["type"] == "list":
                for it in b["items"]:
                    level = min(max(it["level"], 0), 2)
                    style = ("List Number" if b["ordered"] else "List Bullet") + (f" {level + 1}" if level else "")
                    _add_runs(doc.add_paragraph(style=style), it["text"], it.get("marks"))
            else:
                para = doc.add_paragraph()
                _align(para, b.get("align"))
                heading = b["tag"] in ("h1", "h2", "h3", "h4", "h5", "h6")
                marks = b.get("marks") or [None] * len(b["lines"])
                for i, (line, m) in enumerate(zip(b["lines"], marks)):
                    if i:
                        para.add_run().add_break()
                    _add_runs(para, line, m, bold=heading, size=HEADING_SIZES.get(b["tag"]), mono=b["tag"] == "pre")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# PDF


def find_fonts(configured: tuple = ()) -> list[str] | None:
    """4 file phông (thường, đậm, nghiêng, đậm nghiêng); None nếu không tìm thấy bộ nào."""
    if configured:
        files = list(configured) + [configured[0]] * (4 - len(configured))
        if all(os.path.isfile(f) for f in files[:4]):
            return files[:4]
        log.warning("PDF_FONT_FILES not found; looking for system fonts")
    for folder, names in _FONT_SETS:
        files = [os.path.join(folder, n) for n in names]
        if all(os.path.isfile(f) for f in files):
            return files
    return None


# MuPDF đặt <caption> dưới bảng: chuyển thành đoạn căn giữa phía trên bảng
_CAPTION = re.compile(r"(<table[^>]*>)<caption>(.*?)</caption>", re.DOTALL)

_PDF_CSS = """
body { font-family: %(family)s; font-size: %(size)spt; line-height: 1.35; }
p, pre, ul, ol { margin: 0 0 6pt 0; }
h1 { font-size: 16pt; margin: 6pt 0; } h2 { font-size: 14pt; margin: 6pt 0; }
h3, h4, h5, h6 { font-size: %(size)spt; margin: 6pt 0; }
pre { font-family: monospace; font-size: 11pt; }
table { border-collapse: collapse; margin: 4pt 0 8pt 0; }
th, td { border: 1px solid black; padding: 2pt 4pt; vertical-align: top; }
th { font-weight: bold; text-align: center; }
p.caption { font-weight: bold; text-align: center; margin: 6pt 0 3pt 0; }
"""


_CMAP_SWAP = {b"<00a0>": b"<0020>", b"<00ad>": b"<002d>"}


def _fix_cmap_line(line: bytes) -> bytes:
    """Dòng bfchar "<glyph> <00a0>" hoặc bfrange một glyph "<g> <g> <00a0>": đổi sang dấu cách / "-"."""
    tokens = line.split()
    if len(tokens) not in (2, 3) or tokens[-1].lower() not in _CMAP_SWAP:
        return line
    if len(tokens) == 3 and tokens[0].lower() != tokens[1].lower():
        return line
    return b" ".join(tokens[:-1] + [_CMAP_SWAP[tokens[-1].lower()]])


def _fix_to_unicode(doc) -> None:
    """Times New Roman dùng chung glyph cho dấu cách / NBSP và "-" / gạch nối mềm; MuPDF ghi bảng ToUnicode theo
    ký tự sau, nên chép hoặc tìm chữ trong PDF ra NBSP và gạch mềm. Sửa lại thành dấu cách và "-"."""
    for xref in range(1, doc.xref_length()):
        try:
            kind, ref = doc.xref_get_key(xref, "ToUnicode")
            if kind != "xref":
                continue
            target = int(ref.split()[0])
            data = doc.xref_stream(target)
            fixed = b"\n".join(_fix_cmap_line(line) for line in data.split(b"\n"))
            if fixed != data:
                doc.update_stream(target, fixed)
        except Exception:
            continue


def to_pdf(pages: list[list[dict]], title: str = "", fonts: list[str] | None = None) -> bytes:
    import pymupdf

    # Không có phông: dùng Charis SIL có sẵn trong MuPDF (đủ dấu tiếng Việt; "serif" chung thì mất dấu)
    archive, faces, family = None, "", "'Charis SIL'"
    if fonts:
        archive = pymupdf.Archive()
        for folder in {str(Path(f).parent) for f in fonts}:
            archive.add(folder)
        styles = [("normal", "normal"), ("bold", "normal"), ("normal", "italic"), ("bold", "italic")]
        faces = "".join(
            f"@font-face {{ font-family: DocSerif; src: url({Path(f).name}); font-weight: {w}; font-style: {s}; }}\n"
            for f, (w, s) in zip(fonts, styles)
        )
        family = "DocSerif"
    else:
        log.warning("no Times New Roman / Liberation / DejaVu / Noto Serif TTF found for PDF export; using built-in Charis SIL")
    css = faces + _PDF_CSS % {"family": family, "size": FONT_SIZE}
    mm = 72 / 25.4
    box = pymupdf.paper_rect("a4")
    where = box + (MARGINS_MM["left"] * mm, MARGINS_MM["top"] * mm, -MARGINS_MM["right"] * mm, -MARGINS_MM["bottom"] * mm)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    for blocks in pages or [[]]:
        # Mỗi trang gốc bắt đầu trang PDF mới; HTML dựng lại từ bố cục nên chỉ có thẻ an toàn
        page_html = _CAPTION.sub(r'<p class="caption">\2</p>\1', blocks_html(blocks)) or "<p></p>"
        story = pymupdf.Story(page_html, user_css=css, archive=archive)
        more = 1
        while more:
            device = writer.begin_page(box)
            more, _ = story.place(where)
            story.draw(device)
            writer.end_page()
    writer.close()
    doc = pymupdf.open("pdf", buf.getvalue())
    if title:
        doc.set_metadata({**(doc.metadata or {}), "title": title})
    if fonts:
        try:
            # Chỉ nhúng các ký tự dùng tới (phông TTF đầy đủ vài MB); phông có sẵn của MuPDF nhỏ, cắt thì hỏng chữ
            doc.subset_fonts()
        except Exception:
            log.warning("could not subset PDF fonts; file is larger")
    _fix_to_unicode(doc)
    return doc.tobytes(garbage=3, deflate=True)


def export(pages_html: list[str], fmt: str, title: str = "", fonts: tuple = ()) -> bytes:
    pages = page_layouts(pages_html)
    if fmt == "docx":
        return to_docx(pages, title)
    if fmt == "pdf":
        return to_pdf(pages, title, find_fonts(fonts))
    raise ValueError(f"unknown format {fmt}")
