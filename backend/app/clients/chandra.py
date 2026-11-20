import asyncio
import html
import json
import logging
import re
import unicodedata

import httpx

from .http import post_with_retry

log = logging.getLogger("ocr.chandra")


class ChandraError(Exception):
    pass


# Prompt chuẩn của Chandra (datalab-to/chandra, chandra/prompts.py - OCR_LAYOUT_PROMPT).
# Chỉ gửi "<image>" thì model có lúc trả JSON (<analyze>[{"titles": ...}]) hoặc Markdown thay vì HTML.
_ALLOWED_TAGS = [
    "math", "br", "i", "b", "u", "del", "sup", "sub", "table", "tr", "td", "p", "th", "div",
    "pre", "h1", "h2", "h3", "h4", "h5", "ul", "ol", "li", "input", "a", "span", "img", "hr",
    "tbody", "small", "caption", "strong", "thead", "big", "code", "chem",
]
_ALLOWED_ATTRIBUTES = [
    "class", "colspan", "rowspan", "display", "checked", "type", "border", "value", "style",
    "href", "alt", "align", "data-bbox", "data-label",
]
OCR_LAYOUT_PROMPT = f"""
OCR this image to HTML, arranged as layout blocks.  Each layout block should be a div with the data-bbox attribute representing the bounding box of the block in x0 y0 x1 y1 format.  Bboxes are normalized 0-1000. The data-label attribute is the label for the block.

Use the following labels:
- Caption
- Footnote
- Equation-Block
- List-Group
- Page-Header
- Page-Footer
- Image
- Section-Header
- Table
- Text
- Complex-Block
- Code-Block
- Form
- Table-Of-Contents
- Figure
- Chemical-Block
- Diagram
- Bibliography
- Blank-Page

Only use these tags {_ALLOWED_TAGS}, and these attributes {_ALLOWED_ATTRIBUTES}.

Guidelines:
* Inline math: Surround math with <math>...</math> tags. Math expressions should be rendered in KaTeX-compatible LaTeX. Use display for block math.
* Tables: Use colspan and rowspan attributes to match table structure.
* Formatting: Maintain consistent formatting with the image, including spacing, indentation, subscripts/superscripts, and special characters.
* Images: Include a description of any images in the alt attribute of an <img> tag. Do not fill out the src property. Describe in detail inside the div tag. Also convert charts to high fidelity data, and convert diagrams to mermaid.
* Forms: Mark checkboxes and radio buttons properly.
* Text: join lines together properly into paragraphs using <p>...</p> tags.  Use <br> tags for line breaks within paragraphs, but only when absolutely necessary to maintain meaning.
* Chemistry: Use <chem>...</chem> tags for chemical formulas with reactive SMILES.
* Lists: Preserve indents and proper list markers.
* Use the simplest possible HTML structure that accurately represents the content of the block.
* Make sure the text is accurate and easy for a human to read and interpret.  Reading order should be correct and natural.
""".strip()

# Lần gửi lại khi đầu ra không phải HTML: nhiệt độ nhỏ để model không lặp y hệt lần đầu
RETRY_TEMPERATURE = 0.2


class ChandraClient:
    def __init__(self, http: httpx.AsyncClient, url: str, model: str, api_key: str, max_tokens: int, timeout: float,
                 concurrency: int, html_retries: int = 2, log_output: bool = False):
        self._http = http
        self._url = url
        self._model = model
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._sem = asyncio.Semaphore(concurrency)
        self._html_retries = max(0, html_retries)
        self._log_output = log_output

    async def ocr(self, jpeg_b64: str, label: str = "") -> str:
        """OCR một trang; luôn trả HTML."""
        return (await self.ocr_raw(jpeg_b64, label))[0]

    async def ocr_raw(self, jpeg_b64: str, label: str = "") -> tuple[str, str]:
        """(HTML đã chuẩn hoá, đầu ra nguyên bản của Chandra ở lần dùng được)."""
        content = ""
        tag = f"[{label}] " if label else ""
        tries = self._html_retries + 1
        for attempt in range(tries):
            content = await self._ask(jpeg_b64, 0 if attempt == 0 else RETRY_TEMPERATURE)
            if self._log_output:
                log.info("%sChandra output, attempt %d/%d, %d chars:\n%s", tag, attempt + 1, tries, len(content), content)
            page, ok = normalize_html(content)
            if ok:
                if attempt:
                    log.info("%sChandra returned HTML on retry %d", tag, attempt)
                return page, content
            log.warning("%sChandra output is not HTML (starts with %r), attempt %d/%d",
                        tag, content.strip()[:20], attempt + 1, tries)
        log.warning("%sChandra never returned HTML; converting its output to HTML", tag)
        return to_html(content), content

    async def _ask(self, jpeg_b64: str, temperature: float) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{jpeg_b64}"}},
                        {"type": "text", "text": OCR_LAYOUT_PROMPT},
                    ],
                }
            ],
            "temperature": temperature,
            "max_tokens": self._max_tokens,
        }
        try:
            async with self._sem:
                resp = await post_with_retry(self._http, self._url, payload, self._api_key, self._timeout)
            choice = resp.json()["choices"][0]
        except Exception as exc:
            raise ChandraError(type(exc).__name__) from exc
        content = (choice["message"]["content"] or "").strip()
        if choice.get("finish_reason") == "length":
            cut = content.rfind("\n")
            if cut != -1:
                content = content[:cut]
            log.warning("Chandra output hit max_tokens; last line dropped")
        return content


