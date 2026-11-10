# OCR toàn trang và trích xuất lời nói (Chandra, PhoWhisper + từ điển + ProtonX)

Phiên bản mới của hệ thống OCR, dựng trên khung của `ocr-qldd` (Celery, RabbitMQ, PostgreSQL, webhook, quản lý worker).
Chỉ còn một luồng OCR toàn trang (`app/pipeline/fullpage.py`): OCR toàn trang bằng Chandra, sửa chính tả bằng **ProtonX + 3 bộ từ điển**,
**báo từ không có trong từ điển**. Các flow QLDD (chính quyền, chỉnh lý, phiếu tin, gcn, ubql), Detectron2 và Qwen đã bị bỏ;
dự án `ocr-qldd` gốc không bị chỉnh sửa gì và vẫn chứa đủ các flow đó.

```
POST /api/ocr-fullpage ──► RabbitMQ
                              │
   fullpage_prepare_queue     ▼  tải file từ MinIO/S3, render từng trang, chỉnh nghiêng, co ảnh
   fullpage_ocr_queue         ▼  Chandra (vLLM) đọc toàn trang → text
   fullpage_spell_queue       ▼  ProtonX sửa, từ điển kiểm duyệt → sửa chính tả → báo từ lạ
   fullpage_db_queue          ▼  lưu PostgreSQL + gửi webhook + dọn file tạm

POST /api/speech-to-text ──► RabbitMQ
                              │
   speech_prepare_queue       ▼  tải file âm thanh, ffmpeg đổi ra 16 kHz mono, cắt đoạn ≤ 28 giây ở chỗ lặng
   speech_asr_queue           ▼  PhoWhisper-large (vLLM) chép lời từng đoạn, gửi song song
   speech_spell_queue         ▼  ProtonX sửa, từ điển kiểm duyệt → sửa chính tả → báo từ lạ
   speech_db_queue            ▼  lưu PostgreSQL + gửi webhook + dọn file tạm
```

## Cấu trúc thư mục `app/`

```
app/
├── config.py            đọc cấu hình từ biến môi trường (Settings)
├── api/                 HTTP API
│   ├── main.py          FastAPI app: xác thực, các endpoint      → uvicorn app.api.main:create_app --factory
│   ├── documents.py     API cho web xem kết quả, trang, chỗ đã sửa
│   ├── transcripts.py   API cho web xem bản chép lời, đoạn, chỗ đã sửa
│   └── schemas.py       model request/response (pydantic)
├── worker/              Celery
│   ├── celery_app.py    Celery app, hàng đợi, định tuyến          → celery -A app.worker.celery_app worker
│   ├── tasks.py         4 tác vụ OCR: tải/render → OCR → chính tả → lưu DB + webhook
│   ├── speech_tasks.py  4 tác vụ speech: tải/giải mã → PhoWhisper → chính tả → lưu DB + webhook
│   └── manager.py       bật/tắt worker qua Flower
├── audio/               âm thanh
│   └── decode.py        ffmpeg → PCM 16 kHz mono, cắt đoạn ở chỗ lặng, bỏ đoạn im lặng
├── inputs/              file đầu vào
│   ├── select.py        phân loại file, chọn file văn bản / file cần OCR
│   ├── textfile.py      đọc docx, doc, odt, rtf, txt; đổi TCVN3; kiểm tra chất lượng
│   ├── pdftext.py       phát hiện lỗi Unicode trong lớp text PDF (có lỗi là OCR cả file)
│   └── collect.py       lấy từ S3: thử file văn bản trước, không được mới render để OCR; lấy file âm thanh
├── pipeline/            nghiệp vụ OCR toàn trang
│   ├── fullpage.py      ghép các bước: OCR từng trang, sửa chính tả, báo từ lạ, kết quả cuối
│   ├── speech.py        speech-to-text: chép lời từng đoạn, sửa chính tả, báo từ lạ, kết quả cuối
│   └── pages.py         đọc PDF/ảnh, render trang, chỉnh nghiêng, co ảnh
├── spell/               kiểm tra chính tả
│   ├── lexicon.py       3 bộ từ điển, sinh ứng viên
│   ├── checker.py       phát hiện từ lạ, ProtonX + kiểm duyệt bằng từ điển
│   ├── compare.py       so sánh trước/sau: dòng bị sửa, đánh dấu, đoạn tô màu
│   └── unknown.py       báo cáo từ không có trong từ điển
├── clients/             gọi dịch vụ bên ngoài qua HTTP
│   ├── http.py          POST có thử lại (dùng chung)
│   ├── chandra.py       Chandra OCR (vLLM), HTML → text
│   ├── phowhisper.py    PhoWhisper (vLLM, /v1/audio/transcriptions)
│   ├── protonx.py       client ProtonX
│   └── webhook.py       gửi kết quả tới callback_url
├── storage/             dữ liệu vào/ra
│   ├── s3.py            liệt kê và tải file từ S3/MinIO
│   ├── db.py            lưu kết quả vào PostgreSQL
│   └── files.py         dọn file tạm
└── services/            model server chạy thành tiến trình riêng
    └── spellfix_api.py  ProtonX trên GPU, cổng 8006              → uvicorn app.services.spellfix_api:app
```

