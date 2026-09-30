# cassie-gateway (FastAPI build)

A framework-only port of the production Flask gateway (`../flask-backup/`) to
FastAPI, restructured into a clean package. **Same endpoints, same request/
response shapes, same status codes** — only the framework and file layout
changed. Every handler is `async` and offloads blocking upstream work to the
threadpool, so it scales cleanly under concurrent load.

## Layout

```
app/
├── main.py                  FastAPI app factory + lifespan (warms singletons)
├── core/
│   ├── config.py            all tunable knobs (env-driven)
│   └── runtime.py           reliability-stack singletons, job dispatch, bridge wiring
├── reliability/
│   ├── gateway.py           SqliteCache, Coalescer, Breaker, RateBucket, GatewayCall
│   └── jobs.py              JobStore (SQLite + SSE stream)
├── search/
│   ├── workers.py           run_find — the fan-out worker
│   ├── name_variants.py     phonetic name variant generation
│   └── enrich/              normalize, confidence, sensitivity, court_ranking,
│                            llm_variants, sc_stub
├── upstream/
│   ├── ecourts_v4.py        AES-envelope v4 eCourts client
│   └── bridge.py            party / advocate / case-detail adapters
├── tribunals/               self-contained ITAT + CESTAT providers (copied verbatim)
│   ├── itat/                fetch_itat_casestatus, fetch_itat_orders, itat_captcha, itat_pdf_cache
│   └── cestat/              fetch_cestat_casestatus, fetch_cestat_orders
├── schemas/
│   └── examples.py          Swagger request-body examples
└── routers/
    ├── health.py            /health, /health/cache
    ├── find.py              /api/find, /api/find/smart, /{jid}, /cancel, /stream
    ├── cases.py             /api/case/{cnr}, /order/url, /batch
    ├── metadata.py          /api/metadata/{states,districts,courts,case-types}
    ├── admin.py             /api/admin/warm
    ├── tribunal.py          /api/find/tribunal (501 stub)
    ├── itat.py              /itat/case-details/*, /itat/orders/*  (12 endpoints)
    └── cestat.py            /cestat/case-details/*, /cestat/orders/*  (13 endpoints)
```

## Tribunals (ITAT + CESTAT)

Two tribunals with full case-details + order-link endpoints were shifted in from
the local `Tribunal/` build and mounted on this same app:

- **ITAT** — 4 case-details searches + 4 orders searches + options + 2 order-PDF
  stream endpoints. Solves the site captcha via **ffmpeg** (audio leak) with a
  **ddddocr** OCR fallback — so `ffmpeg` must be on PATH (the Dockerfile installs it).
- **CESTAT** — 4 case-details searches + 6 orders searches + options + report.
  No captcha; each order carries a direct PDF link.

The providers live under `app/tribunals/` and were copied **verbatim** (no logic
changes); the routers are thin async wrappers that offload the blocking provider
calls with `asyncio.to_thread`. These are purely additive — the eCourts gateway
routes are untouched.

## What changed vs. the Flask build

- **Framework:** Flask + flasgger → FastAPI + built-in OpenAPI (`/docs`, `/redoc`).
- **Structure:** flat files → `app/` package with subpackages by concern.
- **Async:** every route is `async def`; blocking calls (SQLite, upstream)
  run via `fastapi.concurrency.run_in_threadpool`.
- **Dead code removed:** the legacy v3 client (`EcourtFetch.py`) and the unused
  FIR / filing-number / case-number / case-type / act bridges were dropped
  (no route or worker referenced them). `numpy`/`pandas` fell out with them.

The public JSON contract is unchanged — POST handlers still validate manually
and return the exact legacy `{"error": ...}` / `{"success": false, ...}` bodies
(a strict Pydantic model would emit FastAPI's 422 shape and break clients).

## Run locally

```sh
./run_local.sh                # binds 127.0.0.1:9021, reuses ../cassie-gateway/.venv
# or
GATEWAY_PORT=9021 python -m uvicorn app.main:app --port 9021 --workers 1
```

Docs: http://127.0.0.1:9021/docs

## Deploy (Fly.io)

Single process only — the rate bucket / breaker / cache / job store are
per-process singletons; never scale past one machine.

```sh
fly deploy        # uses Dockerfile + fly.toml (app: cassie-gateway-fastapi)
```

`fly.toml` intentionally uses a **separate app name** so deploying never
disturbs the live Flask `cassie-gateway`.
