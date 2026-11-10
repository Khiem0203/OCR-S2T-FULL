import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

QUEUES = (
    "fullpage_prepare_queue", "fullpage_ocr_queue", "fullpage_spell_queue", "fullpage_db_queue",
    "speech_prepare_queue", "speech_asr_queue", "speech_spell_queue", "speech_db_queue",
)


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> tuple:
    return tuple(x.strip().lower() for x in _str(name, default).split(",") if x.strip())


def _path(name: str, default: str = "") -> str:
    raw = _str(name, default)
    if not raw:
        return ""
    p = Path(raw)
    return str(p if p.is_absolute() else ROOT / p)


@dataclass(frozen=True)
class Settings:
    # repr=False: không in khóa ra log
    api_keys: frozenset = field(default=frozenset(), repr=False)
    chandra_api_key: str = field(default="", repr=False)
    phowhisper_api_key: str = field(default="", repr=False)
    broker_url: str = field(default="", repr=False)
    result_backend: str = field(default="", repr=False)
    pg_password: str = field(default="", repr=False)
    s3_access_key: str = field(default="", repr=False)
    s3_secret_key: str = field(default="", repr=False)
    flower_auth: str = field(default="", repr=False)

    auth_disabled: bool = False
    enable_docs: bool = False
    cors_origins: tuple = ()

    chandra_url: str = "http://localhost:8101/v1/chat/completions"
    chandra_model: str = "datalab-to/chandra-ocr-2"
    chandra_max_tokens: int = 4096
    chandra_timeout: float = 300.0
    ocr_concurrency: int = 4

    # Speech-to-text: PhoWhisper (vLLM)
    phowhisper_url: str = "http://localhost:8120/v1/audio/transcriptions"
    phowhisper_model: str = "phowhisper-large"
    phowhisper_timeout: float = 300.0
    asr_concurrency: int = 16
    speech_language: str = "vi"
    speech_min_chunk: float = 15.0
    speech_max_chunk: float = 28.0
    speech_silence_level: int = 300
    speech_max_minutes: int = 360
    speech_max_upload_mb: int = 500
    speech_spell_review: str = "all"

    spellfix_url: str = "http://localhost:8006/spellfix/correct"
    spellfix_timeout: float = 300.0
    spellfix_concurrency: int = 2

    render_dpi: int = 200
    max_long_side: int = 1536
    deskew: bool = True
    max_pages: int = 100
    max_upload_mb: int = 50
    pdf_text_fallback: bool = True
    # Đầu vào: ưu tiên file văn bản (docx, doc, txt...) rồi mới OCR
    text_first: bool = True
    input_siblings: bool = True
    text_page_chars: int = 3000
    spell_text_files: bool = False
    # Lấy text PDF trực tiếp; có lỗi Unicode thì OCR cả file
    pdf_text_first: bool = True
    pdf_text_min_known: float = 0.9

    spell_mode: str = "both"  # off | dict | both
    spell_review: str = "suspects"  # suspects | all
    spell_batch_chars: int = 1800
    dictionary_dir: str = str(ROOT / "data" / "dictionaries")
    dictionaries: tuple = ("hongocduc", "tudientv", "wiktionary")
    extra_words_file: str = ""
    replacements_file: str = ""

    unknown_report: bool = True
    unknown_include: tuple = ("vietnamese", "foreign", "acronym")
    unknown_min_len: int = 2
    unknown_max_words: int = 200

    result_expires: int = 86400
    temp_dir: str = str(ROOT / "temp_processing")
    # S3 mặc định khi request không gửi khóa
    s3_endpoint: str = ""
    s3_region: str = "us-east-1"
    s3_ssl: bool = False
    webhook_timeout: float = 30.0
    webhook_retries: int = 3
    webhook_allowed_hosts: tuple = ()

    pg_host: str = ""
    pg_port: int = 5432
    pg_db: str = ""
    pg_user: str = ""

    flower_url: str = "http://localhost:5555"
    celery_bin: str = ""


