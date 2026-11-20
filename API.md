# Tài liệu tích hợp API OCR / Speech-to-text (dành cho lập trình viên phía client)

Tài liệu này viết cho người lập trình hệ thống gọi đến backend OCR. Mỗi API có đủ năm phần:

1. Dùng khi nào.
2. Phía client gửi gì: method, URL, header.
3. Body hoặc form đầy đủ, kèm kiểu dữ liệu, bắt buộc hay không, giá trị mặc định.
4. Ví dụ gọi.
5. JSON backend trả về: đầy đủ khi thành công, và khi lỗi.

---

## Phần A. Thông tin chung

### A.1. Địa chỉ và xác thực

| Mục | Giá trị |
|---|---|
| Địa chỉ gốc (`BASE_URL`) | `http://<backend-host>:9090`, do phía backend cung cấp |
| Tài khoản | **tên đăng nhập** và **mật khẩu** do phía backend cấp; gửi trong **mọi** request (trừ `/health`) |
| Cách gửi | HTTP Basic: header `Authorization: Basic <base64 của "tên_đăng_nhập:mật_khẩu">` |
| Mã hoá | UTF-8 cho cả request và response |
| Gọi trực tiếp từ trình duyệt | báo phía backend thêm domain web của bạn vào danh sách CORS |

Hầu hết thư viện HTTP có sẵn tùy chọn đăng nhập kiểu Basic, không cần tự mã hoá base64:

```bash
curl -u "<username>:<password>" "$BASE_URL/api/workers/presets"
```

```js
// Trình duyệt / Node.js 18+
const AUTH = "Basic " + btoa(`${USERNAME}:${PASSWORD}`);
const res = await fetch(`${BASE_URL}/api/documents`, { headers: { Authorization: AUTH } });
```

```python
import requests
r = requests.get(f"{BASE_URL}/api/documents", auth=("<username>", "<password>"))
```

Lưu ý:
- Mật khẩu chỉ gồm ký tự không dấu (ASCII); `@`, `:` và các ký tự đặc biệt khác vẫn dùng được.
- Thiếu hoặc sai tên đăng nhập / mật khẩu thì backend trả `401` kèm header `WWW-Authenticate: Basic realm="ocr"`.
- Header `X-API-Key` của phiên bản cũ không còn được nhận.
- Nên gọi qua HTTPS khi đi qua mạng ngoài, vì Basic chỉ mã hoá base64, không phải mã hoá bảo mật.

### A.2. Hai kiểu gửi dữ liệu

| Kiểu | Header | Dùng cho |
|---|---|---|
| JSON | `Content-Type: application/json` | API nhận đường dẫn file trên S3/MinIO, API sửa chính tả |
| Form tải file | `multipart/form-data` (thư viện HTTP tự đặt header, kèm `boundary`) | API tải file từ máy lên |

Với form, giá trị đúng/sai gửi dạng chuỗi `"true"` hoặc `"false"`.

### A.3. Hai cách nhận kết quả

**Bất đồng bộ (khuyên dùng):**
1. Gửi việc.
2. Backend trả ngay một `request_id` (HTTP `202`).
3. Lấy kết quả bằng một trong hai cách:
   - client hỏi lại định kỳ bằng `GET /api/request-status/{request_id}` (mục B.4);
   - backend tự gửi kết quả tới `callback_url` của client (webhook, mục B.5).

Kết quả cũng được lưu lại, có thể đọc về sau (phần E).

**Đồng bộ (`.../sync`):**
- Gửi file và chờ đến khi xử lý xong; kết quả nằm luôn trong response.
- Chỉ nên dùng cho file nhỏ, vài trang hoặc vài phút audio. Đặt thời gian chờ (timeout) của client ít nhất 10 phút.

### A.4. Mẫu JSON khi lỗi

Mọi lỗi HTTP trả về cùng một dạng:

```json
{
  "detail": "Unauthorized"
}
```

Riêng lỗi `422` (dữ liệu gửi lên sai kiểu hoặc thiếu trường), `detail` là một danh sách:

```json
{
  "detail": [
    {
      "loc": ["body", "s3_path"],
      "msg": "Value error, s3_path must look like s3://bucket/path/to/file.pdf or s3://bucket/folder/",
      "type": "value_error"
    }
  ]
}
```

| Mã HTTP | Khi nào |
|---|---|
| `200` | thành công (API đọc, API đồng bộ) |
| `202` | đã nhận việc, đang xử lý (API bất đồng bộ) |
| `400` | thiếu thông tin S3, hoặc `callback_url` không thuộc danh sách được phép |
| `401` | thiếu hoặc sai tên đăng nhập / mật khẩu |
| `404` | không tìm thấy (`request_id`, trang...) |
| `413` | file quá lớn, quá nhiều trang hoặc file, audio quá dài |
| `415` | định dạng file không hỗ trợ |
| `422` | dữ liệu gửi lên sai kiểu hoặc thiếu trường bắt buộc |
| `502` | model OCR hoặc nhận dạng giọng nói tạm thời không phản hồi; thử lại sau |
| `503` | phía backend chưa bật cơ sở dữ liệu (chỉ gặp ở các API ở phần E) |

### A.5. Quy ước chung

- `request_id`: chuỗi 32 ký tự hex do backend sinh, ví dụ `3f9a1c7e2b8d4e6fa0c5b1d2e3f4a5b6`.
- `document_id`: mã tài liệu **của phía client** (số hoặc chuỗi). Backend không dùng nó để xử lý mà chỉ trả lại, để client ghép kết quả với dữ liệu của mình.
- Thời gian (`created_at`) theo ISO 8601, múi giờ UTC.
- Client nên **bỏ qua trường lạ**: các phiên bản sau có thể thêm trường mới vào JSON trả về.

### A.6. Danh sách API

| # | Method | Đường dẫn | Việc |
|---|---|---|---|
| B.1 | POST | `/api/ocr-fullpage` | OCR toàn văn tài liệu trên S3 (bất đồng bộ) |
| B.2 | POST | `/api/book-metadata` | lấy thông tin xuất bản của sách trên S3 (bất đồng bộ) |
| B.3 | POST | `/api/speech-to-text` | chép lời audio/video trên S3 (bất đồng bộ) |
| B.4 | GET | `/api/request-status/{request_id}` | hỏi trạng thái và lấy kết quả |
| B.5 | (backend gọi client) | `callback_url` | webhook trả kết quả |
| C.1 | POST | `/api/uploads/ocr` | tải file lên rồi OCR (bất đồng bộ) |
| C.2 | POST | `/api/uploads/metadata` | tải file sách lên, lấy thông tin xuất bản (bất đồng bộ) |
| C.3 | POST | `/api/uploads/speech` | tải audio lên rồi chép lời (bất đồng bộ) |
| D.1 | POST | `/api/ocr-fullpage/sync` | OCR một file, trả kết quả ngay |
| D.2 | POST | `/api/speech-to-text/sync` | chép lời một file, trả kết quả ngay |
| D.3 | POST | `/api/book-metadata/sync` | thông tin xuất bản, trả kết quả ngay |
| D.4 | POST | `/api/spellcheck` | sửa chính tả một đoạn văn bản |
| E.1–E.4 | GET | `/api/documents...` | đọc kết quả OCR đã lưu |
| E.5–E.8 | GET | `/api/transcripts...` | đọc bản chép lời đã lưu |
| E.9–E.10 | GET | `/api/book-metadata...` | đọc thông tin xuất bản đã lưu |
| F.1 | GET | `/health` | kiểm tra backend còn sống |

---

## Phần B. Xử lý file nằm trên S3 / MinIO (bất đồng bộ)

Dùng khi file đã nằm trên kho S3/MinIO mà backend truy cập được. Ba API B.1, B.2, B.3 nhận **cùng một dạng body**.

