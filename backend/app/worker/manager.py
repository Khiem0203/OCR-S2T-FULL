"""Quản lý worker Celery qua Flower."""

import os
import shutil
import subprocess
import time
import json
import httpx
from datetime import datetime
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # Gốc dự án
LOG_DIR = os.path.join(BASE_DIR, "logs")
STATE_DIR = os.path.join(BASE_DIR, "worker_state")

QUEUE_LOG_SUBDIR = {
    "fullpage_prepare_queue": "prepare",
    "fullpage_ocr_queue":     "ocr",
    "fullpage_spell_queue":   "spell",
    "fullpage_db_queue":      "db",
    "speech_prepare_queue":   "speech",
    "speech_asr_queue":       "speech",
    "speech_spell_queue":     "speech",
    "speech_db_queue":        "speech",
}


def _ensure_dirs() -> None:
    for sub in {"", *QUEUE_LOG_SUBDIR.values()}:
        os.makedirs(os.path.join(LOG_DIR, sub), exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)


try:
    _ensure_dirs()
except OSError as exc:  # Không tạo được thư mục; báo lại khi bật worker
    logger.warning("cannot create log/state directories under %s: %s", BASE_DIR, exc)

FLOWER_URL = os.getenv("FLOWER_URL", "http://localhost:5555")
FLOWER_TIMEOUT = 10
_FLOWER_AUTH = tuple(os.environ["FLOWER_BASIC_AUTH"].split(":", 1)) if ":" in os.getenv("FLOWER_BASIC_AUTH", "") else None

CELERY_BIN = os.getenv("CELERY_BIN") or shutil.which("celery") or "celery"
CELERY_APP = "app.worker.celery_app"

WORKER_PRESETS = {
    "fullpage_prepare_queue": {"num_workers": 2, "concurrency": 1, "description": "Download S3 + render pages (I/O + CPU-bound)"},
    "fullpage_ocr_queue":     {"num_workers": 2, "concurrency": 1, "description": "Chandra full-page OCR (GPU-bound)"},
    "fullpage_spell_queue":   {"num_workers": 2, "concurrency": 1, "description": "Dictionary + ProtonX spelling check (GPU-bound)"},
    "fullpage_db_queue":      {"num_workers": 2, "concurrency": 2, "description": "Save to database + webhook (I/O-bound)"},
    "speech_prepare_queue":   {"num_workers": 1, "concurrency": 1, "description": "Download S3 + ffmpeg decode + split audio (CPU-bound)"},
    "speech_asr_queue":       {"num_workers": 1, "concurrency": 1, "description": "PhoWhisper speech-to-text (GPU-bound)"},
    "speech_spell_queue":     {"num_workers": 1, "concurrency": 1, "description": "Dictionary + ProtonX spelling check (GPU-bound)"},
    "speech_db_queue":        {"num_workers": 1, "concurrency": 2, "description": "Save transcript + webhook (I/O-bound)"},
}



def _flower_get(path: str) -> Optional[dict]:
    try:
        resp = httpx.get(f"{FLOWER_URL}{path}", timeout=FLOWER_TIMEOUT, auth=_FLOWER_AUTH)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Flower GET {path} failed: {e}")
        return None


def _flower_post(path: str, **kwargs) -> bool:
    """True nếu thành công."""
    try:
        resp = httpx.post(f"{FLOWER_URL}{path}", timeout=FLOWER_TIMEOUT, auth=_FLOWER_AUTH, **kwargs)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Flower POST {path} failed: {e}")
        return False


def _get_workers_for_queue(queue_name: str) -> List[str]:
    """Các worker đang nhận hàng đợi."""
    data = _flower_get("/api/workers?refresh=true")
    if not data:
        return []

    workers = []
    for worker_name, info in data.items():
        active_queues = info.get("active_queues") or []
        for q in active_queues:
            if q.get("name") == queue_name:
                workers.append(worker_name)
                break
    return workers


def _get_all_workers() -> dict:
    return _flower_get("/api/workers?refresh=true") or {}


