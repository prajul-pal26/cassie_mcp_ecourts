"""Case-detail routes: single CNR lookup, order-PDF resolve, batch lookup.

All handlers async; blocking upstream work is offloaded to the threadpool.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool

from app.core import config
from app.core.runtime import get_components
from app.reliability.gateway import GatewayCall, UpstreamDown
from app.routers.common import json_response, parse_json_body
from app.schemas.examples import CASE_BATCH_EXAMPLE, ORDER_URL_EXAMPLE

log = logging.getLogger("routers.cases")

router = APIRouter(tags=["eCourts · CNR (case details)"])

_MAX_BATCH_SIZE = 25


# --------------------------------------------------------------------------- #
# POST /api/case/batch — E4  (declared before /{cnr})                          #
# --------------------------------------------------------------------------- #

@router.post("/api/case/batch", openapi_extra=CASE_BATCH_EXAMPLE,
             summary="Bulk case lookup — up to 25 CNRs in one call",
             description="Fetch full details for many CNRs at once (e.g. "
             "refreshing a saved-cases list) instead of one request each.")
async def case_batch(request: Request):
    body = await parse_json_body(request)
    if not isinstance(body, dict):
        return json_response({"error": "body must be JSON object"}, 400)
    cnrs = body.get("cnrs")
    if not isinstance(cnrs, list) or not cnrs:
        return json_response({"error": "cnrs must be a non-empty array"}, 400)
    if len(cnrs) > _MAX_BATCH_SIZE:
        return json_response({
            "error": f"max {_MAX_BATCH_SIZE} CNRs per request",
            "received": len(cnrs),
        }, 400)

    # Optional per-CNR court_type override — keyed by CNR string (matched
    # case-insensitively). Same override the single-CNR endpoints already
    # support: CNR-string inference alone misclassifies HC formats like
    # Bombay's "HCBMnnnn..." or a Calcutta circuit bench's "WBCHCJnnnn...",
    # and for batch this isn't just a display bug — the wrong court_type
    # means the fetch itself hits the wrong upstream API. Pass this when the
    # caller (e.g. a saved-cases refresh) already knows each CNR's court type.
    court_types_raw = body.get("court_types")
    court_types: dict = {}
    if court_types_raw is not None:
        if not isinstance(court_types_raw, dict):
            return json_response({"error": "court_types must be an object mapping cnr -> 'dc'/'hc'"}, 400)
        for k, v in court_types_raw.items():
            if v is None:
                continue
            v_norm = str(v).strip().lower()
            if v_norm not in ("dc", "hc"):
                return json_response({
                    "error": f"court_types['{k}'] must be 'dc' or 'hc' if provided",
                }, 400)
            court_types[str(k).strip().upper()] = v_norm

    do_normalize = bool(body.get("normalize", True))
    # The whole fan-out is blocking (rate-bucketed upstream calls); run it off
    # the event loop so concurrent requests aren't stalled.
    result = await run_in_threadpool(_run_batch, cnrs, do_normalize, court_types)
    return json_response(result)


def _run_batch(cnrs: list, do_normalize: bool, court_types: dict | None = None) -> dict:
    court_types = court_types or {}
    from app.upstream.bridge import _infer_court_type_from_cnr, bridge_case_detail, is_valid_cnr
    try:
        from app.search.enrich.normalize import normalize_case_detail as normalize_fn
    except Exception:
        normalize_fn = None
    try:
        from app.search.enrich.sensitivity import apply as sensitivity_fn
    except Exception:
        sensitivity_fn = None
    try:
        from app.search.enrich.sc_stub import detect as sc_detect_fn
    except Exception:
        sc_detect_fn = None

    comp = get_components()

    work: list = []
    for idx, cnr_raw in enumerate(cnrs):
        cnr = (str(cnr_raw) if cnr_raw is not None else "").strip().upper()
        if not is_valid_cnr(cnr):
            work.append((idx, cnr_raw, {"_invalid": True}))
            continue
        if sc_detect_fn is not None:
            sc = sc_detect_fn(cnr)
            if sc.get("is_sc"):
                work.append((idx, cnr, {"_sc": sc}))
                continue
        work.append((idx, cnr, None))

    results: list = [None] * len(work)

    def _do_one(item):
        idx, cnr, prebaked = item
        if prebaked is not None:
            if prebaked.get("_invalid"):
                return idx, {"cnr": cnr, "detail": None,
                             "error": "invalid CNR format", "degraded": False}
            if prebaked.get("_sc"):
                return idx, {
                    "cnr": cnr, "detail": None,
                    "status": "not_supported_yet",
                    "deeplink_url": prebaked["_sc"].get("deeplink_url"),
                    "message": prebaked["_sc"].get("message"),
                    "degraded": False,
                }

        # Resolve court_type explicitly BEFORE building the cache key, rather
        # than letting bridge_case_detail infer it internally with court_type
        # =None — see the cache-key-collision bug fixed throughout this file:
        # the key must reflect whichever court_type is actually used for this
        # fetch, or a batch lookup and an explicit-override lookup for the
        # same CNR can silently share (and poison) each other's cache entry.
        # Prefer a caller-supplied override over inference — same reasoning
        # as the single-CNR endpoints: inference alone gets the FETCH itself
        # wrong for HC formats it doesn't recognize, not just the label.
        effective_court_type = court_types.get(cnr) or _infer_court_type_from_cnr(cnr)

        def fetch():
            return bridge_case_detail(cnr, court_type=effective_court_type)

        gw = GatewayCall(
            cache=comp["cache"], coalescer=comp["coalescer"],
            breaker=comp["breaker"], bucket=comp["bucket"],
            key=f"case_detail:{effective_court_type}:{cnr}",
            ttl=config.CACHE_TTL_CASE_DETAIL,
            stale_ok_horizon=config.CACHE_STALE_OK_HORIZON,
        )
        try:
            res = gw.run(fetch)
        except UpstreamDown:
            return idx, {"cnr": cnr, "detail": None,
                         "error": "upstream unavailable", "degraded": True}
        except Exception as e:
            log.warning("case batch fetch failed cnr=%s err=%r", cnr, e)
            return idx, {"cnr": cnr, "detail": None,
                         "error": str(e)[:200], "error_type": type(e).__name__,
                         "degraded": False}

        raw = res.value
        detail = normalize_fn(raw, court_level=effective_court_type) if (do_normalize and normalize_fn) else raw
        if do_normalize and detail and sensitivity_fn:
            sensitivity_fn(detail)
        return idx, {
            "cnr": cnr, "detail": detail,
            "source": res.source, "age_seconds": round(res.age_seconds, 2),
            "degraded": (res.source == "stale"),
        }

    max_workers = min(len(work), 8)
    with ThreadPoolExecutor(max_workers=max_workers,
                            thread_name_prefix="casebatch") as pool:
        futures = [pool.submit(_do_one, w) for w in work]
        for fut in as_completed(futures):
            try:
                idx, item = fut.result()
            except Exception as e:  # pragma: no cover - defensive
                log.exception("case batch worker crashed: %r", e)
                continue
            results[idx] = item

    for i, r in enumerate(results):
        if r is None:
            results[i] = {"cnr": str(cnrs[i]), "detail": None,
                          "error": "worker dropped", "degraded": True}

    degraded_overall = any(r.get("degraded") for r in results if isinstance(r, dict))
    return {"results": results, "count": len(results), "degraded": degraded_overall}


# --------------------------------------------------------------------------- #
# GET /api/case/{cnr}                                                          #
# --------------------------------------------------------------------------- #

async def case_detail_response(cnr: str, court_type, normalize_flag: bool):
    """Shared CNR case-detail resolver — the core behind GET /api/case/{cnr}
    and POST /api/case-details/cnr. Returns an identical JSONResponse for both."""
    from app.upstream.bridge import _infer_court_type_from_cnr, bridge_case_detail, is_valid_cnr

    # Case/whitespace-tolerant: "DC", "Hc", " hc " all resolve like "dc"/"hc".
    if court_type is not None:
        court_type = str(court_type).strip().lower() or None

    cnr_clean = (cnr or "").strip().upper()
    if not is_valid_cnr(cnr_clean):
        return json_response({
            "success": False,
            "error": "invalid CNR format (expected 16 chars: 2 letters + 2 alnum + 12 digits)",
        }, 400)

    # SC short-circuit (E5)
    try:
        from app.search.enrich.sc_stub import detect as sc_detect
        sc = sc_detect(cnr_clean)
        if sc.get("is_sc"):
            return json_response({
                "success": True,
                "status": "not_supported_yet",
                "cnr": cnr_clean,
                "deeplink_url": sc.get("deeplink_url"),
                "message": sc.get("message"),
                "waitlist_url": sc.get("waitlist_url"),
            })
    except ImportError:
        pass

    if court_type not in (None, "dc", "hc"):
        return json_response({
            "success": False,
            "error": "court_type must be 'dc' or 'hc' if provided",
        }, 400)

    # Resolve BEFORE building the cache key — this is the fix for a real,
    # confirmed cache-key-collision bug: the key used to be keyed on CNR
    # alone (f"case_detail:{cnr_clean}"), so the FIRST caller for a given
    # CNR (whether relying on inference or passing an explicit override)
    # would silently poison the cache for every subsequent caller of that
    # same CNR for the full CACHE_TTL_CASE_DETAIL window (2h) — including
    # ones passing a *different, correct* explicit court_type. Verified
    # live: a Bombay HC CNR ("HCBM...", a format _infer_court_type_from_cnr
    # doesn't recognize — it only matches Delhi-style "XXHCnnnn...") got
    # wrongly cached as "dc" via an inferred call, and a subsequent
    # explicit ?court_type=hc request on the SAME CNR still returned the
    # stale wrong "dc" result, because the cache check happens before the
    # override is ever considered. Namespacing the key by the court_type
    # actually used means an inferred lookup and an explicit-override
    # lookup for the same CNR are never the same cache entry.
    effective_court_type = court_type or _infer_court_type_from_cnr(cnr_clean)

    comp = get_components()

    def fetch():
        return bridge_case_detail(cnr_clean, court_type=effective_court_type)

    gw = GatewayCall(
        cache=comp["cache"], coalescer=comp["coalescer"],
        breaker=comp["breaker"], bucket=comp["bucket"],
        key=f"case_detail:{effective_court_type}:{cnr_clean}",
        ttl=config.CACHE_TTL_CASE_DETAIL,
        stale_ok_horizon=config.CACHE_STALE_OK_HORIZON,
    )

    try:
        res = await run_in_threadpool(gw.run, fetch)
    except UpstreamDown:
        return json_response({
            "success": False,
            "error": "upstream unavailable",
            "degraded": True,
            "retry_after_seconds": config.BREAKER_WAF_RESET_TIMEOUT,
        }, 503)
    except ValueError as e:
        return json_response({"success": False, "error": str(e)}, 400)
    except Exception as e:
        return json_response({
            "success": False,
            "error": str(e)[:200],
            "error_type": type(e).__name__,
        }, 502)

    data = res.value
    if normalize_flag:
        try:
            from app.search.enrich.normalize import normalize_case_detail
            from app.search.enrich.sensitivity import apply as apply_sensitivity
            data = normalize_case_detail(data, court_level=effective_court_type)
            apply_sensitivity(data)
        except ImportError as e:
            log.warning("normalize unavailable, returning raw: %r", e)
        except Exception as e:
            log.warning("normalize failed for %s: %r", cnr_clean, e)

    return json_response({
        "success": True,
        "data": data,
        "court_type": effective_court_type,
        "source": res.source,
        "age_seconds": round(res.age_seconds, 2),
        "degraded": (res.source == "stale"),
        "normalized": normalize_flag,
    })


@router.get("/api/case/{cnr}",
            summary="Case details by CNR — full parties, history & orders",
            description="The main case lookup. Give a 16-char CNR → normalized, "
            "cached case details from eCourts (DC or HC, auto-detected).")
async def get_case_detail(cnr: str, request: Request):
    court_type = request.query_params.get("court_type")
    normalize_flag = request.query_params.get("normalize", "1") != "0"
    return await case_detail_response(cnr, court_type, normalize_flag)


# --------------------------------------------------------------------------- #
# POST /api/case/{cnr}/order/url                                               #
# --------------------------------------------------------------------------- #

@router.post("/api/case/{cnr}/order/url", openapi_extra=ORDER_URL_EXAMPLE,
             summary="Resolve a downloadable PDF link for one order of a case",
             description="Order links on a case are session-bound; this mints a "
             "fresh, directly-downloadable PDF URL for a specific order. Works "
             "for **both District Courts and High Courts** (the mobile API's "
             "`display_pdf_new.php` serves HC order PDFs too — no captcha). The "
             "returned `pdf_url` is short-lived (~5 min), so open it promptly.")
async def resolve_order_pdf_url(cnr: str, request: Request):
    from app.upstream.bridge import _get_v4, _infer_court_type_from_cnr, is_valid_cnr

    cnr_clean = (cnr or "").strip().upper()
    if not is_valid_cnr(cnr_clean):
        return json_response({"success": False, "error": "invalid CNR format"}, 400)

    body = await parse_json_body(request) or {}
    if not isinstance(body, dict):
        body = {}
    required = ("state_cd", "dist_cd", "court_code", "caseno", "filename")
    missing = [k for k in required if not body.get(k)]
    if missing:
        return json_response({
            "success": False,
            "error": f"missing required fields: {missing}",
        }, 400)

    # DC or HC — both resolve via display_pdf_new.php on their respective base.
    # Prefer an explicit override when the caller already knows the court
    # type (e.g. Find My Case already knows this from the search that
    # produced the CNR) — CNR-string inference is unreliable across High
    # Courts: _infer_court_type_from_cnr only recognizes Delhi-style
    # "XXHCnnnn..." CNRs. Confirmed live: Bombay ("HCBMnnnn...") and a
    # Calcutta circuit bench ("WBCHCJnnnn...") both use different prefix
    # conventions and are silently misclassified as "dc" by the heuristic.
    override = body.get("court_type")
    # Case/whitespace-tolerant: "DC", "Hc", " hc " all resolve like "dc"/"hc".
    if override is not None:
        override = str(override).strip().lower() or None
    if override not in (None, "dc", "hc"):
        return json_response({
            "success": False,
            "error": "court_type must be 'dc' or 'hc' if provided",
        }, 400)
    court_type = override or _infer_court_type_from_cnr(cnr_clean)

    def _display_pdf():
        cli = _get_v4(court_type)
        return cli.display_pdf(
            state_cd=int(body["state_cd"]),
            dist_cd=int(body["dist_cd"]),
            court_code=int(body["court_code"]),
            caseno=str(body["caseno"]),
            filename=str(body["filename"]),
            appFlag=str(body.get("appFlag") or "1"),
            cCode=int(body.get("cCode") or 1),
        )

    try:
        raw = await run_in_threadpool(_display_pdf)
    except Exception as e:
        log.warning("display_pdf failed for cnr=%s: %r", cnr_clean, e)
        return json_response({
            "success": False,
            "error": str(e)[:200],
            "error_type": type(e).__name__,
        }, 502)

    if not isinstance(raw, dict):
        return json_response({"success": False, "error": "unexpected upstream shape"}, 502)
    if raw.get("status") == "N":
        return json_response({
            "success": False,
            "error": raw.get("msg") or "upstream refused",
            "upstream_status": "N",
        }, 503)

    pdf_url = raw.get("pdf_url")
    if not pdf_url:
        return json_response({
            "success": False,
            "error": "upstream did not return a pdf_url",
            "raw_status": raw.get("status"),
        }, 502)

    return json_response({
        "success": True,
        "pdf_url": pdf_url,
        "expires_hint_seconds": 300,
    })


# --------------------------------------------------------------------------- #
# POST /api/case/{cnr}/orders — one call: CNR -> all orders with PDF links     #
# --------------------------------------------------------------------------- #

_ORDERS_MAX = 100


@router.post("/api/case/{cnr}/orders",
             summary="All orders of a case WITH PDF links, from just the CNR",
             description="One call does both steps: fetches the case, then "
             "resolves every order's downloadable PDF link. Works for **both "
             "District Courts and High Courts** (the mobile API's "
             "`display_pdf_new.php` serves HC order PDFs too — no captcha).\n\n"
             "`court_type` (query param, optional) overrides CNR-based "
             "inference — pass it when the caller already knows the court "
             "type (e.g. from the search that produced this CNR), since "
             "inference doesn't recognize every High Court's CNR format.")
async def case_orders(cnr: str, request: Request):
    from app.upstream.bridge import _get_v4, _infer_court_type_from_cnr, bridge_case_detail, is_valid_cnr

    cnr_clean = (cnr or "").strip().upper()
    if not is_valid_cnr(cnr_clean):
        return json_response({"success": False, "error": "invalid CNR format"}, 400)

    override = request.query_params.get("court_type")
    # Case/whitespace-tolerant: "DC", "Hc", " hc " all resolve like "dc"/"hc".
    if override is not None:
        override = override.strip().lower() or None
    if override not in (None, "dc", "hc"):
        return json_response({
            "success": False,
            "error": "court_type must be 'dc' or 'hc' if provided",
        }, 400)
    # Prefer an explicit override over CNR-string inference — see the
    # cache-key-collision + inference-format notes in case_detail_response
    # and resolve_order_pdf_url above. Same bug, same fix, applied here too.
    court_type = override or _infer_court_type_from_cnr(cnr_clean)
    comp = get_components()

    # 1) fetch + normalize the case (cached through the reliability stack)
    def fetch():
        return bridge_case_detail(cnr_clean, court_type=court_type)

    gw = GatewayCall(
        cache=comp["cache"], coalescer=comp["coalescer"],
        breaker=comp["breaker"], bucket=comp["bucket"],
        key=f"case_detail:{court_type}:{cnr_clean}",
        ttl=config.CACHE_TTL_CASE_DETAIL,
        stale_ok_horizon=config.CACHE_STALE_OK_HORIZON,
    )
    try:
        res = await run_in_threadpool(gw.run, fetch)
    except UpstreamDown:
        return json_response({"success": False, "error": "upstream unavailable",
                              "degraded": True}, 503)
    except ValueError as e:
        return json_response({"success": False, "error": str(e)}, 400)
    except Exception as e:
        return json_response({"success": False, "error": str(e)[:200],
                              "error_type": type(e).__name__}, 502)

    try:
        from app.search.enrich.normalize import normalize_case_detail
        data = normalize_case_detail(res.value, court_level=court_type)
    except Exception as e:
        return json_response({"success": False,
                              "error": f"could not parse orders: {e}"[:200]}, 502)

    raw_orders = (data.get("orders") or [])[:_ORDERS_MAX]

    def _meta(o):
        return {
            "number": o.get("number"),
            "date": o.get("date_iso"),
            "label": o.get("label"),
            "order_type": o.get("kind"),
            "caseno": o.get("caseno"),
            "filename": o.get("filename"),
        }

    # Resolve every order's PDF link (paced by the rate bucket). Works for both
    # DC and HC — display_pdf_new.php serves HC order PDFs too (no captcha).
    def resolve_all():
        cli = _get_v4(court_type)
        out = []
        for o in raw_orders:
            entry = {**_meta(o), "pdf_url": None}
            try:
                comp["bucket"].acquire()  # throttle to the 15 RPS cap
                r = cli.display_pdf(
                    state_cd=int(o["state_cd"]), dist_cd=int(o["dist_cd"]),
                    court_code=int(o["court_code"]), caseno=str(o["caseno"]),
                    filename=str(o["filename"]),
                    appFlag=str(o.get("appFlag") or "1"),
                    cCode=int(o.get("cCode") or 1))
                if isinstance(r, dict) and r.get("pdf_url"):
                    entry["pdf_url"] = r["pdf_url"]
                elif isinstance(r, dict) and r.get("status") == "N":
                    entry["error"] = r.get("msg") or "upstream refused"
            except Exception as e:  # per-order failure never fails the batch
                entry["error"] = str(e)[:120]
            out.append(entry)
        return out

    orders = await run_in_threadpool(resolve_all)
    return json_response({
        "success": True,
        "cnr": cnr_clean,
        "court_type": court_type,
        "count": len(orders),
        "resolved": sum(1 for o in orders if o.get("pdf_url")),
        "orders": orders,
        "expires_hint_seconds": 300,
    })
