"""Kiểm tra lớp text của PDF: có lỗi Unicode thì không dùng, chuyển sang OCR."""
import re
import unicodedata

from ..spell.checker import is_acronym, tokenize
from .textfile import TCVN3_MARKERS

_CID = re.compile(r"\(cid:\d+\)")
# Ký tự riêng của VNI, không có trong tiếng Việt Unicode
_VNI_MARKERS = set("øïûöñåäëüæîÑÖØÏÛÅÄËÜÆÎ")
# Dấu đứng riêng thành ký tự (dấu bị tách khỏi chữ)
_LOOSE_MARKS = set("´`ˆ˜¨¸˘ˇ˙˚˛˝ˀ̣̀́̃̉")
_VN_UNICODE = re.compile("[ĂăĐđĨĩŨũƠơƯưẠ-ỹ]")


def unicode_problem(text: str) -> str:
    """Lý do lớp text bị lỗi Unicode; rỗng nếu không thấy lỗi."""
    text = unicodedata.normalize("NFC", text)
    if "�" in text:
        return f"có {text.count(chr(0xFFFD))} ký tự thay thế �"
    if cid := _CID.findall(text):
        return f"có {len(cid)} mã glyph (cid:…)"
    if pua := [c for c in text if 0xE000 <= ord(c) <= 0xF8FF]:
        return f"có {len(pua)} ký tự vùng riêng (font không có bảng Unicode)"
    if ctrl := [c for c in text if unicodedata.category(c) == "Cc" and c not in "\n\r\t\f"]:
        return f"có {len(ctrl)} ký tự điều khiển"
    # Dòng đã có chữ Việt Unicode thì không xét dấu hiệu bảng mã cũ
    legacy = "\n".join(line for line in text.split("\n") if not _VN_UNICODE.search(line))
    # Hai bảng mã dùng chung vài ký tự: ghi tên bảng khớp nhiều hơn
    tcvn = sum(c in TCVN3_MARKERS for c in legacy)
    vni = sum(c in _VNI_MARKERS for c in legacy)
    if tcvn or vni:
        return f"có {vni} ký tự font VNI" if vni >= tcvn else f"có {tcvn} ký tự font TCVN3 (.Vn*)"
    if loose := [c for c in text if c in _LOOSE_MARKS]:
        return f"có {len(loose)} dấu bị tách rời khỏi chữ"
    return ""


def known_ratio(texts: list[str], lexicon) -> tuple[float, int]:
    """Tỉ lệ từ có trong từ điển (bỏ số và viết tắt) và số từ đã xét."""
    words = [t for text in texts for line in text.split("\n") for t in tokenize(line)
             if not t.low.isdigit() and not is_acronym(t.text)]
    if not words:
        return 1.0, 0
    return sum(t.low in lexicon for t in words) / len(words), len(words)


def file_problem(texts: list[str], lexicon, min_known: float, min_words: int = 50) -> str:
    """Lý do không dùng lớp text của cả file PDF; rỗng nếu dùng được.

    Một trang lỗi Unicode là bỏ cả file. Tỉ lệ từ điển tính trên cả file
    (trang tham khảo tiếng Anh không kéo cả file xuống).
    """
    for i, text in enumerate(texts, start=1):
        if text.strip() and (problem := unicode_problem(text)):
            return f"trang {i}: {problem}"
    if lexicon is None:
        return ""
    ratio, count = known_ratio(texts, lexicon)
    if count >= min_words and ratio < min_known:
        return f"chỉ {ratio:.0%} từ có trong từ điển (nghi mất dấu, vỡ chữ hoặc lớp text scan kém)"
    return ""