def _wait_for_worker_online(worker_hostname: str, timeout: int = 30) -> bool:
    """Chờ worker hiện trên Flower."""
    start = time.time()
    while time.time() - start < timeout:
        data = _flower_get("/api/workers")
        if data:
            for name in data:
                if worker_hostname.replace("%h", "") in name:
                    return True
        time.sleep(2)
    return False



def start_workers(queue_name: str, num_workers: int, concurrency: int) -> Dict:
    """Bật worker cho hàng đợi rồi kiểm tra trên Flower."""
    if num_workers <= 0:
        return {"status": "skipped", "message": f"num_workers={num_workers}", "pids": []}

    _ensure_dirs()
    pids = []
    failed = []

    for i in range(1, num_workers + 1):
        worker_hostname = f"{queue_name}_worker_{i}@%h"
        subdir = QUEUE_LOG_SUBDIR.get(queue_name, "")
        log_file = os.path.join(LOG_DIR, subdir, f"{queue_name}_worker_{i}.log")

        cmd = [
            CELERY_BIN, "-A", CELERY_APP, "worker",
            "--loglevel=INFO",
            f"--queues={queue_name}",
            f"--hostname={worker_hostname}",
            f"--concurrency={concurrency}",
            f"--logfile={log_file}",
        ]

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
                cwd=BASE_DIR
            )
            pids.append({"worker_id": i, "pid": process.pid, "log_file": log_file})
            logger.info(f"Started {queue_name} worker {i} (PID: {process.pid})")

        except Exception as e:
            failed.append({"worker_id": i, "error": str(e)})
            logger.error(f"Failed to start {queue_name} worker {i}: {e}")

    if failed:
        return {
            "status": "partial",
            "message": f"Started {len(pids)}/{num_workers} workers, {len(failed)} failed",
            "pids": pids, "failed": failed
        }

    return {
        "status": "success",
        "message": f"Started {len(pids)} worker(s) for {queue_name}",
        "pids": pids
    }


def stop_workers(queue_name: str) -> Dict:
    """Tắt worker qua Flower, không được thì SIGTERM."""
    workers = _get_workers_for_queue(queue_name)

    if not workers:
        logger.warning(f"No workers found for {queue_name} in Flower")
        return {
            "status": "not_found",
            "message": f"No workers found for {queue_name}",
            "stopped": []
        }

    stopped = []
    failed = []

    for worker_name in workers:
        ok = _flower_post(f"/api/worker/shutdown/{worker_name}")
        if ok:
            stopped.append({"worker": worker_name})
            logger.info(f"Shutdown {worker_name} via Flower")
        else:
            failed.append({"worker": worker_name, "reason": "Flower shutdown failed"})
            logger.error(f"Failed to shutdown {worker_name}")

    return {
        "status": "success" if stopped else "error",
        "message": f"Stopped {len(stopped)}/{len(workers)} worker(s) for {queue_name}",
        "stopped": stopped,
        "not_found": failed if failed else None
    }


def restart_workers(queue_name: str, num_workers: int = None, concurrency: int = None) -> Dict:
    stop_result = stop_workers(queue_name)

    time.sleep(3)

    if num_workers is None or concurrency is None:
        if queue_name in WORKER_PRESETS:
            preset = WORKER_PRESETS[queue_name]
            num_workers = num_workers or preset["num_workers"]
            concurrency = concurrency or preset["concurrency"]
        else:
            num_workers = num_workers or len(stop_result.get("stopped", []))
            concurrency = concurrency or 1

    start_result = start_workers(queue_name, num_workers, concurrency)

    return {
        "status": "success",
        "message": f"Restarted {queue_name}",
        "stop_result": stop_result,
        "start_result": start_result
    }


def get_worker_status(queue_name: str = None) -> Dict:
    all_workers = _get_all_workers()
    workers = []

    for worker_name, info in all_workers.items():
        active_queues = [q.get("name", "") for q in (info.get("active_queues") or [])]

        if queue_name and queue_name not in active_queues:
            continue

        stats = info.get("stats") or {}
        workers.append({
            "worker_name": worker_name,
            "queue": ", ".join(active_queues) if active_queues else "none",
            "status": "running",
            "stats": {
                "pool_size": stats.get("pool", {}).get("max-concurrency", "?"),
                "tasks_completed": stats.get("total", {}).get("tasks.total", 0),
            }
        })

    return {
        "total_workers": len(workers),
        "workers": workers,
        "filter": queue_name,
        "source": "flower"
    }


