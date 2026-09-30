"""AIROnline citation lookup (aol1.aironline.in).

ONE endpoint: `GET /aironline/citation`.

The dropdown values are NOT served over HTTP — they live in a plain file,
`app/tribunals/aironline/data/aironline_dropdowns.json`, holding every
publication / year / segment / judicial body / volume combination. Read it
directly to populate a form; there is no request to make for it.

The one thing that file does NOT carry is the Publication Number (page): those
are ~120k separate lists that change as AIROnline publishes. This endpoint
resolves the chosen page against the site, then returns the record — citation,
court, judge(s), petitioner v. respondent, case number and decision date. If
the page does not exist for that selection, the error lists the ones that do.

Blocking work runs in the threadpool via `asyncio.to_thread`, matching the
SAT / SCI routers.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Query

from app.routers.common import json_response
from app.tribunals.aironline import fetch_aironline as air

router = APIRouter(tags=["AIROnline"])


@router.get("/aironline/citation",
            summary="Look up a citation — court, judges, parties, case no, date",
            description=(
                "Give it the dropdown values and it returns the record exactly "
                "as AIROnline renders it.\n\n"
                "Pick the values from "
                "`app/tribunals/aironline/data/aironline_dropdowns.json` — that "
                "file holds every valid combination (351 publications, and for "
                "each one its years, segments, judicial bodies and volumes), so "
                "no request is needed to build the form.\n\n"
                "`volume` is optional: the file's `volume_count` says how many "
                "volumes that combination has — 0 means the site shows no "
                "Volume field at all (a 5-field form rather than a 6-field "
                "one). `year` is optional for the one publication whose year "
                "field the site hides.\n\n"
                "Pass `include_html=true` to also get the raw fragment."))
async def aironline_citation(
    publication: str = Query(..., examples=["AIR JHARKHAND HIGH COURT REPORTS"]),
    segment: str = Query(..., examples=["Full Report"]),
    judicial_body: str = Query(..., examples=["JHA"]),
    page: str = Query(..., description="Publication Number (page)", examples=["1"]),
    year: Optional[str] = Query(None, examples=["2021"]),
    volume: Optional[str] = Query(None, examples=["3"]),
    include_html: bool = Query(False, description="also return the raw HTML fragment"),
):
    try:
        res = await asyncio.to_thread(air.lookup_citation, publication, year,
                                      segment, judicial_body, page, volume,
                                      include_html)
        return json_response(res, 200 if res.get("success") else 404)
    except Exception as e:  # noqa: BLE001
        return json_response({"success": False,
                              "error": f"upstream request failed: {e!r}"}, 502)
