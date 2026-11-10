from typing import Optional, Union

from pydantic import BaseModel, Field, SecretStr, field_validator

from ..config import QUEUES
from ..storage.s3 import parse_s3_path


def _http_url(value: str) -> str:
    if not value.lower().startswith(("http://", "https://")):
        raise ValueError("must start with http:// or https://")
    return value


class FullPageRequest(BaseModel):
    # Một file hoặc thư mục (kết thúc bằng /)
    s3_path: str = Field(..., description="s3://bucket/path/file.pdf or s3://bucket/folder/ (PDF, image, docx, doc, odt, rtf, txt, md)")
    # Không gửi thì dùng S3_* trong .env
    access_key: Optional[SecretStr] = Field(default=None, description="S3 access key (default: S3_ACCESS_KEY)")
    secret_key: Optional[SecretStr] = Field(default=None, description="S3 secret key (default: S3_SECRET_KEY)")
    endpoint: str = Field(default="", description="MinIO / S3 endpoint URL (default: S3_ENDPOINT)")
    callback_url: str = Field(default="", description="Webhook that receives the result (optional)")
    document_id: Optional[Union[int, str]] = Field(default=None, description="Your own document id, echoed back")
    spellcheck: bool = Field(default=True, description="Run the dictionary + ProtonX spelling check")

    @field_validator("s3_path")
    @classmethod
    def _s3(cls, v: str) -> str:
        parse_s3_path(v)
        return v

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, v: str) -> str:
        return _http_url(v) if v else v

    @field_validator("callback_url")
    @classmethod
    def _callback(cls, v: str) -> str:
        return _http_url(v) if v else v


class SpeechRequest(FullPageRequest):
    s3_path: str = Field(..., description="s3://bucket/path/file.m4a or s3://bucket/folder/ (mp3, m4a, wav, flac, ogg, opus, aac, mp4...)")
    spellcheck: bool = Field(default=True, description="Run the dictionary + ProtonX spelling check on the transcript")


class SubmitResponse(BaseModel):
    error_code: int
    error_message: str
    request_id: str


class SpellRequest(BaseModel):
    text: str = Field(..., max_length=200_000)


class _QueueModel(BaseModel):
    queue_name: str

    @field_validator("queue_name")
    @classmethod
    def _known_queue(cls, v: str) -> str:
        if v not in QUEUES:
            raise ValueError(f"queue_name must be one of {list(QUEUES)}")
        return v


class WorkerStartRequest(_QueueModel):
    num_workers: int = Field(default=1, ge=1, le=16)
    concurrency: int = Field(default=1, ge=1, le=8)


class WorkerStopRequest(_QueueModel):
    pass


class WorkerRestartRequest(_QueueModel):
    num_workers: Optional[int] = Field(default=None, ge=1, le=16)
    concurrency: Optional[int] = Field(default=None, ge=1, le=8)