# Tạm dừng / chạy lại hàng đợi

def pause_queue(queue_name: str) -> Dict:
    """Worker vẫn sống nhưng ngừng nhận hàng đợi."""
    workers = _get_workers_for_queue(queue_name)

    if not workers:
        if _load_pause_state(queue_name):
            return {
                "status": "already_paused",
                "message": f"Queue '{queue_name}' is already paused"
            }
        return {
            "status": "not_found",
            "message": f"No workers consuming '{queue_name}'"
        }

    if queue_name in WORKER_PRESETS:
        preset = WORKER_PRESETS[queue_name]
        _save_pause_state(queue_name, preset["num_workers"], preset["concurrency"])
    else:
        _save_pause_state(queue_name, len(workers), 1)

    paused_workers = []
    failed_workers = []

    for worker_name in workers:
        ok = _flower_post(
            f"/api/worker/queue/cancel-consumer/{worker_name}",
            params={"queue": queue_name}
        )
        if ok:
            paused_workers.append(worker_name)
            logger.info(f"Cancelled consumer '{queue_name}' on {worker_name}")
        else:
            failed_workers.append(worker_name)
            logger.error(f"Failed to cancel consumer '{queue_name}' on {worker_name}")

    if paused_workers:
        return {
            "status": "paused",
            "message": f"Paused '{queue_name}' on {len(paused_workers)} worker(s)",
            "paused_workers": paused_workers,
            "failed_workers": failed_workers if failed_workers else None
        }

    return {
        "status": "error",
        "message": f"Failed to pause '{queue_name}' on all workers",
        "failed_workers": failed_workers
    }


def resume_queue(queue_name: str) -> Dict:
    all_workers = _get_all_workers()

    if not all_workers:
        # Không còn worker: bật mới
        logger.warning(f"No workers found in Flower, starting new ones for {queue_name}")
        pause_state = _load_pause_state(queue_name)
        if pause_state:
            num_workers = pause_state["num_workers"]
            concurrency = pause_state["concurrency"]
        elif queue_name in WORKER_PRESETS:
            preset = WORKER_PRESETS[queue_name]
            num_workers = preset["num_workers"]
            concurrency = preset["concurrency"]
        else:
            return {"status": "error", "message": f"No workers and no preset for '{queue_name}'"}

        start_result = start_workers(queue_name, num_workers, concurrency)
        _delete_pause_state(queue_name)
        return {
            "status": "resumed",
            "message": f"Started {len(start_result.get('pids', []))} new worker(s) for '{queue_name}'",
            "start_result": start_result
        }

    already_consuming = _get_workers_for_queue(queue_name)
    if already_consuming:
        _delete_pause_state(queue_name)
        return {
            "status": "already_running",
            "message": f"'{queue_name}' already has {len(already_consuming)} consumer(s)",
            "workers": already_consuming
        }

    # Chọn worker nhận hàng đợi
    # Ưu tiên tên khớp
    target_workers = []
    for worker_name in all_workers:
        if queue_name in worker_name:
            target_workers.append(worker_name)

    # Không khớp: dùng tất cả
    if not target_workers:
        target_workers = list(all_workers.keys())

    resumed_workers = []
    failed_workers = []

    for worker_name in target_workers:
        ok = _flower_post(
            f"/api/worker/queue/add-consumer/{worker_name}",
            params={"queue": queue_name}
        )
        if ok:
            resumed_workers.append(worker_name)
            logger.info(f"Added consumer '{queue_name}' on {worker_name}")
        else:
            failed_workers.append(worker_name)
            logger.error(f"Failed to add consumer '{queue_name}' on {worker_name}")

    if resumed_workers:
        _delete_pause_state(queue_name)
        return {
            "status": "resumed",
            "message": f"Resumed '{queue_name}' on {len(resumed_workers)} worker(s)",
            "resumed_workers": resumed_workers,
            "failed_workers": failed_workers if failed_workers else None
        }

    return {
        "status": "error",
        "message": f"Failed to resume '{queue_name}' on all workers",
        "failed_workers": failed_workers
    }