### Body chung của B.1, B.2, B.3

```json
{
  "s3_path": "s3://tai-lieu/2026/sach-abc.pdf",
  "access_key": "your_s3_access_key",
  "secret_key": "your_s3_secret_key",
  "endpoint": "https://minio.example.com",
  "callback_url": "https://client.example.com/api/ocr-callback",
  "document_id": 12345,
  "spellcheck": true
}
```

| Trường | Kiểu | Bắt buộc | Mặc định | Mô tả |
|---|---|---|---|---|
| `s3_path` | string | **có** | — | dạng `s3://<bucket>/<key>`. Có thể là một file, hoặc một thư mục kết thúc bằng `/`. Với thư mục, mọi file trong đó gộp thành **một** tài liệu, xếp theo tên file |
| `access_key` | string | không | khóa cấu hình sẵn ở backend | access key của S3/MinIO |
| `secret_key` | string | không | khóa cấu hình sẵn ở backend | secret key của S3/MinIO |
| `endpoint` | string | không | endpoint cấu hình sẵn ở backend | URL S3/MinIO, phải bắt đầu bằng `http://` hoặc `https://` |
| `callback_url` | string | không | `""` (không gửi webhook) | URL phía client nhận kết quả (mục B.5). Host phải được backend cho phép |
| `document_id` | number hoặc string | không | `null` | mã tài liệu phía client, được trả lại trong kết quả |
| `spellcheck` | boolean | không | `true` | sửa chính tả bằng từ điển và model ProtonX. B.2 bỏ qua trường này |

Nếu phía backend đã cấu hình sẵn kho S3, body tối thiểu chỉ cần:

```json
{
  "s3_path": "s3://tai-lieu/2026/sach-abc.pdf"
}
```

### B.1. `POST /api/ocr-fullpage`: OCR toàn văn

Định dạng nhận:
- PDF (kể cả PDF scan);
- ảnh: jpg, jpeg, png, tif, tiff, bmp, webp;
- văn bản: docx, doc, odt, rtf, txt, md.

**Request:**

```bash
curl -X POST "$BASE_URL/api/ocr-fullpage" \
  -u "<username>:<password>" \
  -H "Content-Type: application/json" \
  -d '{"s3_path": "s3://tai-lieu/2026/sach-abc.pdf", "document_id": 12345, "callback_url": "https://client.example.com/api/ocr-callback"}'
```

```js
const res = await fetch(`${BASE_URL}/api/ocr-fullpage`, {
  method: "POST",
  headers: { Authorization: AUTH, "Content-Type": "application/json" },
  body: JSON.stringify({
    s3_path: "s3://tai-lieu/2026/sach-abc.pdf",
    document_id: 12345,
    callback_url: "https://client.example.com/api/ocr-callback",
  }),
});
const { request_id } = await res.json();
```

**Response `202`:**

```json
{
  "error_code": 202,
  "error_message": "Đã nhận yêu cầu OCR toàn trang.",
  "request_id": "3f9a1c7e2b8d4e6fa0c5b1d2e3f4a5b6"
}
```

**Response lỗi:**

`400` (thiếu thông tin S3, backend cũng không cấu hình sẵn):

```json
{
  "detail": "S3 access_key, secret_key and endpoint are required (in the request or in .env)"
}
```

`400` (`callback_url` không được phép):

```json
{
  "detail": "callback_url host is not allowed"
}
```

Ngoài ra có thể gặp `401` và `422` (xem A.4).

Kết quả OCR lấy qua B.4 hoặc B.5. Dạng kết quả là **Kết quả OCR** ở mục B.4.

### B.2. `POST /api/book-metadata`: thông tin xuất bản của sách

Đọc các trang cuối sách và tách thông tin xuất bản: nhà xuất bản, ISBN, lưu chiểu, biên tập, chế bản... Định dạng nhận giống B.1. Nếu `s3_path` là thư mục (ví dụ PDF + Word của cùng một cuốn), backend ưu tiên Word, sau đó đến lớp chữ PDF, sau cùng mới OCR.

**Request:** body như phần chung.

```json
{
  "s3_path": "s3://tai-lieu/2026/sach-abc/",
  "document_id": "SACH-0001",
  "callback_url": "https://client.example.com/api/book-callback"
}
```

**Response `202`:**

```json
{
  "error_code": 202,
  "error_message": "Đã nhận yêu cầu lấy metadata sách.",
  "request_id": "7b2e9d4c1a0f4b3e8d6c5a4b3c2d1e0f"
}
```

Lỗi giống B.1. Kết quả là **Kết quả thông tin xuất bản** ở mục B.4.

### B.3. `POST /api/speech-to-text`: chép lời

Định dạng nhận: mp3, m4a, m4b, aac, wav, flac, ogg, oga, opus, wma, amr, aiff, mp4, webm, mkv, mov. Với video, backend chỉ lấy tiếng. Nếu `s3_path` là thư mục, các file nối nhau theo thứ tự tên.

**Request:**

```json
{
  "s3_path": "s3://ghi-am/2026/hop-giao-ban.m4a",
  "document_id": 778,
  "callback_url": "https://client.example.com/api/speech-callback",
  "spellcheck": true
}
```

**Response `202`:**

```json
{
  "error_code": 202,
  "error_message": "Đã nhận yêu cầu trích xuất lời nói.",
  "request_id": "c4d5e6f7a8b94c0d9e1f2a3b4c5d6e7f"
}
```

Lỗi giống B.1. Kết quả là **Kết quả chép lời** ở mục B.4.

### B.4. `GET /api/request-status/{request_id}`: hỏi trạng thái, lấy kết quả

Dùng cho mọi `request_id` nhận được từ B.1–B.3 và C.1–C.3.

**Request:**

```bash
curl "$BASE_URL/api/request-status/3f9a1c7e2b8d4e6fa0c5b1d2e3f4a5b6" -u "<username>:<password>"
```

Các giá trị `status`:

| `status` | Nghĩa | Client làm gì |
|---|---|---|
| `PENDING` | đang chờ hoặc đang xử lý. `request_id` không tồn tại cũng trả `PENDING` | hỏi lại sau 3–5 giây |
| `SUCCESS` | xong; kết quả nằm trong `result` | dừng hỏi, dùng `result` |
| `FAILURE` | lỗi; lý do nằm trong `error` | dừng hỏi, báo lỗi |

Việc lớn (sách vài trăm trang, audio vài giờ) có thể mất vài chục phút. Client nên đặt giới hạn thời gian chờ tổng, ví dụ 2 giờ.

**Response `200`, đang xử lý:**

```json
{
  "request_id": "3f9a1c7e2b8d4e6fa0c5b1d2e3f4a5b6",
  "status": "PENDING"
}
```

**Response `200`, lỗi:**

```json
{
  "request_id": "3f9a1c7e2b8d4e6fa0c5b1d2e3f4a5b6",
  "status": "FAILURE",
  "error": "File not found in storage"
}
```

Các giá trị `error` thường gặp:
- `File not found in storage`
- `Access to storage was denied`
- `Storage endpoint unreachable`
- `OCR backend unavailable`
- `Speech-to-text backend unavailable`
- `Processing failed`
- câu báo file không hỗ trợ hoặc quá lớn.

**Response `200`, thành công: Kết quả OCR (B.1, C.1):**

