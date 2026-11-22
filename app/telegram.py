from __future__ import annotations

import logging
import math
import os
import re
import time
from collections.abc import Callable
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_RETRY_AFTER_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 30.0

SendStatus = Literal["sent", "failed", "dry_run"]

_BOT_TOKEN_PATH_RE = re.compile(r"/bot[^/\s\"']+/")


class _TelegramTokenRedactFilter(logging.Filter):
    """Ensure Telegram bot tokens never appear in httpx/httpcore log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "/bot" in record.msg:
            record.msg = _BOT_TOKEN_PATH_RE.sub("/bot<REDACTED>/", record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(self._sanitize(arg) for arg in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: self._sanitize(v) for k, v in record.args.items()}
            else:
                record.args = self._sanitize(record.args)
        return True

    @staticmethod
    def _sanitize(value: Any) -> Any:
        if isinstance(value, (str, httpx.URL)):
            text = str(value)
            if "/bot" in text:
                return _BOT_TOKEN_PATH_RE.sub("/bot<REDACTED>/", text)
        return value


def _install_redaction_filters() -> None:
    for logger_name in ("httpx", "httpcore"):
        target = logging.getLogger(logger_name)
        if not any(isinstance(f, _TelegramTokenRedactFilter) for f in target.filters):
            target.addFilter(_TelegramTokenRedactFilter())


_install_redaction_filters()


def _clean_secret(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _extract_retry_after(response: httpx.Response) -> float | int:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return DEFAULT_RETRY_AFTER_SECONDS

    if not isinstance(payload, dict):
        return DEFAULT_RETRY_AFTER_SECONDS

    params = payload.get("parameters")
    if not isinstance(params, dict):
        return DEFAULT_RETRY_AFTER_SECONDS

    raw = params.get("retry_after")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return DEFAULT_RETRY_AFTER_SECONDS
    if not math.isfinite(raw) or raw < 0:
        return DEFAULT_RETRY_AFTER_SECONDS
    return raw


class Notifier:
    """Telegram Bot API notifier with mandatory dry_run fallback and 429 retry."""

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        dry_run: bool = True,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = _clean_secret(token)
        self._chat_id = _clean_secret(chat_id)
        self.dry_run: bool = bool(dry_run) or self._token is None or self._chat_id is None
        self._client = client
        self._sleep = sleep

    def __repr__(self) -> str:
        return (
            f"Notifier(dry_run={self.dry_run!r}, "
            f"has_token={self._token is not None!r}, "
            f"has_chat_id={self._chat_id is not None!r})"
        )

    def send(self, text: str) -> SendStatus:
        if self.dry_run:
            return "dry_run"

        assert self._token is not None and self._chat_id is not None
        url = f"{TELEGRAM_API_BASE}/bot{self._token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text}

        owns_client = self._client is None
        active_client = (
            httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=False)
            if owns_client
            else self._client
        )

        try:
            for attempt in range(2):
                try:
                    response = active_client.post(
                        url,
                        json=payload,
                        timeout=DEFAULT_TIMEOUT_SECONDS,
                    )
                except httpx.HTTPError as exc:
                    logger.warning("Telegram send failed: %s", type(exc).__name__)
                    return "failed"

                if response.status_code == 200:
                    try:
                        data = response.json()
                    except (ValueError, TypeError):
                        data = None
                    if isinstance(data, dict) and data.get("ok") is False:
                        logger.warning("Telegram API returned ok=false with HTTP 200")
                        return "failed"
                    return "sent"

                if response.status_code == 429 and attempt == 0:
                    retry_after = _extract_retry_after(response)
                    if retry_after > MAX_RETRY_AFTER_SECONDS:
                        logger.warning("Telegram retry_after too large; giving up")
                        return "failed"
                    self._sleep(retry_after)
                    continue

                logger.warning("Telegram send failed with HTTP %d", response.status_code)
                return "failed"

            return "failed"
        finally:
            if owns_client:
                active_client.close()


def notifier_from_env(
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Notifier:
    """Create a Notifier from TELEGRAM_* environment variables (defaults to dry_run)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    raw_dry_run = os.environ.get("TELEGRAM_DRY_RUN", "true").strip().lower()
    dry_run = raw_dry_run != "false"
    return Notifier(
        token=token,
        chat_id=chat_id,
        dry_run=dry_run,
        client=client,
        sleep=sleep,
    )
