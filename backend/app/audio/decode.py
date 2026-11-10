"""Giải mã âm thanh, cắt đoạn ở chỗ lặng."""
import io
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

SR = 16000  # Whisper nhận 16 kHz mono
FRAME = 480  # 30 ms mỗi khung khi tìm chỗ lặng
AUDIO_EXT = (".mp3", ".m4a", ".m4b", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus", ".wma", ".amr", ".aiff", ".aif",
             ".mp4", ".webm", ".mkv", ".mov")


class UnsupportedAudio(Exception):
    pass


class AudioTooLong(Exception):
    pass


def _wav_fallback(src: Path, dest: Path) -> None:
    """Không có ffmpeg: chỉ đọc được wav PCM 16-bit."""
    try:
        with wave.open(str(src), "rb") as w:
            if w.getsampwidth() != 2:
                raise UnsupportedAudio("only 16-bit WAV can be read without ffmpeg")
            rate, channels = w.getframerate(), w.getnchannels()
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    except (wave.Error, EOFError) as exc:
        raise UnsupportedAudio("invalid WAV file") from exc
    audio = data.reshape(-1, channels).mean(axis=1) if channels > 1 else data.astype(np.float32)
    if rate != SR and len(audio):
        n = int(len(audio) * SR / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio)
    dest.write_bytes(np.clip(audio, -32768, 32767).astype(np.int16).tobytes())


def decode_to_pcm(src: str | Path, dest: str | Path) -> float:
    """Ghi PCM 16 kHz mono ra dest; trả số giây."""
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("ffmpeg"):
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", str(dest)]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            raise UnsupportedAudio("ffmpeg could not decode the file")
    elif src.suffix.lower() == ".wav":
        _wav_fallback(src, dest)
    else:
        raise UnsupportedAudio("ffmpeg is required to decode this audio format")
    seconds = dest.stat().st_size / 2 / SR
    if seconds <= 0:
        raise UnsupportedAudio("audio has no samples")
    return seconds


def load_pcm(path: str | Path) -> np.ndarray:
    """Đọc PCM bằng memmap (không nạp cả file)."""
    return np.memmap(path, dtype=np.int16, mode="r")


def cut_points(audio: np.ndarray, min_s: float, max_s: float) -> list[int]:
    """Điểm cắt: đoạn dài min_s..max_s giây, cắt ở chỗ lặng nhất."""
    n = len(audio) // FRAME
    if n == 0:
        return [0, len(audio)]
    energy = np.sqrt((audio[: n * FRAME].astype(np.float32).reshape(n, FRAME) ** 2).mean(axis=1))
    cuts, start = [0], 0
    while len(audio) - start > max_s * SR:
        a, b = (start + int(min_s * SR)) // FRAME, min(n, (start + int(max_s * SR)) // FRAME)
        start = (a + int(np.argmin(energy[a:b]))) * FRAME
        cuts.append(start)
    cuts.append(len(audio))
    return cuts


def segments(audio: np.ndarray, min_s: float, max_s: float, silence_level: int) -> list[dict]:
    """Các đoạn cần chép lời; bỏ đoạn im lặng để Whisper không bịa chữ."""
    cuts = cut_points(audio, min_s, max_s)
    out = []
    for a, b in zip(cuts, cuts[1:]):
        if b > a and int(np.abs(audio[a:b].astype(np.int32)).max()) > silence_level:
            out.append({"start": a, "end": b})
    return out


def wav_bytes(seg: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.ascontiguousarray(seg, dtype=np.int16).tobytes())
    return buf.getvalue()


def clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
