"""Báo các từ không có trong từ điển."""
from collections import OrderedDict

from .checker import is_acronym, tokenize
from .lexicon import VN_WORD, Lexicon

KIND_ORDER = {"vietnamese": 0, "foreign": 1, "acronym": 2}


def classify(text: str, low: str) -> str:
    if not VN_WORD.fullmatch(low):
        return "foreign"  # Có chữ ngoài bảng chữ tiếng Việt
    if is_acronym(text):
        return "acronym"
    return "vietnamese"


def _context(line: str, start: int, end: int, radius: int = 40) -> str:
    left = max(0, start - radius)
    right = min(len(line), end + radius)
    return ("..." if left else "") + line[left:right].strip() + ("..." if right < len(line) else "")


def find_unknown_words(
    pages: list[dict],
    lexicon: Lexicon,
    include: tuple = ("vietnamese", "foreign", "acronym"),
    min_len: int = 2,
    max_words: int = 200,
    max_samples: int = 2,
) -> tuple[list[dict], int]:
    """Trả (danh sách từ lạ, tổng số)."""
    found: "OrderedDict[str, dict]" = OrderedDict()
    for page in pages:
        for line in (page.get("text") or "").split("\n"):
            for tok in tokenize(line):
                if len(tok.low) < min_len or tok.low in lexicon:
                    continue
                kind = classify(tok.text, tok.low)
                if kind not in include:
                    continue
                entry = found.get(tok.low)
                if entry is None:
                    entry = found[tok.low] = {"word": tok.text, "kind": kind, "count": 0, "pages": [], "samples": []}
                entry["count"] += 1
                if page["page"] not in entry["pages"]:
                    entry["pages"].append(page["page"])
                if len(entry["samples"]) < max_samples:
                    sample = _context(line, tok.start, tok.end)
                    if sample not in entry["samples"]:
                        entry["samples"].append(sample)
    words = sorted(found.items(), key=lambda kv: (KIND_ORDER[kv[1]["kind"]], -kv[1]["count"], kv[0]))
    total = len(words)
    out = []
    for low, entry in words[:max_words]:
        entry["pages"].sort()
        entry["suggestions"] = [c for c, _ in lexicon.candidates(low, limit=3)] if entry["kind"] == "vietnamese" else []
        out.append(entry)
    return out, total


def build_notice(words: list[dict], total: int, preview: int = 15) -> str | None:
    if not total:
        return None
    shown = ", ".join(w["word"] for w in words[:preview])
    more = f" và {total - preview} từ khác" if total > preview else ""
    return f"Phát hiện {total} từ không có trong từ điển: {shown}{more}."