# Kiểm tra / chuẩn hoá đầu ra HTML

_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", re.DOTALL)
_HTML_TAG = re.compile(r"<(?:div|p|table|tr|td|th|h[1-6]|ul|ol|li|span|br|pre|math|img|hr|b|i|u|strong|sup|sub|code|small|big)\b",
                       re.IGNORECASE)
_ANALYZE = re.compile(r"</?analyze>", re.IGNORECASE)


def normalize_html(content: str) -> tuple[str, bool]:
    """(HTML đã bỏ khung ```html```, có phải HTML không). Trang trống coi là HTML hợp lệ."""
    text = (content or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text:
        return "", True
    if text.startswith(("[", "{")) or _ANALYZE.match(text):
        return text, False
    return text, text.startswith("<") and bool(_HTML_TAG.search(text))


_MD_PREFIX = re.compile(r"^\s{0,3}(?:#{1,6}\s+|[-*+]\s+|>\s?)")


def to_html(content: str) -> str:
    """Đổi đầu ra không phải HTML (JSON, Markdown, chữ thường) sang HTML đơn giản: mỗi đoạn một <p>."""
    text = _ANALYZE.sub("", normalize_html(content)[0]).strip()
    text = _from_json_variant(text)
    paragraphs = []
    for block in re.split(r"\n\s*\n", text):
        lines = [_MD_PREFIX.sub("", ln).replace("**", "").strip() for ln in block.split("\n")]
        lines = [html.escape(ln, quote=False) for ln in lines if ln and not re.fullmatch(r"[|:\-\s]+", ln)]
        if lines:
            paragraphs.append("<p>" + "<br>".join(lines) + "</p>")
    return "\n".join(paragraphs)


_FIGURE = re.compile(r'<div[^>]*data-label="(?:Figure|Image)"[^>]*>.*?(?:</div>|$)', re.DOTALL | re.IGNORECASE)
_VN_DIACRITICS = re.compile(
    r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", re.IGNORECASE
)


def _from_json_variant(text: str) -> str:
    """Lấy text khi Chandra trả JSON thay vì HTML."""
    stripped = text.strip()
    if not stripped.startswith("[{"):
        return text
    parts: list[str] = []
    try:
        objs = json.loads(stripped.split("\n")[0])
        for obj in objs if isinstance(objs, list) else []:
            if not isinstance(obj, dict):
                continue
            for key in ("titles", "text", "table", "other"):
                val = obj.get(key)
                if isinstance(val, str):
                    parts += [p for p in re.findall(r"'([^']{2,200})'", val) if _VN_DIACRITICS.search(p)]
        for m in re.finditer(r'"text"\s*:\s*"([^"]{1,200})"', "\n".join(stripped.split("\n")[1:])):
            if not m.group(1).startswith("\\"):
                parts.append(m.group(1))
    except ValueError:
        return text
    return "\n".join(parts) if parts else text


def html_to_text(raw: str) -> str:
    t = _from_json_variant(raw)
    t = _FIGURE.sub(" ", t)
    t = re.sub(r"<img[^>]*/?>", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"<su[pb][^>]*>(.*?)</su[pb]>", r"\1", t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r"</t[dh]>\s*<t[dh][^>]*>", " | ", t, flags=re.IGNORECASE)
    t = re.sub(r"<br\s*/?>|</(?:p|div|tr|h[1-6]|li|ul|ol|table|caption)>", "\n", t, flags=re.IGNORECASE)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in t.split("\n")]
    t = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return unicodedata.normalize("NFC", t)
