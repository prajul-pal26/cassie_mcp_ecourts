"""FastAPI application factory for the cassie-gateway.

A framework-only port of the Flask production gateway: identical endpoint
paths, request shapes, JSON bodies, and status codes, restructured into a
clean package with async handlers. The reliability stack (SqliteCache ->
Coalescer -> Breaker -> RateBucket -> upstream -> store) and the async job
model are reused unchanged.

Run locally:
    uvicorn app.main:app --host 127.0.0.1 --port 9021
or:
    python -m app.main

Interactive docs (Swagger) at /docs, ReDoc at /redoc.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core import config
from app.core.runtime import warm_singletons
from app.routers import (
    admin, aironline, cases, cestat, ecourts_search, find, health, itat,
    metadata, proxies, sat, sci,
)

log = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build the reliability stack + executor and wire upstream bridges before
    # the first request, so a cold-start burst doesn't all hit init and no
    # find task is silently skipped due to unwired bridges.
    warm_singletons()

    # Raise the threadpool limiter so many blocking upstream calls (proxy lanes)
    # can run concurrently. Starlette's default is 40; size it to the rate cap.
    try:
        import anyio
        from app.upstream import proxy_pool
        want = int(max(64, proxy_pool.effective_rps_cap(config.UPSTREAM_RPS_CAP) * 1.5))
        limiter = anyio.to_thread.current_default_thread_limiter()
        if limiter.total_tokens < want:
            limiter.total_tokens = want
            log.info("threadpool limiter raised to %d", want)
    except Exception as e:
        log.warning("could not raise threadpool limiter: %r", e)
    log.info("gateway ready (rps_cap=%.1f burst=%d)",
             config.UPSTREAM_RPS_CAP, config.UPSTREAM_BURST)
    yield


# Swagger sections. eCourts is split into intuitive subsections (CNR / Find /
# Metadata / Admin); each tribunal stays a single section. Order = display order.
OPENAPI_TAGS = [
    {"name": "eCourts · CNR (case details)", "description":
     "Look up a specific case by its 16-char CNR — full details, its order "
     "PDFs, and bulk (multi-CNR) lookup."},
    {"name": "eCourts · Find (party / advocate)", "description":
     "Search for cases by party name or advocate across courts. Runs as an "
     "async job: submit → poll or stream → results."},
    {"name": "eCourts · Case details (search by field)", "description":
     "Fetch case details via a POST body: by CNR, filing number, FIR "
     "(DC only), Act, case type, or case number."},
    {"name": "eCourts · Metadata", "description":
     "Dropdown lists (states, districts/benches, court complexes, case types) "
     "used to build a search. Cached 24h."},
    {"name": "eCourts · Admin", "description":
     "Operational endpoints (cron cache pre-warming)."},
    {"name": "eCourts · Proxies (admin)", "description":
     "Inspect and add Webshare proxies at runtime — raises the throughput of "
     "all case-details searches. Gated by X-Admin-Token."},
    {"name": "ITAT", "description":
     "Income Tax Appellate Tribunal — case details + orders (search + PDF)."},
    {"name": "CESTAT", "description":
     "Customs, Excise & Service Tax Appellate Tribunal — case details + "
     "orders (search + PDF)."},
    {"name": "SAT", "description":
     "Securities Appellate Tribunal — case details (3 searches), orders "
     "(4 searches), and an order_link resolver that turns a stored order "
     "link into the live PDF."},
    {"name": "SCI (Supreme Court)", "description":
     "Supreme Court of India (sci.gov.in) — case status (6 searches, each with "
     "full View details), daily orders (4), and judgements (5). Every order/"
     "judgement row carries a direct session-free PDF url. Math-equation "
     "captcha solved automatically; GET /sci/captcha exposes the solver."},
    {"name": "AIROnline", "description":
     "AIROnline (aol1.aironline.in) citation lookup. The dropdown values are "
     "not served over HTTP — they are a plain file, "
     "app/tribunals/aironline/data/aironline_dropdowns.json, holding every "
     "publication / year / segment / judicial body / volume combination. Pick "
     "values from it and pass them here to get the record: citation, court, "
     "judge(s), parties, case number and decision date."},
    {"name": "System", "description": "Liveness / health probes."},
]


def create_app() -> FastAPI:
    app = FastAPI(
        title="cassie-gateway API",
        description=(
            "Case-search gateway for Indian courts & tribunals. "
            "One section per source:\n\n"
            "- **eCourts** — District & High Court case details (by CNR), "
            "party / advocate search, dropdown metadata, order PDFs.\n"
            "- **ITAT** — Income Tax Appellate Tribunal case details + orders.\n"
            "- **CESTAT** — Customs/Excise/Service-Tax Appellate Tribunal "
            "case details + orders.\n"
            "- **System** — health probes."
        ),
        version="1.0",
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )

    app.include_router(health.router)
    app.include_router(find.router)
    app.include_router(cases.router)
    app.include_router(ecourts_search.router)
    app.include_router(metadata.router)
    app.include_router(admin.router)
    app.include_router(proxies.router)
    # Tribunal case-details + orders (ITAT, CESTAT) — additive; the eCourts
    # gateway routes above are untouched.
    app.include_router(itat.router)
    app.include_router(cestat.router)
    app.include_router(sat.router)
    app.include_router(sci.router)
    app.include_router(aironline.router)
    return app


app = create_app()


if __name__ == "__main__":
    import os
    import uvicorn

    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.getenv("GATEWAY_HOST", "127.0.0.1")
    port = int(os.getenv("GATEWAY_PORT", "9021"))
    log.info("starting FastAPI gateway on %s:%d", host, port)
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")
