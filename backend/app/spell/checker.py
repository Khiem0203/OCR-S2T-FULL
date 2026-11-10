import asyncio
import csv
import difflib
import logging
import math
import re
import unicodedata
from dataclasses import dataclass

from ..clients.protonx import ProtonXClient
from .lexicon import VN_WORD, Lexicon, norm, unaccent

log = logging.getLogger("ocr.spell")

_WORD = re.compile(r"[^\W\d_]+")
_OPTIONS = 5  # Số ứng viên từ điển ProtonX phải chọn trong đó


@dataclass
class Token:
    text: str
    start: int
    end: int
    low: str


def tokenize(line: str) -> list[Token]:
    return [Token(m.group(), m.start(), m.end(), norm(m.group())) for m in _WORD.finditer(line)]


def has_tone_marks(s: str) -> bool:
    return any(unicodedata.category(c) == "Mn" for c in unicodedata.normalize("NFD", s))


def is_acronym(text: str) -> bool:
    return text.isupper() and len(text) <= 6 and not has_tone_marks(text)


def match_case(original: str, new: str) -> str:
    if original.isupper():
        return new.upper()
    if original[:1].isupper():
        return new[:1].upper() + new[1:]
    return new


def load_replacements(path: str) -> list[tuple[re.Pattern, str]]:
    rules = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 2:
                continue
            wrong, right = row[0].strip(), row[1].strip()
            if not wrong or not right or wrong.startswith("#") or wrong.lower() == "sai":
                continue
            rules.append((re.compile(r"(?<!\w)" + re.escape(wrong) + r"(?!\w)", re.IGNORECASE | re.UNICODE), right))
    return rules


