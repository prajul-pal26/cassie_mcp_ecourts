"""SAT (Securities Appellate Tribunal) routes — satweb.sat.gov.in.

Case details (3 searches, each returns full 'View' details + order links),
Orders (4 searches, each returns order links), and a GET order_link resolver
that turns a stored (session-bound) view-order link into the live PDF —
directly viewable in the browser (Content-Disposition: inline).

Every order in the search results also carries a ready-to-click `pdf_url` that
points back at this API's GET resolver, so the stored link just works.

Captcha is bypassed in the provider (server only checks the CSRF token +
session). Blocking scraping runs in the threadpool via asyncio.to_thread.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.routers.common import json_response
from app.tribunals.sat import fetch_sat as sat

router = APIRouter(tags=["SAT"])


# ── helpers ──────────────────────────────────────────────────────────────────

def _absolutize(data: dict, base_url) -> dict:
    """Add a clickable `pdf_url` (GET → our resolver) to every order in the
    result, so a stored order is one click away from the actual PDF."""
    base = str(base_url).rstrip("/")

    def pu(oid):
        return f"{base}/sat/order_link/{oid}" if oid else None

    if isinstance(data, dict):
        for o in (data.get("orders") or []):
            if o.get("order_id"):
                o["pdf_url"] = pu(o["order_id"])
        for c in (data.get("cases") or []):
            det = c.get("details") or {}
            for o in (det.get("orders") or []):
                if o.get("order_id"):
                    o["pdf_url"] = pu(o["order_id"])
            for h in (det.get("listing_history") or []):
                if h.get("order_id"):
                    h["pdf_url"] = pu(h["order_id"])
    return data


async def _stream_order(ref: str):
    """Resolve an order id/link → live PDF, served inline."""
    res = await asyncio.to_thread(sat.resolve_order, ref)
    if not res:
        return json_response({
            "success": False,
            "error": "could not resolve order PDF (order not found or upstream refused)",
            "input": ref,
        }, 404)
    fname, pdf = res
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{fname}"'})


# ── options ──────────────────────────────────────────────────────────────────

@router.get("/sat/options", summary="Dropdown choices: Bench, Appeal Type, Order date type")
async def sat_options():
    return await asyncio.to_thread(sat.list_options)


# ── order-link resolver → live PDF (GET, clickable) ──────────────────────────

@router.get("/sat/order_link/{order_id}",
            summary="Open an order PDF by id (GET — click to view the actual PDF)",
            description="Direct-hit GET: returns the live SAT order PDF inline "
            "(like clicking the View button). `order_id` is the numeric id at the "
            "end of a view-order link. This is the `pdf_url` returned with every order.")
async def order_link_by_id(order_id: str):
    return await _stream_order(order_id)


@router.get("/sat/order_link",
            summary="Open an order PDF from a stored view-order link (GET)",
            description="Pass a stored (session-bound / stale) "
            "`view-order/<hash>/<id>` link as ?link=… — we re-mint a fresh token "
            "server-side and return the actual PDF inline.")
async def order_link_by_url(
    link: str = Query(...,
                      description="Stored view-order link or numeric order id",
                      examples=["https://satweb.sat.gov.in/view-order/7354864f9563dcdb6f7a5c4fba18a8917c7581b1f7151845353e2bea5c78661b/40215"])):
    return await _stream_order(link)


# ── case details (3 searches) ────────────────────────────────────────────────

class _CaseByNumber(BaseModel):
    case_type: str = Field("1", description="1=SEBI, 2=IRDAI, 3=PFRDA")
    case_no: str = Field("909", description="Case/Appeal number, e.g. 909")
    filing_year: str = Field("2023", description="Filing year, e.g. 2023")
    bench: str = Field("1", description="Bench (1=Mumbai)")


class _CaseByAppeal(BaseModel):
    al_number: str = Field("877", description="AL (diary) number, e.g. 877")
    filing_year: str = Field("2023", description="Filing year, e.g. 2023")
    bench: str = Field("1", description="Bench (1=Mumbai)")


class _CaseByParty(BaseModel):
    prty_name: str = Field("Himanshu", description="Party name, e.g. Himanshu")
    filing_year: str = Field("2023", description="Filing year, e.g. 2023")
    bench: str = Field("1", description="Bench (1=Mumbai)")


@router.post("/sat/case-details/by-case-number",
             summary="Case details by Appeal Type + Case No + Year (full View details)")
async def case_by_case_number(body: _CaseByNumber, request: Request):
    res = await asyncio.to_thread(
        sat.case_by_case_number, body.case_type, body.case_no, body.filing_year, body.bench)
    return _absolutize(res, request.base_url)


@router.post("/sat/case-details/by-appeal-number",
             summary="Case details by AL (diary) Number + Year (full View details)")
async def case_by_appeal_number(body: _CaseByAppeal, request: Request):
    res = await asyncio.to_thread(
        sat.case_by_appeal_number, body.al_number, body.filing_year, body.bench)
    return _absolutize(res, request.base_url)


@router.post("/sat/case-details/by-party-name",
             summary="Case details by Party Name + Year (full View details, many)")
async def case_by_party_name(body: _CaseByParty, request: Request):
    res = await asyncio.to_thread(
        sat.case_by_party_name, body.prty_name, body.filing_year, body.bench)
    return _absolutize(res, request.base_url)


# ── orders (4 searches) ──────────────────────────────────────────────────────

class _OrdByNumber(BaseModel):
    case_type: str = Field("1", description="1=SEBI, 2=IRDAI, 3=PFRDA")
    case_no: str = Field("213", description="Case/Appeal number")
    filing_year: str = Field("2023", description="Year")
    bench: str = Field("1", description="Bench (1=Mumbai)")


class _OrdByAppeal(BaseModel):
    al_number: str = Field("80", description="AL (diary) number")
    filing_year: str = Field("2023", description="Year")
    bench: str = Field("1", description="Bench (1=Mumbai)")


class _OrdByParty(BaseModel):
    prty_name: str = Field("SEBI", description="Party name")
    filing_year: str = Field("2023", description="Year")
    bench: str = Field("1", description="Bench (1=Mumbai)")


class _OrdByDate(BaseModel):
    apl_type: str = Field("1", description="1=SEBI, 2=IRDA, 3=PFRDA")
    start_date: str = Field("01-02-2023", description="From date, dd-mm-yyyy")
    end_date: str = Field("28-02-2023", description="To date, dd-mm-yyyy")


@router.post("/sat/orders/by-case-number",
             summary="Orders by Appeal Type + Case No + Year (each with clickable pdf_url)")
async def orders_by_case_number(body: _OrdByNumber, request: Request):
    res = await asyncio.to_thread(
        sat.orders_by_case_number, body.case_type, body.case_no, body.filing_year, body.bench)
    return _absolutize(res, request.base_url)


@router.post("/sat/orders/by-appeal-number",
             summary="Orders by AL (diary) Number + Year (each with clickable pdf_url)")
async def orders_by_appeal_number(body: _OrdByAppeal, request: Request):
    res = await asyncio.to_thread(
        sat.orders_by_appeal_number, body.al_number, body.filing_year, body.bench)
    return _absolutize(res, request.base_url)


@router.post("/sat/orders/by-party-name",
             summary="Orders by Party Name + Year (each with clickable pdf_url)")
async def orders_by_party_name(body: _OrdByParty, request: Request):
    res = await asyncio.to_thread(
        sat.orders_by_party_name, body.prty_name, body.filing_year, body.bench)
    return _absolutize(res, request.base_url)


@router.post("/sat/orders/by-date",
             summary="Orders by Type + From/To date range (each with clickable pdf_url)")
async def orders_by_date(body: _OrdByDate, request: Request):
    res = await asyncio.to_thread(
        sat.orders_by_date, body.apl_type, body.start_date, body.end_date)
    return _absolutize(res, request.base_url)
