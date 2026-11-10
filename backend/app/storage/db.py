import logging

from ..config import Settings

log = logging.getLogger("ocr.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fullpage_documents (
    id            SERIAL PRIMARY KEY,
    request_id    TEXT UNIQUE NOT NULL,
    document_id   TEXT,
    s3_path       TEXT,
    page_count    INTEGER,
    text          TEXT,
    pages         JSONB,
    unknown_count INTEGER,
    notice        TEXT,
    created_at    TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE fullpage_documents ADD COLUMN IF NOT EXISTS correction_count INTEGER DEFAULT 0;
ALTER TABLE fullpage_documents ADD COLUMN IF NOT EXISTS changes JSONB DEFAULT '[]';
ALTER TABLE fullpage_documents ADD COLUMN IF NOT EXISTS input JSONB;
CREATE INDEX IF NOT EXISTS fullpage_documents_document_idx ON fullpage_documents (document_id);
CREATE TABLE IF NOT EXISTS fullpage_unknown_words (
    id          SERIAL PRIMARY KEY,
    request_id  TEXT NOT NULL,
    word        TEXT NOT NULL,
    kind        TEXT,
    occurrences INTEGER,
    pages       JSONB,
    sample      TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fullpage_unknown_words_request_idx ON fullpage_unknown_words (request_id);
CREATE INDEX IF NOT EXISTS fullpage_unknown_words_word_idx ON fullpage_unknown_words (word);
CREATE TABLE IF NOT EXISTS fullpage_corrections (
    id          SERIAL PRIMARY KEY,
    request_id  TEXT NOT NULL,
    page        INTEGER,
    line        INTEGER,
    wrong       TEXT,
    "right"     TEXT,
    fixed_by    TEXT,
    before_text TEXT,
    after_text  TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fullpage_corrections_request_idx ON fullpage_corrections (request_id);
"""

_SUMMARY = "request_id, document_id, s3_path, page_count, correction_count, unknown_count, notice, input, created_at"
_SUMMARY_KEYS = [c.strip() for c in _SUMMARY.split(",")]


def configured(settings: Settings) -> bool:
    return bool(settings.pg_host and settings.pg_db and settings.pg_user)


def connect(settings: Settings):
    import psycopg2

    return psycopg2.connect(
        host=settings.pg_host, port=settings.pg_port, dbname=settings.pg_db,
        user=settings.pg_user, password=settings.pg_password, connect_timeout=10,
    )


def save_result(settings: Settings, request_id: str, s3_path: str, document_id, result: dict) -> bool:
    """Lưu kết quả; False nếu chưa cấu hình DB."""
    if not configured(settings):
        log.info("[%s] POSTGRES_* not set; result not stored in a database", request_id)
        return False
    from psycopg2.extras import Json

    conn = connect(settings)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(_SCHEMA)
            cur.execute(
                """
                INSERT INTO fullpage_documents
                    (request_id, document_id, s3_path, page_count, text, pages, unknown_count, notice, correction_count, changes, input)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (request_id) DO UPDATE SET
                    document_id = EXCLUDED.document_id, s3_path = EXCLUDED.s3_path,
                    page_count = EXCLUDED.page_count, text = EXCLUDED.text, pages = EXCLUDED.pages,
                    unknown_count = EXCLUDED.unknown_count, notice = EXCLUDED.notice,
                    correction_count = EXCLUDED.correction_count, changes = EXCLUDED.changes, input = EXCLUDED.input
                """,
                (
                    request_id,
                    None if document_id is None else str(document_id),
                    s3_path,
                    result["page_count"],
                    result["text"],
                    Json(result["pages"]),
                    result["unknown_total"],
                    result["notice"],
                    result.get("correction_total", 0),
                    Json(result.get("changes", [])),
                    Json(result.get("input")),
                ),
            )
            cur.execute("DELETE FROM fullpage_unknown_words WHERE request_id = %s", (request_id,))
            for w in result["unknown_words"]:
                cur.execute(
                    "INSERT INTO fullpage_unknown_words (request_id, word, kind, occurrences, pages, sample) VALUES (%s, %s, %s, %s, %s, %s)",
                    (request_id, w["word"], w["kind"], w["count"], Json(w["pages"]), (w["samples"] or [""])[0]),
                )
            # Mỗi từ đã sửa một dòng, kèm dòng trước/sau
            cur.execute("DELETE FROM fullpage_corrections WHERE request_id = %s", (request_id,))
            for ch in result.get("changes", []):
                for fx in ch["fixes"]:
                    cur.execute(
                        'INSERT INTO fullpage_corrections (request_id, page, line, wrong, "right", fixed_by, before_text, after_text)'
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (request_id, ch["page"], ch["line"], fx["wrong"], fx["right"], fx["by"], ch["before"], ch["after"]),
                    )
    finally:
        conn.close()
    return True


def _summary(row) -> dict:
    doc = dict(zip(_SUMMARY_KEYS, row))
    doc["correction_count"] = doc["correction_count"] or 0
    if doc["created_at"] is not None:
        doc["created_at"] = doc["created_at"].isoformat()
    return doc


def list_documents(settings: Settings, limit: int, offset: int, document_id: str | None = None) -> tuple[list[dict], int]:
    """Danh sách tài liệu mới nhất trước, kèm tổng số."""
    where, params = ("WHERE document_id = %s", (document_id,)) if document_id else ("", ())
    conn = connect(settings)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(_SCHEMA)
            cur.execute(f"SELECT count(*) FROM fullpage_documents {where}", params)
            total = cur.fetchone()[0]
            cur.execute(f"SELECT {_SUMMARY} FROM fullpage_documents {where} ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s", params + (limit, offset))
            return [_summary(r) for r in cur.fetchall()], total
    finally:
        conn.close()


def get_document(settings: Settings, request_id: str) -> dict | None:
    """Một tài liệu đầy đủ; None nếu không có."""
    conn = connect(settings)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(_SCHEMA)
            cur.execute(f"SELECT {_SUMMARY}, pages, changes FROM fullpage_documents WHERE request_id = %s", (request_id,))
            row = cur.fetchone()
            if row is None:
                return None
            doc = _summary(row[: len(_SUMMARY_KEYS)])
            doc["pages"], doc["changes"] = row[-2] or [], row[-1] or []
            cur.execute(
                "SELECT word, kind, occurrences, pages, sample FROM fullpage_unknown_words WHERE request_id = %s ORDER BY id", (request_id,)
            )
            doc["unknown_words"] = [dict(zip(("word", "kind", "count", "pages", "sample"), r)) for r in cur.fetchall()]
            return doc
    finally:
        conn.close()


# Speech-to-text

_SPEECH_SCHEMA = """
CREATE TABLE IF NOT EXISTS speech_transcripts (
    id               SERIAL PRIMARY KEY,
    request_id       TEXT UNIQUE NOT NULL,
    document_id      TEXT,
    s3_path          TEXT,
    duration         REAL,
    segment_count    INTEGER,
    text             TEXT,
    segments         JSONB,
    correction_count INTEGER DEFAULT 0,
    changes          JSONB DEFAULT '[]',
    unknown_count    INTEGER,
    unknown_words    JSONB DEFAULT '[]',
    notice           TEXT,
    input            JSONB,
    created_at       TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS speech_transcripts_document_idx ON speech_transcripts (document_id);
CREATE TABLE IF NOT EXISTS speech_corrections (
    id          SERIAL PRIMARY KEY,
    request_id  TEXT NOT NULL,
    segment     INTEGER,
    start_sec   REAL,
    end_sec     REAL,
    wrong       TEXT,
    "right"     TEXT,
    fixed_by    TEXT,
    before_text TEXT,
    after_text  TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS speech_corrections_request_idx ON speech_corrections (request_id);
"""

_SPEECH_SUMMARY = "request_id, document_id, s3_path, duration, segment_count, correction_count, unknown_count, notice, input, created_at"
_SPEECH_KEYS = [c.strip() for c in _SPEECH_SUMMARY.split(",")]


def save_transcript(settings: Settings, request_id: str, s3_path: str, document_id, result: dict) -> bool:
    """Lưu bản chép lời; False nếu chưa cấu hình DB."""
    if not configured(settings):
        log.info("[%s] POSTGRES_* not set; transcript not stored in a database", request_id)
        return False
    from psycopg2.extras import Json

    conn = connect(settings)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(_SPEECH_SCHEMA)
            cur.execute(
                """
                INSERT INTO speech_transcripts
                    (request_id, document_id, s3_path, duration, segment_count, text, segments,
                     correction_count, changes, unknown_count, unknown_words, notice, input)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (request_id) DO UPDATE SET
                    document_id = EXCLUDED.document_id, s3_path = EXCLUDED.s3_path, duration = EXCLUDED.duration,
                    segment_count = EXCLUDED.segment_count, text = EXCLUDED.text, segments = EXCLUDED.segments,
                    correction_count = EXCLUDED.correction_count, changes = EXCLUDED.changes,
                    unknown_count = EXCLUDED.unknown_count, unknown_words = EXCLUDED.unknown_words,
                    notice = EXCLUDED.notice, input = EXCLUDED.input
                """,
                (
                    request_id,
                    None if document_id is None else str(document_id),
                    s3_path,
                    result["duration"],
                    result["segment_count"],
                    result["text"],
                    Json(result["segments"]),
                    result["correction_total"],
                    Json(result["changes"]),
                    result["unknown_total"],
                    Json(result["unknown_words"]),
                    result["notice"],
                    Json(result.get("input")),
                ),
            )
            # Mỗi từ đã sửa một dòng
            cur.execute("DELETE FROM speech_corrections WHERE request_id = %s", (request_id,))
            for ch in result["changes"]:
                for fx in ch["fixes"]:
                    cur.execute(
                        'INSERT INTO speech_corrections (request_id, segment, start_sec, end_sec, wrong, "right", fixed_by, before_text, after_text)'
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (request_id, ch["segment"], ch["start"], ch["end"], fx["wrong"], fx["right"], fx["by"], ch["before"], ch["after"]),
                    )
    finally:
        conn.close()
    return True


def _speech_summary(row) -> dict:
    doc = dict(zip(_SPEECH_KEYS, row))
    doc["correction_count"] = doc["correction_count"] or 0
    if doc["created_at"] is not None:
        doc["created_at"] = doc["created_at"].isoformat()
    return doc


def list_transcripts(settings: Settings, limit: int, offset: int, document_id: str | None = None) -> tuple[list[dict], int]:
    """Danh sách bản chép lời mới nhất trước, kèm tổng số."""
    where, params = ("WHERE document_id = %s", (document_id,)) if document_id else ("", ())
    conn = connect(settings)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(_SPEECH_SCHEMA)
            cur.execute(f"SELECT count(*) FROM speech_transcripts {where}", params)
            total = cur.fetchone()[0]
            cur.execute(
                f"SELECT {_SPEECH_SUMMARY} FROM speech_transcripts {where} ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                params + (limit, offset),
            )
            return [_speech_summary(r) for r in cur.fetchall()], total
    finally:
        conn.close()


def get_transcript(settings: Settings, request_id: str) -> dict | None:
    """Một bản chép lời đầy đủ; None nếu không có."""
    conn = connect(settings)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(_SPEECH_SCHEMA)
            cur.execute(
                f"SELECT {_SPEECH_SUMMARY}, segments, changes, unknown_words FROM speech_transcripts WHERE request_id = %s",
                (request_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            doc = _speech_summary(row[: len(_SPEECH_KEYS)])
            doc["segments"], doc["changes"], doc["unknown_words"] = (x or [] for x in row[-3:])
            return doc
    finally:
        conn.close()