def force_pause_queue(queue_name: str, terminate_active: bool = True) -> Dict:
    """Hủy consumer, thu hồi task đang chờ, có thể dừng task đang chạy."""
    from .celery_app import celery_app

    # B1: ngừng nhận task mới
    pause_result = pause_queue(queue_name)
    logger.info(f"Force pause step 1 - cancel consumer: {pause_result}")

    # B2: lấy task đang chạy và đang chờ
    inspect = celery_app.control.inspect()
    active_tasks = inspect.active() or {}
    reserved_tasks = inspect.reserved() or {}

    revoked_reserved = []
    revoked_active = []
    errors = []

    # B3: thu hồi task đang chờ
    for worker_name, tasks in reserved_tasks.items():
        if queue_name not in worker_name:
            continue
        for task in tasks:
            task_id = task.get("id")
            if not task_id:
                continue
            try:
                celery_app.control.revoke(task_id, terminate=False)
                revoked_reserved.append(task_id)
                logger.info(f"Revoked reserved task {task_id} on {worker_name}")
            except Exception as e:
                errors.append({"task_id": task_id, "error": str(e)})
                logger.error(f"Failed to revoke reserved task {task_id}: {e}")

    # B4: dừng task đang chạy nếu cần
    if terminate_active:
        for worker_name, tasks in active_tasks.items():
            if queue_name not in worker_name:
                continue
            for task in tasks:
                task_id = task.get("id")
                if not task_id:
                    continue
                try:
                    celery_app.control.revoke(task_id, terminate=True, signal='SIGTERM')
                    revoked_active.append(task_id)
                    logger.info(f"Terminated active task {task_id} on {worker_name}")
                except Exception as e:
                    errors.append({"task_id": task_id, "error": str(e)})
                    logger.error(f"Failed to terminate active task {task_id}: {e}")

    total_revoked = len(revoked_reserved) + len(revoked_active)
    return {
        "status": "force_paused",
        "message": (
            f"Force paused '{queue_name}': "
            f"{len(revoked_reserved)} reserved + {len(revoked_active)} active tasks revoked"
        ),
        "pause_result": pause_result,
        "revoked_reserved": revoked_reserved,
        "revoked_active": revoked_active,
        "total_revoked": total_revoked,
        "errors": errors if errors else None
    }


def get_pause_status(queue_name: str) -> Dict:
    consumers = _get_workers_for_queue(queue_name)
    pause_state = _load_pause_state(queue_name)

    # Tạm dừng = có cờ pause và không còn consumer
    is_paused = len(consumers) == 0 and pause_state is not None

    queue_length = 0
    queue_data = _flower_get("/api/queues/length")
    if queue_data and "active_queues" in queue_data:
        for q in queue_data["active_queues"]:
            if q.get("name") == queue_name:
                queue_length = q.get("messages", 0)
                break

    return {
        "queue_name": queue_name,
        "is_paused": is_paused,
        "consumers": consumers,
        "consumer_count": len(consumers),
        "queue_length": queue_length,
        "pause_state": pause_state,
        "source": "flower"
    }


# Lưu trạng thái tạm dừng

def _save_pause_state(queue_name: str, num_workers: int, concurrency: int) -> None:
    state_file = os.path.join(STATE_DIR, f"{queue_name}_pause_state.json")
    state = {
        "queue_name": queue_name,
        "num_workers": num_workers,
        "concurrency": concurrency,
        "paused_at": datetime.now().isoformat()
    }
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)
    logger.info(f"Saved pause state for {queue_name}: {state}")


def _load_pause_state(queue_name: str) -> Optional[Dict]:
    state_file = os.path.join(STATE_DIR, f"{queue_name}_pause_state.json")
    if not os.path.exists(state_file):
        return None
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading pause state: {e}")
        return None


def _delete_pause_state(queue_name: str) -> None:
    state_file = os.path.join(STATE_DIR, f"{queue_name}_pause_state.json")
    if os.path.exists(state_file):
        os.remove(state_file)
        logger.info(f"Deleted pause state for {queue_name}")


def get_presets() -> Dict:
    return WORKER_PRESETS
