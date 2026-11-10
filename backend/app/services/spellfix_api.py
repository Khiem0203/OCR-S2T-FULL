"""Service sửa chính tả ProtonX (T5, chạy bằng transformers; vLLM không hỗ trợ).

Chạy: uvicorn app.services.spellfix_api:app --host 127.0.0.1 --port 8006
Không có xác thực: chỉ nghe localhost.
"""
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# Chọn GPU trước khi nạp torch
if os.getenv("SPELLFIX_CUDA_DEVICE", "").strip():
    os.environ["CUDA_VISIBLE_DEVICES"] = os.environ["SPELLFIX_CUDA_DEVICE"].strip()

import torch  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("spellfix")

MODEL_PATH = os.getenv("SPELLFIX_MODEL", "protonx-models/protonx-legal-tc")
NUM_BEAMS = int(os.getenv("SPELLFIX_NUM_BEAMS", "4"))
MAX_TOKENS = int(os.getenv("SPELLFIX_MAX_TOKENS", "160"))
BATCH_SIZE = int(os.getenv("SPELLFIX_BATCH_SIZE", "16"))
DTYPE = {"float32": torch.float32, "float16": torch.float16, "bfloat16": torch.bfloat16}[os.getenv("SPELLFIX_DTYPE", "float32")]
MIN_LEN_RATIO = float(os.getenv("SPELLFIX_MIN_LEN_RATIO", "0.8"))
MAX_LEN_RATIO = float(os.getenv("SPELLFIX_MAX_LEN_RATIO", "1.25"))

_SENTENCE_SPLIT = re.compile(r"(?<=[.;:!?\n])\s+")

_model = None
_tokenizer = None
_device = None
_lock = threading.Lock()


class CorrectRequest(BaseModel):
    texts: list[str]


class CorrectResponse(BaseModel):
    texts: list[str]


def _load_tokenizer():
    try:
        return AutoTokenizer.from_pretrained(MODEL_PATH)
    except Exception as exc:
        log.warning("AutoTokenizer failed (%s: %s); loading tokenizer.json directly", type(exc).__name__, exc)
    tok_file = os.path.join(MODEL_PATH, "tokenizer.json")
    if not os.path.isfile(tok_file):
        files = sorted(os.listdir(MODEL_PATH)) if os.path.isdir(MODEL_PATH) else []
        raise RuntimeError(f"cannot load a tokenizer and {tok_file} is missing (directory has: {files})")
    try:
        from transformers import PreTrainedTokenizerFast
    except ImportError:
        from transformers import TokenizersBackend as PreTrainedTokenizerFast

    special = {"eos_token": "</s>", "pad_token": "<pad>", "unk_token": "<unk>"}
    map_file = os.path.join(MODEL_PATH, "special_tokens_map.json")
    if os.path.isfile(map_file):
        with open(map_file, encoding="utf-8") as f:
            for key, value in json.load(f).items():
                if key in special:
                    special[key] = value["content"] if isinstance(value, dict) else value
    log.info("tokenizer loaded from %s (%s)", tok_file, special)
    return PreTrainedTokenizerFast(tokenizer_file=tok_file, **special)


def get_model():
    """Nạp model một lần; hết VRAM thì chạy CPU."""
    global _model, _tokenizer, _device
    with _lock:
        if _model is None:
            _device = "cuda" if torch.cuda.is_available() else "cpu"
            gpu = os.getenv("CUDA_VISIBLE_DEVICES", "all")
            log.info("loading %s on %s (CUDA_VISIBLE_DEVICES=%s, %s)", MODEL_PATH, _device, gpu, DTYPE)
            _tokenizer = _load_tokenizer()
            try:
                _model = AutoModelForSeq2SeqLM.from_pretrained(MODEL_PATH, dtype=DTYPE).to(_device).eval()
            except torch.cuda.OutOfMemoryError:
                log.error("out of VRAM on GPU %s; using CPU (float32, slower)", gpu)
                torch.cuda.empty_cache()
                _device = "cpu"
                _model = AutoModelForSeq2SeqLM.from_pretrained(MODEL_PATH, dtype=torch.float32).eval()
            if _device == "cuda":
                log.info("model loaded, %.0f MiB VRAM", torch.cuda.memory_allocated() / 1024**2)
    return _model, _tokenizer, _device


def _n_tokens(tokenizer, text: str) -> int:
    return len(tokenizer(text, add_special_tokens=True)["input_ids"])


def _chunk(tokenizer, text: str, limit: int) -> list[str]:
    """Cắt text thành đoạn tối đa `limit` token."""
    if _n_tokens(tokenizer, text) <= limit:
        return [text]
    pieces = []
    for sentence in _SENTENCE_SPLIT.split(text):
        if _n_tokens(tokenizer, sentence) <= limit:
            pieces.append(sentence)
            continue
        current: list[str] = []
        for word in sentence.split():
            if current and _n_tokens(tokenizer, " ".join(current + [word])) > limit:
                pieces.append(" ".join(current))
                current = []
            current.append(word)
        if current:
            pieces.append(" ".join(current))
    chunks, current = [], ""
    for piece in pieces:
        candidate = f"{current} {piece}".strip()
        if current and _n_tokens(tokenizer, candidate) > limit:
            chunks.append(current)
            current = piece
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _accept(original: str, corrected: str) -> str:
    """Giữ bản gốc nếu output rỗng hoặc lệch độ dài nhiều."""
    if not corrected:
        return original
    ratio = len(corrected) / max(len(original), 1)
    if MIN_LEN_RATIO <= ratio <= MAX_LEN_RATIO:
        return corrected
    log.warning("rejected (length ratio %.2f outside %s-%s): %r -> %r", ratio, MIN_LEN_RATIO, MAX_LEN_RATIO, original, corrected)
    return original


def correct_texts(texts: list[str]) -> list[str]:
    model, tokenizer, device = get_model()
    limit = MAX_TOKENS - 10
    chunk_map, flat = [], []
    for i, text in enumerate(texts):
        stripped = (text or "").strip()
        if not stripped:
            continue
        for part in _chunk(tokenizer, stripped, limit):
            chunk_map.append(i)
            flat.append(part)

    corrected = []
    for start in range(0, len(flat), BATCH_SIZE):
        batch = flat[start : start + BATCH_SIZE]
        enc = tokenizer(batch, return_tensors="pt", padding=True, truncation=True, max_length=MAX_TOKENS).to(device)
        with torch.no_grad():
            out = model.generate(**enc, num_beams=NUM_BEAMS, max_new_tokens=MAX_TOKENS, early_stopping=True)
        decoded = tokenizer.batch_decode(out, skip_special_tokens=True)
        corrected.extend(_accept(o, c.strip()) for o, c in zip(batch, decoded))

    results: list[list[str]] = [[] for _ in texts]
    for i, part in zip(chunk_map, corrected):
        results[i].append(part)
    return [" ".join(parts) if parts else (texts[i] or "") for i, parts in enumerate(results)]


@asynccontextmanager
async def _lifespan(_: FastAPI):
    get_model()
    yield


app = FastAPI(title="ProtonX spellfix service", lifespan=_lifespan)


@app.post("/spellfix/correct", response_model=CorrectResponse)
def correct(req: CorrectRequest):
    t0 = time.time()
    texts = correct_texts(req.texts)
    changed = sum(a != b for a, b in zip(req.texts, texts))
    log.info("request: %d texts, %d changed, %.2fs", len(req.texts), changed, time.time() - t0)
    return CorrectResponse(texts=texts)


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_PATH, "device": _device}