```json
{
  "request_id": "3f9a1c7e2b8d4e6fa0c5b1d2e3f4a5b6",
  "status": "SUCCESS",
  "result": {
    "request_id": "3f9a1c7e2b8d4e6fa0c5b1d2e3f4a5b6",
    "document_id": 12345,
    "saved_to_db": true,
    "page_count": 2,
    "text": "CHƯƠNG 1\nLỊCH SỬ HÌNH THÀNH\nNăm 1945, đất nước bước vào thời kỳ mới.\nHội nghị họp tại Pác Bó.\n\nTrang thứ hai của tài liệu.",
    "pages": [
      {
        "page": 1,
        "text": "CHƯƠNG 1\nLỊCH SỬ HÌNH THÀNH\nNăm 1945, đất nước bước vào thời kỳ mới.\nHội nghị họp tại Pác Bó.",
        "source": "chandra",
        "corrections": [
          {"line": 3, "wrong": "nuớc", "right": "nước", "by": "dict"}
        ],
        "error": null
      },
      {
        "page": 2,
        "text": "Trang thứ hai của tài liệu.",
        "source": "chandra",
        "corrections": [
          {"line": 1, "wrong": "cũa", "right": "của", "by": "protonx"}
        ],
        "error": null
      }
    ],
    "correction_total": 2,
    "changes": [
      {
        "page": 1,
        "line": 3,
        "before": "Năm 1945, đất nuớc bước vào thời kỳ mới.",
        "after": "Năm 1945, đất nước bước vào thời kỳ mới.",
        "marked": "Năm 1945, đất [nuớc→nước] bước vào thời kỳ mới.",
        "segments": [
          {"type": "same", "text": "Năm 1945, đất"},
          {"type": "fix", "before": "nuớc", "after": "nước", "by": "dict"},
          {"type": "same", "text": "bước vào thời kỳ mới."}
        ],
        "fixes": [
          {"wrong": "nuớc", "right": "nước", "by": "dict"}
        ]
      },
      {
        "page": 2,
        "line": 1,
        "before": "Trang thứ hai cũa tài liệu.",
        "after": "Trang thứ hai của tài liệu.",
        "marked": "Trang thứ hai [cũa→của] tài liệu.",
        "segments": [
          {"type": "same", "text": "Trang thứ hai"},
          {"type": "fix", "before": "cũa", "after": "của", "by": "protonx"},
          {"type": "same", "text": "tài liệu."}
        ],
        "fixes": [
          {"wrong": "cũa", "right": "của", "by": "protonx"}
        ]
      }
    ],
    "unknown_words": [
      {
        "word": "Pác",
        "kind": "foreign",
        "count": 1,
        "pages": [1],
        "samples": ["Hội nghị họp tại Pác Bó."],
        "suggestions": []
      }
    ],
    "unknown_total": 1,
    "notice": "Phát hiện 1 từ không có trong từ điển: Pác.",
    "input": {
      "mode": "ocr",
      "files": ["sach-abc.pdf"],
      "skipped": []
    },
    "elapsed_ms": 15342
  }
}
```

Ý nghĩa các trường trong **Kết quả OCR**:

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `request_id` | string | mã việc |
| `document_id` | number / string / null | mã phía client gửi lên; tải file lên (C.1) thì là `null` |
| `saved_to_db` | boolean | `true` nếu kết quả đã lưu, đọc lại được ở phần E |
| `page_count` | number | số trang |
| `text` | string | **toàn văn sau khi sửa chính tả**. Dòng cách nhau bằng `\n`, trang cách nhau bằng `\n\n`. Đa số client chỉ cần trường này |
| `pages[]` | array | từng trang |
| `pages[].page` | number | số trang, bắt đầu từ 1 |
| `pages[].text` | string | text của trang |
| `pages[].source` | string | `chandra` (OCR ảnh), `pdf_text` (lớp chữ có sẵn trong PDF), `text_file` (đọc từ Word/txt), `none` (trang lỗi) |
| `pages[].corrections[]` | array | các từ đã sửa trong trang: `line` là số dòng trong trang (từ 1), `wrong` là từ cũ, `right` là từ mới, `by` là cách sửa |
| `pages[].error` | string / null | `null` nếu trang xử lý được. Ngược lại là lý do, ví dụ `"OCR backend error"`; khi đó `text` của trang rỗng, các trang khác vẫn có kết quả |
| `correction_total` | number | tổng số từ đã sửa |
| `changes[]` | array | các **dòng** đã bị sửa, dùng để hiển thị so sánh trước/sau |
| `changes[].before` / `after` | string | dòng trước / sau khi sửa |
| `changes[].marked` | string | dòng sau khi sửa, chỗ đổi ghi `[cũ→mới]` |
| `changes[].segments[]` | array | dòng chia sẵn để tô màu: `type: "same"` (giữ nguyên, có `text`), `type: "fix"` (đã sửa, có `before`, `after`, `by`) |
| `changes[].fixes[]` | array | các từ đã sửa trong dòng |
| `unknown_words[]` | array | từ không có trong từ điển, nên cho người kiểm tra lại |
| `unknown_words[].kind` | string | `vietnamese` (giống tiếng Việt nhưng lạ), `foreign` (từ nước ngoài, tên riêng), `acronym` (viết tắt) |
| `unknown_words[].pages` | array | các trang có từ đó |
| `unknown_words[].samples` | array | tối đa 2 câu ngữ cảnh |
| `unknown_words[].suggestions` | array | từ gợi ý, chỉ có với `kind = vietnamese` |
| `unknown_total` | number | tổng số từ lạ. `unknown_words` có thể bị cắt bớt nếu quá nhiều |
| `notice` | string / null | câu tóm tắt cảnh báo, hoặc `null` |
| `input.mode` | string | `ocr` (có OCR ảnh) hoặc `text` (đọc thẳng file Word/txt) |
| `input.files` | array | các file đã dùng |
| `input.skipped[]` | array | các file bị bỏ qua, dạng `{"file": "...", "reason": "..."}` |
| `elapsed_ms` | number | thời gian xử lý (mili giây) |

Giá trị `by` (cách sửa):
- `dict`: sửa theo từ điển;
- `protonx`: sửa bằng model ProtonX;
- `replace`: sửa theo bảng thay thế cố định.

Văn bản đọc từ Word, txt hoặc lớp chữ PDF mặc định **không** bị sửa chính tả, nên `corrections` rỗng.

**Response `200`, thành công: Kết quả chép lời (B.3, C.3):**

```json
{
  "request_id": "c4d5e6f7a8b94c0d9e1f2a3b4c5d6e7f",
  "status": "SUCCESS",
  "result": {
    "request_id": "c4d5e6f7a8b94c0d9e1f2a3b4c5d6e7f",
    "document_id": 778,
    "saved_to_db": true,
    "duration": 12.6,
    "segment_count": 3,
    "text": "Xin chào các đồng chí.\nHôm nay chúng ta họp giao ban tuần.\nĐề nghị các phòng báo cáo tiến độ.",
    "segments": [
      {
        "index": 1,
        "file": "hop-giao-ban.m4a",
        "start": 0.0,
        "end": 2.4,
        "text": "Xin chào các đồng chí.",
        "corrections": [],
        "error": null
      },
      {
        "index": 2,
        "file": "hop-giao-ban.m4a",
        "start": 2.4,
        "end": 7.1,
        "text": "Hôm nay chúng ta họp giao ban tuần.",
        "corrections": [
          {"wrong": "bang", "right": "ban", "by": "protonx"}
        ],
        "error": null
      },
      {
        "index": 3,
        "file": "hop-giao-ban.m4a",
        "start": 7.1,
        "end": 12.6,
        "text": "Đề nghị các phòng báo cáo tiến độ.",
        "corrections": [],
        "error": null
      }
    ],
    "correction_total": 1,
    "changes": [
      {
        "segment": 2,
        "file": "hop-giao-ban.m4a",
        "start": 2.4,
        "end": 7.1,
        "before": "Hôm nay chúng ta họp giao bang tuần.",
        "after": "Hôm nay chúng ta họp giao ban tuần.",
        "marked": "Hôm nay chúng ta họp giao [bang→ban] tuần.",
        "segments": [
          {"type": "same", "text": "Hôm nay chúng ta họp giao"},
          {"type": "fix", "before": "bang", "after": "ban", "by": "protonx"},
          {"type": "same", "text": "tuần."}
        ],
        "fixes": [
          {"wrong": "bang", "right": "ban", "by": "protonx"}
        ]
      }
    ],
    "unknown_words": [],
    "unknown_total": 0,
    "notice": null,
    "input": {
      "mode": "speech",
      "files": [
        {"file": "hop-giao-ban.m4a", "duration": 12.6}
      ]
    },
    "elapsed_ms": 8120
  }
}
```

