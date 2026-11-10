import base64
import io
import threading

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = 200_000_000


class UnsupportedFile(Exception):
    pass


class TooManyPages(Exception):
    pass


class PageSource:
    """Render trang khi cần để đỡ tốn RAM."""

    def __init__(self, data: bytes, dpi: int, max_pages: int):
        self._lock = threading.Lock()
        self._dpi = dpi
        self._pdf = None
        self._img = None
        if data[:5] == b"%PDF-":
            import fitz

            try:
                self._pdf = fitz.open(stream=data, filetype="pdf")
            except Exception as exc:
                raise UnsupportedFile("invalid PDF") from exc
            if self._pdf.needs_pass:
                raise UnsupportedFile("encrypted PDF is not supported")
            self.count = self._pdf.page_count
        else:
            try:
                self._img = Image.open(io.BytesIO(data))
                self._img.load()
            except Exception as exc:
                raise UnsupportedFile("unsupported file type; send a PDF or an image") from exc
            self.count = getattr(self._img, "n_frames", 1)
        if self.count == 0:
            raise UnsupportedFile("document has no pages")
        if self.count > max_pages:
            raise TooManyPages(f"document has {self.count} pages; limit is {max_pages}")

    def render(self, index: int) -> Image.Image:
        with self._lock:
            if self._pdf is not None:
                import fitz

                pix = self._pdf[index].get_pixmap(dpi=self._dpi, colorspace=fitz.csRGB, alpha=False)
                return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            self._img.seek(index)
            return self._img.convert("RGB")

    @property
    def is_pdf(self) -> bool:
        return self._pdf is not None

    def embedded_text(self, index: int) -> str:
        if self._pdf is None:
            return ""
        with self._lock:
            return self._pdf[index].get_text().strip()

    def close(self) -> None:
        if self._pdf is not None:
            self._pdf.close()


def _estimate_skew(img: Image.Image, limit: float = 5.0, step: float = 0.25) -> float:
    small = img.convert("L")
    small.thumbnail((700, 700))
    arr = np.asarray(small)
    ink = (arr < max(60, min(200, int(arr.mean()) - 25))).astype(np.uint8) * 255
    mask = Image.fromarray(ink)
    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-limit, limit + step, step):
        rotated = np.asarray(mask.rotate(float(angle), resample=Image.NEAREST, fillcolor=0))
        score = float(np.var(rotated.sum(axis=1, dtype=np.int64)))
        if score > best_score:
            best_angle, best_score = float(angle), score
    return best_angle


def deskew(img: Image.Image) -> Image.Image:
    angle = _estimate_skew(img)
    if abs(angle) < 0.5:
        return img
    return img.rotate(angle, resample=Image.BICUBIC, fillcolor=(255, 255, 255))


def prepare_image(img: Image.Image, max_long_side: int, do_deskew: bool) -> Image.Image:
    if do_deskew:
        img = deskew(img)
    w, h = img.size
    if max(w, h) > max_long_side:
        scale = max_long_side / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    return img


def jpeg_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def to_jpeg_b64(img: Image.Image, max_long_side: int, do_deskew: bool) -> str:
    return base64.b64encode(jpeg_bytes(prepare_image(img, max_long_side, do_deskew))).decode("ascii")
