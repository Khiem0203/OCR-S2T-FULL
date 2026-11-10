import logging

from celery import Celery
from celery.signals import worker_process_init
from kombu import Exchange, Queue

from ..config import QUEUES, load_settings

log = logging.getLogger("ocr.celery")

_settings = load_settings()
if not _settings.broker_url or not _settings.result_backend:
    raise ValueError("CELERY_BROKER_URL and CELERY_RESULT_BACKEND must be set.")

celery_app = Celery("fullpage", broker=_settings.broker_url, backend=_settings.result_backend, include=["app.worker.tasks", "app.worker.speech_tasks"])

celery_app.conf.task_queues = tuple(
    Queue(q, Exchange(q.replace("_queue", "_exchange"), type="direct"), routing_key=q.replace("_queue", "_task")) for q in QUEUES
)

_ROUTES = {
    "fullpage_download_task": "fullpage_prepare_queue",
    "fullpage_ocr_task": "fullpage_ocr_queue",
    "fullpage_spell_task": "fullpage_spell_queue",
    "save_fullpage_to_db_task": "fullpage_db_queue",
    "speech_download_task": "speech_prepare_queue",
    "speech_asr_task": "speech_asr_queue",
    "speech_spell_task": "speech_spell_queue",
    "save_speech_to_db_task": "speech_db_queue",
}
celery_app.conf.task_routes = {
    task: {"queue": q, "routing_key": q.replace("_queue", "_task")} for task, q in _ROUTES.items()
}

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    database_engine_options={"pool_recycle": 3600},
    result_expires=_settings.result_expires,
    worker_prefetch_multiplier=1,  # Mỗi tiến trình lấy 1 task
    task_acks_late=True,  # Ack khi task xong
    task_reject_on_worker_lost=True,  # Worker chết thì trả task về hàng đợi
)


@worker_process_init.connect
def _warm_dictionaries(**_):
    """Nạp từ điển một lần mỗi tiến trình."""
    from ..pipeline.fullpage import get_lexicon

    try:
        get_lexicon(load_settings())
    except Exception:
        log.exception("could not load dictionaries; spelling checks will fail until this is fixed")
