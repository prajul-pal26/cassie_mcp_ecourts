"""Metadata routes — the dropdowns needed to build a search.

state → district/bench → court complex → (case types | acts). Each runs through
the reliability stack with the 24h dropdown TTL, so a cache hit is the common
case and even an open breaker still serves yesterday's data. All handlers async;
the blocking gateway call is offloaded to the threadpool.

Every dropdown is available as BOTH:
  * GET  /api/metadata/<x>?state_code=..&district_code=..   (query params)
  * POST /api/metadata/<x>  { "state_code": .., "district_code": .. }  (JSON body)
The POST form mirrors the /api/case-details/* search bodies, so you fill the same
fields to get the dropdown you'd then search with. Both return the full list.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool

from app.core import config
from app.core.runtime import get_components
from app.reliability.gateway import GatewayCall, UpstreamDown
from app.routers.common import json_response

log = logging.getLogger("routers.metadata")

router = APIRouter(tags=["eCourts · Metadata"])


def _wrap_call(*, key: str, fetch_fn, ttl: float = config.CACHE_TTL_DROPDOWNS):
    comp = get_components()
    gw = GatewayCall(
        cache=comp["cache"], coalescer=comp["coalescer"],
        breaker=comp["breaker"], bucket=comp["bucket"],
        key=key, ttl=ttl, stale_ok_horizon=config.CACHE_STALE_OK_HORIZON,
    )
    return gw.run(fetch_fn)


def _norm_ct(v) -> str:
    ct = (str(v or "dc")).lower()
    return ct if ct in ("dc", "hc") else "dc"


def _qp(request: Request, *names: str):
    """First present query param among aliases. Lets the GET metadata endpoints
    accept BOTH the metadata-style `dist_code` and the search-body-style
    `district_code` (same for court_code / court_code_arr)."""
    for n in names:
        v = request.query_params.get(n)
        if v:
            return v
    return None


# ── shared core (used by both GET and POST) ──────────────────────────────────

async def _serve(key: str, fetch_fn, extra: dict, parse=None):
    try:
        res = await run_in_threadpool(_wrap_call, key=key, fetch_fn=fetch_fn)
    except UpstreamDown:
        return json_response({"success": False, "error": "upstream unavailable",
                              "degraded": True}, 503)
    except Exception as e:
        log.warning("metadata %s failed: %r", key, e)
        return json_response({"success": False, "error": str(e)[:200]}, 502)
    body = {"success": True, "data": res.value, "source": res.source,
            "age_seconds": round(res.age_seconds, 2),
            "degraded": (res.source == "stale")}
    body.update(extra)
    if parse:                       # add cleaned, ready-to-use fields (e.g. acts[])
        try:
            body.update(parse(res.value))
        except Exception as e:
            log.warning("metadata %s parse failed: %r", key, e)
    return json_response(body)


def _parse_acts(data) -> list:
    """eCourts returns acts as one `code~name#code~name#...` string. Flatten it
    into a clean [{act_type, name}] list the frontend can render directly — each
    `act_type` plugs straight into POST /api/case-details/act."""
    out = []
    if isinstance(data, dict):
        for it in (data.get("actsList") or data.get("acts") or []):
            acts_str = it.get("acts") if isinstance(it, dict) else None
            if isinstance(acts_str, str):
                for entry in acts_str.split("#"):
                    if "~" in entry:
                        code, name = entry.split("~", 1)
                        code = code.strip()
                        if code:
                            out.append({"act_type": code, "name": name.strip()})
    return out


async def _states_core(ct: str):
    from app.upstream.bridge import _get_v4
    return await _serve(f"meta:states:{ct}",
                        lambda: _get_v4(ct).list_states(), {"court_type": ct})


async def _districts_core(ct: str, state_code):
    from app.upstream.bridge import _get_v4
    if not state_code:
        return json_response({"success": False, "error": "state_code is required"}, 400)
    return await _serve(f"meta:districts:{ct}:{state_code}",
                        lambda: _get_v4(ct).list_districts(state_code),
                        {"court_type": ct, "state_code": state_code})


async def _courts_core(ct: str, state_code, dist_code):
    from app.upstream.bridge import _get_v4
    if not state_code:
        return json_response({"success": False, "error": "state_code is required"}, 400)
    if not dist_code:
        return json_response({"success": False, "error": "district_code is required"}, 400)
    if ct == "hc":
        return json_response({
            "success": True,
            "data": {"note": "HC has no separate complexes; "
                             "use the bench from /api/metadata/districts"},
            "source": "static", "age_seconds": 0.0,
            "degraded": False, "court_type": ct})
    return await _serve(f"meta:courts:dc:{state_code}:{dist_code}",
                        lambda: _get_v4("dc").list_court_complexes(state_code, dist_code),
                        {"court_type": ct, "state_code": state_code, "dist_code": dist_code})


async def _case_types_core(ct: str, state_code, dist_code, court_code):
    from app.upstream.bridge import _get_v4
    if not (state_code and dist_code and court_code):
        return json_response({"success": False,
                              "error": "state_code, district_code, court_code are required"}, 400)
    return await _serve(f"meta:case_types:{ct}:{state_code}:{dist_code}:{court_code}",
                        lambda: _get_v4(ct).list_case_types(state_code, dist_code, court_code),
                        {"court_type": ct})


async def _acts_core(ct: str, state_code, dist_code, court_code, search_text):
    from app.upstream.bridge import _get_v4
    if not (state_code and dist_code):
        return json_response({"success": False,
                              "error": "state_code and district_code are required"}, 400)
    if ct == "hc":
        # HC has no user-facing court-establishment dropdown for acts; the
        # upstream still wants a code, and the main seat ("1") works.
        court_code = court_code or "1"
    elif not court_code:
        return json_response({"success": False,
                              "error": "court_code is required for dc"}, 400)
    return await _serve(
        f"meta:acts:{ct}:{state_code}:{dist_code}:{court_code}:{search_text or ''}",
        lambda: _get_v4(ct).list_acts(state_code, dist_code, court_code, search_text),
        {"court_type": ct, "state_code": state_code, "district_code": dist_code,
         "court_code": court_code},
        parse=lambda v: {"acts": (a := _parse_acts(v)), "count": len(a)})


# ── endpoints (GET dropdowns; metadata is fetched internally by the searches,
#    so these GETs are just for building a manual frontend dropdown) ───────────

@router.get("/api/metadata/states", summary="List states / High Courts (dropdown)")
async def metadata_states(request: Request):
    return await _states_core(_norm_ct(request.query_params.get("court_type")))


@router.get("/api/metadata/districts", summary="List districts / HC benches for a state")
async def metadata_districts(request: Request):
    return await _districts_core(_norm_ct(request.query_params.get("court_type")),
                                 _qp(request, "state_code"))


@router.get("/api/metadata/courts", summary="List court complexes in a district — gives court_code")
async def metadata_courts(request: Request):
    return await _courts_core(_norm_ct(request.query_params.get("court_type")),
                              _qp(request, "state_code"),
                              _qp(request, "dist_code", "district_code"))


@router.get("/api/metadata/case-types", summary="List case-type codes for a court")
async def metadata_case_types(request: Request):
    return await _case_types_core(_norm_ct(request.query_params.get("court_type")),
                                  _qp(request, "state_code"),
                                  _qp(request, "dist_code", "district_code"),
                                  _qp(request, "court_code", "court_code_arr"))


@router.get("/api/metadata/acts", summary="List ALL act codes for a court (dropdown)",
            description="Returns the FULL act dropdown for a court as a clean "
            "acts[] of {act_type, name} — each act_type plugs straight into "
            "POST /api/case-details/act. DC: state_code + district_code + "
            "court_code (establishment). HC: court_code is OPTIONAL (defaults to "
            "the main seat). Optional search_text filters by name. Accepts "
            "dist_code / court_code_arr aliases.")
async def metadata_acts(request: Request):
    return await _acts_core(_norm_ct(request.query_params.get("court_type")),
                            _qp(request, "state_code"),
                            _qp(request, "dist_code", "district_code"),
                            _qp(request, "court_code", "court_code_arr"),
                            request.query_params.get("search_text") or None)
