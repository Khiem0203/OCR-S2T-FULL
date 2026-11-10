import json
import logging
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

log = logging.getLogger("ocr.lexicon")

_ACCENTED = "àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ"
_BASE = "abcdeghiklmnopqrstuvxy"  # Tiếng Việt không có f, j, w, z
LETTERS = _BASE + _ACCENTED
VN_WORD = re.compile(f"[{LETTERS}]+")
_KEY_ALPHABET = _BASE + "d"


def norm(s: str) -> str:
    return unicodedata.normalize("NFC", s).lower()


def unaccent(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower().replace("đ", "d"))
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _neighbours(key: str) -> set[str]:
    """Các chuỗi cách `key` 1 lần sửa."""
    out: set[str] = set()
    for i in range(len(key) + 1):
        left, right = key[:i], key[i:]
        if right:
            out.add(left + right[1:])
            if len(right) > 1:
                out.add(left + right[1] + right[0] + right[2:])
            for c in _KEY_ALPHABET:
                out.add(left + c + right[1:])
        for c in _KEY_ALPHABET:
            out.add(left + c + right)
    out.discard(key)
    return out


def read_dictionary_file(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    yield json.loads(line)["text"]
                except (ValueError, KeyError):
                    continue
            else:
                yield line


class Lexicon:
    """Âm tiết và cặp âm tiết liền nhau lấy từ từ điển."""

    def __init__(self, entries=(), source: str = "custom"):
        self.freq: Counter = Counter()
        self.bigrams: set[tuple[str, str]] = set()
        self.sources: dict[str, int] = {}
        self._by_key: dict[str, list[str]] | None = None
        self._cache: dict[str, list[tuple[str, int]]] = {}
        if entries:
            self.add(entries, source)

    def add(self, entries, source: str) -> int:
        usable = 0
        for entry in entries:
            parts = norm(entry).split()
            if not parts or not all(VN_WORD.fullmatch(p) for p in parts):
                continue
            usable += 1
            self.freq.update(parts)
            self.bigrams.update(zip(parts, parts[1:]))
        self.sources[source] = self.sources.get(source, 0) + usable
        self._by_key = None
        self._cache.clear()
        return usable

    @classmethod
    def from_dictionaries(cls, directory: str, names=("hongocduc", "tudientv", "wiktionary"), extra_words_file: str = "") -> "Lexicon":
        lex = cls()
        for name in names:
            path = Path(directory) / name / "words.txt"
            if not path.is_file():
                raise FileNotFoundError(f"dictionary '{name}' not found at {path}")
            lex.add(read_dictionary_file(path), name)
        if extra_words_file:
            lex.add(read_dictionary_file(Path(extra_words_file)), "extra")
        log.info("lexicon ready: %d syllables, %d pairs, entries per source %s", len(lex), len(lex.bigrams), lex.sources)
        return lex

    def _ensure_index(self) -> dict[str, list[str]]:
        if self._by_key is None:
            by_key: dict[str, list[str]] = defaultdict(list)
            for syl, _ in self.freq.most_common():
                by_key[unaccent(syl)].append(syl)
            self._by_key = by_key
        return self._by_key

    def __contains__(self, word: str) -> bool:
        return word in self.freq

    def __len__(self) -> int:
        return len(self.freq)

    def candidates(self, word: str, limit: int = 6) -> list[tuple[str, int]]:
        """Trả các cặp (ứng viên, chi phí) cho một từ."""
        word = norm(word)
        cache_key = f"{word}|{limit}"
        hit = self._cache.get(cache_key)
        if hit is not None:
            return hit
        index = self._ensure_index()
        key = unaccent(word)
        found: dict[str, int] = {c: 0 for c in index.get(key, ()) if c != word}
        for nk in _neighbours(key):
            for c in index.get(nk, ()):
                found.setdefault(c, 1)
        ranked = sorted(found.items(), key=lambda kv: (kv[1], -self.freq[kv[0]]))[: max(limit * 3, limit)]
        ranked = sorted(ranked, key=lambda kv: -self.freq[kv[0]] + 1000 * kv[1])[:limit]
        self._cache[cache_key] = ranked
        return ranked