Khác với Kết quả OCR:
- Đơn vị là **đoạn** (`segments[]`), không phải trang.
- Mỗi đoạn có `index` (từ 1), `file` (file audio chứa đoạn đó), `start` và `end` (giây, tính từ đầu file đó).
- `text` là toàn bộ lời, mỗi đoạn một dòng.
- `corrections` của đoạn không có `line`.
- `changes[].segment` là số đoạn bị sửa.
- `unknown_words[]` dùng `segments` (danh sách số đoạn) thay cho `pages`.
- `duration` là tổng thời lượng (giây).
- `notice` là `"Không có đoạn nào có tiếng nói."` khi file im lặng.

**Response `200`, thành công: Kết quả thông tin xuất bản (B.2, C.2):**

```json
{
  "request_id": "7b2e9d4c1a0f4b3e8d6c5a4b3c2d1e0f",
  "status": "SUCCESS",
  "result": {
    "request_id": "7b2e9d4c1a0f4b3e8d6c5a4b3c2d1e0f",
    "document_id": "SACH-0001",
    "saved_to_db": true,
    "thong_tin": {
      "nha_xuat_ban": "Nhà xuất bản Chính trị quốc gia Sự thật",
      "ban_bien_tap": "Nguyễn Văn A; Trần Thị B",
      "isbn": "978-604-57-1234-5",
      "luu_chieu": "Quý IV năm 2025",
      "luu_chieu_thang": 12,
      "luu_chieu_nam": 2025,
      "giam_doc_tong_bien_tap": "Lê Văn C",
      "che_ban": "Phạm Thị D",
      "trinh_bay_bia": "Hoàng Văn E",
      "dia_chi_nxb": "Số 6/86 Duy Tân, Cầu Giấy, Hà Nội",
      "dien_thoai_nxb": "024.38221234",
      "sdt_quan_ly_xuat_ban": "024.38221235",
      "sdt_tong_bien_tap": "024.38221236",
      "fax_nxb": "024.38221237",
      "dia_chi_co_so_in": "Công ty in ABC, Hà Nội",
      "ngay_xac_nhan_dkxb": "Số ĐKXB: 1234-2025/CXBIPH/1-10/CTQG"
    },
    "field_count": 14,
    "text": "NHÀ XUẤT BẢN CHÍNH TRỊ QUỐC GIA SỰ THẬT\nSố 6/86 Duy Tân, Cầu Giấy, Hà Nội\nChịu trách nhiệm xuất bản\nGIÁM ĐỐC - TỔNG BIÊN TẬP\nLê Văn C\nBiên tập nội dung: Nguyễn Văn A; Trần Thị B\nTrình bày bìa: Hoàng Văn E\nChế bản vi tính: Phạm Thị D\nISBN: 978-604-57-1234-5\nIn xong và nộp lưu chiểu Quý IV năm 2025.",
    "source": "text_file",
    "file": "sach-abc.docx",
    "page": 1,
    "page_count": 1,
    "error": "",
    "input": {
      "files": ["sach-abc.docx", "sach-abc.pdf"],
      "skipped": []
    },
    "elapsed_ms": 2310
  }
}
```

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `thong_tin` | object | 16 trường thông tin xuất bản, như ví dụ. Trường không tìm thấy là `""`; riêng `luu_chieu_thang` và `luu_chieu_nam` là số hoặc `null` |
| `field_count` | number | số trường chữ đã điền được (không tính 2 trường số) |
| `text` | string | khối văn bản trang thông tin xuất bản mà backend đã đọc |
| `source` | string | nguồn text: `text_file` (Word/txt), `pdf_text` (lớp chữ PDF), `ocr` (OCR ảnh); `""` khi không tìm thấy |
| `file`, `page` | string, number | lấy từ file nào, trang nào |
| `page_count` | number / null | số trang của file nguồn |
| `error` | string | `""` khi thành công; ngược lại là lý do, ví dụ `"không tìm thấy thông tin xuất bản"` hoặc `"OCR backend unavailable"` |

### B.5. Webhook: backend gọi `callback_url` của client

Nếu request có `callback_url`, khi xong (thành công hoặc lỗi) backend gửi:
- method `POST`;
- header `Content-Type: application/json`.

Phía client **phải** dựng một API nhận request này:
- Trả về mã `2xx` (thường là `200`), body tùy ý.
- Nếu trả mã khác hoặc không phản hồi kịp, backend gửi lại vài lần, mỗi lần cách nhau lâu hơn lần trước.
- Có thể nhận trùng một kết quả, nên client nên xử lý theo `request_id` sao cho nhận lần hai không gây sai.
- Host của `callback_url` phải nằm trong danh sách backend cho phép; báo phía backend để thêm.

**Backend gửi khi thành công (ví dụ OCR):**

`data` gồm 4 trường `path`, `document_id`, `creator`, `created_at`, cộng với toàn bộ các trường của kết quả ở B.4. Riêng `request_id` nằm ngoài `data`, và `data` không có `saved_to_db`.

```json
{
  "meta": {
    "error_code": 200,
    "error_message": "OK"
  },
  "data": {
    "path": "s3://tai-lieu/2026/sach-abc.pdf",
    "document_id": 12345,
    "creator": "ocr-fullpage",
    "created_at": "2026-11-18T08:30:12.345678+00:00",
    "page_count": 2,
    "text": "CHƯƠNG 1\nLỊCH SỬ HÌNH THÀNH\nNăm 1945, đất nước bước vào thời kỳ mới.\nHội nghị họp tại Pác Bó.\n\nTrang thứ hai của tài liệu.",
    "pages": [
      {
        "page": 1,
        "text": "CHƯƠNG 1\nLỊCH SỬ HÌNH THÀNH\nNăm 1945, đất nước bước vào thời kỳ mới.\nHội nghị họp tại Pác Bó.",
        "source": "chandra",
        "corrections": [{"line": 3, "wrong": "nuớc", "right": "nước", "by": "dict"}],
        "error": null
      },
      {
        "page": 2,
        "text": "Trang thứ hai của tài liệu.",
        "source": "chandra",
        "corrections": [{"line": 1, "wrong": "cũa", "right": "của", "by": "protonx"}],
        "error": null
      }
    ],
    "correction_total": 2,
    "changes": [
      {
        "page": 1, "line": 3,
        "before": "Năm 1945, đất nuớc bước vào thời kỳ mới.",
        "after": "Năm 1945, đất nước bước vào thời kỳ mới.",
        "marked": "Năm 1945, đất [nuớc→nước] bước vào thời kỳ mới.",
        "segments": [
          {"type": "same", "text": "Năm 1945, đất"},
          {"type": "fix", "before": "nuớc", "after": "nước", "by": "dict"},
          {"type": "same", "text": "bước vào thời kỳ mới."}
        ],
        "fixes": [{"wrong": "nuớc", "right": "nước", "by": "dict"}]
      },
      {
        "page": 2, "line": 1,
        "before": "Trang thứ hai cũa tài liệu.",
        "after": "Trang thứ hai của tài liệu.",
        "marked": "Trang thứ hai [cũa→của] tài liệu.",
        "segments": [
          {"type": "same", "text": "Trang thứ hai"},
          {"type": "fix", "before": "cũa", "after": "của", "by": "protonx"},
          {"type": "same", "text": "tài liệu."}
        ],
        "fixes": [{"wrong": "cũa", "right": "của", "by": "protonx"}]
      }
    ],
    "unknown_words": [
      {"word": "Pác", "kind": "foreign", "count": 1, "pages": [1], "samples": ["Hội nghị họp tại Pác Bó."], "suggestions": []}
    ],
    "unknown_total": 1,
    "notice": "Phát hiện 1 từ không có trong từ điển: Pác.",
    "input": {"mode": "ocr", "files": ["sach-abc.pdf"], "skipped": []},
    "elapsed_ms": 15342
  },
  "request_id": "3f9a1c7e2b8d4e6fa0c5b1d2e3f4a5b6"
}
```

