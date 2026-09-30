"""CESTAT (Customs, Excise & Service Tax Appellate Tribunal) routes.

Two groups, by path prefix:
  /cestat/case-details/*  -> full case details (casestatus). No captcha.
  /cestat/orders/*        -> orders (daily + final merged). Each carries a
                             direct pdf_url; client-side paginated.

Thin async wrappers over the self-contained providers under
app/tribunals/cestat/ — copied verbatim, no logic changes. Blocking work runs
in the threadpool via asyncio.to_thread.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.tribunals.cestat import fetch_cestat_casestatus as cestat
from app.tribunals.cestat import fetch_cestat_orders as cestat_orders

router = APIRouter(tags=["CESTAT"])


# ── CESTAT CASE DETAILS ────────────────────────────────────────────────────

class _CesDiary(BaseModel):
    bench: str = Field("delhi", description="Bench/zone, e.g. delhi. See /cestat/case-details/options")
    diary_no: str = Field("02121", description="Diary number, e.g. 02121")
    year: str = Field("2011", description="Year, e.g. 2011")


class _CesCase(BaseModel):
    bench: str = Field("delhi", description="Bench/zone, e.g. delhi")
    case_type: str = Field("2", description="Case type code (1=Customs,2=Excise,3=Service Tax,4=Antidumping,5=Central Sale Tax)")
    case_no: str = Field("0002298", description="Case number, e.g. 0002298")
    year: str = Field("2011", description="Year, e.g. 2011")


class _CesParty(BaseModel):
    bench: str = Field("delhi", description="Bench/zone, e.g. delhi")
    party_name: str = Field("SINGH", description="Party name, e.g. SINGH")


class _CesImpugned(BaseModel):
    bench: str = Field("delhi", description="Bench/zone, e.g. delhi")
    impugned_order: str = Field("IND-CEX-OOO-APP-269-2011", description="Impugned order no (O-I-A / O-I-O)")


@router.post("/cestat/case-details/by-diary-number",
             summary="Case details by Bench + Diary Number + Year")
async def ces_by_diary(body: _CesDiary):
    return await asyncio.to_thread(
        cestat.by_diary_number, body.bench, body.diary_no, body.year)


@router.post("/cestat/case-details/by-case-number",
             summary="Case details by Bench + Case Type + Case Number + Year")
async def ces_by_case(body: _CesCase):
    return await asyncio.to_thread(
        cestat.by_case_number, body.bench, body.case_type, body.case_no, body.year)


@router.post("/cestat/case-details/by-party-name",
             summary="Case details by Bench + Party Name (many, list)")
async def ces_by_party(body: _CesParty):
    return await asyncio.to_thread(
        cestat.by_party_name, body.bench, body.party_name)


@router.post("/cestat/case-details/by-impugned-order",
             summary="Case details by Bench + Impugned Order No (O-I-A / O-I-O)")
async def ces_by_impugned(body: _CesImpugned):
    return await asyncio.to_thread(
        cestat.by_impugned_order, body.bench, body.impugned_order)


@router.get("/cestat/case-details/options",
            summary="Dropdown choices: Bench, Case Type")
async def ces_options():
    return await asyncio.to_thread(cestat.list_options)


@router.get("/cestat/case-details/report",
            summary="Full case details for a report_id + bench (e.g. from a party-name row)")
async def ces_report(
    report_id: str = Query("0707910021212011", description="Report id from a search row"),
    bench: str = Query("delhi", description="Bench, e.g. delhi"),
):
    return await asyncio.to_thread(cestat.fetch_full_details, report_id, bench)


# ── CESTAT ORDERS (daily + final merged; each order carries a direct pdf_url) ──

class _CoCase(BaseModel):
    bench: str = Field("107079", description="Bench code, e.g. 107079 (Delhi). See /cestat/orders/options")
    case_type: str = Field("3", description="1=Customs,2=Excise,3=Service Tax,4=Antidumping,5=Central Sales Tax")
    case_no: str = Field("51486", description="Case number, e.g. 51486")
    case_year: str = Field("2022", description="Case year, e.g. 2022")


class _CoDiary(BaseModel):
    bench: str = Field("107079", description="Bench code, e.g. 107079 (Delhi)")
    diary_no: str = Field("02121", description="Diary number")
    diary_year: str = Field("2011", description="Diary year")


class _CoMember(BaseModel):
    bench: str = Field("107079", description="Bench code, e.g. 107079 (Delhi)")
    member_name: str = Field("143", description="Member code. See /cestat/orders/options")
    from_date: str = Field("01-01-2023", description="From date, dd-mm-yyyy")
    to_date: str = Field("31-12-2023", description="To date, dd-mm-yyyy")


class _CoOrderDate(BaseModel):
    bench: str = Field("107079", description="Bench code, e.g. 107079 (Delhi)")
    from_date: str = Field("01-06-2024", description="From date, dd-mm-yyyy")
    to_date: str = Field("07-06-2024", description="To date, dd-mm-yyyy")


class _CoParty(BaseModel):
    bench: str = Field("107079", description="Bench code, e.g. 107079 (Delhi)")
    party_name: str = Field("SINGH", description="Party name, e.g. SINGH")


class _CoDesc(BaseModel):
    bench: str = Field("107079", description="Bench code, e.g. 107079 (Delhi)")
    description: str = Field("appeal", description="Brief description / keyword")
    from_date: str = Field("01-01-2024", description="From date, dd-mm-yyyy")
    to_date: str = Field("31-12-2024", description="To date, dd-mm-yyyy")


@router.post("/cestat/orders/by-case-number",
             summary="Orders (daily+final merged) by Bench + Case Type + Case No + Case Year")
async def co_by_case(body: _CoCase):
    return await asyncio.to_thread(
        cestat_orders.by_case_number, body.bench, body.case_type, body.case_no, body.case_year)


@router.post("/cestat/orders/by-diary-number",
             summary="Orders (daily+final merged) by Bench + Diary No + Diary Year")
async def co_by_diary(body: _CoDiary):
    return await asyncio.to_thread(
        cestat_orders.by_diary_number, body.bench, body.diary_no, body.diary_year)


@router.post("/cestat/orders/by-member",
             summary="Orders (daily+final merged) by Bench + Member + From/To Date")
async def co_by_member(body: _CoMember):
    return await asyncio.to_thread(
        cestat_orders.by_member, body.bench, body.member_name, body.from_date, body.to_date)


@router.post("/cestat/orders/by-order-date",
             summary="Orders (daily+final merged) by Bench + From/To Date")
async def co_by_order_date(body: _CoOrderDate):
    return await asyncio.to_thread(
        cestat_orders.by_order_date, body.bench, body.from_date, body.to_date)


@router.post("/cestat/orders/by-party-name",
             summary="Orders (daily+final merged) by Bench + Party Name")
async def co_by_party(body: _CoParty):
    return await asyncio.to_thread(
        cestat_orders.by_party_name, body.bench, body.party_name)


@router.post("/cestat/orders/by-brief-description",
             summary="Orders (daily+final merged) by Bench + Brief Description + From/To Date")
async def co_by_desc(body: _CoDesc):
    return await asyncio.to_thread(
        cestat_orders.by_brief_description, body.bench, body.description, body.from_date, body.to_date)


@router.get("/cestat/orders/options",
            summary="Dropdown choices: Bench, Case Type, Member")
async def co_options():
    return await asyncio.to_thread(cestat_orders.list_options)
