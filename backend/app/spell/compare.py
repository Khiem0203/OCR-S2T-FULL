"""So sánh text trước và sau khi sửa chính tả."""
import difflib


def segments(before: str, after: str, fixes: list[dict] = ()) -> list[dict]:
    """Chia dòng thành đoạn giữ nguyên / đoạn đã sửa để web tô màu; các đoạn nối bằng 1 dấu cách."""
    old, new = before.split(" "), after.split(" ")
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if op == "equal":
            out.append({"type": "same", "text": " ".join(new[j1:j2])})
            continue
        was, now = " ".join(old[i1:i2]), " ".join(new[j1:j2])
        by = next((f["by"] for f in fixes if f["wrong"] in was), None)
        out.append({"type": "fix", "before": was, "after": now, "by": by})
    return out


def mark_changes(before: str, after: str) -> str:
    """Dòng sau khi sửa, chỗ đổi ghi dạng [cũ→mới]."""
    parts = segments(before, after)
    return " ".join(p["text"] if p["type"] == "same" else f"[{p['before']}→{p['after']}]" for p in parts)


def compare_lines(raw: str, text: str, corrections: list[dict]) -> list[dict]:
    """Các dòng bị sửa: trước, sau, bản đánh dấu, đoạn tô màu và các từ đã sửa."""
    fixes: dict[int, list[dict]] = {}
    for c in corrections:
        fixes.setdefault(c["line"], []).append({"wrong": c["wrong"], "right": c["right"], "by": c["by"]})
    changes = []
    for n, (before, after) in enumerate(zip(raw.split("\n"), text.split("\n")), start=1):
        if before != after:
            line_fixes = fixes.get(n, [])
            changes.append({
                "line": n, "before": before, "after": after, "marked": mark_changes(before, after),
                "segments": segments(before, after, line_fixes), "fixes": line_fixes,
            })
    return changes
