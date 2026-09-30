"""Health / liveness routes."""
from __future__ import annotations

from fastapi import APIRouter

from app.core import config
from app.core.runtime import get_components, get_executor
from app.routers.common import json_response

router = APIRouter(tags=["System"])


@router.get("/health",
            summary="Liveness probe — breaker state, queue depth, config (O(1))")
async def health():
    # Liveness MUST stay O(1) and touch only in-memory state. Do NOT call
    # cache.size_bytes() here — a full-table SUM under write load tipped the
    # 5s health check past its deadline during the 2026-06-06 outage. Cache
    # size lives on /health/cache for dashboards.
    from app.upstream import proxy_pool
    comp = get_components()
    return json_response({
        "breaker_state": comp["breaker"].state,
        "queue_depth": getattr(get_executor()._work_queue, "qsize", lambda: 0)(),
        "proxy_pool": proxy_pool.summary(),
        "config": config.as_dict(),
    })


@router.get("/health/cache",
            summary="Cache size in MB (heavier full-table scan — kept off /health)")
async def health_cache():
    # Heavier introspection (full-table SUM) kept OFF the liveness path.
    comp = get_components()
    return json_response({
        "cache_size_mb": round(comp["cache"].size_bytes() / (1024 * 1024), 2),
    })
