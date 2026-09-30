"""eCourts case-details searches (POST body → case details).

Six endpoints under /api/case-details/*:
  cnr, filing-number, fir, act, case-type, case-number.

`cnr` returns one case (reuses the shared resolver behind GET /api/case/{cnr}).
The other five are single-shot field searches (one court, one query → a case
list). Unlike party/advocate (which fan out across many courts as async jobs),
these target one establishment, so they're direct async endpoints: the blocking
upstream call runs in the threadpool and is wrapped in the reliability
GatewayCall (cache + rate limit + breaker), so repeats are instant and the
upstream stays protected.

Response shape (the five searches):
  { success, count, cases: [...], source, age_seconds, degraded }
`cnr` returns the case-detail shape { success, data, source, degraded, ... }.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.core import config
from app.core.runtime import get_components
from app.reliability.gateway import GatewayCall, UpstreamDown
from app.routers.common import json_response

log = logging.getLogger("routers.ecourts_search")

router = APIRouter(tags=["eCourts · Case details (search by field)"])


# ── shared runner ───────────────────────────────────────────────────────────

def _run(key: str, body_dict: dict, bridge_fn):
    """Blocking: wrap the bridge call in the reliability stack (cache/rate/breaker)."""
    comp = get_components()
    gw = GatewayCall(
        cache=comp["cache"], coalescer=comp["coalescer"],
        breaker=comp["breaker"], bucket=comp["bucket"],
        key=key, ttl=config.CACHE_TTL_SEARCH,
        stale_ok_horizon=config.CACHE_STALE_OK_HORIZON,
    )
    return gw.run(lambda: bridge_fn(body_dict))


async def _handle(key: str, body_dict: dict, bridge_fn):
    try:
        res = await run_in_threadpool(_run, key, body_dict, bridge_fn)
    except UpstreamDown:
        return json_response({"success": False, "error": "upstream unavailable",
                              "degraded": True}, 503)
    except ValueError as e:
        return json_response({"success": False, "error": str(e)}, 400)
    except Exception as e:
        return json_response({"success": False, "error": str(e)[:200],
                              "error_type": type(e).__name__}, 502)
    cases = res.value or []
    return json_response({
        "success": True,
        "count": len(cases),
        "cases": cases,
        "source": res.source,
        "age_seconds": round(res.age_seconds, 2),
        "degraded": (res.source == "stale"),
    })


# ── request models (defaults = Delhi / New Delhi, est court_code 1) ──────────

class _Loc(BaseModel):
    court_type: str = Field("dc", description="'dc' (district) or 'hc' (high court)")
    state_code: str = Field("26", description="State code. See /api/metadata/states")
    district_code: str = Field("7", description="District/bench code. See /api/metadata/districts")
    court_code: str = Field("1", description="Establishment code (njdg_est_code). See /api/metadata/courts")


class FilingNumberReq(_Loc):
    filing_no: str = Field("1", description="Filing number")
    filing_year: str = Field("2023", description="Filing year, e.g. 2023")


class FirReq(_Loc):
    police_station: str = Field("", description="Police station code (optional). See /api/metadata (police stations)")
    fir_no: str = Field("1", description="FIR number")
    fir_year: str = Field("2023", description="FIR year, e.g. 2023")
    status: str = Field("Both", description="Pending / Disposed / Both")


class ActReq(_Loc):
    act_type: str = Field("1", description="Act code — the ONLY required act field. "
                          "Get valid codes from GET /api/metadata/acts?court_type=dc"
                          "&state_code=26&district_code=7&court_code=1")
    status: str = Field("Pending", description="Which pool to search: 'Pending' "
                        "or 'Disposed' (one at a time — there is no 'Both'). "
                        "Each returned case is tagged pending_disposed.")


class CaseTypeReq(_Loc):
    case_type: str = Field("1", description="Case type code. See /api/metadata/case-types")
    year: str = Field("2023", description="Year (optional), e.g. 2023")
    status: str = Field("Both", description="Pending / Disposed / Both")


class CaseNumberReq(_Loc):
    case_type: str = Field("1", description="Case type code. See /api/metadata/case-types")
    case_no: str = Field("1", description="Case number")
    year: str = Field("2023", description="Year, e.g. 2023")


class CnrReq(BaseModel):
    cnr: str = Field("MHPU010104452016", description="16-char CNR, e.g. MHPU010104452016")
    court_type: str | None = Field(None, description="'dc'/'hc' (optional; auto-detected from CNR)")
    normalize: bool = Field(True, description="Return normalized shape (default) or raw (false)")


class BarCodeReq(_Loc):
    # Defaults = the verified MP/687/2012 sample (Bhopal District & Sessions Court)
    state_code: str = Field("23", description="Court state code (23=Madhya Pradesh). See /api/metadata/states")
    district_code: str = Field("50", description="Court district code (50=Bhopal). See /api/metadata/districts")
    court_code: str = Field("1", description="Establishment code (1=District & Sessions Court). See /api/metadata/courts")
    bar_code: str = Field("MP/687/2012", description="Advocate Bar Registration: STATE/NUMBER/YEAR, e.g. MP/687/2012")
    status: str = Field("Both", description="Pending / Disposed / Both")
    # Pre-split alternative to bar_code, for callers that already hold the
    # parts (the onboarding search does). bridge_advocate_barcode has always
    # accepted these, but they were never declared here, and pydantic drops
    # undeclared fields silently: sending them used to fall through to
    # splitting the DEFAULT bar_code above and search a different advocate.
    # Supply all three, or none and use bar_code.
    bar_state: str | None = Field(None, description="Bar state letters, e.g. MP (use with bar_number + bar_year)")
    bar_number: str | None = Field(None, description="Bar registration number, e.g. 687")
    bar_year: str | None = Field(None, description="Bar registration year, e.g. 2012 (2-digit accepted)")


class AdvocateNameReq(_Loc):
    # No `year`: the v4 advocate endpoint takes no year filter (see
    # ecourts_v4.search_by_advocate), and advertising one we silently drop
    # would be worse than not offering it.
    state_code: str = Field("23", description="Court state code. See /api/metadata/states")
    district_code: str = Field("50", description="Court district code. See /api/metadata/districts")
    court_code: str = Field("1", description="Establishment code. See /api/metadata/courts")
    advocate_name: str = Field("Sharma", description="Advocate name, full or partial")
    status: str = Field("Both", description="Pending / Disposed / Both")


# ── endpoints ────────────────────────────────────────────────────────────────

@router.post("/api/case-details/cnr",
             summary="Case details by CNR (POST body — same result as GET /api/case/{cnr})")
async def case_details_cnr(body: CnrReq):
    from app.routers.cases import case_detail_response
    return await case_detail_response(body.cnr, body.court_type, body.normalize)


@router.post("/api/case-details/bar_code",
             summary="Search cases by Advocate Bar Registration (bar code, e.g. MP/687/2012)")
async def search_bar_code(body: BarCodeReq):
    from app.upstream.bridge import bridge_advocate_barcode
    b = body.model_dump()
    # The pre-split parts win over bar_code when present, so they MUST be in
    # the cache key: keyed on bar_code alone, two advocates supplying different
    # bar_state/number/year would share one entry and get each other's cases.
    ident = (f"{b['bar_state']}/{b['bar_number']}/{b['bar_year']}"
             if (b.get('bar_state') and b.get('bar_number') and b.get('bar_year'))
             else b['bar_code'])
    key = f"barcode:{b['court_type']}:{b['state_code']}:{b['district_code']}:{b['court_code']}:{ident}:{b['status']}"
    return await _handle(key, b, bridge_advocate_barcode)


@router.post("/api/case-details/advocate_name",
             summary="Search cases by Advocate NAME in one court (synchronous)")
async def search_advocate_name(body: AdvocateNameReq):
    """The name half of advocate search, alongside bar_code.

    bridge_advocate(mode="name") already existed but nothing exposed it over
    HTTP, so the only way to search by advocate name was /api/find, which fans
    out across courts as an async job. Advocate onboarding needs the opposite:
    one court, one answer, synchronously, so the caller can drive its own
    per-court progress. Same request shape as bar_code, so the two onboarding
    tabs are symmetric.
    """
    from app.upstream.bridge import bridge_advocate
    b = body.model_dump()
    # The bridge reads `pending_disposed`; the HTTP surface says `status` to
    # match bar_code. Map it rather than leaking the upstream's spelling, and
    # rather than silently defaulting every search to Both.
    b["pending_disposed"] = b.get("status", "Both")
    key = (f"advname:{b['court_type']}:{b['state_code']}:{b['district_code']}:"
           f"{b['court_code']}:{b['advocate_name']}:{b['status']}")
    return await _handle(key, b, lambda body_dict, proxies=None: bridge_advocate(
        body_dict, proxies=proxies, mode="name"))


@router.post("/api/case-details/filing-number",
             summary="Search cases by Filing Number + Year")
async def search_filing_number(body: FilingNumberReq):
    from app.upstream.bridge import bridge_filing_number
    b = body.model_dump()
    key = f"filing:{b['court_type']}:{b['state_code']}:{b['district_code']}:{b['court_code']}:{b['filing_no']}:{b['filing_year']}"
    return await _handle(key, b, bridge_filing_number)


@router.post("/api/case-details/fir",
             summary="Search cases by FIR Number + Year (District Courts only)")
async def search_fir(body: FirReq):
    from app.upstream.bridge import bridge_fir
    b = body.model_dump()
    key = f"fir:{b['state_code']}:{b['district_code']}:{b['court_code']}:{b['police_station']}:{b['fir_no']}:{b['fir_year']}:{b['status']}"
    return await _handle(key, b, bridge_fir)


@router.post("/api/case-details/act",
             summary="Search cases by Act type (act_type + status Pending|Disposed)")
async def search_act(body: ActReq):
    from app.upstream.bridge import bridge_act
    b = body.model_dump()
    key = f"act:{b['court_type']}:{b['state_code']}:{b['district_code']}:{b['court_code']}:{b['act_type']}:{b['status']}"
    return await _handle(key, b, bridge_act)


@router.post("/api/case-details/case-type",
             summary="Search all cases of a Case Type (+ Year)")
async def search_case_type(body: CaseTypeReq):
    from app.upstream.bridge import bridge_case_type
    b = body.model_dump()
    key = f"casetype:{b['court_type']}:{b['state_code']}:{b['district_code']}:{b['court_code']}:{b['case_type']}:{b['year']}:{b['status']}"
    return await _handle(key, b, bridge_case_type)


@router.post("/api/case-details/case-number",
             summary="Search a case by Case Type + Case Number + Year")
async def search_case_number(body: CaseNumberReq):
    from app.upstream.bridge import bridge_case_number
    b = body.model_dump()
    key = f"caseno:{b['court_type']}:{b['state_code']}:{b['district_code']}:{b['court_code']}:{b['case_type']}:{b['case_no']}:{b['year']}"
    return await _handle(key, b, bridge_case_number)
