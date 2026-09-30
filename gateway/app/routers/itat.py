"""ITAT (Income Tax Appellate Tribunal) routes.

Two groups, by path prefix:
  /itat/case-details/*  -> full case details (casestatus)
  /itat/orders/*        -> tribunal orders + PDFs (tribunalorders)

Handlers are thin async wrappers over the self-contained providers under
app/tribunals/itat/ — copied verbatim from the local Tribunal/ITAT build, no
logic changes. Blocking provider work (captcha solve + upstream fetch) runs in
the threadpool via asyncio.to_thread so the event loop stays responsive.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.tribunals.itat import fetch_itat_casestatus as itat
from app.tribunals.itat import fetch_itat_orders as itat_orders

router = APIRouter(tags=["ITAT"])


def _absolutize_pdf_urls(data: dict, base_url: str) -> dict:
    """Rewrite relative order-PDF links in a response into full, directly-
    openable URLs (prefixed with the host the caller used)."""
    base = str(base_url).rstrip("/")

    def fix(u):
        return base + u if isinstance(u, str) and u.startswith("/itat/") else u

    if isinstance(data, dict):
        for c in (data.get("cases") or []):
            for o in (c.get("tribunal_orders") or []):
                if o.get("pdf_url"):
                    o["pdf_url"] = fix(o["pdf_url"])
        for o in (data.get("orders") or []):
            if o.get("order_pdf_url"):
                o["order_pdf_url"] = fix(o["order_pdf_url"])
    return data


# ── ITAT CASE DETAILS ──────────────────────────────────────────────────────

class _CDByAppealNumber(BaseModel):
    bench: str = Field("199", description="Bench code, e.g. 199 (Mumbai). See /itat/case-details/options")
    appeal_type: str = Field("ITA", description="Appeal type, e.g. ITA")
    appeal_number: str = Field("1", description="Appeal number, e.g. 1")
    filing_year: str = Field("2024", description="Filing year, e.g. 2024")


class _CDByDateOfFiling(BaseModel):
    bench: str = Field("199", description="Bench code, e.g. 199 (Mumbai)")
    appeal_type: str = Field("ITA", description="Appeal type, e.g. ITA")
    date_of_filing: str = Field("01/01/2024", description="Date of filing, dd/mm/yyyy")


class _CDByAssesseeName(BaseModel):
    bench: str = Field("199", description="Bench code, e.g. 199 (Mumbai)")
    appeal_type: str = Field("ITA", description="Appeal type, e.g. ITA")
    assessee_name: str = Field("LML", description="Assessee name, e.g. LML")


class _CDByAckNumber(BaseModel):
    bench: str = Field("199", description="Bench code, e.g. 199 (Mumbai)")
    appeal_type: str = Field("ITA", description="Appeal type, e.g. ITA")
    acknowledgement_number: str = Field("123456", description="Acknowledgement number")


@router.post("/itat/case-details/by-appeal-number",
             summary="Case details by Bench + Appeal Type + Appeal Number + Filing Year")
async def cd_by_appeal_number(body: _CDByAppealNumber, request: Request):
    res = await asyncio.to_thread(
        itat.by_appeal_number, body.bench, body.appeal_type,
        body.appeal_number, body.filing_year)
    return _absolutize_pdf_urls(res, request.base_url)


@router.post("/itat/case-details/by-date-of-filing",
             summary="Case details by Bench + Appeal Type + Date of Filing (many)")
async def cd_by_date_of_filing(body: _CDByDateOfFiling, request: Request):
    res = await asyncio.to_thread(
        itat.by_date_of_filing, body.bench, body.appeal_type, body.date_of_filing)
    return _absolutize_pdf_urls(res, request.base_url)


@router.post("/itat/case-details/by-assessee-name",
             summary="Case details by Bench + Appeal Type + Assessee Name (many)")
async def cd_by_assessee_name(body: _CDByAssesseeName, request: Request):
    res = await asyncio.to_thread(
        itat.by_assessee_name, body.bench, body.appeal_type, body.assessee_name)
    return _absolutize_pdf_urls(res, request.base_url)


@router.post("/itat/case-details/by-acknowledgement-number",
             summary="Case details by Bench + Appeal Type + Acknowledgement Number")
async def cd_by_acknowledgement_number(body: _CDByAckNumber, request: Request):
    res = await asyncio.to_thread(
        itat.by_acknowledgement_number, body.bench, body.appeal_type,
        body.acknowledgement_number)
    return _absolutize_pdf_urls(res, request.base_url)


@router.get("/itat/case-details/options",
            summary="Dropdown choices: Bench, Appeal Type, Filing Year")
async def cd_options():
    return await asyncio.to_thread(itat.list_options)


@router.get("/itat/case-details/order-pdf",
            summary="Download an order PDF listed on a case-details page")
async def cd_order_pdf(
    bench: str = Query("199", description="Bench code, e.g. 199"),
    appeal_type: str = Query("ITA", description="Appeal type, e.g. ITA"),
    appeal_number: str = Query("1", description="Appeal number, e.g. 1"),
    filing_year: str = Query("2024", description="Filing year, e.g. 2024"),
    order_index: int = Query(0, description="Which order in the case's tribunal_orders (0-based)"),
):
    """Streams a case-details order PDF (the viewOrder link is session-bound,
    so we re-fetch it server-side). Appears as each tribunal_orders[].pdf_url."""
    res = await asyncio.to_thread(
        itat.download_order_pdf, bench, appeal_type, appeal_number,
        filing_year, order_index)
    if not res:
        raise HTTPException(404, "order PDF not available (not uploaded, or case/order not found)")
    fname, pdf = res
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{fname}"'})


# ── ITAT ORDERS ────────────────────────────────────────────────────────────

class _OrdByAppealNumber(BaseModel):
    bench: str = Field("199", description="Bench code, e.g. 199. See /itat/orders/options")
    appeal_type: str = Field("ITA", description="Appeal type, e.g. ITA")
    appeal_number: str = Field("1", description="Appeal number, e.g. 1")
    filing_year: str = Field("2024", description="Filing year, e.g. 2024")


class _OrdByOrderDate(BaseModel):
    bench: str = Field("199", description="Bench code, e.g. 199")
    appeal_type: str = Field("ITA", description="Appeal type, e.g. ITA")
    order_date: str = Field("11/06/2024", description="Date of order, dd/mm/yyyy")


class _OrdByPronDate(BaseModel):
    bench: str = Field("199", description="Bench code, e.g. 199")
    appeal_type: str = Field("ITA", description="Appeal type, e.g. ITA")
    pronouncement_date: str = Field("11/06/2024", description="Date of pronouncement, dd/mm/yyyy")


class _OrdByMember(BaseModel):
    bench: str = Field("199", description="Bench code, e.g. 199")
    member: str = Field("184", description="Member code (184=Anikesh Banerjee). See /itat/orders/options")
    order_date: str = Field("11/06/2024", description="Date of order, dd/mm/yyyy")


@router.post("/itat/orders/by-appeal-number",
             summary="Orders by Bench + Appeal Type + Appeal Number + Filing Year")
async def ord_by_appeal_number(body: _OrdByAppealNumber, request: Request):
    res = await asyncio.to_thread(
        itat_orders.by_appeal_number, body.bench, body.appeal_type,
        body.appeal_number, body.filing_year)
    return _absolutize_pdf_urls(res, request.base_url)


@router.post("/itat/orders/by-order-date",
             summary="Orders by Bench + Appeal Type + Date of Order (many)")
async def ord_by_order_date(body: _OrdByOrderDate, request: Request):
    res = await asyncio.to_thread(
        itat_orders.by_order_date, body.bench, body.appeal_type, body.order_date)
    return _absolutize_pdf_urls(res, request.base_url)


@router.post("/itat/orders/by-pronouncement-date",
             summary="Orders by Bench + Appeal Type + Date of Pronouncement (many)")
async def ord_by_pron_date(body: _OrdByPronDate, request: Request):
    res = await asyncio.to_thread(
        itat_orders.by_pronouncement_date, body.bench, body.appeal_type,
        body.pronouncement_date)
    return _absolutize_pdf_urls(res, request.base_url)


@router.post("/itat/orders/by-member-name",
             summary="Orders by Bench + Member + Date of Order (many)")
async def ord_by_member(body: _OrdByMember, request: Request):
    res = await asyncio.to_thread(
        itat_orders.by_member_name, body.bench, body.member, body.order_date)
    return _absolutize_pdf_urls(res, request.base_url)


@router.get("/itat/orders/options",
            summary="Dropdown choices: Bench, Appeal Type, Member")
async def ord_options():
    return await asyncio.to_thread(itat_orders.list_options)


@router.get("/itat/orders/order-pdf",
            summary="Download an order PDF from the orders search (the 'View Order' link)")
async def ord_order_pdf(
    bench: str = Query("199", description="Bench code, e.g. 199"),
    appeal_type: str = Query("ITA", description="Appeal type, e.g. ITA"),
    appeal_number: str = Query("1", description="Appeal number, e.g. 1"),
    filing_year: str = Query("2024", description="Filing year, e.g. 2024"),
    order_index: int = Query(0, description="Which order (0-based) for that appeal"),
):
    """Streams an order PDF from the orders search (session-bound viewOrder
    link, re-fetched server-side). Appears as each order's order_pdf_url."""
    res = await asyncio.to_thread(
        itat_orders.download_order_pdf, bench, appeal_type, appeal_number,
        filing_year, order_index)
    if not res:
        raise HTTPException(404, "order PDF not available (not uploaded, or case/order not found)")
    fname, pdf = res
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{fname}"'})
