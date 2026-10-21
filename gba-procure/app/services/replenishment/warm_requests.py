"""Bounded singleflight warming; request cancellation never owns a shared build."""
from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from app.core.logging import get_logger

log = get_logger("procure_warm_requests")


class BoundedWarmQueue:
    def __init__(self, capacity: int = 8, cooldown_seconds: float = 30):
        if not 1 <= capacity <= 32 or not 0 <= cooldown_seconds <= 600:
            raise ValueError("invalid warm queue bounds")
        self._capacity = capacity
        self._cooldown = cooldown_seconds
        self._lock = Lock()
        self._pending: set[str] = set()
        self._failed_until: OrderedDict[str, float] = OrderedDict()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="procure-request-warm")

    def submit(self, key: str, work: Callable[[], None]) -> bool:
        """True means already queued or accepted; false means bounded busy/cooldown."""
        with self._lock:
            if key in self._pending:
                return True
            if len(self._pending) >= self._capacity or self._failed_until.get(key, 0) > time.monotonic():
                return False
            self._pending.add(key)
        try:
            self._executor.submit(self._run, key, work)
        except RuntimeError:
            with self._lock:
                self._pending.discard(key)
            return False
        return True

    def _run(self, key: str, work: Callable[[], None]) -> None:
        failed = False
        try:
            work()
        except Exception as error:  # noqa: BLE001
            failed = True
            # Never log URLs, keys, financial payloads or arbitrary exception text.
            log.warning("request_warm_failed", failure_type=type(error).__name__)
        finally:
            with self._lock:
                self._pending.discard(key)
                if failed:
                    self._failed_until[key] = time.monotonic() + self._cooldown
                    self._failed_until.move_to_end(key)
                    while len(self._failed_until) > 128:
                        self._failed_until.popitem(last=False)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)


# Executor threads start only on the first submission, never at module import.
REQUEST_WARMS = BoundedWarmQueue()
