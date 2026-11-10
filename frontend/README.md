# Frontend: tải file lên, theo dõi và xem kết quả

Trang web thuần HTML/CSS/JS (không cần build, không tải gì từ CDN) cùng một server Python nhỏ (`server.py`):

- phục vụ trang trong `public/`;
- chuyển tiếp mọi `/api/*` và `/health` sang backend, nên trình duyệt chỉ cần mở **một** địa chỉ và backend không cần mở CORS.

```
public/
├── index.html   khung trang
├── style.css    giao diện (tự sáng/tối theo máy)
└── app.js       chọn file, tải lên, theo dõi việc, xem kết quả
server.py        trang tĩnh + chuyển tiếp /api sang BACKEND_URL
run.sh           chạy trên Linux
.env             FRONTEND_HOST, FRONTEND_PORT, BACKEND_URL
frontend.service mẫu systemd
```

## Chạy trên Linux

Dùng chung venv với backend (đã có `starlette`, `uvicorn`, `httpx`):

```bash
cd frontend
VENV_BIN=../backend/.venv/bin ./run.sh
# Frontend on http://0.0.0.0:8080 -> backend http://127.0.0.1:9090
```

Đổi cổng và địa chỉ trong `frontend/.env` hoặc ngay trên dòng lệnh:

```bash
FRONTEND_PORT=3000 BACKEND_URL=http://10.0.0.5:9090 VENV_BIN=../backend/.venv/bin ./run.sh
```

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `FRONTEND_HOST` | `0.0.0.0` | Nghe trên mọi card mạng; máy khác trong mạng mở được `http://<ip-máy>:<cổng>` |
| `FRONTEND_PORT` | `8080` | Cổng tùy ý |
| `BACKEND_URL` | `http://127.0.0.1:9090` | Địa chỉ API backend (`API_PORT` của backend) |

Chạy nền bằng systemd: `frontend.service` ghi cố định cổng 8080, nghe `0.0.0.0`, backend `http://127.0.0.1:9090`,
venv `/opt/ocr-fullpage/backend/.venv` (không đọc `.env`; muốn đổi thì sửa thẳng trong file), rồi
`sudo cp frontend.service /etc/systemd/system/ && sudo systemctl enable --now frontend`.
Nhớ mở cổng trên tường lửa nếu cần, ví dụ `sudo ufw allow 8080/tcp`.

## Dùng

1. Nhập **API key** (một khóa trong `API_KEYS` của backend; lưu trên trình duyệt). Chấm xanh cạnh ô là backend đang chạy.
2. Chọn tab **OCR tài liệu** hoặc **Trích xuất lời nói**.
3. Chọn file bằng **Chọn file** (chọn được nhiều file), **Chọn thư mục** (lấy mọi file trong thư mục và thư mục con),
   hoặc **kéo thả** file / thư mục vào khung. File không hỗ trợ bị bỏ qua và liệt kê ra.
4. **Gộp thành một tài liệu**: tự bật khi chọn thư mục.
   - OCR: cả lô là một tài liệu. Ảnh là các trang (sắp theo tên, `trang2` trước `trang10`); có Word/txt dùng được thì lấy text, không OCR.
   - Chép lời: nối các file theo tên thành một bản chép lời.
   - Tắt: mỗi file là một việc riêng.
5. **Tải lên & xử lý**: có thanh tiến độ. Mỗi việc hiện trong **Việc đã gửi** và tự cập nhật 3 giây một lần.
6. Bấm vào việc đã xong để xem:
   - **Văn bản**: OCR theo từng trang (ghi rõ OCR / text PDF / file văn bản); chép lời theo từng đoạn có mốc thời gian.
     Bấm mốc thời gian để nghe lại đúng đoạn đó (khi file còn trong phiên trình duyệt đã tải lên).
   - **Chỗ đã sửa**: từ cũ gạch đỏ, từ mới xanh, kèm cách sửa (`protonx`, `dict`, `replace`).
   - **Từ lạ**: từ không có trong từ điển, kèm ngữ cảnh.
   - **Đầu vào**: file đã dùng, file bị bỏ qua, lý do bỏ lớp text PDF.
   - **Tải .txt**: tải văn bản về máy.

Kết quả lấy từ `/api/request-status/{id}` (lưu trong Celery tối đa `RESULT_EXPIRES` giây); danh sách việc lưu trên trình duyệt.
