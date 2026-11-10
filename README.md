# OCR toàn trang và trích xuất lời nói

| Thư mục | Nội dung |
|---|---|
| [`backend/`](backend/README.md) | API FastAPI + worker Celery: OCR (Chandra), chép lời (PhoWhisper), sửa chính tả (ProtonX + từ điển), PostgreSQL, webhook |
| [`frontend/`](frontend/README.md) | Trang web tải file lên từ máy (nhiều file hoặc cả thư mục), theo dõi việc, xem kết quả và chỗ đã sửa (web mang mục đích demo) |

```
Trình duyệt ──► frontend (0.0.0.0:8080) ──/api──► backend API (127.0.0.1:9090) ──► RabbitMQ ──► worker Celery
                trang web + chuyển tiếp           lưu file tải lên, xếp việc                    OCR / chép lời / chính tả
```

Chạy trên Linux:

```bash
cd backend && VENV_BIN=$PWD/.venv/bin ./start-service.sh                       # API + worker (xem backend/README.md)
cd ../frontend && VENV_BIN=../backend/.venv/bin FRONTEND_PORT=8080 ./run.sh    # trang web ở http://<ip-máy>:8080
```

API và worker phải chạy trên cùng máy (hoặc dùng chung ổ đĩa `UPLOAD_DIR`) vì file tải lên được lưu ra đĩa rồi worker đọc lại.