## API

Mọi endpoint `/api/*` cần khóa: `X-API-Key: <khóa>` hoặc `Authorization: Bearer <khóa>`. `/health` không cần khóa.

| Endpoint | Mô tả |
|---|---|
| `POST /api/ocr-fullpage` | Nộp việc OCR toàn trang (file nằm trên MinIO/S3). Trả `202` + `request_id`. |
| `POST /api/ocr-fullpage/sync` | Tải file lên trực tiếp, nhận kết quả ngay (không qua hàng đợi, không lưu DB, không webhook). |
| `POST /api/speech-to-text` | Nộp việc trích xuất lời nói (file hoặc thư mục trên MinIO/S3). Trả `202` + `request_id`. Xem [Speech-to-text](#speech-to-text). |
| `POST /api/speech-to-text/sync` | Tải file âm thanh lên, nhận bản chép lời ngay. |
| `POST /api/uploads/ocr`, `POST /api/uploads/speech` | Tải **nhiều file / cả thư mục** từ máy lên rồi xếp hàng như S3. Xem [Tải file lên từ máy](#tải-file-lên-từ-máy). |
| `GET /api/transcripts`, `/{id}`, `/{id}/segments`, `/{id}/changes` | Bản chép lời đã lưu (cho web). |
| `POST /api/spellcheck` | Chỉ kiểm tra chính tả một đoạn text, kèm báo cáo từ lạ. |
| `GET /api/request-status/{id}` | Trạng thái; khi `SUCCESS` trả luôn kết quả. |
| `GET /api/documents` | Danh sách tài liệu đã lưu (cho web), xem [API cho web](#api-cho-web). |
| `GET /api/documents/{id}` | Tổng quan một tài liệu và từng trang. |
| `GET /api/documents/{id}/pages/{page}` | Mọi dòng của một trang, dòng bị sửa có bản trước và đoạn tô màu. |
| `GET /api/documents/{id}/changes` | Các dòng đã sửa, lọc theo `page`, `by`. |
| `GET /api/workers/status\|presets`, `POST /api/workers/start\|stop\|restart` | Quản lý worker qua Flower. |
| `GET /health` | Kiểm tra sống. |

```bash
curl -H "X-API-Key: $KEY" -H "Content-Type: application/json" localhost:9090/api/ocr-fullpage -d '{
  "s3_path": "s3://bucket/thu-muc/hoso.pdf",
  "access_key": "...", "secret_key": "...", "endpoint": "http://minio:9000",
  "callback_url": "https://noi-nhan-ket-qua.example/hook",
  "document_id": 12345
}'
# → {"error_code":202,"error_message":"...","request_id":"3f2a..."}

curl -H "X-API-Key: $KEY" -F file=@hoso.pdf localhost:9090/api/ocr-fullpage/sync
```
`access_key`, `secret_key`, `endpoint` có thể bỏ; khi đó dùng `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_ENDPOINT` trong `.env`.

### File đầu vào: ưu tiên text có sẵn, không có mới OCR

Nhận PDF, ảnh (`jpg, png, tif, bmp, webp`) và file văn bản (`docx, doc, odt, rtf, txt, md`).
`s3_path` có thể là:

- **Một file**, vd `s3://bucket/sach/a.pdf`: lấy thêm các file **cùng tên khác đuôi** trong cùng thư mục (`a.docx`, `a.txt`...).
  Tắt bằng `INPUT_SIBLINGS=false`. Không có quyền liệt kê bucket thì chỉ lấy đúng file đó.
- **Một thư mục** (kết thúc bằng `/`), vd `s3://bucket/sach/`: xét mọi file ngay trong thư mục (không vào thư mục con).

Thứ tự xử lý:

1. Có file văn bản thì tải và đọc lần lượt theo ưu tiên `docx > doc > odt > rtf > txt > md` (file trùng tên với file chỉ định
   được thử trước). File đầu tiên đọc được và qua kiểm tra chất lượng thì **dùng luôn, không OCR**.
   - docx: chia trang theo ngắt trang của Word, gồm cả bảng và text box; đổi font TCVN3 (`.VnTime`...) sang Unicode.
   - doc: `antiword` → `catdoc` → LibreOffice; odt/rtf: LibreOffice. txt: thử UTF-8, UTF-16, cp1258, rồi đổi TCVN3 nếu cần.
   - Không có ngắt trang thì chia trang ảo khoảng `TEXT_PAGE_CHARS` ký tự (theo dòng).
   - Bỏ file nếu ít hơn 50% ký tự là chữ cái hoặc còn sót ký tự TCVN3 (nghi lỗi mã hoá); lý do ghi trong `input.skipped`.
2. Không có file văn bản dùng được thì xử lý PDF/ảnh: chỉ định file PDF/ảnh thì xử lý đúng file đó; thư mục thì **mọi PDF**
   (nối trang theo tên), không có PDF thì **mọi ảnh** (mỗi ảnh một trang, sắp `trang2` trước `trang10`).
   PDF có lớp text thì lấy text trực tiếp, xem [Lớp text của PDF](#lớp-text-của-pdf).
3. Không có gì để OCR mà file văn bản kém chất lượng thì vẫn dùng tạm, kèm `input.warning`.

Text lấy từ file văn bản mặc định **không** qua sửa chính tả (văn bản gõ máy), nhưng vẫn báo từ lạ; bật `SPELL_TEXT_FILES=true`
nếu muốn sửa. `TEXT_FIRST=false` thì luôn OCR khi có PDF/ảnh. `/sync` cũng nhận file văn bản (nhận diện theo đuôi tên file).

### Lớp text của PDF

PDF xuất từ Word có sẵn text, lấy trực tiếp nhanh hơn OCR nhiều. Nhưng PDF scan "2 lớp" thường có lớp text kém,
nên **chỉ cần một trang có lỗi Unicode là bỏ lớp text của cả file, OCR toàn bộ**. Các lỗi bắt được:

| Lỗi | Ví dụ |
|---|---|
| Font TCVN3 (.VnTime...) | `Th− viÖn Quèc gia ViÖt Nam` |
| Font VNI | `Coäng hoøa xaõ hoäi chuû nghóa Vieät Nam` |
| Ký tự thay thế | `Vi�t Nam` |
| Mã glyph, font thiếu bảng Unicode | `(cid:68)(cid:72)`, ký tự vùng riêng U+E000–F8FF |
| Ký tự điều khiển | `Vi\x07t` |
| Dấu tách khỏi chữ | `đê ́n`, `Vie^.t` |
| Mất dấu, vỡ chữ, text scan kém | tỉ lệ từ có trong từ điển trên cả file dưới `PDF_TEXT_MIN_KNOWN` (0.9) |

Tỉ lệ từ điển tính trên cả file (bỏ số và viết tắt; cần ít nhất 50 từ) để vài trang tài liệu tham khảo tiếng Anh không làm
hỏng cả file; PDF xuất từ Word thường đạt 0.97–0.999. Trang trống hoặc dưới 30 chữ cái (trang ảnh, trang scan) không phải lỗi:
chỉ OCR riêng trang đó. Lớp text lỗi cũng không được dùng làm dự phòng khi Chandra lỗi. Trang lấy từ text PDF có
`source: "pdf_text"`, không render ảnh và (như file văn bản) mặc định không sửa chính tả. Tắt hẳn bằng `PDF_TEXT_FIRST=false`.

Kết quả ghi trong `input.pdf_text`:
```json
"pdf_text": [{"file": "a.pdf", "text_pages": 0, "ocr_pages": 155, "reason": "trang 1: có 42 ký tự font TCVN3 (.Vn*)"}]
```

Kết quả có thêm trường `input`, trang lấy từ file văn bản có `source: "text_file"`:
```json
"input": {"mode": "text", "files": ["a.docx"], "skipped": [{"file": "a.txt", "reason": "chỉ 20% ký tự là chữ cái, nghi lỗi mã hoá"}]}
```

Kết quả (webhook trường `data`, `result` của `/api/request-status`, và phản hồi của `/sync`):

```json
{
  "page_count": 2,
  "text": "toàn bộ văn bản, các trang cách nhau một dòng trống",
  "pages": [{"page": 1, "text": "...", "source": "chandra",
             "corrections": [{"line": 3, "wrong": "đưoc", "right": "được", "by": "protonx"}], "error": null}],
  "correction_total": 1,
  "changes": [{"page": 1, "line": 3,
               "before": "Người đưoc giao đất", "after": "Người được giao đất",
               "marked": "Người [đưoc→được] giao đất",
               "segments": [{"type": "same", "text": "Người"}, {"type": "fix", "before": "đưoc", "after": "được", "by": "protonx"},
                            {"type": "same", "text": "giao đất"}],
               "fixes": [{"wrong": "đưoc", "right": "được", "by": "protonx"}]}],
  "unknown_words": [{"word": "TTr", "kind": "vietnamese", "count": 1, "pages": [1],
                     "samples": ["...số 123/TTr-STNMT..."], "suggestions": ["tt"]}],
  "unknown_total": 1,
  "notice": "Phát hiện 1 từ không có trong từ điển: TTr.",
  "input": {"mode": "ocr", "files": ["hoso.pdf"], "skipped": []},
  "elapsed_ms": 4210
}
```

`changes` để đối chiếu: mỗi dòng bị sửa có trang, số dòng (trong trang), dòng trước (`before`), dòng sau (`after`),
bản đánh dấu `[cũ→mới]` (`marked`) và các từ đã sửa (`fixes`, `by` là `protonx` / `dict` / `replace`).
`/api/spellcheck` cũng trả `changes`. Khi có PostgreSQL, mỗi từ đã sửa được lưu một dòng trong bảng `fullpage_corrections`
(`page, line, wrong, right, fixed_by, before_text, after_text`).

Webhook thành công: `{"meta": {"error_code": 200, ...}, "data": {path, document_id, ...kết quả trên}, "request_id": ...}`.
Webhook lỗi: `meta.error_code` là 404/403/413/415/422/502/500 kèm thông điệp chung (không lộ chi tiết nội bộ).

## Tải file lên từ máy

Thay cho S3: frontend (thư mục `../frontend`) gửi file lên đây. File được lưu vào `UPLOAD_DIR`
(mặc định `TEMP_DIR/uploads`), rồi chạy đúng 4 bước Celery như S3 với đường dẫn `upload://<id>/`. Phần chọn file
(ưu tiên Word/txt, thư mục ảnh, thư mục âm thanh) giống hệt S3. Xong bước đầu thì file tải lên bị xóa.

```bash
# Nhiều file, mỗi file một việc
curl -H "X-API-Key: $KEY" -F files=@a.pdf -F files=@b.docx localhost:9090/api/uploads/ocr
# Cả thư mục ảnh là một tài liệu (ảnh là các trang theo tên)
curl -H "X-API-Key: $KEY" -F group=true $(for f in scan/*.jpg; do printf -- '-F files=@%s ' "$f"; done) localhost:9090/api/uploads/ocr
# Chép lời, nối các file theo tên
curl -H "X-API-Key: $KEY" -F group=true -F files=@phan1.m4a -F files=@phan2.m4a localhost:9090/api/uploads/speech
# → 202 {"jobs": [{"request_id": "3f2a...", "files": ["phan1.m4a", "phan2.m4a"]}], "skipped": []}
```

| Trường form | Mặc định | Ý nghĩa |
|---|---|---|
| `files` | | Một hoặc nhiều file; tên có thể kèm thư mục (`ho-so/trang1.jpg`), chỉ giữ tên file |
| `group` | `false` | `true`: cả lô là một việc (như thư mục S3); `false`: mỗi file một việc |
| `spellcheck` | `true` | Sửa chính tả |

File không hỗ trợ được liệt kê trong `skipped`. Giới hạn: `MAX_UPLOAD_FILES` file mỗi lần, mỗi file `MAX_UPLOAD_MB` (OCR) hoặc
`SPEECH_MAX_UPLOAD_MB` (âm thanh). Theo dõi bằng `GET /api/request-status/{request_id}`.
API và worker phải dùng chung ổ đĩa `UPLOAD_DIR` (cùng máy là đủ).

## API cho web

Đọc kết quả đã lưu trong PostgreSQL (`POSTGRES_*`; chưa cấu hình thì trả `503`). `{id}` là `request_id`.
Cần API key như các endpoint khác: web nên gọi qua backend của mình, không để key trong trình duyệt; nếu gọi thẳng thì đặt `CORS_ORIGINS`.

**`GET /api/documents?limit=20&offset=0&document_id=42`** — danh sách, mới nhất trước:
```json
{"total": 1, "limit": 20, "offset": 0,
 "items": [{"request_id": "3f2a...", "document_id": "42", "s3_path": "s3://b/a.pdf", "page_count": 2,
            "correction_count": 1, "unknown_count": 1, "notice": "...",
            "input": {"mode": "text", "files": ["a.docx"], "skipped": []}, "created_at": "2026-11-03T09:30:00+07:00"}]}
```

**`GET /api/documents/{id}`** — tổng quan, số chỗ sửa và từ lạ theo trang (để vẽ danh sách trang):
```json
{"request_id": "3f2a...", "page_count": 2, "correction_count": 1, "unknown_count": 1, "...": "...",
 "pages": [{"page": 1, "source": "chandra", "error": null, "correction_count": 1, "unknown_count": 0}],
 "unknown_words": [{"word": "qwxzv", "kind": "vietnamese", "count": 1, "pages": [2], "sample": "..."}]}
```

**`GET /api/documents/{id}/pages/{page}`** — toàn bộ dòng của trang, để hiển thị song song trước/sau:
```json
{"page": 1, "page_count": 2, "correction_count": 1,
 "lines": [
   {"line": 1, "text": "Dòng đầu", "changed": false},
   {"line": 2, "text": "Người được giao đất", "changed": true,
    "before": "Người đưoc giao đất",
    "segments": [{"type": "same", "text": "Người"},
                 {"type": "fix", "before": "đưoc", "after": "được", "by": "protonx"},
                 {"type": "same", "text": "giao đất"}],
    "fixes": [{"wrong": "đưoc", "right": "được", "by": "protonx"}]}],
 "unknown_words": []}
```
- `text` là dòng sau khi sửa; dòng bị sửa có thêm `before` (dòng gốc OCR).
- `segments` để tô màu: đoạn `same` giữ nguyên, đoạn `fix` có `before` / `after` / `by`; nối các đoạn bằng **một dấu cách**.
  Ví dụ cột trái hiện `before` gạch đỏ, cột phải hiện `after` tô xanh.

**`GET /api/documents/{id}/changes?page=1&by=protonx&limit=50&offset=0`** — danh sách các dòng đã sửa (bảng đối chiếu),
mỗi mục giống một phần tử `changes` của kết quả (`page, line, before, after, marked, segments, fixes`). `by`: `protonx` / `dict` / `replace`.

Kết quả của `/api/ocr-fullpage/sync` và `/api/spellcheck` cũng có `changes` với `segments`, nên web dùng được ngay mà không cần DB.

## Speech-to-text

Chép lời file âm thanh bằng `vinai/PhoWhisper-large` (Whisper large-v2 tinh chỉnh cho tiếng Việt) chạy trên vLLM, rồi sửa chính tả
bằng ProtonX + 3 bộ từ điển như OCR. Nhận `mp3, m4a, m4b, aac, wav, flac, ogg, oga, opus, wma, amr, aiff` và cả video
`mp4, webm, mkv, mov` (lấy tiếng).

```bash
curl -H "X-API-Key: $KEY" -H "Content-Type: application/json" localhost:9090/api/speech-to-text -d '{
  "s3_path": "s3://bucket/ghi-am/hop-ngay-03.m4a",
  "callback_url": "https://noi-nhan-ket-qua.example/hook", "document_id": 12345
}'
curl -H "X-API-Key: $KEY" -F file=@hop.m4a localhost:9090/api/speech-to-text/sync
```
`s3_path` là một file, hoặc một thư mục (kết thúc bằng `/`): mọi file âm thanh trong thư mục, xử lý theo tên (`phan2` trước `phan10`).
Khóa S3 có thể bỏ, giống OCR. Trạng thái xem ở `GET /api/request-status/{id}` như OCR.

Cách làm (theo `transcribe.py` của `aside`):

1. ffmpeg đổi file ra PCM 16 kHz mono (ghi ra đĩa, không nạp cả file vào RAM). Không có ffmpeg thì chỉ đọc được `wav`.
2. Cắt đoạn dài `SPEECH_MIN_CHUNK`–`SPEECH_MAX_CHUNK` giây (15–28) ở **chỗ lặng nhất**, vì Whisper chỉ nghe tối đa 30 giây.
   Đoạn có biên độ không vượt `SPEECH_SILENCE_LEVEL` bị bỏ để Whisper không bịa chữ.
3. Gửi song song `ASR_CONCURRENCY` đoạn lên `/v1/audio/transcriptions` (`temperature 0`, `language vi`). Lỗi mạng / 5xx thử lại,
   4xx báo luôn. Đoạn lỗi có `error`; mọi đoạn đều lỗi thì trả 502.
4. Sửa chính tả: mỗi đoạn là một dòng. Whisper ra âm tiết đúng chính tả nhưng có thể sai từ, nên mặc định
   `SPEECH_SPELL_REVIEW=all`: ProtonX xem **mọi đoạn**, không chỉ đoạn có từ lạ. Từ điển vẫn kiểm duyệt mọi chỗ ProtonX sửa.

Kết quả:
```json
{
  "duration": 42.0,
  "segment_count": 2,
  "text": "mỗi đoạn một dòng",
  "segments": [{"index": 1, "file": "hop.m4a", "start": 0.0, "end": 21.0, "text": "Việc thực hiện nghị quyết",
                "corrections": [{"wrong": "thưc", "right": "thực", "by": "protonx"}], "error": null}],
  "correction_total": 1,
  "changes": [{"segment": 1, "file": "hop.m4a", "start": 0.0, "end": 21.0,
               "before": "Việc thưc hiện nghị quyết", "after": "Việc thực hiện nghị quyết",
               "marked": "Việc [thưc→thực] hiện nghị quyết", "segments": [...], "fixes": [...]}],
  "unknown_words": [{"word": "...", "kind": "foreign", "count": 1, "segments": [1], "samples": ["..."]}],
  "unknown_total": 1,
  "notice": "...",
  "input": {"mode": "speech", "files": [{"file": "hop.m4a", "duration": 42.0}]},
  "elapsed_ms": 9100
}
```
`start`, `end` tính bằng giây trong từng file. File không có tiếng nói nào trả `text` rỗng và `notice`.
Giới hạn: `SPEECH_MAX_MINUTES` (tổng thời lượng) và `SPEECH_MAX_UPLOAD_MB` (mỗi file).

API cho web (cần PostgreSQL; bảng `speech_transcripts`, mỗi từ đã sửa một dòng trong `speech_corrections` kèm thời gian):

| Endpoint | Trả |
|---|---|
| `GET /api/transcripts?limit&offset&document_id` | Danh sách, mới nhất trước |
| `GET /api/transcripts/{id}` | Tổng quan: thời lượng, số đoạn, số chỗ sửa, từ lạ |
| `GET /api/transcripts/{id}/segments?file&changed&limit&offset` | Các đoạn theo thời gian; đoạn bị sửa có `before`, `segments` (tô màu), `fixes` |
| `GET /api/transcripts/{id}/changes?file&by&limit&offset` | Các đoạn đã sửa, lọc theo file hoặc cách sửa |

## Báo từ không có trong từ điển

Sau khi OCR **và** sửa chính tả xong, mọi từ còn lại không có trong bộ từ điển đã nạp đều được báo ở 4 nơi:
1. trường `unknown_words` + `notice` trong kết quả và webhook;
2. log cảnh báo của worker (`Phát hiện N từ không có trong từ điển: ...`);
3. bảng PostgreSQL `fullpage_unknown_words` (mỗi từ một dòng: `word, kind, occurrences, pages, sample`) và `fullpage_documents.notice`;
4. mỗi từ kèm số lần xuất hiện, các trang, câu ví dụ và (với từ tiếng Việt) tối đa 3 gợi ý.

`kind`: `vietnamese` (chữ Việt nhưng không có trong từ điển), `foreign` (có f/j/w/z hoặc chữ viết khác), `acronym` (viết tắt in hoa ≤6 ký tự không dấu như `UBND`, `QSDĐ`).
Lọc bằng `UNKNOWN_INCLUDE`, tắt hẳn bằng `UNKNOWN_REPORT=false`. Từ được coi là *đã biết* nếu âm tiết có trong **bất kỳ** bộ từ điển nào đang bật.

## Ba bộ từ điển và kiểm tra chính tả

`data/dictionaries/` chứa nguyên văn 3 bộ từ điển mở (Hồ Ngọc Đức, tudientv, wiktionary — nguồn
[undertheseanlp/dictionary](https://github.com/undertheseanlp/dictionary)). Chọn bộ nào dùng bằng `DICTIONARIES`
(mặc định cả ba). Khi nạp, các mục chứa chữ số, dấu gạch nối, dấu câu hoặc chữ ngoài bảng chữ cái tiếng Việt bị bỏ;
phần còn lại thành ~9 nghìn âm tiết và ~76 nghìn cặp âm tiết liền nhau.

1. Âm tiết **không có trong từ điển** là *nghi vấn*. Ứng viên sinh bằng cách **chỉ đổi dấu** (`thưc` → thực/thức/thúc/thục)
   hoặc sai thêm một chữ cái, xếp hạng theo tần suất và việc ghép với từ bên cạnh có trong từ điển hay không.
2. Các dòng có từ nghi vấn được gửi (theo lô) tới **ProtonX** (`protonx-models/protonx-legal-tc`, model seq2seq sửa chính tả văn bản
   hành chính, dịch vụ `app/services/spellfix_api.py`, cổng 8006, gọi qua client `app/clients/protonx.py`). ProtonX trả lại cả dòng đã sửa (`đưoc` → được).
3. Dòng trả về được so khớp từng âm tiết với dòng gốc; chỉ nhận các âm tiết **bị thay một-đổi-một** và hợp lệ: với từ lạ, từ mới phải là
   **một ứng viên của từ điển**; với từ vốn đã đúng, chỉ được đổi dấu. Mọi thay đổi khác (thêm/bớt/gộp từ, đổi số, mã, ngày tháng,
   dấu câu, từ viết tắt, từ dưới 3 chữ cái) bị bỏ qua — chữ gốc giữ nguyên.
4. Từ nghi vấn mà ProtonX không sửa (hoặc sửa không hợp lệ), và cả dòng khi ProtonX lỗi/không phản hồi → dùng quy tắc từ điển thuần
   (chỉ sửa khi có bằng chứng ngữ cảnh rõ ràng). `corrections[].by` cho biết `protonx`, `dict` hay `replace`.

`SPELL_MODE`: `off` / `dict` (không cần ProtonX) / `both` (mặc định). `SPELL_REVIEW=all` cho ProtonX duyệt cả dòng không có từ lạ
để bắt lỗi sai dấu nhưng vẫn là từ có nghĩa (chậm hơn). Tuỳ biến: `EXTRA_WORDS_FILE` (từ riêng coi là đúng, mỗi dòng một từ),
`REPLACEMENTS_FILE` (CSV `sai,dung` áp dụng trước; `data/spell_dict.csv` là bản lấy từ QLDD, đổi "ủy ban nhân dân" thành `UBND`
nên không bật mặc định).

## Triển khai

Cần RabbitMQ, PostgreSQL, Chandra (vLLM) và ProtonX chạy sẵn (hoặc để `start-service.sh` bật ProtonX):
```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo apt install antiword catdoc libreoffice-writer-nogui   # đọc .doc / .odt / .rtf
sudo apt install ffmpeg                                      # giải mã âm thanh cho speech-to-text
# Sửa .env: điền API_KEYS, CELERY_BROKER_URL, CELERY_RESULT_BACKEND, POSTGRES_*; sửa CHANDRA_URL, SPELLFIX_API_URL
python -c "import secrets; print(secrets.token_urlsafe(32))"   # dùng để tạo khóa/mật khẩu (an toàn cho URL)
VENV_BIN=$PWD/.venv/bin ./start-service.sh
# ProtonX trong cùng venv: .venv/bin/pip install -r requirements-spellfix.txt, rồi START_SPELLFIX=true (nghe 127.0.0.1:8006)
# Chandra trong cùng venv (cần vllm): START_CHANDRA=true (nghe 127.0.0.1:CHANDRA_PORT)
# PhoWhisper trong cùng venv (cần pip install "vllm[audio]"): START_PHOWHISPER=true (nghe 127.0.0.1:PHOWHISPER_PORT)
./stop-service.sh
```
Chọn card GPU (số thứ tự trong `nvidia-smi`, vd `0`, `1` hoặc `0,1`): ProtonX bằng `SPELLFIX_CUDA_DEVICE` trong `.env`; Chandra và PhoWhisper bằng `CUDA_VISIBLE_DEVICES` trong file service.
Áp dụng cho `start-service.sh` (đọc `.env`). Các file systemd trong `service/` ghi **giá trị cố định**, không đọc `.env`:
đổi card, cổng, model thì sửa thẳng trong file service.
Chỉ khi bật model bằng `start-service.sh` (`START_CHANDRA=true`, `START_PHOWHISPER=true`) mới cần thêm vào `.env` các biến
tham số vLLM (không có thì script dùng mặc định); ví dụ với Chandra: `CHANDRA_PORT`, `CHANDRA_MAX_MODEL_LEN`, `CHANDRA_GPU_MEMORY_UTILIZATION`,
`CHANDRA_MAX_NUM_BATCHED_TOKENS`, `CHANDRA_MAX_NUM_SEQS`, `CHANDRA_PREFIX_CACHING`, `CHANDRA_VLLM_EXTRA_ARGS` (cờ khác); đặt `CHANDRA_API_KEY`
thì vLLM bắt buộc khóa đó và worker tự gửi kèm.
Chạy bằng đúng user sở hữu thư mục dự án (script sẽ báo lỗi rõ ràng nếu `logs/` hoặc `temp_processing/` không ghi được).
Mẫu systemd (giá trị cố định):

| File | Chạy |
|---|---|
| `service/ocr-fullpage.service` | `start-service.sh` / `stop-service.sh` trong `/opt/ocr-fullpage/backend` |
| `service/vllm-chandra.service` | `datalab-to/chandra-ocr-2`, GPU 0, cổng 8101, max-model-len 6000, VRAM 0.95, batched tokens 9216, 20 seqs, prefix caching |
| `service/vllm-phowhisper.service` | `vinai/PhoWhisper-large` (tên gọi `phowhisper-large`), GPU 0, cổng 8120, fp16 (khoảng 3 GB VRAM), VRAM 0.8, 64 seqs |

Cổng phải khớp `CHANDRA_URL` và `PHOWHISPER_URL` trong `.env`.
API ở cổng `API_PORT` (mặc định 9090). Worker mặc định: prepare 2, ocr 2, spell 2, db 2 (đổi bằng `PREPARE_WORKERS`, `OCR_WORKERS`, `SPELL_WORKERS`, `DB_WORKERS`);
speech mỗi hàng đợi 1 (`SPEECH_PREPARE_WORKERS`, `SPEECH_ASR_WORKERS`, `SPEECH_SPELL_WORKERS`, `SPEECH_DB_WORKERS`), log ở `logs/speech/`.
RabbitMQ, PostgreSQL và Flower nên chỉ nghe `127.0.0.1` (Flower do `start-service.sh` bật đã như vậy).

## Bảo mật

- Không có mật khẩu/khóa nào trong mã nguồn; tất cả đọc từ biến môi trường. Không đưa `.env` vào git (chứa mật khẩu).
  Thiếu `API_KEYS` thì dịch vụ từ chối khởi động (trừ khi `AUTH_DISABLED=true`, chỉ dùng khi phát triển).
- Khóa MinIO/S3 lấy từ request, thiếu thì lấy `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` trong `.env` (khóa trong `.env` không đi qua RabbitMQ).
  Chỉ dùng để tải file, không lưu vào DB, không ghi log, không trả lại trong phản hồi/lỗi 422.
  Lưu ý: khóa gửi trong request nằm trong thông điệp RabbitMQ của tác vụ đầu tiên và **Flower hiển thị tham số tác vụ** — luôn đặt `FLOWER_BASIC_AUTH`
  và để Flower/RabbitMQ chỉ nghe `127.0.0.1`.
- `callback_url` chỉ nhận http(s); đặt `WEBHOOK_ALLOWED_HOSTS` để giới hạn máy nhận (tránh bị dùng để gọi dịch vụ nội bộ).
- `/docs` tắt mặc định; thông điệp lỗi trả ra ngoài là thông điệp chung. Chạy bằng user riêng, không phải root.
- Đặt dịch vụ sau reverse proxy có TLS; bản thân nó chỉ nói HTTP.

## Kiểm thử

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t . -v
```
58 test, chạy Chandra/ProtonX giả lập (httpx MockTransport) và Celery ở chế độ eager: đủ chuỗi 4 tác vụ, webhook, dọn file tạm,
xác thực, fallback, giới hạn file, không lộ khóa, chống "bịa từ" của ProtonX, báo từ lạ, lưu DB (kết nối giả).

**Chưa kiểm chứng:** chạy với Chandra/ProtonX thật trên GPU, PostgreSQL/RabbitMQ thật. Nên chạy thử vài tài liệu thật qua `/sync`
và xem `corrections`, `unknown_words` trước khi đưa vào sản xuất.

## Lưu ý giấy phép

`data/dictionaries/` là dữ liệu của dự án *Từ điển mở tiếng Việt* theo **GPL-3.0** (`data/LICENSE-dictionary-GPL-3.0.txt`).
Hãy kiểm tra điều khoản này có phù hợp khi phân phối dịch vụ ra ngoài tổ chức.
