"""itat_pdf_cache.py — tiny in-memory cache for fetched order PDFs.

An order PDF is expensive to fetch (fresh session + captcha solve + search +
download). But a given order's PDF never changes, so once fetched we keep the
bytes and serve every later view instantly — no session, no captcha. This is
what stops "view 20 orders = 20 captcha solves": each unique order is solved at
most once, then cached.

Bounded LRU-ish (evicts oldest) so memory stays capped. Thread-safe (the PDF
endpoints run in a thread pool).
"""

from __future__ import annotations

import threading
from typing import Optional, Tuple

_MAX = 400                      # ~400 PDFs cached (bounded memory)
_cache = {}                    # key -> (filename, pdf_bytes)
_order = []                    # insertion order for eviction
_lock = threading.Lock()


def get(key) -> Optional[Tuple[str, bytes]]:
    with _lock:
        return _cache.get(key)


def put(key, value: Tuple[str, bytes]) -> None:
    with _lock:
        if key not in _cache:
            _order.append(key)
            while len(_order) > _MAX:
                _cache.pop(_order.pop(0), None)
        _cache[key] = value