`creator` cho biết loại việc:

| `creator` | Phần còn lại của `data` có dạng |
|---|---|
| `ocr-fullpage` | Kết quả OCR (B.4) |
| `speech-to-text` | Kết quả chép lời (B.4) |
| `book-metadata` | Kết quả thông tin xuất bản (B.4) |

**Backend gửi khi lỗi:**

```json
{
  "meta": {
    "error_code": 404,
    "error_message": "File not found in storage"
  },
  "data": {
    "path": "s3://tai-lieu/2026/sach-abc.pdf",
    "document_id": 12345
  },
  "request_id": "3f9a1c7e2b8d4e6fa0c5b1d2e3f4a5b6"
}
```

| `meta.error_code` | Nghĩa |
|---|---|
| `200` | thành công |
| `403` | S3 từ chối quyền truy cập |
| `404` | không thấy file trên S3 |
| `413` | file quá lớn, quá nhiều trang, audio quá dài |
| `415` | định dạng không hỗ trợ |
| `422` | dữ liệu không hợp lệ |
| `502` | S3 hoặc model không phản hồi |
| `500` | lỗi khác phía backend (`"Processing failed"`) |

Ví dụ API nhận webhook phía client (Node.js / Express):

```js
app.post("/api/ocr-callback", express.json({ limit: "200mb" }), async (req, res) => {
  const { meta, data, request_id } = req.body;
  if (meta.error_code === 200) {
    await saveResult(request_id, data.document_id, data.text, data);
  } else {
    await markFailed(request_id, data.document_id, meta.error_message);
  }
  res.sendStatus(200);
});
```

---

## Phần C. Tải file từ máy lên (bất đồng bộ)

Dùng khi file không nằm trên S3: client gửi thẳng file trong request. Backend trả `request_id` ngay, kết quả lấy bằng B.4.

Ở phiên bản này, ba API tải file **không** nhận `callback_url` và `document_id`. Client tự lưu cặp (`request_id`, mã tài liệu của mình).

### Form chung của C.1, C.2, C.3

| Trường form | Kiểu | Bắt buộc | Mô tả |
|---|---|---|---|
| `files` | file, lặp lại được | **có** | mỗi file thêm một trường `files`. Tối đa 200 file mỗi request |
| `group` | `"true"` / `"false"` | không | `"false"`: mỗi file thành một việc riêng (một `request_id`). `"true"`: cả lô thành **một** việc |
| `spellcheck` | `"true"` / `"false"` | không | mặc định `"true"`; C.2 không có trường này |

| API | Định dạng nhận | Dung lượng mỗi file | Mặc định `group` | `group = "true"` nghĩa là |
|---|---|---|---|---|
| C.1 `POST /api/uploads/ocr` | PDF, ảnh, docx, doc, odt, rtf, txt, md | 50 MB | `"false"` | các file ghép thành một tài liệu, xếp theo tên |
| C.2 `POST /api/uploads/metadata` | như C.1 | 50 MB | `"true"` | cả lô là **một cuốn sách** (ví dụ PDF + Word cùng tên) |
| C.3 `POST /api/uploads/speech` | audio/video như B.3 | 500 MB | `"false"` | các file audio nối nhau theo tên |

Giới hạn dung lượng và số file do phía backend cấu hình; số trên là mặc định.

### C.1. `POST /api/uploads/ocr`

**Request:**

```bash
curl -X POST "$BASE_URL/api/uploads/ocr" \
  -u "<username>:<password>" \
  -F "files=@hop-dong-01.pdf" \
  -F "files=@hop-dong-02.jpg" \
  -F "files=@ghi-chu.xlsx" \
  -F "group=false" \
  -F "spellcheck=true"
```

```js
const fd = new FormData();
for (const file of inputElement.files) fd.append("files", file);
fd.append("group", "false");
fd.append("spellcheck", "true");
// Không tự đặt Content-Type: trình duyệt tự thêm boundary
const res = await fetch(`${BASE_URL}/api/uploads/ocr`, {
  method: "POST",
  headers: { Authorization: AUTH },
  body: fd,
});
const { jobs, skipped } = await res.json();
```

**Response `202`:**

```json
{
  "jobs": [
    {
      "request_id": "a1b2c3d4e5f64a7b8c9d0e1f2a3b4c5d",
      "files": ["hop-dong-01.pdf"]
    },
    {
      "request_id": "b2c3d4e5f6a74b8c9d0e1f2a3b4c5d6e",
      "files": ["hop-dong-02.jpg"]
    }
  ],
  "skipped": [
    {
      "file": "ghi-chu.xlsx",
      "reason": "unsupported file type"
    }
  ]
}
```

Nếu gửi cùng các file đó nhưng với `group=true`, `jobs` chỉ có **một** phần tử:

```json
{
  "jobs": [
    {
      "request_id": "a1b2c3d4e5f64a7b8c9d0e1f2a3b4c5d",
      "files": ["hop-dong-01.pdf", "hop-dong-02.jpg"]
    }
  ],
  "skipped": [
    {"file": "ghi-chu.xlsx", "reason": "unsupported file type"}
  ]
}
```

**Response lỗi:**

`415` (không có file nào hợp lệ):

```json
{
  "detail": "no supported file in the upload"
}
```

`413` (quá nhiều file):

```json
{
  "detail": "too many files (limit 200)"
}
```

`413` (có file quá lớn):

```json
{
  "detail": "hop-dong-01.pdf is larger than 50 MB"
}
```

Kết quả của từng `request_id` lấy bằng B.4, có dạng **Kết quả OCR**, trong đó `document_id` là `null`.

### C.2. `POST /api/uploads/metadata`

```bash
curl -X POST "$BASE_URL/api/uploads/metadata" \
  -u "<username>:<password>" \
  -F "files=@sach-abc.pdf" -F "files=@sach-abc.docx"
```

**Response `202`:**

```json
{
  "jobs": [
    {
      "request_id": "7b2e9d4c1a0f4b3e8d6c5a4b3c2d1e0f",
      "files": ["sach-abc.pdf", "sach-abc.docx"]
    }
  ],
  "skipped": []
}
```

Lỗi giống C.1. Kết quả lấy bằng B.4, có dạng **Kết quả thông tin xuất bản**.

### C.3. `POST /api/uploads/speech`

```bash
curl -X POST "$BASE_URL/api/uploads/speech" \
  -u "<username>:<password>" \
  -F "files=@hop-giao-ban.m4a" -F "spellcheck=true"
```

**Response `202`:**

```json
{
  "jobs": [
    {
      "request_id": "c4d5e6f7a8b94c0d9e1f2a3b4c5d6e7f",
      "files": ["hop-giao-ban.m4a"]
    }
  ],
  "skipped": []
}
```

Lỗi giống C.1 (giới hạn 500 MB mỗi file). Kết quả lấy bằng B.4, có dạng **Kết quả chép lời**.

---

## Phần D. Xử lý đồng bộ: trả kết quả ngay trong response

