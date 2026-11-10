"""File tải lên từ máy, đọc lại như S3."""
import os
import re
import shutil
import uuid
from pathlib import Path

from .s3 import FileTooLarge

SCHEME = "upload://"
_ID = re.compile(r"^[0-9a-f]{32}$")


def is_upload(path: str) -> bool:
    return path.startswith(SCHEME)


def safe_name(name: str) -> str:
    """Bỏ phần thư mục và ký tự lạ."""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = re.sub(r"[\x00-\x1f/]", "", base).lstrip(".")
    return base[:200] or "file"


def new_upload(root: str | Path) -> tuple[str, Path]:
    upload_id = uuid.uuid4().hex
    folder = Path(root) / upload_id
    folder.mkdir(parents=True)
    return upload_id, folder


def save_file(src, folder: Path, name: str, max_bytes: int) -> str:
    """Lưu file, tên trùng thì thêm số; trả tên đã lưu."""
    name = safe_name(name)
    stem, ext = os.path.splitext(name)
    target, n = folder / name, 1
    while target.exists():
        n += 1
        target = folder / f"{stem} ({n}){ext}"
    size = 0
    with open(target, "wb") as out:
        while chunk := src.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                out.close()
                target.unlink(missing_ok=True)
                raise FileTooLarge(f"{name} is larger than {max_bytes // (1024 * 1024)} MB")
            out.write(chunk)
    if not size:
        target.unlink(missing_ok=True)
        raise ValueError(f"{name} is empty")
    return target.name


class LocalStorage:
    """Giống S3Storage; đường dẫn upload://<id>/[tên file]."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def folder(self, path: str) -> tuple[str, str, Path]:
        upload_id, _, name = path[len(SCHEME):].partition("/")
        if not _ID.match(upload_id) or "/" in name or name in (".", ".."):
            raise ValueError("invalid upload path")
        return upload_id, name, self.root / upload_id

    def candidates(self, path: str, siblings: bool = True) -> tuple[str, list[str], str]:
        upload_id, name, folder = self.folder(path)
        if not folder.is_dir():
            raise ValueError("upload not found or already processed")
        names = sorted(p.name for p in folder.iterdir() if p.is_file())
        if not name:
            return upload_id, names, ""
        if not siblings:
            return upload_id, [name], name
        stem = os.path.splitext(name)[0].lower()
        return upload_id, [name] + [n for n in names if n != name and os.path.splitext(n)[0].lower() == stem], name

    def download(self, upload_id: str, name: str, dest_dir: str | Path, max_bytes: int) -> Path:
        """Chuyển file vào thư mục xử lý."""
        src = self.root / upload_id / safe_name(name)
        if src.stat().st_size > max_bytes:
            raise FileTooLarge(f"{name} is larger than {max_bytes // (1024 * 1024)} MB")
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        return Path(shutil.move(str(src), str(dest / src.name)))

    def cleanup(self, path: str) -> None:
        shutil.rmtree(self.folder(path)[2], ignore_errors=True)
