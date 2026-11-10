"""Liệt kê và tải file từ S3/MinIO."""
import os
from pathlib import Path
from urllib.parse import urlparse


class FileTooLarge(Exception):
    pass


def parse_s3_path(s3_path: str) -> tuple[str, str]:
    parsed = urlparse(s3_path)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError("s3_path must look like s3://bucket/path/to/file.pdf or s3://bucket/folder/")
    return parsed.netloc, parsed.path.lstrip("/")


def endpoint_url(endpoint: str, ssl: bool) -> str:
    """Thêm http/https nếu endpoint chưa có."""
    endpoint = endpoint.strip()
    if endpoint and "://" not in endpoint:
        endpoint = ("https://" if ssl else "http://") + endpoint
    return endpoint


class S3Storage:
    """Liệt kê và tải file trong một bucket; khóa chỉ giữ trong bộ nhớ."""

    def __init__(self, access_key: str, secret_key: str, endpoint: str, region: str, ssl: bool = False):
        import boto3
        from botocore.config import Config

        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url(endpoint, ssl),
            use_ssl=ssl,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3},
                connect_timeout=10,
                read_timeout=60,
            ),
        )

    def list_folder(self, bucket: str, prefix: str) -> list[str]:
        """Các key ngay trong thư mục (không vào thư mục con)."""
        keys, token = [], None
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "/"}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            keys += [o["Key"] for o in page.get("Contents", []) if not o["Key"].endswith("/")]
            if not page.get("IsTruncated"):
                return keys
            token = page["NextContinuationToken"]

    def candidates(self, s3_path: str, siblings: bool = True) -> tuple[str, list[str], str]:
        """Trả (bucket, các key, key được chỉ định; rỗng nếu là thư mục).

        Chỉ định một file thì lấy thêm file cùng tên khác đuôi trong cùng thư mục.
        """
        bucket, key = parse_s3_path(s3_path)
        if key.endswith("/"):
            return bucket, self.list_folder(bucket, key), ""
        if not siblings:
            return bucket, [key], key
        folder = key.rsplit("/", 1)[0] + "/" if "/" in key else ""
        try:
            keys = self.list_folder(bucket, folder)
        except Exception:
            # Không có quyền liệt kê thì chỉ lấy đúng file
            return bucket, [key], key
        name = os.path.splitext(os.path.basename(key))[0].lower()
        same = [k for k in keys if k != key and os.path.splitext(os.path.basename(k))[0].lower() == name]
        return bucket, [key] + same, key

    def download(self, bucket: str, key: str, dest_dir: str | Path, max_bytes: int) -> Path:
        size = self.client.head_object(Bucket=bucket, Key=key)["ContentLength"]
        if size > max_bytes:
            raise FileTooLarge(f"{os.path.basename(key)} is {size // (1024 * 1024)} MB; limit is {max_bytes // (1024 * 1024)} MB")
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / (os.path.basename(key) or "document")
        self.client.download_file(bucket, key, str(target))
        return target
