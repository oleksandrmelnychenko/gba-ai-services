"""Hand freed heap pages back to the OS after a large rebuild.

A portfolio or product-facts rebuild allocates several times the size of what it keeps
(driver rows, Decimals, the JSON text written to Redis). CPython frees those objects, but glibc
keeps the pages in its arenas, so the resident size ratchets up to the rebuild peak and stays
there. Trimming after the rebuild returns them.
"""
from __future__ import annotations

import ctypes
import gc

from app.core.logging import get_logger

log = get_logger("memory")
_libc: ctypes.CDLL | None | bool = None


def resident_mb() -> float | None:
    try:
        with open("/proc/self/statm") as handle:
            return round(int(handle.read().split()[1]) * 4096 / 1_000_000, 1)
    except (OSError, ValueError, IndexError):
        return None


def release_free_memory() -> bool:
    global _libc
    before = resident_mb()
    gc.collect()
    if _libc is None:
        try:
            _libc = ctypes.CDLL("libc.so.6")
        except OSError:
            _libc = False
    if not _libc:
        return False
    try:
        _libc.malloc_trim(0)
    except (AttributeError, OSError):
        _libc = False
        return False
    log.info("memory_trimmed", rss_before_mb=before, rss_after_mb=resident_mb())
    return True