Client gửi file và chờ đến khi xong. Kết quả **không** được lưu lại, nên không đọc lại được ở phần E. Chỉ nên dùng cho file nhỏ.

### D.1. `POST /api/ocr-fullpage/sync`

| Trường form | Kiểu | Bắt buộc | Mặc định | Mô tả |
|---|---|---|---|---|
| `file` | file | **có** | — | **một** file: PDF, ảnh, docx, doc, odt, rtf, txt, md; tối đa 50 MB |
| `spellcheck` | `"true"` / `"false"` | không | `"true"` | sửa chính tả |
| `include_raw` | `"true"` / `"false"` | không | `"false"` | `"true"` thì mỗi trang có thêm `raw_text` (text trước khi sửa chính tả) |

```bash
curl -X POST "$BASE_URL/api/ocr-fullpage/sync" \
  -u "<username>:<password>" \
  -F "file=@trang-1.jpg" -F "spellcheck=true" -F "include_raw=true"
```

**Response `200`:** dạng Kết quả OCR (B.4) nhưng **không** có `request_id`, `document_id`, `saved_to_db`. Khi `include_raw=true`, mỗi trang có thêm `raw_text`.

```json
{
  "page_count": 1,
  "text": "Năm 1945, đất nước bước vào thời kỳ mới.",
  "pages": [
    {
      "page": 1,
      "raw_text": "Năm 1945, đất nuớc bước vào thời kỳ mới.",
      "text": "Năm 1945, đất nước bước vào thời kỳ mới.",
      "source": "chandra",
      "corrections": [
        {"line": 1, "wrong": "nuớc", "right": "nước", "by": "dict"}
      ],
      "error": null
    }
  ],
  "correction_total": 1,
  "changes": [
    {
      "page": 1,
      "line": 1,
      "before": "Năm 1945, đất nuớc bước vào thời kỳ mới.",
      "after": "Năm 1945, đất nước bước vào thời kỳ mới.",
      "marked": "Năm 1945, đất [nuớc→nước] bước vào thời kỳ mới.",
      "segments": [
        {"type": "same", "text": "Năm 1945, đất"},
        {"type": "fix", "before": "nuớc", "after": "nước", "by": "dict"},
        {"type": "same", "text": "bước vào thời kỳ mới."}
      ],
      "fixes": [
        {"wrong": "nuớc", "right": "nước", "by": "dict"}
      ]
    }
  ],
  "unknown_words": [],
  "unknown_total": 0,
  "notice": null,
  "input": {
    "mode": "ocr",
    "files": ["trang-1.jpg"],
    "skipped": []
  },
  "elapsed_ms": 4210
}
```

**Response lỗi:**

| Mã | Ví dụ body |
|---|---|
| `413` | `{"detail": "File too large (limit 50 MB)"}` hoặc thông báo quá số trang |
| `415` | `{"detail": "unsupported file type; send a PDF or an image"}` |
| `422` | `{"detail": "Empty file"}` |
| `502` | `{"detail": "OCR backend unavailable"}` |

### D.2. `POST /api/speech-to-text/sync`

Form giống D.1: `file` (một file audio/video, tối đa 500 MB), `spellcheck`, `include_raw`.

```bash
curl -X POST "$BASE_URL/api/speech-to-text/sync" \
  -u "<username>:<password>" \
  -F "file=@loi-chao.m4a"
```

**Response `200`:** dạng Kết quả chép lời (B.4), không có `request_id`, `document_id`, `saved_to_db`.

```json
{
  "duration": 2.4,
  "segment_count": 1,
  "text": "Xin chào các đồng chí.",
  "segments": [
    {
      "index": 1,
      "file": "loi-chao.m4a",
      "start": 0.0,
      "end": 2.4,
      "text": "Xin chào các đồng chí.",
      "corrections": [],
      "error": null
    }
  ],
  "correction_total": 0,
  "changes": [],
  "unknown_words": [],
  "unknown_total": 0,
  "notice": null,
  "input": {
    "mode": "speech",
    "files": [
      {"file": "loi-chao.m4a", "duration": 2.4}
    ]
  },
  "elapsed_ms": 1830
}
```

Lỗi: `413` (file quá lớn hoặc audio quá dài), `415` (không đọc được audio), `422` (`{"detail": "Empty file"}`), `502` (`{"detail": "Speech-to-text backend unavailable"}`).

### D.3. `POST /api/book-metadata/sync`

| Trường form | Kiểu | Bắt buộc | Mô tả |
|---|---|---|---|
| `files` | file, lặp lại được | **có** | một hoặc nhiều file của **cùng một cuốn sách** |

```bash
curl -X POST "$BASE_URL/api/book-metadata/sync" \
  -u "<username>:<password>" \
  -F "files=@sach-abc.pdf" -F "files=@sach-abc.docx"
```

**Response `200`:** dạng Kết quả thông tin xuất bản (B.4), không có `request_id`, `document_id`, `saved_to_db`.

```json
{
  "thong_tin": {
    "nha_xuat_ban": "Nhà xuất bản Chính trị quốc gia Sự thật",
    "ban_bien_tap": "Nguyễn Văn A; Trần Thị B",
    "isbn": "978-604-57-1234-5",
    "luu_chieu": "Quý IV năm 2025",
    "luu_chieu_thang": 12,
    "luu_chieu_nam": 2025,
    "giam_doc_tong_bien_tap": "Lê Văn C",
    "che_ban": "Phạm Thị D",
    "trinh_bay_bia": "Hoàng Văn E",
    "dia_chi_nxb": "Số 6/86 Duy Tân, Cầu Giấy, Hà Nội",
    "dien_thoai_nxb": "024.38221234",
    "sdt_quan_ly_xuat_ban": "",
    "sdt_tong_bien_tap": "",
    "fax_nxb": "",
    "dia_chi_co_so_in": "Công ty in ABC, Hà Nội",
    "ngay_xac_nhan_dkxb": "Số ĐKXB: 1234-2025/CXBIPH/1-10/CTQG"
  },
  "field_count": 11,
  "text": "NHÀ XUẤT BẢN CHÍNH TRỊ QUỐC GIA SỰ THẬT\nSố 6/86 Duy Tân, Cầu Giấy, Hà Nội\nChịu trách nhiệm xuất bản\nGIÁM ĐỐC - TỔNG BIÊN TẬP\nLê Văn C",
  "source": "text_file",
  "file": "sach-abc.docx",
  "page": 1,
  "page_count": 1,
  "error": "",
  "input": {
    "files": ["sach-abc.docx", "sach-abc.pdf"],
    "skipped": []
  },
  "elapsed_ms": 2310
}
```

Lỗi: `413` (quá nhiều file hoặc file quá lớn), `415` (không có file hỗ trợ), `422` (dữ liệu không hợp lệ).

### D.4. `POST /api/spellcheck`: sửa chính tả một đoạn văn bản

**Request** (`Content-Type: application/json`):

```json
{
  "text": "Hôm nay troi đẹp.\nChúng ta đi học bài mới."
}
```

| Trường | Kiểu | Bắt buộc | Mô tả |
|---|---|---|---|
| `text` | string | **có** | văn bản cần sửa, tối đa 200.000 ký tự, nhiều dòng cách nhau bằng `\n` |

**Response `200`:**

```json
{
  "text": "Hôm nay trời đẹp.\nChúng ta đi học bài mới.",
  "corrections": [
    {"line": 1, "wrong": "troi", "right": "trời", "by": "protonx"}
  ],
  "changes": [
    {
      "line": 1,
      "before": "Hôm nay troi đẹp.",
      "after": "Hôm nay trời đẹp.",
      "marked": "Hôm nay [troi→trời] đẹp.",
      "segments": [
        {"type": "same", "text": "Hôm nay"},
        {"type": "fix", "before": "troi", "after": "trời", "by": "protonx"},
        {"type": "same", "text": "đẹp."}
      ],
      "fixes": [
        {"wrong": "troi", "right": "trời", "by": "protonx"}
      ]
    }
  ],
  "unknown_words": [],
  "unknown_total": 0,
  "notice": null
}
```

