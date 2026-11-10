"""Gửi kết quả tới callback_url."""
import logging
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger("ocr.webhook")


def webhook_allowed(url: str, allowed_hosts: tuple) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    return not allowed_hosts or parsed.hostname.lower() in allowed_hosts


def send_webhook(url: str, payload: dict, timeout: float, retries: int) -> bool:
    host = urlparse(url).hostname or "?"
    for attempt in range(1, retries + 1):
        try:
            resp = httpx.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            log.info("webhook delivered to %s (HTTP %d)", host, resp.status_code)
            return True
        except httpx.HTTPError as exc:
            log.warning("webhook to %s failed (attempt %d/%d): %s", host, attempt, retries, type(exc).__name__)
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))
    return False
