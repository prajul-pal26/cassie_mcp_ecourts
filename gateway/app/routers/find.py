"""Find-my-case routes: submit / smart / poll / cancel / SSE stream.

All handlers are async; blocking work (SQLite job store, upstream calls via
GatewayCall) is offloaded to the threadpool so the event loop never stalls
under concurrent load.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse

from app.core import config
from app.core.runtime import dispatch_find, get_components, get_executor
from app.reliability.gateway import GatewayCall, UpstreamDown
from app.routers.common import json_response, parse_json_body
from app.schemas.examples import FIND_EXAMPLE, FIND_SMART_EXAMPLE
from app.search.name_variants import name_variants

log = logging.getLogger("routers.find")

router = APIRouter(tags=["eCourts · Find (party / advocate)"])


# --------------------------------------------------------------------------- #
# Validation / normalization (legacy contract preserved)                       #
# --------------------------------------------------------------------------- #

def _validate_find_body(body: Optional[dict]) -> Optional[str]:
    if not isinstance(body, dict):
        return "body must be JSON object"
    if not body.get("search_term"):
        return "search_term is required"
    if not body.get("state_code"):
        return "state_code is required"
    if not body.get("district_code"):
        return "district_code is required"
    courts = body.get("courts")
    if not isinstance(courts, list) or len(courts) == 0:
        return "courts must be a non-empty array"
    for c in courts:
        if not isinstance(c, dict) or not c.get("court_code"):
            return "each court must have court_code"
    return None


def _normalize_find_params(body: dict) -> dict:
    search_term = (body.get("search_term") or "").strip()
    cap = max(1, config.FIND_VARIANT_CAP)
    variants = name_variants(search_term, cap=cap) if search_term else []
    if search_term and search_term not in variants:
        variants.insert(0, search_term)
        variants = variants[:cap]
    modes = body.get("modes") or ["party", "advocate"]
    return {
        "search_term": search_term,
        "state_code": body.get("state_code"),
        "district_code": body.get("district_code"),
        "court_type": body.get("court_type", "dc"),
        "courts": body.get("courts"),
        "modes": modes,
        "variants": variants,
    }


# --------------------------------------------------------------------------- #
# POST /api/find — legacy submit                                               #
# --------------------------------------------------------------------------- #

@router.post("/api/find", openapi_extra=FIND_EXAMPLE,
             summary="Submit a party/advocate search (raw) → returns job_id",
             description="Start a case search by party name or advocate. Caller "
             "supplies state_code, district_code and courts[]. Returns a job_id "
             "you then poll (`GET /api/find/{jid}`) or stream. This is the raw/"
             "legacy entry; `/api/find/smart` is the friendlier version.")
async def submit_find(request: Request):
    body = await parse_json_body(request)
    err = _validate_find_body(body)
    if err:
        return json_response({"error": err}, 400)
    params = _normalize_find_params(body)
    comp = get_components()
    jid = await run_in_threadpool(comp["store"].submit, "find", params)
    get_executor().submit(dispatch_find, jid)
    return json_response({"job_id": jid, "status": "pending"})


# --------------------------------------------------------------------------- #
# POST /api/find/smart — R1 citizen API  (declared before /{jid})             #
# --------------------------------------------------------------------------- #

@router.post("/api/find/smart", openapi_extra=FIND_SMART_EXAMPLE,
             summary="Submit a citizen-friendly search (auto-resolves courts) → job_id",
             description="Same as /api/find but takes clean input "
             "(court_level, where, who) and resolves the courts for you. "
             "Accepts a CNR, party name, or advocate name. Returns a job_id.")
async def find_smart(request: Request):
    body = await parse_json_body(request)
    if not isinstance(body, dict):
        return json_response({"error": "body must be JSON object"}, 400)

    # Optional enrichment helpers — degrade gracefully if the module is absent.
    try:
        from app.search.enrich.llm_variants import (
            augmented_variants as llm_variants_fn,
        )
        from app.search.enrich.llm_variants import (
            merge_variants as merge_variants_fn,
        )
    except Exception:
        llm_variants_fn = merge_variants_fn = None
    try:
        from app.search.enrich.court_ranking import rank_courts as court_ranking_fn
    except Exception:
        court_ranking_fn = None
    try:
        from app.search.enrich.sc_stub import detect as sc_detect_fn
    except Exception:
        sc_detect_fn = None

    # 1. Validate court_level
    court_level = (body.get("court_level") or "dc").lower()
    if court_level == "sc":
        return json_response({
            "error": "Supreme Court search is not yet supported",
            "use_endpoint": "/api/case/<cnr> with SC CNR",
            "tribunals_or_sc_waitlist": "/find-my-case/sc-waitlist",
        }, 501)
    if court_level == "tribunal":
        return json_response({
            "error": "tribunal search not supported",
            "use_endpoint": "/api/find/tribunal",
        }, 501)
    if court_level not in ("dc", "hc"):
        return json_response({"error": "court_level must be 'dc' or 'hc'"}, 400)

    # 2. Validate `who` (exactly one of cnr / party_name / advocate_name)
    who = body.get("who") or {}
    if not isinstance(who, dict):
        return json_response({"error": "who must be an object"}, 400)
    cnr = (who.get("cnr") or "").strip().upper() if who.get("cnr") else None
    party_name = (who.get("party_name") or "").strip() if who.get("party_name") else None
    advocate_name = (who.get("advocate_name") or "").strip() if who.get("advocate_name") else None
    provided = [x for x in (cnr, party_name, advocate_name) if x]
    if len(provided) != 1:
        return json_response({
            "error": "who must specify exactly one of cnr / party_name / advocate_name",
        }, 400)

    # 3. CNR short-circuit
    if cnr:
        return _handle_cnr_shortcircuit(cnr, sc_detect_fn=sc_detect_fn)

    # 4. Validate `where`
    where = body.get("where") or {}
    if not isinstance(where, dict):
        return json_response({"error": "where must be an object"}, 400)
    state_code = where.get("state_code")
    district_code = where.get("district_code")
    if not state_code:
        return json_response({"error": "where.state_code is required"}, 400)
    if not district_code:
        return json_response({"error": "where.district_code is required"}, 400)

    # 5. Resolve courts (prefer frontend-supplied)
    from app.upstream.bridge import _get_v4
    courts = body.get("courts")
    if not isinstance(courts, list) or not courts:
        try:
            courts = await run_in_threadpool(
                _resolve_courts,
                court_level=court_level,
                state_code=state_code,
                district_code=district_code,
                get_v4_client=_get_v4,
            )
        except UpstreamDown:
            return json_response({
                "error": "could not resolve courts; upstream unavailable",
                "degraded": True,
            }, 503)
        except Exception as e:
            log.warning("court resolution failed: %r", e)
            return json_response({
                "error": "could not resolve courts list; supply 'courts' in request",
            }, 502)
        if not courts:
            return json_response({
                "error": "no courts found for the given state/district",
            }, 404)

    bad_courts = [c for c in courts if not (isinstance(c, dict) and c.get("court_code"))]
    if bad_courts:
        return json_response({
            "error": "each court must be an object with a court_code",
            "bad_count": len(bad_courts),
        }, 400)

    # 6. Refinements
    refine = body.get("refine") or {}
    if not isinstance(refine, dict):
        refine = {}
    priority_hint = (refine.get("court_priority_hint") or "").strip().lower() or None

    year_raw = refine.get("year") if isinstance(refine.get("year"), (str, int)) else None
    year_val = str(year_raw).strip() if year_raw is not None else None
    if year_val == "":
        year_val = None
    expand_strategy = (refine.get("expand_strategy") or "single").strip().lower()
    if expand_strategy not in ("single", "pm1", "pm2"):
        expand_strategy = "single"

    # 7. Court ranking
    if court_ranking_fn and priority_hint:
        try:
            courts = court_ranking_fn(courts, hint=priority_hint)
        except Exception as e:
            log.warning("court_ranking_fn failed: %r — falling back to input order", e)

    # 8. Search term + modes
    search_term = party_name or advocate_name
    modes = ["party"] if party_name else ["advocate"]

    # 9. Variant generation (rule-based + optional LLM)
    # Multi-word names get a SMALLER cap: eCourts party search is a substring
    # match (verified: searching "Verma" returns every "Ashok Verma"), so a
    # spelling variant on each word cross-multiplies into many upstream calls
    # that a single well-chosen token already covers. Dividing the cap by the
    # word count keeps the fan-out sane ("Ram Kumar Soni" went 54 tasks -> a
    # handful) without losing the variants that matter.
    n_words = max(1, len(search_term.split()))
    cap = max(1, config.FIND_VARIANT_CAP // n_words)
    effective_cap = max(cap, 8 // n_words) if llm_variants_fn else cap
    effective_cap = max(1, effective_cap)
    rule_variants = name_variants(search_term, cap=effective_cap)
    if search_term and search_term not in rule_variants:
        rule_variants.insert(0, search_term)
        rule_variants = rule_variants[:effective_cap]

    llm_v: list = []
    if llm_variants_fn:
        try:
            llm_v = llm_variants_fn(search_term, max_variants=effective_cap)
        except Exception as e:
            log.warning("llm_variants_fn failed: %r", e)
            llm_v = []

    if merge_variants_fn and llm_v:
        try:
            variants = merge_variants_fn(rule_variants, llm_v, cap=effective_cap)
        except Exception:
            variants = rule_variants
    else:
        variants = rule_variants

    # 9b. Substring-subsumption dedup. Because search is a substring match,
    # results(A) is a SUPERSET of results(B) whenever A is a substring of B, so
    # querying B as well is a strictly wasted upstream call. Drop any variant
    # that contains a shorter kept variant (case-insensitively). Keeps the
    # shortest of each subsumption chain; order otherwise preserved.
    variants = _dedupe_subsumed(variants)

    # 10. Hand off to the shared job pipeline
    params = {
        "search_term": search_term,
        "state_code": state_code,
        "district_code": district_code,
        "court_type": court_level,
        "courts": courts,
        "modes": modes,
        "variants": variants,
        "year": year_val,
        "expand_strategy": expand_strategy,
        "query": {
            "party_name": party_name,
            "advocate_name": advocate_name,
            "year": year_val,
            "status": refine.get("status"),
        },
        "court_priority_hint": priority_hint,
    }

    comp = get_components()
    jid = await run_in_threadpool(comp["store"].submit, "find", params)
    get_executor().submit(dispatch_find, jid)

    return json_response({
        "job_id": jid,
        "status": "pending",
        "courts_resolved": len(courts),
        "variants_planned": len(variants),
    })


# --------------------------------------------------------------------------- #
# GET /api/find/{jid}  |  cancel  |  stream                                    #
# --------------------------------------------------------------------------- #

@router.get("/api/find/{jid}",
            summary="Poll a search job — progress + facets (+ rows when rows=1)")
async def get_find(jid: str, rows: int = None):
    """Snapshot of a find job.

    rows=1 (the default, legacy behaviour) includes `partial_cases` + `result`.
    rows=0 returns progress + facets + honest totals only (~2 KB vs ~1 MB) — the
    new client polls this and fetches rows separately from /rows. The default is
    config.FIND_DEFAULT_ROWS so it can be flipped by env once no legacy client
    remains, with no redeploy.

    Facets / total_matched / total_selected / counts_exact are hoisted OUT of
    `meta` (they also stay inside it for compat) so the client doesn't dig.
    """
    include_rows = config.FIND_DEFAULT_ROWS if rows is None else bool(rows)
    comp = get_components()
    # Progress + meta is cheap and never touches the row blob.
    prog = await run_in_threadpool(comp["store"].get_progress, jid)
    if prog is None:
        return json_response({"error": "not_found"}, 404)
    meta = prog.get("meta") or {}
    degraded = bool(meta.get("degraded"))

    body = {
        "id": prog["id"],
        "status": prog["status"],
        "completed": prog["completed"],
        "total": prog["total"],
        "error": prog["error"],
        "created_at": prog["created_at"],
        "updated_at": prog["updated_at"],
        "meta": meta,
        "degraded": degraded,
        # Hoisted, so the client reads counts without digging into meta.
        "total_matched": meta.get("total_matched"),
        "total_selected": meta.get("total_selected"),
        "counts_exact": meta.get("counts_exact"),
        "facets": meta.get("facets"),
    }
    if include_rows:
        blob = await run_in_threadpool(comp["store"].get_rows, jid)
        blob = blob or {}
        body["partial_cases"] = blob.get("partial_cases", [])
        body["result"] = blob.get("result")
    return json_response(body)


@router.post("/api/find/{jid}/cancel",
             summary="Cancel a running search job")
async def cancel_find(jid: str):
    comp = get_components()
    row = await run_in_threadpool(comp["store"].get, jid)
    if row is None:
        return json_response({"error": "not_found"}, 404)
    await run_in_threadpool(comp["store"].cancel, jid)
    return json_response({"status": "cancelled"})


def _dedupe_subsumed(variants: list) -> list:
    """Drop any variant that contains a shorter kept variant as a substring.

    eCourts party search matches the query as a substring of the record, so
    results(short) supersets results(long-containing-short); the longer query is
    a wasted upstream call. Compare case-insensitively; keep first occurrence.
    """
    kept: list = []
    kept_lower: list = []
    for v in variants:
        vl = (v or "").strip().lower()
        if not vl:
            continue
        if any(k in vl for k in kept_lower):
            continue
        # This new, shorter variant may subsume ones already kept — drop those.
        kept = [k for k, kl in zip(kept, kept_lower) if vl not in kl]
        kept_lower = [kl for kl in kept_lower if vl not in kl]
        kept.append(v)
        kept_lower.append(vl)
    return kept


def _filter_rows(rows: list, *, establishment: Optional[str],
                 case_type: Optional[str], year: Optional[str],
                 q: Optional[str]) -> list:
    """Filter the selected rows by facet + a free-text party substring. Pure."""
    ql = (q or "").strip().lower()

    def keep(r: dict) -> bool:
        if establishment and str(r.get("establishment_code") or "") != establishment:
            return False
        if case_type and str(r.get("type_name") or r.get("case_type") or "") != case_type:
            return False
        if year and str(r.get("case_year") or "") != year:
            return False
        if ql:
            hay = f"{r.get('pet_name','')} {r.get('res_name','')}".lower()
            if ql not in hay:
                return False
        return True

    return [r for r in rows if keep(r)]


@router.get("/api/find/{jid}/rows",
            summary="Paginated, filtered rows from a search job's selected set")
async def get_find_rows(jid: str, establishment: str = None, case_type: str = None,
                        year: str = None, q: str = None,
                        offset: int = 0, limit: int = 25):
    """Page over the SELECTED rows, optionally filtered by facet + party text.

    Honesty contract: this pages the SELECTED set (bounded per court), not the
    full match set. `total_matched_in_filter` says how many exist upstream;
    `total_selected_in_filter` is what is actually pageable here. When they
    differ the client should offer the per-court drill.
    """
    if limit < 1 or limit > config.FIND_ROWS_PAGE_MAX:
        return json_response(
            {"error": f"limit must be 1..{config.FIND_ROWS_PAGE_MAX}"}, 400)
    if offset < 0:
        return json_response({"error": "offset must be >= 0"}, 400)

    comp = get_components()
    prog = await run_in_threadpool(comp["store"].get_progress, jid)
    if prog is None:
        return json_response({"error": "not_found"}, 404)
    blob = await run_in_threadpool(comp["store"].get_rows, jid)
    selected = (blob or {}).get("partial_cases") or []

    filtered = _filter_rows(selected, establishment=establishment,
                            case_type=case_type, year=year, q=q)
    page = filtered[offset:offset + limit]

    meta = prog.get("meta") or {}
    # What exists upstream in this filter, from the pre-trim facet counts.
    total_matched_in_filter = None
    if establishment and not (case_type or year or q):
        for f in (meta.get("facets") or {}).get("establishment", []):
            if f.get("key") == establishment:
                total_matched_in_filter = f.get("count")
                break

    return json_response({
        "id": jid,
        "status": prog["status"],
        "rows": page,
        "offset": offset,
        "limit": limit,
        "returned": len(page),
        "total_selected_in_filter": len(filtered),
        "total_matched_in_filter": total_matched_in_filter,
        "complete": offset + len(page) >= len(filtered),
    })


@router.get("/api/find/{jid}/stream",
            summary="Live-stream search results as they arrive (SSE)")
async def stream_find(jid: str, rows: int = None):
    include_rows = config.FIND_DEFAULT_ROWS if rows is None else bool(rows)
    comp = get_components()
    row = await run_in_threadpool(comp["store"].get, jid)
    if row is None:
        return json_response({"error": "not_found"}, 404)

    # Already-terminal job: emit ONE synthetic SSE event + close cleanly,
    # instead of a non-2xx (which makes EventSource auto-reconnect forever).
    if row["status"] in ("done", "error", "cancelled", "interrupted"):
        terminal_kind = (
            "done" if row["status"] == "done"
            else "error" if row["status"] == "error"
            else "cancelled" if row["status"] == "cancelled"
            else "error"  # "interrupted" surfaces as error
        )
        terminal_event = {
            "type": terminal_kind,
            "status": row["status"],
            "result": row.get("result"),
            "error": row.get("error"),
            "completed": row.get("completed"),
            "total": row.get("total"),
            "meta": row.get("meta") or {},
            "degraded": bool((row.get("meta") or {}).get("degraded")),
        }
        payload = json.dumps(terminal_event, separators=(",", ":"))
        body = f"event: {terminal_kind}\ndata: {payload}\n\n"
        return Response(
            content=body,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "close",
            },
        )

    def gen():
        # Sync generator — Starlette iterates it in a threadpool, so the
        # blocking poll loop never stalls the event loop.
        try:
            for ev in comp["store"].stream(
                jid,
                heartbeat=config.SSE_HEARTBEAT_SECONDS,
                poll_interval=0.25,
                include_rows=include_rows,
            ):
                payload = json.dumps(ev, separators=(",", ":"))
                yield f"event: {ev.get('type', 'message')}\n"
                yield f"data: {payload}\n\n"
                if ev.get("type") in ("done", "error", "cancelled", "not_found"):
                    return
        except GeneratorExit:
            return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# --------------------------------------------------------------------------- #
# Helpers (framework-agnostic)                                                 #
# --------------------------------------------------------------------------- #

def _handle_cnr_shortcircuit(cnr: str, *, sc_detect_fn):
    from app.upstream.bridge import is_valid_cnr
    if not is_valid_cnr(cnr):
        return json_response({"error": "invalid CNR format"}, 400)
    if sc_detect_fn is not None:
        sc = sc_detect_fn(cnr)
        if sc.get("is_sc"):
            return json_response({
                "status": "not_supported_yet",
                "cnr": cnr,
                "deeplink_url": sc.get("deeplink_url"),
                "message": sc.get("message"),
                "waitlist_url": sc.get("waitlist_url"),
            }, 200)
    return json_response({
        "status": "redirect",
        "redirect_to": f"/api/case/{cnr}",
        "cnr": cnr,
    }, 200)


def _resolve_courts(*, court_level: str, state_code: str, district_code: str,
                    get_v4_client) -> list:
    comp = get_components()
    cache_key = f"meta:courts:{court_level}:{state_code}:{district_code}"
    if court_level == "hc":
        # HC: the district_code IS the bench. Synthesise a one-item list.
        return [{"court_code": str(district_code),
                 "name": f"HC bench {district_code}",
                 "establishment_name": None,
                 "establishment_code": None}]
    gw = GatewayCall(
        cache=comp["cache"], coalescer=comp["coalescer"],
        breaker=comp["breaker"], bucket=comp["bucket"],
        key=cache_key, ttl=config.CACHE_TTL_DROPDOWNS,
        stale_ok_horizon=config.CACHE_STALE_OK_HORIZON,
    )
    raw = gw.run(lambda: get_v4_client("dc").list_court_complexes(
        state_code, district_code)).value
    return _extract_court_complexes(raw)


def _extract_court_complexes(raw: Any) -> list:
    if not isinstance(raw, dict):
        return []
    candidates = (
        raw.get("courtComplex")
        or raw.get("complexes")
        or raw.get("court_complexes")
        or raw.get("data")
    )
    if not isinstance(candidates, list):
        candidates = [v for k, v in raw.items()
                      if k.isdigit() and isinstance(v, dict)]
    out: list = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        cc = item.get("court_code") or item.get("complex_code") or item.get("est_code")
        if not cc:
            continue
        out.append({
            "court_code": str(cc),
            "name": item.get("court_complex_name") or item.get("name"),
            "establishment_name": item.get("establishment_name"),
            "establishment_code": item.get("est_code") or item.get("establishment_code"),
            "complex_code": item.get("complex_code"),
        })
    return out
