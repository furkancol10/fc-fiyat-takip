from __future__ import annotations

import hmac
import math
import os
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import Request

MAX_PRODUCTS = 10
MAX_COMPETITORS = 20
CHECK_COOLDOWN_SECONDS = 30.0
PANEL_RATE_LIMIT_REQUESTS = 30
PANEL_RATE_LIMIT_WINDOW_SECONDS = 60.0

CSRF_COOKIE_NAME = "csrf_token"
CSRF_FIELD_NAME = "csrf_token"


class CheckCooldown:
    """Thread-safe global cooldown shared across /jobs/check and /panel/check."""

    def __init__(
        self,
        cooldown_seconds: float = CHECK_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cooldown_seconds = float(cooldown_seconds)
        self._default_clock = clock
        self._clock = clock
        self._last_run: float | None = None
        self._lock = threading.Lock()

    def set_clock(self, clock: Callable[[], float]) -> None:
        with self._lock:
            self._clock = clock

    def reset(self, *, restore_clock: bool = False) -> None:
        with self._lock:
            self._last_run = None
            if restore_clock:
                self._clock = self._default_clock

    def try_acquire(self) -> tuple[bool, int]:
        with self._lock:
            now = self._clock()
            if self._last_run is not None:
                elapsed = now - self._last_run
                if elapsed < self._cooldown_seconds:
                    remaining = self._cooldown_seconds - elapsed
                    return False, max(1, math.ceil(remaining))
            self._last_run = now
            return True, 0


class IpRateLimiter:
    """Thread-safe in-memory sliding-window rate limiter keyed by client IP."""

    def __init__(
        self,
        max_requests: int = PANEL_RATE_LIMIT_REQUESTS,
        window_seconds: float = PANEL_RATE_LIMIT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_requests = int(max_requests)
        self._window_seconds = float(window_seconds)
        self._default_clock = clock
        self._clock = clock
        self._buckets: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def set_clock(self, clock: Callable[[], float]) -> None:
        with self._lock:
            self._clock = clock

    def reset(self, *, restore_clock: bool = False) -> None:
        with self._lock:
            self._buckets.clear()
            if restore_clock:
                self._clock = self._default_clock

    def check(self, ip: str) -> tuple[bool, int]:
        with self._lock:
            now = self._clock()
            cutoff = now - self._window_seconds

            if len(self._buckets) > 10_000:
                stale_keys = [
                    key
                    for key, q in self._buckets.items()
                    if not q or q[-1] <= cutoff
                ]
                for key in stale_keys:
                    del self._buckets[key]

            bucket = self._buckets.get(ip)
            if bucket is None:
                bucket = deque()
                self._buckets[ip] = bucket

            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= self._max_requests:
                retry_after = max(1, math.ceil(self._window_seconds - (now - bucket[0])))
                return False, retry_after

            bucket.append(now)
            return True, 0


check_cooldown = CheckCooldown()
panel_rate_limiter = IpRateLimiter()


def reset_limits() -> None:
    """Reset in-memory cooldown and rate limiter state (and restore clocks)."""
    check_cooldown.reset(restore_clock=True)
    panel_rate_limiter.reset(restore_clock=True)


def get_client_ip(request: Request) -> str:
    """Resolve client IP from socket peer (or first X-Forwarded-For only if TRUST_PROXY=1)."""
    if os.environ.get("TRUST_PROXY", "0").strip() == "1":
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            first_hop = xff.split(",", 1)[0].strip()
            if first_hop:
                return first_hop
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_csrf_token(cookie_token: str | None, form_token: str | None) -> bool:
    if not cookie_token or not form_token:
        return False
    return hmac.compare_digest(cookie_token, form_token)
