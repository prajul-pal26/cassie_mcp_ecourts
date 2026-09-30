"""Process-wide runtime singletons + job dispatch.

Holds the reliability stack (cache / coalescer / breaker / rate bucket),
the JobStore, and the find-job thread pool. Built once per process with
double-checked locking so concurrent cold-starts don't each construct their
own state (which would defeat the rate cap and re-run orphan recovery).

Framework-agnostic — the FastAPI routers depend on this, not the reverse.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from app.core import config
from app.reliability.gateway import Breaker, Coalescer, RateBucket, SqliteCache
from app.reliability.jobs import JobStore
from app.search.workers import run_find

log = logging.getLogger("core.runtime")

_components: dict = {}
_components_lock = threading.Lock()
_bridges: dict = {}
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()


def set_bridges(**kwargs) -> None:
    """Inject bridge functions per mode, e.g. set_bridges(party=..., advocate=...)."""
    global _bridges
    _bridges.update(kwargs)


def get_bridges() -> dict:
    return _bridges


def get_components() -> dict:
    """Lazy-build the reliability stack (double-checked locking)."""
    if _components:
        return _components
    with _components_lock:
        if _components:
            return _components
        _components["store"] = JobStore(path=str(config.JOBS_DB_PATH))
        _components["cache"] = SqliteCache(
            path=str(config.CACHE_DB_PATH),
            default_ttl=config.CACHE_TTL_SEARCH,
            max_size_mb=config.CACHE_MAX_SIZE_MB,
        )
        _components["coalescer"] = Coalescer(max_wait=config.COALESCER_MAX_WAIT)
        _components["breaker"] = Breaker(
            fail_max=config.BREAKER_FAIL_MAX,
            reset_timeout=config.BREAKER_RESET_TIMEOUT,
            waf_reset_timeout=config.BREAKER_WAF_RESET_TIMEOUT,
        )
        # With Webshare proxies configured, load is spread across many IPs, so
        # the global upstream cap scales up (accounts × IPs × safe-RPS/IP).
        # No proxies → the original single-IP default.
        from app.upstream import proxy_pool
        rps = proxy_pool.effective_rps_cap(config.UPSTREAM_RPS_CAP)
        burst = max(config.UPSTREAM_BURST, int(rps // 3))
        _components["bucket"] = RateBucket(rps=rps, burst=burst)
        if proxy_pool.enabled():
            log.info("proxy pool ON: %s", proxy_pool.summary())
            log.info("upstream rate cap raised to %.0f RPS (burst %d)", rps, burst)
        return _components


def get_executor() -> ThreadPoolExecutor:
    """One process-wide pool for find-job workers. Sized far above the rate
    cap — workers spend most time blocked on the token bucket, so we want
    plenty ready to grab a token the instant one frees up."""
    global _executor
    if _executor is not None:
        return _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=config.FIND_EXECUTOR_MAX_WORKERS,
                thread_name_prefix="findjob",
            )
        return _executor


def dispatch_find(jid: str) -> None:
    """Worker callable submitted to the executor for each find job."""
    comp = get_components()
    try:
        run_find(
            store=comp["store"], jid=jid,
            cache=comp["cache"], coalescer=comp["coalescer"],
            breaker=comp["breaker"], bucket=comp["bucket"],
            bridge_funcs=_bridges,
            cache_ttl=config.CACHE_TTL_SEARCH,
            stale_ok_horizon=config.CACHE_STALE_OK_HORIZON,
        )
    except Exception as e:  # pragma: no cover - defensive
        log.exception("dispatch_find: unhandled error: %r", e)
        try:
            comp["store"].finish_error(jid, f"worker crashed: {e!r}")
        except Exception:
            pass


def wire_production_bridges() -> None:
    """Wire the real upstream bridge functions. Must run before serving —
    otherwise `_bridges` is empty and every find task is skipped."""
    try:
        from app.upstream.bridge import bridge_party, bridge_advocate
        set_bridges(party=bridge_party, advocate=bridge_advocate)
        log.info("production bridges wired (party, advocate)")
    except Exception as e:
        log.warning("could not wire production bridges: %r", e)


def warm_singletons() -> None:
    """Eagerly build the reliability stack, executor, and bridges so the
    first burst of concurrent requests doesn't hit cold-init."""
    get_components()
    get_executor()
    wire_production_bridges()