Lỗi: `422` (thiếu `text` hoặc quá 200.000 ký tự).

---

## Phần E. Đọc lại kết quả đã lưu

Kết quả của các việc bất đồng bộ (phần B, C) được lưu lại. Client có thể đọc lại bất cứ lúc nào, ví dụ để làm trang xem lịch sử.

**Query chung của các API danh sách** (E.1, E.5, E.9):

| Query | Kiểu | Mặc định | Mô tả |
|---|---|---|---|
| `limit` | number | 20 | số bản ghi mỗi trang, tối đa 100 |
| `offset` | number | 0 | bỏ qua bao nhiêu bản ghi |
| `document_id` | string | — | chỉ lấy bản ghi có `document_id` này |

Danh sách xếp mới nhất trước. Để phân trang, tăng `offset` thêm `limit` cho đến khi `offset >= total`.

Lưu ý: khi đọc lại, `document_id` luôn là **chuỗi**. Ví dụ gửi `12345` thì đọc lại được `"12345"`.

### E.1. `GET /api/documents`: danh sách tài liệu OCR

```bash
curl "$BASE_URL/api/documents?limit=20&offset=0&document_id=12345" -u "<username>:<password>"
```

**Response `200`:**

```json
{
  "total": 1,
  "limit": 20,
  "offset": 0,
  "items": [
    {
      "request_id": "3f9a1c7e2b8d4e6fa0c5b1d2e3f4a5b6",
      "document_id": "12345",
      "s3_path": "s3://tai-lieu/2026/sach-abc.pdf",
      "page_count": 2,
      "correction_count": 2,
      "unknown_count": 1,
      "notice": "Phát hiện 1 từ không có trong từ điển: Pác.",
      "input": {"mode": "ocr", "files": ["sach-abc.pdf"], "skipped": []},
      "created_at": "2026-11-18T08:30:12.345678+00:00"
    }
  ]
}
```

Với việc tải file lên (C.1), `s3_path` có dạng `upload://<mã>/<tên file>` và `document_id` là `null`.

### E.2. `GET /api/documents/{request_id}`: tổng quan một tài liệu

**Response `200`:**

```json
{
  "request_id": "3f9a1c7e2b8d4e6fa0c5b1d2e3f4a5b6",
  "document_id": "12345",
  "s3_path": "s3://tai-lieu/2026/sach-abc.pdf",
  "page_count": 2,
  "correction_count": 2,
  "unknown_count": 1,
  "notice": "Phát hiện 1 từ không có trong từ điển: Pác.",
  "input": {"mode": "ocr", "files": ["sach-abc.pdf"], "skipped": []},
  "created_at": "2026-11-18T08:30:12.345678+00:00",
  "pages": [
    {"page": 1, "source": "chandra", "error": null, "correction_count": 1, "unknown_count": 1},
    {"page": 2, "source": "chandra", "error": null, "correction_count": 1, "unknown_count": 0}
  ],
  "unknown_words": [
    {"word": "Pác", "kind": "foreign", "count": 1, "pages": [1], "sample": "Hội nghị họp tại Pác Bó."}
  ]
}
```

Ở dữ liệu đã lưu, mỗi từ lạ chỉ có **một** câu ngữ cảnh trong trường `sample` (số ít), và không có `suggestions`.

Lỗi `404`:

```json
{
  "detail": "Document not found"
}
```

### E.3. `GET /api/documents/{request_id}/pages/{page}`: toàn bộ dòng của một trang

```bash
curl "$BASE_URL/api/documents/3f9a1c7e2b8d4e6fa0c5b1d2e3f4a5b6/pages/1" -u "<username>:<password>"
```

**Response `200`:**

```json
{
  "request_id": "3f9a1c7e2b8d4e6fa0c5b1d2e3f4a5b6",
  "page": 1,
  "page_count": 2,
  "source": "chandra",
  "error": null,
  "correction_count": 1,
  "lines": [
    {"line": 1, "text": "CHƯƠNG 1", "changed": false},
    {"line": 2, "text": "LỊCH SỬ HÌNH THÀNH", "changed": false},
    {
      "line": 3,
      "text": "Năm 1945, đất nước bước vào thời kỳ mới.",
      "changed": true,
      "before": "Năm 1945, đất nuớc bước vào thời kỳ mới.",
      "segments": [
        {"type": "same", "text": "Năm 1945, đất"},
        {"type": "fix", "before": "nuớc", "after": "nước", "by": "dict"},
        {"type": "same", "text": "bước vào thời kỳ mới."}
      ],
      "fixes": [
        {"wrong": "nuớc", "right": "nước", "by": "dict"}
      ]
    },
    {"line": 4, "text": "Hội nghị họp tại Pác Bó.", "changed": false}
  ],
  "unknown_words": [
    {"word": "Pác", "kind": "foreign", "count": 1, "pages": [1], "sample": "Hội nghị họp tại Pác Bó."}
  ]
}
```

Lỗi `404`: `{"detail": "Document not found"}` hoặc `{"detail": "Page not found"}`.

### E.4. `GET /api/documents/{request_id}/changes`: các dòng đã sửa

| Query | Kiểu | Mặc định | Mô tả |
|---|---|---|---|
| `page` | number | — | chỉ lấy một trang |
| `by` | string | — | chỉ lấy dòng có sửa bằng `protonx`, `dict` hoặc `replace` |
| `limit` | number | 50 | tối đa 500 |
| `offset` | number | 0 | |

```bash
curl "$BASE_URL/api/documents/3f9a1c7e2b8d4e6fa0c5b1d2e3f4a5b6/changes?by=protonx" -u "<username>:<password>"
```

**Response `200`:**

```json
{
  "total": 1,
  "limit": 50,
  "offset": 0,
  "items": [
    {
      "page": 2,
      "line": 1,
      "before": "Trang thứ hai cũa tài liệu.",
      "after": "Trang thứ hai của tài liệu.",
      "marked": "Trang thứ hai [cũa→của] tài liệu.",
      "segments": [
        {"type": "same", "text": "Trang thứ hai"},
        {"type": "fix", "before": "cũa", "after": "của", "by": "protonx"},
        {"type": "same", "text": "tài liệu."}
      ],
      "fixes": [
        {"wrong": "cũa", "right": "của", "by": "protonx"}
      ]
    }
  ]
}
```

### E.5. `GET /api/transcripts`: danh sách bản chép lời

**Response `200`:**

```json
{
  "total": 1,
  "limit": 20,
  "offset": 0,
  "items": [
    {
      "request_id": "c4d5e6f7a8b94c0d9e1f2a3b4c5d6e7f",
      "document_id": "778",
      "s3_path": "s3://ghi-am/2026/hop-giao-ban.m4a",
      "duration": 12.6,
      "segment_count": 3,
      "correction_count": 1,
      "unknown_count": 0,
      "notice": null,
      "input": {"mode": "speech", "files": [{"file": "hop-giao-ban.m4a", "duration": 12.6}]},
      "created_at": "2026-11-18T09:02:44.120000+00:00"
    }
  ]
}
```

### E.6. `GET /api/transcripts/{request_id}`: tổng quan một bản chép lời

**Response `200`:**

```json
{
  "request_id": "c4d5e6f7a8b94c0d9e1f2a3b4c5d6e7f",
  "document_id": "778",
  "s3_path": "s3://ghi-am/2026/hop-giao-ban.m4a",
  "duration": 12.6,
  "segment_count": 3,
  "correction_count": 1,
  "unknown_count": 0,
  "notice": null,
  "input": {"mode": "speech", "files": [{"file": "hop-giao-ban.m4a", "duration": 12.6}]},
  "created_at": "2026-11-18T09:02:44.120000+00:00",
  "unknown_words": []
}
```

