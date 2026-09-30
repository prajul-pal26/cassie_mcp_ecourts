"""R3 — Metadata pre-warming route (cron-triggered).

Walks state → district for DC (and HC unless skipped) and primes the 24h
dropdown cache. Gated by an `X-Warm-Token` header. The whole walk is blocking
(hundreds of rate-bucketed calls) so it runs off the event loop.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool

from app.core import config
from app.core.runtime import get_components
from app.reliability.gateway import GatewayCall, UpstreamDown
from app.routers.common import json_response

log = logging.getLogger("routers.admin")

router = APIRouter(tags=["eCourts · Admin"])


@router.post("/api/admin/warm",
             summary="Cron cache pre-warmer (X-Warm-Token gated)",
             description="Pre-loads states/districts metadata into the cache so "
             "real users always hit a warm cache. Called by a scheduled cron "
             "with the X-Warm-Token header.")
async def admin_warm(request: Request):
    # Fail CLOSED. This used to read `if expected and supplied != expected`,
    # which skipped the check entirely whenever WARM_TOKEN was unset: a missing
    # env var silently published an endpoint that drives hundreds of upstream
    # calls. A missing secret is a misconfiguration, not consent to serve
    # anonymously. Inherited from gateway/routes/admin_warm.py, where the same
    # branch is commented "dev-friendly default"; convenience in dev is not
    # worth an open endpoint in prod.
    #
    # This means WARM_TOKEN MUST be set on the Fly app
    # (`fly secrets set WARM_TOKEN=... -a cassie-gateway-fastapi`) or every warm
    # cycle 401s. The caller already holds itself to the same rule:
    # app/api/cron/warm-ecourts-metadata/route.ts refuses to run without it.
    expected = os.getenv("WARM_TOKEN")
    if not expected:
        log.error("WARM_TOKEN is not set; refusing /api/admin/warm. "
                  "Set it on the app or the cache never warms.")
        return json_response({"error": "unauthorized"}, 401)
    # Constant-time: a plain != leaks the token bytewise to a caller who can
    # time it, and this one guards real upstream spend.
    supplied = request.headers.get("X-Warm-Token") or ""
    if not secrets.compare_digest(supplied, expected):
        return json_response({"error": "unauthorized"}, 401)

    skip_hc = (request.query_params.get("skip_hc") == "1")
    only_court_type = request.query_params.get("only")
    result = await run_in_threadpool(_run_warm, skip_hc, only_court_type)
    return json_response(result)


def _run_warm(skip_hc: bool, only_court_type) -> dict:
    from app.upstream.bridge import _get_v4

    comp = get_components()

    def _wrap(key: str, fetch_fn, ttl: float = config.CACHE_TTL_DROPDOWNS):
        gw = GatewayCall(
            cache=comp["cache"], coalescer=comp["coalescer"],
            breaker=comp["breaker"], bucket=comp["bucket"],
            key=key, ttl=ttl, stale_ok_horizon=config.CACHE_STALE_OK_HORIZON,
        )
        try:
            res = gw.run(fetch_fn)
            return True, res.source
        except UpstreamDown:
            return False, "upstream_down"
        except Exception as e:
            return False, f"{type(e).__name__}: {str(e)[:80]}"

    started = time.monotonic()
    stats: dict[str, Any] = {
        "calls": 0, "errors": 0, "cache_hits": 0, "fresh": 0, "stale": 0,
        "by_court_type": {}, "started_at": time.time(),
    }
    errors: list = []

    if only_court_type in ("dc", "hc"):
        court_types = [only_court_type]
    else:
        court_types = ["dc"]
        if not skip_hc:
            court_types.append("hc")

    for ct in court_types:
        ct_stats = {"states": 0, "districts": 0, "errors": 0}
        ok, source = _wrap(key=f"meta:states:{ct}",
                           fetch_fn=lambda ct=ct: _get_v4(ct).list_states())
        stats["calls"] += 1
        if ok:
            stats[source] = stats.get(source, 0) + 1
            ct_stats["states"] = 1
        else:
            stats["errors"] += 1
            ct_stats["errors"] += 1
            errors.append(f"states({ct}): {source}")
            stats["by_court_type"][ct] = ct_stats
            continue

        cached = comp["cache"].get(f"meta:states:{ct}") or {}
        for state in _safe_extract_states(cached):
            state_code = str(state.get("state_code") or state.get("code") or "").strip()
            if not state_code:
                continue
            ok, source = _wrap(
                key=f"meta:districts:{ct}:{state_code}",
                fetch_fn=lambda ct=ct, sc=state_code: _get_v4(ct).list_districts(sc))
            stats["calls"] += 1
            if ok:
                stats[source] = stats.get(source, 0) + 1
                ct_stats["districts"] += 1
            else:
                stats["errors"] += 1
                ct_stats["errors"] += 1
                errors.append(f"districts({ct},{state_code}): {source}")

        stats["by_court_type"][ct] = ct_stats

    stats["cache_hits"] = stats.get("cache", 0)
    stats["duration_s"] = round(time.monotonic() - started, 2)
    return {
        "ok": stats["errors"] == 0,
        "stats": stats,
        "errors_sample": errors[:10],
        "court_types_warmed": court_types,
    }


def _safe_extract_states(raw: Any) -> list:
    if not isinstance(raw, dict):
        return []
    states = raw.get("states")
    if isinstance(states, list):
        return [s for s in states if isinstance(s, dict)]
    out = []
    for k, v in raw.items():
        if isinstance(v, dict) and (
            v.get("state_code") or v.get("code") or v.get("state_name")
        ):
            out.append(v)
    return out
