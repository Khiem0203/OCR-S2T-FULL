"""Luồng speech-to-text: giải mã, cắt đoạn, PhoWhisper, sửa chính tả, báo từ lạ."""
import asyncio
import logging
import tempfile
import time
from pathlib import Path

import httpx

from ..audio.decode import SR, AudioTooLong, decode_to_pcm, load_pcm, segments, wav_bytes
from ..clients.phowhisper import PhoWhisperClient, PhoWhisperError
from ..config import Settings
from ..spell.checker import SpellChecker
from ..spell.compare import compare_lines
from ..spell.unknown import build_notice, find_unknown_words
from .fullpage import build_spell_checker, get_lexicon

log = logging.getLogger("ocr.speech")


class AllSegmentsFailed(Exception):
    pass


def build_phowhisper(settings: Settings, http: httpx.AsyncClient) -> PhoWhisperClient:
    return PhoWhisperClient(
        http, settings.phowhisper_url, settings.phowhisper_model, settings.phowhisper_api_key,
        settings.speech_language, settings.phowhisper_timeout, settings.asr_concurrency,
    )


# Bước 1: chuẩn bị


def prepare_audio(src: Path, out_dir: Path, settings: Settings, index: int, used_seconds: float = 0.0) -> dict:
    """Giải mã một file ra PCM và tính các đoạn cần chép lời."""
    pcm = out_dir / f"audio_{index:03d}.pcm"
    seconds = decode_to_pcm(src, pcm)
    if used_seconds + seconds > settings.speech_max_minutes * 60:
        raise AudioTooLong(f"audio is longer than {settings.speech_max_minutes} minutes")
    parts = segments(load_pcm(pcm), settings.speech_min_chunk, settings.speech_max_chunk, settings.speech_silence_level)
    log.info("%s: %.0fs, %d segment(s)", src.name, seconds, len(parts))
    return {"file": src.name, "pcm": str(pcm), "duration": round(seconds, 2), "segments": parts}


# Bước 2: chép lời


def new_segment(index: int, file: str, start: int, end: int) -> dict:
    return {"index": index, "file": file, "start": round(start / SR, 2), "end": round(end / SR, 2),
            "raw_text": "", "text": "", "corrections": [], "error": None}


async def transcribe_files(files: list[dict], settings: Settings, http: httpx.AsyncClient) -> list[dict]:
    """Gửi song song mọi đoạn của mọi file; kết quả theo đúng thứ tự."""
    client = build_phowhisper(settings, http)
    jobs = []
    for f in files:
        audio = load_pcm(f["pcm"])
        for part in f["segments"]:
            jobs.append((new_segment(len(jobs) + 1, f["file"], part["start"], part["end"]), audio))

    async def one(seg: dict, audio) -> dict:
        start, end = int(seg["start"] * SR), int(seg["end"] * SR)
        try:
            wav = await asyncio.to_thread(wav_bytes, audio[start:end])
            # Whisper đôi khi xuống dòng: mỗi đoạn giữ một dòng
            seg["raw_text"] = " ".join((await client.transcribe(wav)).split())
        except PhoWhisperError:
            log.warning("segment %d: ASR backend failed", seg["index"])
            seg["error"] = "ASR backend error"
        except Exception:
            log.exception("segment %d: unexpected error", seg["index"])
            seg["error"] = "Internal error while processing segment"
        seg["text"] = seg["raw_text"]
        return seg

    out = list(await asyncio.gather(*(one(s, a) for s, a in jobs)))
    if out and all(s["error"] for s in out):
        raise AllSegmentsFailed()
    return out


# Bước 3: hậu xử lý


async def postprocess(segs: list[dict], spell: SpellChecker | None) -> list[dict]:
    """Sửa chính tả cả bản chép lời (mỗi đoạn một dòng); trả các dòng đã sửa kèm thời gian."""
    if spell is None or not segs:
        return []
    raw = "\n".join(s["raw_text"] for s in segs)
    text, corrections = await spell.check(raw)
    lines = text.split("\n")
    for s, line in zip(segs, lines):
        s["text"] = line
    for c in corrections:
        segs[c["line"] - 1]["corrections"].append({k: v for k, v in c.items() if k != "line"})
    changes = []
    for c in compare_lines(raw, text, corrections):
        s = segs[c.pop("line") - 1]
        changes.append({"segment": s["index"], "file": s["file"], "start": s["start"], "end": s["end"], **c})
    return changes


def finalize(files: list[dict], segs: list[dict], changes: list[dict], settings: Settings, elapsed_ms: int,
             include_raw: bool = True) -> dict:
    unknown, unknown_total, notice = [], 0, None
    lexicon = get_lexicon(settings)
    if settings.unknown_report and lexicon is not None:
        pages = [{"page": s["index"], "text": s["text"]} for s in segs]
        unknown, unknown_total = find_unknown_words(
            pages, lexicon, settings.unknown_include, settings.unknown_min_len, settings.unknown_max_words
        )
        # Với âm thanh, "trang" là số thứ tự đoạn
        unknown = [{**{k: v for k, v in w.items() if k != "pages"}, "segments": w["pages"]} for w in unknown]
        notice = build_notice(unknown, unknown_total)
    if not segs:
        notice = "Không có đoạn nào có tiếng nói."
    out_segs = [s if include_raw else {k: v for k, v in s.items() if k != "raw_text"} for s in segs]
    return {
        "duration": round(sum(f["duration"] for f in files), 2),
        "segment_count": len(segs),
        "text": "\n".join(s["text"] for s in segs if s["text"]),
        "segments": out_segs,
        "correction_total": sum(len(s["corrections"]) for s in segs),
        "changes": changes,
        "unknown_words": unknown,
        "unknown_total": unknown_total,
        "notice": notice,
        "input": {"mode": "speech", "files": [{"file": f["file"], "duration": f["duration"]} for f in files]},
        "elapsed_ms": elapsed_ms,
    }


def build_speech_spell_checker(settings: Settings, http: httpx.AsyncClient) -> SpellChecker | None:
    # Whisper ra âm tiết đúng chính tả nhưng sai từ: mặc định cho ProtonX xem mọi đoạn
    return build_spell_checker(settings, http, review=settings.speech_spell_review)


# Xử lý cả file một lần (endpoint sync)


async def process_audio(
    data: bytes,
    filename: str,
    settings: Settings,
    http: httpx.AsyncClient,
    *,
    spellcheck: bool = True,
    include_raw: bool = False,
) -> dict:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        src = Path(tmp) / (Path(filename).name or "audio")
        await asyncio.to_thread(src.write_bytes, data)
        f = await asyncio.to_thread(prepare_audio, src, Path(tmp), settings, 1)
        segs = await transcribe_files([f], settings, http)
    changes = await postprocess(segs, build_speech_spell_checker(settings, http) if spellcheck else None)
    return finalize([f], segs, changes, settings, int((time.perf_counter() - started) * 1000), include_raw)
