"""SCI (Supreme Court of India) routes — https://www.sci.gov.in

Three families, mirroring the court's own site:

  Case status (6 searches)  — by Diary No / Case No / CNR / AOR code /
     Party name / Court. Each result is the case list, and every case is
     enriched with the full "View" details (parties, case no, CNR, listing,
     status, tagged with any linked documents).
  Daily orders (4 searches) — by Diary No / Case No / RoP date / free text.
  Judgements (5 searches)   — by Diary No / Case No / Judge / Judgement date /
     free text.

Orders & judgements return a DIRECT, session-free PDF URL per row (hosted on
api.sci.gov.in) — click `pdf_url` and it opens; no resolver needed.

──────────────────────────────────────────────────────────────────────────────
HOW THE CAPTCHA IS HANDLED  (every response also carries a `captcha` field)
──────────────────────────────────────────────────────────────────────────────
  • captcha BYPASSED — no captcha at all. Used when the query key already maps to
    a case: Diary-No and CNR (CNR decodes to diary+year), and *by-diary* orders /
    judgements (read from the case's captcha-free `judgement_orders` tab). These
    are the fast path (~0.1–0.2 s, one GET).
  • captcha solved by DDDDOCR — the search is captcha-protected, so we OCR the
    securimage math-equation image (ddddocr) → evaluate → retry-until-valid.
    (~0.5 s/solve, ~90 %+ first try. We do NOT use the audio captcha: its clip is
    ~82× larger to download than the 1.8 KB image, so it would be slower.)
  There is no token/no-OCR leak (the server validates the answer and never leaks
  it), and no free validation oracle — so DDDDOCR is the fastest available solve.

All handlers are async: the blocking scrape runs in a worker thread via
asyncio.to_thread, and ddddocr is guarded by a lock, so many users can hit these
endpoints at once without blocking the event loop.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.tribunals.sci import fetch_sci as sci
from app.tribunals.sci import sci_captcha

router = APIRouter(tags=["SCI (Supreme Court)"])

# Reusable captcha-method notes appended to endpoint descriptions.
_BYPASS = ("**Captcha: BYPASSED** (no captcha — the key maps straight to the "
           "case via the captcha-free get_case_details).")
_DDDDOCR = ("**Captcha: solved by DDDDOCR** (securimage math-equation image → "
            "OCR → evaluate → retry-until-valid). Audio captcha is not used "
            "(≈82× larger to download than the image, so slower).")


# ── captcha service + dropdowns ──────────────────────────────────────────────

@router.get("/sci/captcha",
            summary="Solve one SCI captcha  ·  Captcha: DDDDOCR",
            description="The captcha service every captcha'd SCI search uses. "
            "Fetches a live form and " + _DDDDOCR + " Returns the equation + "
            "answer (+ image/audio URLs). No token/no-OCR bypass exists (the "
            "server validates the answer and it isn't leaked).")
async def sci_captcha_demo():
    proxies = sci._pick_proxy() if sci._USE_PROXY else None
    return await asyncio.to_thread(sci_captcha.demo, proxies)


@router.get("/sci/options",
            summary="Dropdown choices (case types, judges, years, party types, statuses)  ·  Captcha: none",
            description="Static/derived dropdown values used to build a search. "
            "No captcha, no search — just the option lists.")
async def sci_options():
    return await asyncio.to_thread(sci.list_options)


@router.get("/sci/case-details/view",
            summary="Full case 'View' details by Diary No + Year  ·  Captcha: BYPASSED",
            description="The details you get after clicking View — parties, case "
            "number, CNR, listing history, status, documents. " + _BYPASS +
            " Fastest way to expand a known case.")
async def sci_case_view(
    diary_no: str = Query("1", description="Diary number, e.g. 1"),
    year: str = Query("2024", description="Diary year, e.g. 2024")):
    return await asyncio.to_thread(sci.case_details, diary_no, year)


# ── CASE STATUS (6 searches) ─────────────────────────────────────────────────

class _CsDiary(BaseModel):
    diary_no: str = Field("1", description="Diary number, e.g. 1")
    year: str = Field("2024", description="Diary year, e.g. 2024")


class _CsCase(BaseModel):
    case_type: str = Field("1", description="Case-type id (1=SLP(C), 2=SLP(Crl), 3=Civil Appeal…; see /sci/options)")
    case_no: str = Field("2767", description="Case number, e.g. 2767")
    year: str = Field("2024", description="Registration year, e.g. 2024")


class _CsCnr(BaseModel):
    cnr_no: str = Field("SCIN010000012024", description="16-char CNR, e.g. SCIN010000012024")


class _CsAor(BaseModel):
    party_type: str = Field("any", description="any / P (Petitioner) / R (Respondent)")
    aor_code: str = Field("1", description="AOR numeric id (see /sci/options — ~3648 advocates)")
    year: str = Field("2023", description="Year, e.g. 2023")
    case_status: str = Field("P", description="P=Pending, D=Disposed")


class _CsParty(BaseModel):
    party_type: str = Field("any", description="any / P / R")
    party_name: str = Field("Union of India", description="Party name, e.g. Union of India")
    year: str = Field("2023", description="Year, e.g. 2023")
    party_status: str = Field("P", description="P=Pending, D=Disposed")


class _CsCourt(BaseModel):
    court: str = Field("1", description="Lower-court type: 1=High Court, 3=District Court, 4=Supreme Court")
    state: str = Field("", description="State code — REQUIRED by this search (dependent dropdown on the site; the server rejects a blank state)")
    bench: str = Field("", description="Bench code (dependent on state)")
    case_type: str = Field("", description="Case-type at that court (optional)")
    case_no: str = Field("", description="Case number at that court (optional)")
    year: str = Field("2023", description="Year, e.g. 2023")
    listing_date: str = Field("", description="Listing date dd-mm-yyyy (required by the form)")


@router.post("/sci/case-details/by-diary-no",
             summary="Case status by Diary No + Year (list + full View details)  ·  Captcha: BYPASSED",
             description="The diary number + year is exactly the key the "
             "captcha-free get_case_details wants, so we skip the search entirely "
             "and fetch full details directly. " + _BYPASS +
             " (Falls back to the captcha'd search only if the direct lookup "
             "finds nothing.)")
async def cs_diary(body: _CsDiary):
    return await asyncio.to_thread(sci.case_status_diary_no, body.diary_no, body.year)


@router.post("/sci/case-details/by-case-no",
             summary="Case status by Case Type + Case No + Year  ·  Captcha: DDDDOCR",
             description="case_no ≠ diary_no, so the search itself is the "
             "case→diary lookup. " + _DDDDOCR)
async def cs_case(body: _CsCase):
    return await asyncio.to_thread(
        sci.case_status_case_no, body.case_type, body.case_no, body.year)


@router.post("/sci/case-details/by-cnr",
             summary="Case status by CNR number  ·  Captcha: BYPASSED",
             description="An SCI CNR encodes the diary number + year "
             "(SCIN01 + diary + year), so we decode it and fetch details "
             "directly. " + _BYPASS + " (Falls back to the captcha'd search for a "
             "non-standard CNR.)")
async def cs_cnr(body: _CsCnr):
    return await asyncio.to_thread(sci.case_status_cnr, body.cnr_no)


@router.post("/sci/case-details/by-aor-code",
             summary="Case status by AOR (Advocate-on-Record) code + Year + status  ·  Captcha: DDDDOCR",
             description="Returns every case for that advocate — only the search "
             "knows them. " + _DDDDOCR)
async def cs_aor(body: _CsAor):
    return await asyncio.to_thread(
        sci.case_status_aor_code, body.party_type, body.aor_code, body.year, body.case_status)


@router.post("/sci/case-details/by-party-name",
             summary="Case status by Party Name + Year + status  ·  Captcha: DDDDOCR",
             description="Resolves a party name to its cases. " + _DDDDOCR)
async def cs_party(body: _CsParty):
    return await asyncio.to_thread(
        sci.case_status_party_name, body.party_type, body.party_name, body.year, body.party_status)


@router.post("/sci/case-details/by-court",
             summary="Case status by originating Court + state/bench + Year  ·  Captcha: DDDDOCR",
             description="Cases that came up from a given lower court. " + _DDDDOCR)
async def cs_court(body: _CsCourt):
    return await asyncio.to_thread(
        sci.case_status_court, body.court, body.state, body.bench,
        body.case_type, body.case_no, body.year, body.listing_date)


# ── DAILY ORDERS (4 searches) ────────────────────────────────────────────────

class _OrdDiary(BaseModel):
    diary_no: str = Field("1", description="Diary number, e.g. 1")
    year: str = Field("2024", description="Diary year, e.g. 2024")


class _OrdCase(BaseModel):
    case_type: str = Field("1", description="Case-type id (see /sci/options)")
    case_no: str = Field("2767", description="Case number, e.g. 2767")
    year: str = Field("2024", description="Year, e.g. 2024")


class _OrdRop(BaseModel):
    from_date: str = Field("01-01-2024", description="From date dd-mm-yyyy")
    to_date: str = Field("10-01-2024", description="To date dd-mm-yyyy (≤ 30 days span)")


class _OrdFreeText(BaseModel):
    search_text: str = Field("bail", description="Free text to match in the order")
    from_date: str = Field("01-01-2024", description="From date dd-mm-yyyy")
    to_date: str = Field("30-01-2024", description="To date dd-mm-yyyy (≤ 30 days span)")


@router.post("/sci/orders/by-diary-no",
             summary="Daily orders by Diary No + Year (direct pdf_url)  ·  Captcha: BYPASSED",
             description="Order PDFs are read straight from the case's "
             "judgement_orders tab (one GET). " + _BYPASS +
             " Each row has a direct session-free pdf_url.")
async def ord_diary(body: _OrdDiary):
    return await asyncio.to_thread(sci.orders_diary_no, body.diary_no, body.year)


@router.post("/sci/orders/by-case-no",
             summary="Daily orders by Case Type + Case No + Year (direct pdf_url)  ·  Captcha: DDDDOCR",
             description=_DDDDOCR)
async def ord_case(body: _OrdCase):
    return await asyncio.to_thread(
        sci.orders_case_no, body.case_type, body.case_no, body.year)


@router.post("/sci/orders/by-rop-date",
             summary="Daily orders by Record-of-Proceedings date range (direct pdf_url)  ·  Captcha: DDDDOCR",
             description=_DDDDOCR)
async def ord_rop(body: _OrdRop):
    return await asyncio.to_thread(sci.orders_rop_date, body.from_date, body.to_date)


@router.post("/sci/orders/free-text",
             summary="Daily orders by free text + date range (direct pdf_url)  ·  Captcha: DDDDOCR",
             description=_DDDDOCR)
async def ord_free(body: _OrdFreeText):
    return await asyncio.to_thread(
        sci.orders_free_text, body.search_text, body.from_date, body.to_date)


# ── JUDGEMENTS (5 searches) ──────────────────────────────────────────────────

class _JgDiary(BaseModel):
    diary_no: str = Field("1", description="Diary number, e.g. 1")
    year: str = Field("2024", description="Diary year, e.g. 2024")


class _JgCase(BaseModel):
    case_type: str = Field("1", description="Case-type id (see /sci/options)")
    case_no: str = Field("2767", description="Case number, e.g. 2767")
    year: str = Field("2024", description="Year, e.g. 2024")


class _JgJudge(BaseModel):
    judge: str = Field("271", description="Judge id (271=CJI…; see /sci/options)")
    from_date: str = Field("01-01-2024", description="From date dd-mm-yyyy")
    to_date: str = Field("30-01-2024", description="To date dd-mm-yyyy")


class _JgDate(BaseModel):
    from_date: str = Field("01-01-2024", description="From date dd-mm-yyyy")
    to_date: str = Field("30-01-2024", description="To date dd-mm-yyyy")


class _JgFreeText(BaseModel):
    search_text: str = Field("bail", description="Free text to match in the judgement")
    from_date: str = Field("01-01-2024", description="From date dd-mm-yyyy")
    to_date: str = Field("30-01-2024", description="To date dd-mm-yyyy (≤ 30 days span)")


@router.post("/sci/judgements/by-diary-no",
             summary="Judgements by Diary No + Year (direct pdf_url)  ·  Captcha: BYPASSED",
             description="Judgement PDFs are read straight from the case's "
             "judgement_orders tab (one GET). " + _BYPASS +
             " Each row has a direct pdf_url.")
async def jg_diary(body: _JgDiary):
    return await asyncio.to_thread(sci.judgements_diary_no, body.diary_no, body.year)


@router.post("/sci/judgements/by-case-no",
             summary="Judgements by Case Type + Case No + Year (direct pdf_url)  ·  Captcha: DDDDOCR",
             description=_DDDDOCR)
async def jg_case(body: _JgCase):
    return await asyncio.to_thread(
        sci.judgements_case_no, body.case_type, body.case_no, body.year)


@router.post("/sci/judgements/by-judge",
             summary="Judgements by Judge + date range (direct pdf_url)  ·  Captcha: DDDDOCR",
             description=_DDDDOCR)
async def jg_judge(body: _JgJudge):
    return await asyncio.to_thread(
        sci.judgements_judge, body.judge, body.from_date, body.to_date)


@router.post("/sci/judgements/by-date",
             summary="Judgements by Judgement date range (direct pdf_url)  ·  Captcha: DDDDOCR",
             description=_DDDDOCR)
async def jg_date(body: _JgDate):
    return await asyncio.to_thread(
        sci.judgements_judgement_date, body.from_date, body.to_date)


@router.post("/sci/judgements/free-text",
             summary="Judgements by free text + date range (direct pdf_url)  ·  Captcha: DDDDOCR",
             description=_DDDDOCR)
async def jg_free(body: _JgFreeText):
    return await asyncio.to_thread(
        sci.judgements_free_text, body.search_text, body.from_date, body.to_date)
