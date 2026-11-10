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


class ChandraClient:
    def __init__(self, http: httpx.AsyncClient, url: str, model: str, api_key: str, max_tokens: int, timeout: float, concurrency: int):
        self._http = http
        self._url = url
        self._model = model
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._sem = asyncio.Semaphore(concurrency)

    async def ocr(self, jpeg_b64: str) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{jpeg_b64}"}},
                        {"type": "text", "text": "<image>"},
                    ],
                }
            ],
            "temperature": 0,
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