def load_settings() -> Settings:
    mode = _str("SPELL_MODE", "both").lower()
    review = _str("SPELL_REVIEW", "suspects").lower()
    if mode not in ("off", "dict", "both"):
        raise ValueError("SPELL_MODE must be one of: off, dict, both")
    if review not in ("suspects", "all"):
        raise ValueError("SPELL_REVIEW must be one of: suspects, all")
    speech_review = _str("SPEECH_SPELL_REVIEW", "all").lower()
    if speech_review not in ("suspects", "all"):
        raise ValueError("SPEECH_SPELL_REVIEW must be one of: suspects, all")
    d = Settings()
    min_chunk = _float("SPEECH_MIN_CHUNK", d.speech_min_chunk)
    max_chunk = _float("SPEECH_MAX_CHUNK", d.speech_max_chunk)
    # Whisper chỉ nghe tối đa 30 giây
    if not 0 < min_chunk < max_chunk <= 30:
        raise ValueError("need 0 < SPEECH_MIN_CHUNK < SPEECH_MAX_CHUNK <= 30")
    include = _list("UNKNOWN_INCLUDE", ",".join(d.unknown_include))
    bad = set(include) - {"vietnamese", "foreign", "acronym"}
    if bad:
        raise ValueError(f"UNKNOWN_INCLUDE has unknown kinds: {sorted(bad)}")
    return Settings(
        api_keys=frozenset(k.strip() for k in _str("API_KEYS").split(",") if k.strip()),
        chandra_api_key=_str("CHANDRA_API_KEY"),
        phowhisper_api_key=_str("PHOWHISPER_API_KEY"),
        broker_url=_str("CELERY_BROKER_URL"),
        result_backend=_str("CELERY_RESULT_BACKEND"),
        pg_password=_str("POSTGRES_PASSWORD"),
        flower_auth=_str("FLOWER_BASIC_AUTH"),
        auth_disabled=_bool("AUTH_DISABLED", False),
        enable_docs=_bool("ENABLE_DOCS", False),
        cors_origins=tuple(o.strip() for o in _str("CORS_ORIGINS").split(",") if o.strip()),
        chandra_url=_str("CHANDRA_URL", d.chandra_url),
        chandra_model=_str("CHANDRA_MODEL", d.chandra_model),
        chandra_max_tokens=_int("CHANDRA_MAX_TOKENS", d.chandra_max_tokens),
        chandra_timeout=_float("CHANDRA_TIMEOUT", d.chandra_timeout),
        ocr_concurrency=max(1, _int("OCR_CONCURRENCY", d.ocr_concurrency)),
        phowhisper_url=_str("PHOWHISPER_URL", d.phowhisper_url),
        phowhisper_model=_str("PHOWHISPER_MODEL_NAME", d.phowhisper_model),
        phowhisper_timeout=_float("PHOWHISPER_TIMEOUT", d.phowhisper_timeout),
        asr_concurrency=max(1, _int("ASR_CONCURRENCY", d.asr_concurrency)),
        speech_language=_str("SPEECH_LANGUAGE", d.speech_language),
        speech_min_chunk=min_chunk,
        speech_max_chunk=max_chunk,
        speech_silence_level=_int("SPEECH_SILENCE_LEVEL", d.speech_silence_level),
        speech_max_minutes=_int("SPEECH_MAX_MINUTES", d.speech_max_minutes),
        speech_max_upload_mb=_int("SPEECH_MAX_UPLOAD_MB", d.speech_max_upload_mb),
        speech_spell_review=speech_review,
        spellfix_url=_str("SPELLFIX_API_URL", d.spellfix_url),
        spellfix_timeout=_float("SPELLFIX_TIMEOUT", d.spellfix_timeout),
        spellfix_concurrency=max(1, _int("SPELLFIX_CONCURRENCY", d.spellfix_concurrency)),
        render_dpi=_int("RENDER_DPI", d.render_dpi),
        max_long_side=_int("MAX_LONG_SIDE", d.max_long_side),
        deskew=_bool("DESKEW", d.deskew),
        max_pages=_int("MAX_PAGES", d.max_pages),
        max_upload_mb=_int("MAX_UPLOAD_MB", d.max_upload_mb),
        pdf_text_fallback=_bool("PDF_TEXT_FALLBACK", d.pdf_text_fallback),
        text_first=_bool("TEXT_FIRST", d.text_first),
        input_siblings=_bool("INPUT_SIBLINGS", d.input_siblings),
        text_page_chars=max(500, _int("TEXT_PAGE_CHARS", d.text_page_chars)),
        spell_text_files=_bool("SPELL_TEXT_FILES", d.spell_text_files),
        pdf_text_first=_bool("PDF_TEXT_FIRST", d.pdf_text_first),
        pdf_text_min_known=_float("PDF_TEXT_MIN_KNOWN", d.pdf_text_min_known),
        spell_mode=mode,
        spell_review=review,
        spell_batch_chars=_int("SPELL_BATCH_CHARS", d.spell_batch_chars),
        dictionary_dir=_path("DICTIONARY_DIR", d.dictionary_dir),
        dictionaries=_list("DICTIONARIES", ",".join(d.dictionaries)),
        extra_words_file=_path("EXTRA_WORDS_FILE"),
        replacements_file=_path("REPLACEMENTS_FILE"),
        unknown_report=_bool("UNKNOWN_REPORT", d.unknown_report),
        unknown_include=include,
        unknown_min_len=_int("UNKNOWN_MIN_LEN", d.unknown_min_len),
        unknown_max_words=_int("UNKNOWN_MAX_WORDS", d.unknown_max_words),
        result_expires=_int("RESULT_EXPIRES", d.result_expires),
        temp_dir=_path("TEMP_DIR", d.temp_dir),
        s3_endpoint=_str("S3_ENDPOINT"),
        s3_access_key=_str("S3_ACCESS_KEY"),
        s3_secret_key=_str("S3_SECRET_KEY"),
        s3_region=_str("S3_REGION", d.s3_region),
        s3_ssl=_bool("S3_SSL", d.s3_ssl),
        webhook_timeout=_float("WEBHOOK_TIMEOUT", d.webhook_timeout),
        webhook_retries=_int("WEBHOOK_RETRIES", d.webhook_retries),
        webhook_allowed_hosts=_list("WEBHOOK_ALLOWED_HOSTS"),
        pg_host=_str("POSTGRES_HOST"),
        pg_port=_int("POSTGRES_PORT", d.pg_port),
        pg_db=_str("POSTGRES_DB"),
        pg_user=_str("POSTGRES_USER"),
        flower_url=_str("FLOWER_URL", d.flower_url),
        celery_bin=_str("CELERY_BIN"),
    )
