"""Bố cục trang từ HTML của Chandra: đoạn văn, danh sách, bảng (giữ colspan / rowspan), định dạng trong dòng.

Text để sửa chính tả lấy từ bố cục theo quy tắc cố định:
- mỗi khối cách nhau một dòng trống;
- đoạn văn: mỗi dòng (<br>) một dòng; danh sách: mỗi mục một dòng;
- bảng: mỗi hàng một dòng, các ô cách nhau " | ".
Nhờ vậy sau khi sửa chính tả (giữ nguyên số dòng) dựng lại được HTML với đúng cấu trúc ban đầu.

Định dạng trong dòng (đậm, nghiêng, gạch chân, chỉ số trên / dưới) lưu riêng ở "marks": danh sách
[đầu, cuối, kiểu] theo vị trí ký tự của dòng, kiểu là "b", "i", "u", "sup", "sub" nối bằng "+" (vd "b+i").
Căn lề (giữa / phải / đều hai bên) lưu ở "align" của khối hoặc ô bảng.
"""
import difflib
import html
import logging
import re
import unicodedata
from html.parser import HTMLParser

log = logging.getLogger("ocr.layout")

_SKIP_LABELS = {"figure", "image"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_TEXT_BLOCKS = _HEADINGS | {"p", "div", "pre", "blockquote", "section", "hr"}
_SPACE_CHARS = " \t\xa0\r"
_SPACES = re.compile(r"[ \t\xa0\r]+")
SEP = " | "

# Thẻ định dạng trong dòng -> kiểu
_INLINE = {"b": "b", "strong": "b", "i": "i", "em": "i", "u": "u", "ins": "u", "sup": "sup", "sub": "sub"}
_ORDER = ("b", "i", "u", "sup", "sub")
_ALIGNS = {"center": "center", "right": "right", "justify": "justify"}
_TEXT_ALIGN = re.compile(r"text-align\s*:\s*([a-z]+)", re.IGNORECASE)


def _clean(text: str) -> str:
    return unicodedata.normalize("NFC", _SPACES.sub(" ", text).strip())


def _span(attrs: dict, name: str) -> int:
    try:
        return max(1, min(int(attrs.get(name) or 1), 100))
    except ValueError:
        return 1


def _align(attrs: dict) -> str | None:
    """Căn lề từ thuộc tính align hoặc style="text-align: ..."; căn trái (mặc định) trả None."""
    value = (attrs.get("align") or "").lower()
    found = _TEXT_ALIGN.search(attrs.get("style") or "")
    if found:
        value = found.group(1).lower()
    return _ALIGNS.get(value)


def fmt_of(kinds) -> str:
    """Tập kiểu -> chuỗi kiểu theo thứ tự cố định ("b+i")."""
    return "+".join(k for k in _ORDER if k in kinds)


# Dòng có định dạng: chuỗi + mảng kiểu từng ký tự


def drop_kind(marks: list, kind: str) -> list:
    """Bỏ một kiểu khỏi marks (vd đậm trong tiêu đề: tiêu đề vốn đã đậm)."""
    out = []
    for start, end, k in marks or []:
        rest = "+".join(x for x in k.split("+") if x != kind)
        if rest:
            out.append([start, end, rest])
    return out


def _runs(fmts: list[str]) -> list[list]:
    """Mảng kiểu từng ký tự -> marks [[đầu, cuối, kiểu]] (bỏ đoạn không định dạng)."""
    marks, start = [], 0
    for i in range(1, len(fmts) + 1):
        if i == len(fmts) or fmts[i] != fmts[start]:
            if fmts[start]:
                marks.append([start, i, fmts[start]])
            start = i
    return marks


def _char_fmts(text: str, marks: list | None) -> list[str]:
    fmts = [""] * len(text)
    for start, end, kind in marks or []:
        for k in range(max(0, start), min(end, len(text))):
            fmts[k] = kind
    return fmts


def remap_marks(old: str, new: str, marks: list | None) -> list:
    """Chuyển định dạng của dòng cũ sang dòng mới (đã sửa chính tả / đổi mã) theo căn chỉnh ký tự."""
    if not marks:
        return []
    if old == new:
        return marks
    src = _char_fmts(old, marks)
    out = [""] * len(new)
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if op == "equal":
            out[j1:j2] = src[i1:i2]
        elif op == "replace":
            for k in range(j1, j2):
                out[k] = src[min(i1 + (k - j1) * (i2 - i1) // (j2 - j1), i2 - 1)]
        elif op == "insert":
            # Chữ chèn thêm thuộc về từ đứng trước (nếu có), không thì từ đứng sau
            if i1 > 0 and not old[i1 - 1].isspace():
                kind = src[i1 - 1]
            elif i1 < len(old):
                kind = src[i1]
            else:
                kind = ""
            out[j1:j2] = [kind] * (j2 - j1)
    return _runs(out)


def segments_to_lines(segs: list[tuple[str, str]], split: bool = True) -> list[tuple[str, list]]:
    """Các đoạn (chữ, kiểu) -> các dòng (chữ đã gọn khoảng trắng, marks); split=False gộp thành một dòng."""
    lines: list[tuple[list[str], list[str]]] = [([], [])]
    for text, kind in segs:
        for ch in text:
            if ch == "\n":
                if split:
                    lines.append(([], []))
                    continue
                ch = " "
            lines[-1][0].append(ch)
            lines[-1][1].append(kind)
    out = []
    for chars, kinds in lines:
        text, fmts = [], []
        for ch, kind in zip(chars, kinds):
            if ch in _SPACE_CHARS:
                if not text or text[-1] == " ":
                    continue
                ch = " "
            text.append(ch)
            fmts.append(kind)
        while text and text[-1] == " ":
            text.pop()
            fmts.pop()
        s = "".join(text)
        marks = _runs(fmts)
        norm = unicodedata.normalize("NFC", s)
        if norm != s:
            marks = remap_marks(s, norm, marks)
        if norm:
            out.append((norm, marks))
    return out


def map_text(text: str, marks: list | None, fn) -> tuple[str, list]:
    """Áp fn (vd đổi phông TCVN3 / VNI) cho từng đoạn cùng kiểu, giữ định dạng."""
    if not marks:
        return fn(text), []
    fmts = _char_fmts(text, marks)
    pieces, start = [], 0
    for i in range(1, len(text) + 1):
        if i == len(text) or fmts[i] != fmts[start]:
            pieces.append((fn(text[start:i]), fmts[start]))
            start = i
    new = "".join(p for p, _ in pieces)
    return new, _runs([k for p, kind in pieces for k in [kind] * len(p)])


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self.stack: list[str] = []
        self.aligns: list[str | None] = []   # căn lề của từng thẻ trong stack
        self.skip_at = 0          # độ sâu stack khi vào khối Figure / Image (0 = không bỏ)
        self.buf: list[tuple[str, str]] = []   # chữ đang gom ngoài bảng: (chữ, kiểu)
        self.buf_align: str | None = None
        self.tag = "p"            # thẻ khối của buf
        self.lists: list[dict] = []
        self.table: dict | None = None
        self.nested = 0           # bảng lồng trong bảng: lấy chữ, bỏ cấu trúc
        self.cell: dict | None = None

    def _kind(self) -> str:
        return fmt_of({_INLINE[t] for t in self.stack if t in _INLINE})

    def _current_align(self) -> str | None:
        return next((a for a in reversed(self.aligns) if a), None)

    # Gom chữ

    def _flush(self) -> None:
        lines = segments_to_lines(self.buf)
        align, self.buf, self.buf_align = self.buf_align, [], None
        if not lines:
            return
        if self.lists:
            level = len(self.lists) - 1
            for text, marks in lines:
                item = {"text": text, "level": level}
                if marks:
                    item["marks"] = marks
                self.lists[0]["items"].append(item)
            return
        if self.tag in _HEADINGS:
            lines = [(t, drop_kind(m, "b")) for t, m in lines]
        block = {"type": "text", "tag": self.tag, "lines": [t for t, _ in lines]}
        if any(m for _, m in lines):
            block["marks"] = [m for _, m in lines]
        if align:
            block["align"] = align
        self.blocks.append(block)

    def handle_data(self, data: str) -> None:
        if self.skip_at:
            return
        kind = self._kind()
        if self.table is not None:
            if self.cell is not None:
                if not self.cell.get("align") and data.strip():
                    self.cell["align"] = self._current_align()
                self.cell["segs"].append((data, kind))
            elif self.table.get("in_caption"):
                self.table["caption_segs"].append((data, kind))
            return
        if not self.buf and data.strip():
            self.buf_align = self._current_align()
        self.buf.append((data, kind))

    # Thẻ

    def handle_starttag(self, tag: str, attrs_list) -> None:
        attrs = dict(attrs_list)
        if tag in ("br", "img", "input", "hr", "meta"):
            self._void(tag)
            return
        self.stack.append(tag)
        self.aligns.append(_align(attrs))
        if self.skip_at:
            return
        if tag in ("script", "style", "head", "title") or (
                tag == "div" and (attrs.get("data-label") or "").lower() in _SKIP_LABELS):
            self._flush()
            self.skip_at = len(self.stack)
            return
        if self.table is not None:
            self._table_start(tag, attrs)
            return
        if tag == "table":
            self._flush()
            self.table = {"type": "table", "caption_segs": [], "rows": []}
        elif tag in ("ul", "ol"):
            self._flush()
            self.lists.append({"type": "list", "ordered": tag == "ol", "items": []})
        elif tag == "li" or tag in _TEXT_BLOCKS:
            self._flush()
            if tag in _HEADINGS or tag == "pre":
                self.tag = tag
            elif tag != "div":
                self.tag = "p"

    def _void(self, tag: str) -> None:
        if self.skip_at or tag != "br":
            return
        if self.cell is not None:
            self.cell["segs"].append((" ", ""))
        elif self.table is None:
            self.buf.append(("\n", ""))

    def _table_start(self, tag: str, attrs: dict) -> None:
        t = self.table
        if tag == "table":
            self.nested += 1
        elif self.nested:
            return
        elif tag == "caption":
            t["in_caption"] = True
        elif tag == "tr":
            t["rows"].append([])
        elif tag in ("td", "th"):
            if not t["rows"]:
                t["rows"].append([])
            self.cell = {"segs": [], "th": tag == "th", "colspan": _span(attrs, "colspan"), "rowspan": _span(attrs, "rowspan")}
            t["rows"][-1].append(self.cell)

    def handle_endtag(self, tag: str) -> None:
        if tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            self._end(top)
            self.aligns.pop()
            if top == tag:
                break

    def _end(self, tag: str) -> None:
        if self.skip_at:
            if len(self.stack) < self.skip_at:
                self.skip_at = 0
            return
        if self.table is not None:
            if tag == "table" and self.nested:
                self.nested -= 1
            elif tag == "table":
                self._table_end()
            elif tag in ("td", "th") and not self.nested:
                self.cell = None
            elif tag == "caption":
                self.table["in_caption"] = False
            return
        if tag in ("ul", "ol"):
            self._flush()
            done = self.lists.pop() if self.lists else None
            if done and not self.lists and done["items"]:
                self.blocks.append(done)
        elif tag == "li" or tag in _TEXT_BLOCKS:
            self._flush()
            self.tag = "p"

    def _table_end(self) -> None:
        t = self.table
        self.table, self.cell = None, None
        rows = []
        for r in t["rows"]:
            if not r:
                continue
            row = []
            for c in r:
                lines = segments_to_lines(c.pop("segs"), split=False)
                text, marks = lines[0] if lines else ("", [])
                cell = {"text": text, "th": c["th"], "colspan": c["colspan"], "rowspan": c["rowspan"]}
                if marks:
                    cell["marks"] = marks
                if c.get("align"):
                    cell["align"] = c["align"]
                row.append(cell)
            rows.append(row)
        if rows:
            caption = segments_to_lines(t["caption_segs"], split=False)
            block = {"type": "table", "caption": caption[0][0] if caption else "", "rows": rows}
            if caption and caption[0][1]:
                block["caption_marks"] = caption[0][1]
            self.blocks.append(block)

    def close(self) -> None:
        super().close()
        while self.stack:
            self.handle_endtag(self.stack[-1])
        if self.table is not None:
            self._table_end()
        self._flush()
        while self.lists:
            done = self.lists.pop()
            if not self.lists and done["items"]:
                self.blocks.append(done)


def parse_layout(page_html: str) -> list[dict]:
    """HTML của Chandra -> danh sách khối (dict, lưu được dạng JSON)."""
    parser = _Parser()
    parser.feed(page_html or "")
    parser.close()
    return parser.blocks


def _block_lines(block: dict) -> list[str]:
    if block["type"] == "text":
        return list(block["lines"])
    if block["type"] == "list":
        return [it["text"] for it in block["items"]]
    lines = [block["caption"]] if block["caption"] else []
    return lines + [SEP.join(c["text"] for c in row) for row in block["rows"]]


def layout_text(blocks: list[dict]) -> str:
    """Text của trang (để sửa chính tả, tìm kiếm)."""
    return "\n\n".join("\n".join(_block_lines(b)) for b in blocks)


def map_layout(blocks: list[dict], fn) -> list[dict]:
    """Áp fn cho mọi chữ trong bố cục, giữ định dạng."""
    out = []
    for b in blocks:
        if b["type"] == "text":
            marks = b.get("marks") or [[] for _ in b["lines"]]
            done = [map_text(x, m, fn) for x, m in zip(b["lines"], marks)]
            nb = {**b, "lines": [t for t, _ in done]}
            if "marks" in b:
                nb["marks"] = [m for _, m in done]
            out.append(nb)
        elif b["type"] == "list":
            out.append({**b, "items": [_with_text(it, *map_text(it["text"], it.get("marks"), fn)) for it in b["items"]]})
        else:
            caption, cmarks = map_text(b["caption"], b.get("caption_marks"), fn)
            nb = {**b, "caption": caption, "rows": [[_with_text(c, *map_text(c["text"], c.get("marks"), fn)) for c in r] for r in b["rows"]]}
            if cmarks:
                nb["caption_marks"] = cmarks
            out.append(nb)
    return out


def _with_text(item: dict, text: str, marks: list) -> dict:
    out = {k: v for k, v in item.items() if k != "marks"}
    out["text"] = text
    if marks:
        out["marks"] = marks
    return out


# Dựng lại HTML


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _retext(item: dict, text: str) -> dict:
    """Thay chữ của mục / ô, chuyển định dạng sang chữ mới."""
    return _with_text(item, text, remap_marks(item["text"], text, item.get("marks")))


def _apply(blocks: list[dict], text: str) -> list[dict]:
    """Đặt chữ đã sửa chính tả vào bố cục; số dòng lệch thì giữ chữ gốc."""
    lines = text.split("\n")
    if len(lines) != len(layout_text(blocks).split("\n")):
        log.warning("corrected text does not match the page layout; table HTML keeps the original text")
        return blocks
    out, pos = [], 0
    for block in blocks:
        n = len(_block_lines(block))
        got, pos = lines[pos : pos + n], pos + n + 1
        if block["type"] == "text":
            nb = {**block, "lines": got}
            if block.get("marks"):
                nb["marks"] = [remap_marks(o, t, m) for o, t, m in zip(block["lines"], got, block["marks"])]
            out.append(nb)
        elif block["type"] == "list":
            out.append({**block, "items": [_retext(it, t) for it, t in zip(block["items"], got)]})
        else:
            nb = dict(block)
            if block["caption"]:
                caption = got.pop(0)
                nb["caption"] = caption
                if block.get("caption_marks"):
                    nb["caption_marks"] = remap_marks(block["caption"], caption, block["caption_marks"])
            rows = []
            for row, line in zip(block["rows"], got):
                cells = [c.strip() for c in line.split("|")]
                if len(cells) != len(row):
                    cells = [c["text"] for c in row]
                rows.append([_retext(c, t) for c, t in zip(row, cells)])
            nb["rows"] = rows
            out.append(nb)
    return out


def rich_html(text: str, marks: list | None) -> str:
    """Chữ đã escape kèm thẻ định dạng (<b>, <i>, <u>, <sup>, <sub>)."""
    if not marks:
        return _esc(text)
    parts, pos = [], 0
    for start, end, kind in sorted(marks):
        start, end = max(start, pos), min(end, len(text))
        if start >= end:
            continue
        parts.append(_esc(text[pos:start]))
        tags = [k for k in kind.split("+") if k in _ORDER]
        parts.append("".join(f"<{t}>" for t in tags) + _esc(text[start:end]) + "".join(f"</{t}>" for t in reversed(tags)))
        pos = end
    parts.append(_esc(text[pos:]))
    return "".join(parts)


def _style(align: str | None) -> str:
    return f' style="text-align:{align}"' if align in _ALIGNS else ""


def _list_html(block: dict) -> str:
    tag = "ol" if block["ordered"] else "ul"
    parts, depth = [], -1
    for it in block["items"]:
        level = max(0, it["level"])
        if level > depth:
            # Danh sách con nằm trong mục đang mở
            parts.append(f"<{tag}>" * (level - depth))
        else:
            parts.append("</li>")
            while depth > level:
                parts.append(f"</{tag}></li>")
                depth -= 1
        parts.append(f"<li>{rich_html(it['text'], it.get('marks'))}")
        depth = level
    parts.append("</li>")
    while depth > 0:
        parts.append(f"</{tag}></li>")
        depth -= 1
    parts.append(f"</{tag}>")
    return "".join(parts)


def _table_html(block: dict) -> str:
    parts = ['<table border="1">']
    if block["caption"]:
        parts.append(f"<caption>{rich_html(block['caption'], block.get('caption_marks'))}</caption>")
    for row in block["rows"]:
        cells = []
        for c in row:
            tag = "th" if c["th"] else "td"
            span = (f' colspan="{c["colspan"]}"' if c["colspan"] > 1 else "") + \
                   (f' rowspan="{c["rowspan"]}"' if c["rowspan"] > 1 else "")
            cells.append(f"<{tag}{span}{_style(c.get('align'))}>{rich_html(c['text'], c.get('marks'))}</{tag}>")
        parts.append("<tr>" + "".join(cells) + "</tr>")
    parts.append("</table>")
    return "".join(parts)


def blocks_html(blocks: list[dict]) -> str:
    """HTML sạch từ bố cục (chỉ thẻ do backend tạo, chữ đã escape)."""
    out = []
    for b in blocks:
        if b["type"] == "table":
            out.append(_table_html(b))
        elif b["type"] == "list":
            out.append(_list_html(b))
        else:
            tag = b["tag"]
            marks = b.get("marks") or [None] * len(b["lines"])
            out.append(f"<{tag}{_style(b.get('align'))}>" + "<br>".join(rich_html(x, m) for x, m in zip(b["lines"], marks)) + f"</{tag}>")
    return "\n".join(out)


def layout_html(blocks: list[dict], text: str | None = None) -> str:
    """HTML sạch của trang; text = chữ đã sửa chính tả (đặt lại vào bố cục, giữ định dạng)."""
    if text is not None:
        blocks = _apply(blocks, text)
    return blocks_html(blocks)


def text_html(text: str) -> str:
    """HTML cho trang không có bố cục (Word, txt, lớp chữ PDF): mỗi đoạn một <p>."""
    paragraphs = [p for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    return "\n".join("<p>" + "<br>".join(_esc(x.strip()) for x in p.split("\n") if x.strip()) + "</p>" for p in paragraphs)
