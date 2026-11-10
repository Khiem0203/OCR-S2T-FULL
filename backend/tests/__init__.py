import os
import tempfile

# Celery cần broker; test chạy trong bộ nhớ.
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")
os.environ.setdefault("TEMP_DIR", tempfile.mkdtemp(prefix="fullpage-tests-"))