class SpellChecker:
    def __init__(
        self,
        lexicon: Lexicon,
        fixer: ProtonXClient | None,
        mode: str = "both",
        review: str = "suspects",
        batch_chars: int = 1800,
        replacements: list[tuple[re.Pattern, str]] | None = None,
    ):
        self.lex = lexicon
        self.fixer = fixer if mode == "both" else None
        self.enabled = mode != "off"
        self.review_all = review == "all"
        self.batch_chars = batch_chars
        self.replacements = replacements or []

    # Phát hiện từ nghi sai

    def _protected(self, tok: Token) -> bool:
        if len(tok.low) < 3 or not VN_WORD.fullmatch(tok.low):
            return True
        return is_acronym(tok.text)

    def _neighbour_forms(self, line: str, toks: list[Token], i: int, step: int) -> set[str]:
        j = i + step
        if j < 0 or j >= len(toks):
            return set()
        a, b = (toks[j], toks[i]) if step < 0 else (toks[i], toks[j])
        if line[a.end : b.start].strip():
            return set()
        n = toks[j]
        forms = {n.low}
        if n.low not in self.lex and not self._protected(n):
            forms.update(c for c, _ in self.lex.candidates(n.low)[:3])
        return forms

    def _rank(self, line: str, toks: list[Token], i: int) -> list[tuple[str, float, int, int]]:
        prev_forms = self._neighbour_forms(line, toks, i, -1)
        next_forms = self._neighbour_forms(line, toks, i, +1)
        ranked = []
        for cand, cost in self.lex.candidates(toks[i].low):
            ctx = int(any((p, cand) in self.lex.bigrams for p in prev_forms))
            ctx += int(any((cand, n) in self.lex.bigrams for n in next_forms))
            score = math.log1p(self.lex.freq[cand]) + 3 * ctx - 1.5 * cost
            ranked.append((cand, score, ctx, cost))
        ranked.sort(key=lambda r: -r[1])
        return ranked

    def _dict_pick(self, tok: Token, i: int, ranked: list) -> str | None:
        if not ranked:
            return None
        best = ranked[0]
        if tok.text[:1].isupper() and not tok.text.isupper() and i > 0 and best[2] == 0:
            return None  # Có thể là tên riêng
        rivals = [r for r in ranked[1:] if r[3] <= best[3]]
        if best[2] > 0 and (not rivals or best[1] - rivals[0][1] >= 1.0):
            return best[0]
        if len(ranked) == 1 and best[3] == 0:
            return best[0]
        return None

    # Duyệt bằng ProtonX

    def _valid_fix(self, tok: Token, right: str, options: set[str]) -> bool:
        right = norm(right)
        if right == tok.low or right not in self.lex or self._protected(tok):
            return False
        if tok.low in self.lex:  # Từ đúng: chỉ cho đổi dấu
            return unaccent(right) == unaccent(tok.low)
        return right in options  # Từ lạ: phải là ứng viên từ điển

    def _model_fixes(self, toks: list[Token], corrected: str, suspects: dict[int, list]) -> dict[int, str]:
        """Vị trí từ -> từ thay; chỉ lấy từ ProtonX thay 1-1."""
        new = tokenize(corrected)
        matcher = difflib.SequenceMatcher(None, [t.low for t in toks], [t.low for t in new], autojunk=False)
        fixes: dict[int, str] = {}
        for op, i1, i2, j1, j2 in matcher.get_opcodes():
            if op != "replace" or i2 - i1 != j2 - j1:
                continue
            for k in range(i2 - i1):
                i, right = i1 + k, new[j1 + k].low
                options = {c for c, *_ in suspects.get(i, [])[:_OPTIONS]}
                if self._valid_fix(toks[i], right, options):
                    fixes[i] = right
        return fixes

    async def _review_batch(self, items: list[dict]) -> dict[int, str] | None:
        fixed = await self.fixer.correct([it["line"] for it in items])
        if fixed is None:
            return None
        return {it["id"]: out for it, out in zip(items, fixed)}

    def _batches(self, items: list[dict]) -> list[list[dict]]:
        batches, cur, size = [], [], 0
        for it in items:
            if cur and (size + len(it["line"]) > self.batch_chars or len(cur) >= 20):
                batches.append(cur)
                cur, size = [], 0
            cur.append(it)
            size += len(it["line"])
        if cur:
            batches.append(cur)
        return batches

    # Hàm chính

    async def check(self, text: str) -> tuple[str, list[dict]]:
        if not self.enabled or not text:
            return text, []
        corrections: list[dict] = []
        lines = text.split("\n")

        def _replace(m: re.Match, right: str, ln: int) -> str:
            new = right if any(c.isupper() for c in right) else match_case(m.group(0), right)
            if new != m.group(0):
                corrections.append({"line": ln + 1, "wrong": m.group(0), "right": new, "by": "replace"})
            return new

        # Thay theo từng dòng để biết số dòng
        for ln in range(len(lines)):
            for pat, right in self.replacements:
                lines[ln] = pat.sub(lambda m, r=right, n=ln: _replace(m, r, n), lines[ln])

        tokens = [tokenize(line) for line in lines]
        suspects: dict[int, dict[int, list]] = {}  # dòng -> vị trí từ -> ứng viên
        for ln, toks in enumerate(tokens):
            for i, tok in enumerate(toks):
                if tok.low not in self.lex and not self._protected(tok):
                    ranked = self._rank(lines[ln], toks, i)
                    if ranked:
                        suspects.setdefault(ln, {})[i] = ranked

        model_fixes: dict[int, dict[int, str]] = {}  # dòng -> vị trí từ -> từ ProtonX thay
        if self.fixer is not None:
            items = [
                {"id": ln, "line": line}
                for ln, line in enumerate(lines)
                if ln in suspects or (self.review_all and len(tokens[ln]) >= 2)
            ]
            answers = await asyncio.gather(*(self._review_batch(b) for b in self._batches(items)))
            for answer in answers:
                for ln, corrected in (answer or {}).items():
                    model_fixes[ln] = self._model_fixes(tokens[ln], corrected, suspects.get(ln, {}))

        for ln, toks in enumerate(tokens):
            # Ưu tiên ProtonX, còn lại dùng từ điển
            fixes: dict[int, tuple[str, str]] = {i: (right, "protonx") for i, right in model_fixes.get(ln, {}).items()}
            for i, ranked in suspects.get(ln, {}).items():
                if i not in fixes:
                    pick = self._dict_pick(toks[i], i, ranked)
                    if pick:
                        fixes[i] = (pick, "dict")
            if not fixes:
                continue
            line = lines[ln]
            for i in sorted(fixes, reverse=True):
                tok = toks[i]
                right, by = fixes[i]
                new = match_case(tok.text, right)
                line = line[: tok.start] + new + line[tok.end :]
                corrections.append({"line": ln + 1, "wrong": tok.text, "right": new, "by": by})
            lines[ln] = line
        corrections.sort(key=lambda c: c["line"])
        return "\n".join(lines), corrections