Lỗi `404`:

```json
{
  "detail": "Transcript not found"
}
```

### E.7. `GET /api/transcripts/{request_id}/segments`: các đoạn theo thời gian

| Query | Kiểu | Mặc định | Mô tả |
|---|---|---|---|
| `file` | string | — | chỉ lấy đoạn của một file audio |
| `changed` | boolean | — | `true`: chỉ đoạn đã sửa; `false`: chỉ đoạn không sửa |
| `limit` | number | 100 | tối đa 1000 |
| `offset` | number | 0 | |

**Response `200`:**

```json
{
  "total": 3,
  "limit": 100,
  "offset": 0,
  "items": [
    {"index": 1, "file": "hop-giao-ban.m4a", "start": 0.0, "end": 2.4, "text": "Xin chào các đồng chí.", "error": null, "changed": false},
    {
      "index": 2,
      "file": "hop-giao-ban.m4a",
      "start": 2.4,
      "end": 7.1,
      "text": "Hôm nay chúng ta họp giao ban tuần.",
      "error": null,
      "changed": true,
      "before": "Hôm nay chúng ta họp giao bang tuần.",
      "segments": [
        {"type": "same", "text": "Hôm nay chúng ta họp giao"},
        {"type": "fix", "before": "bang", "after": "ban", "by": "protonx"},
        {"type": "same", "text": "tuần."}
      ],
      "fixes": [
        {"wrong": "bang", "right": "ban", "by": "protonx"}
      ]
    },
    {"index": 3, "file": "hop-giao-ban.m4a", "start": 7.1, "end": 12.6, "text": "Đề nghị các phòng báo cáo tiến độ.", "error": null, "changed": false}
  ]
}
```

### E.8. `GET /api/transcripts/{request_id}/changes`: các đoạn đã sửa

Query: `file`, `by` (`protonx`, `dict`, `replace`), `limit` (mặc định 50, tối đa 500), `offset`.

**Response `200`:**

```json
{
  "total": 1,
  "limit": 50,
  "offset": 0,
  "items": [
    {
      "segment": 2,
      "file": "hop-giao-ban.m4a",
      "start": 2.4,
      "end": 7.1,
      "before": "Hôm nay chúng ta họp giao bang tuần.",
      "after": "Hôm nay chúng ta họp giao ban tuần.",
      "marked": "Hôm nay chúng ta họp giao [bang→ban] tuần.",
      "segments": [
        {"type": "same", "text": "Hôm nay chúng ta họp giao"},
        {"type": "fix", "before": "bang", "after": "ban", "by": "protonx"},
        {"type": "same", "text": "tuần."}
      ],
      "fixes": [
        {"wrong": "bang", "right": "ban", "by": "protonx"}
      ]
    }
  ]
}
```

### E.9. `GET /api/book-metadata`: danh sách thông tin xuất bản đã lưu

**Response `200`:**

```json
{
  "total": 1,
  "limit": 20,
  "offset": 0,
  "items": [
    {
      "request_id": "7b2e9d4c1a0f4b3e8d6c5a4b3c2d1e0f",
      "document_id": "SACH-0001",
      "s3_path": "s3://tai-lieu/2026/sach-abc/",
      "fields": {
        "nha_xuat_ban": "Nhà xuất bản Chính trị quốc gia Sự thật",
        "ban_bien_tap": "Nguyễn Văn A; Trần Thị B",
        "isbn": "978-604-57-1234-5",
        "luu_chieu": "Quý IV năm 2025",
        "luu_chieu_thang": 12,
        "luu_chieu_nam": 2025,
        "giam_doc_tong_bien_tap": "Lê Văn C",
        "che_ban": "Phạm Thị D",
        "trinh_bay_bia": "Hoàng Văn E",
        "dia_chi_nxb": "Số 6/86 Duy Tân, Cầu Giấy, Hà Nội",
        "dien_thoai_nxb": "024.38221234",
        "sdt_quan_ly_xuat_ban": "024.38221235",
        "sdt_tong_bien_tap": "024.38221236",
        "fax_nxb": "024.38221237",
        "dia_chi_co_so_in": "Công ty in ABC, Hà Nội",
        "ngay_xac_nhan_dkxb": "Số ĐKXB: 1234-2025/CXBIPH/1-10/CTQG"
      },
      "field_count": 14,
      "source": "text_file",
      "file": "sach-abc.docx",
      "page": 1,
      "text": "NHÀ XUẤT BẢN CHÍNH TRỊ QUỐC GIA SỰ THẬT\nSố 6/86 Duy Tân, Cầu Giấy, Hà Nội\nChịu trách nhiệm xuất bản\nGIÁM ĐỐC - TỔNG BIÊN TẬP\nLê Văn C",
      "error": "",
      "input": {"files": ["sach-abc.docx", "sach-abc.pdf"], "skipped": []},
      "created_at": "2026-11-18T09:15:03.500000+00:00"
    }
  ]
}
```

Lưu ý: ở dữ liệu đã lưu, thông tin xuất bản nằm trong trường **`fields`**. Ở kết quả trả trực tiếp (B.4, B.5, D.3), trường đó tên là **`thong_tin`**. Nội dung giống nhau.

### E.10. `GET /api/book-metadata/{request_id}`: một bản ghi

**Response `200`:** một object giống một phần tử `items` ở E.9.

Lỗi `404`:

```json
{
  "detail": "Book metadata not found"
}
```

---

## Phần F. Khác

### F.1. `GET /health`

Không cần đăng nhập. Dùng để kiểm tra backend còn chạy.

**Response `200`:**

```json
{
  "status": "ok"
}
```

---

## Phần G. Mã mẫu đầy đủ phía client (JavaScript)

Luồng: tải file lên, hỏi trạng thái định kỳ, nhận kết quả OCR.

```js
const BASE_URL = "http://<backend-host>:9090";
const USERNAME = "<username>";
const PASSWORD = "<password>";
const AUTH = "Basic " + btoa(`${USERNAME}:${PASSWORD}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, options = {}) {
  const res = await fetch(BASE_URL + path, {
    ...options,
    headers: { Authorization: AUTH, ...(options.headers || {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail;
    throw new Error(`HTTP ${res.status}: ${msg}`);
  }
  return body;
}

async function waitResult(requestId, { intervalMs = 4000, timeoutMs = 2 * 60 * 60 * 1000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const st = await api(`/api/request-status/${requestId}`);
    if (st.status === "SUCCESS") return st.result;
    if (st.status === "FAILURE") throw new Error(st.error);
    await sleep(intervalMs);
  }
  throw new Error("Hết thời gian chờ kết quả");
}

// 1. Tải file lên
async function ocrFiles(fileList) {
  const fd = new FormData();
  for (const f of fileList) fd.append("files", f);
  fd.append("group", "false");
  const { jobs, skipped } = await api("/api/uploads/ocr", { method: "POST", body: fd });
  skipped.forEach((s) => console.warn("Bỏ qua", s.file, s.reason));
  // 2. Chờ kết quả từng việc
  return Promise.all(jobs.map(async (job) => ({ files: job.files, result: await waitResult(job.request_id) })));
}

// 3. Dùng kết quả
// const [first] = await ocrFiles(input.files);
// first.result.text     -> toàn văn
// first.result.changes  -> các dòng đã sửa (tô màu theo segments)
```

Luồng với file trên S3 kèm webhook: gọi B.1 với `callback_url`, lưu `request_id`, rồi xử lý kết quả trong API nhận webhook (ví dụ ở mục B.5). Không cần hỏi trạng thái định kỳ.
