"""AIROnline provider — citation lookup over aol1.aironline.in.

The dropdown VALUES are not fetched at request time: every publication / year /
segment / judicial body / volume combination is already in
`data/aironline_dropdowns.json`, built once by `build_mapping.py`. Callers read
that file to populate a form.

What still has to be live is the last level and the record itself. Page numbers
are the individual citations — hundreds of thousands of rows that change as
AIROnline publishes — so `lookup_citation` resolves the chosen page number
against the site, then fetches and parses the record (citation, court, judges,
parties, case number, decision date) exactly as the site's own page renders it.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.tribunals.aironline import client as C
from app.tribunals.aironline.parse import parse_records

log = logging.getLogger("app.tribunals.aironline")


def _err(msg: str, **extra: Any) -> dict:
    out: dict[str, Any] = {"success": False, "error": msg}
    out.update(extra)
    return out


def ping() -> dict:
    """Upstream reachability, so an outage there is distinguishable from a bug
    here."""
    try:
        s = C.make_session()
        try:
            r = s.get(C.REFERER, timeout=C.TIMEOUT)
        finally:
            s.close()
        return {"success": True, "upstream": C.BASE_URL,
                "status_code": r.status_code, "reachable": r.status_code < 500}
    except Exception as e:  # noqa: BLE001
        log.warning("aironline ping failed: %r", e)
        return _err(f"upstream unreachable: {e!r}", upstream=C.BASE_URL)


# ── page numbers (level 6, live) ─────────────────────────────────────────────

def page_numbers(publication: str, year: Optional[str], segment: str,
                 judicial_body: str, volume: Optional[str] = None) -> dict:
    """The citations available for a fully-narrowed selection.

    Each row carries the page number, the Solr document id, and the formatted
    citation text — the two values `lookup_citation` needs.
    """
    filters = _filters(publication, year, segment, judicial_body, volume)
    rows = C.fetch_facets(C.FIELD_PAGE, filters, publication_number_flag=True)
    out = []
    for r in rows:
        out.append({
            "page": r.get("id"),
            "doc_id": r.get("value"),
            "citation": r.get("filterParamValue2"),
            "count": r.get("count"),
        })
    # Numeric-aware ordering; page ids are strings and may not be pure digits.
    out.sort(key=lambda o: (0, int(o["page"])) if str(o["page"]).isdigit()
             else (1, 0))
    return {
        "success": True,
        "selection": {"publication": publication, "year": year,
                      "segment": segment, "judicial_body": judicial_body,
                      "volume": volume},
        "count": len(out),
        "page_numbers": out,
    }


def _filters(publication: str, year: Optional[str], segment: Optional[str],
             judicial_body: Optional[str], volume: Optional[str],
             page: Optional[str] = None) -> list[dict]:
    """Build the EQUALTO filter chain the site sends.

    An EMPTY VOLUME MUST NOT BE SENT AS A FILTER. When a selection has no
    volumes the site hides the Volume dropdown and omits the field from the
    request entirely — it does NOT send volume="". Sending the empty filter
    matches nothing, so the search returns 0 results for selections that
    actually have judgments:

        AIR Online / 2015 / Full Report / MAD
            volume=""   -> 0 pages
            volume=None -> 12 pages   (the real answer)

    That mistake silently reported ~11,700 selections as empty when they were
    not. Empty string and None therefore mean the same thing for volume: no
    volume was chosen, so send no volume filter.

    Segment is different — "" IS a real, selectable segment value upstream, so
    it is still sent.
    """
    f = [C.eq_filter(C.FIELD_PUBLICATION, publication)]
    if year is not None:
        f.append(C.eq_filter(C.FIELD_YEAR, year))
    if segment is not None:
        f.append(C.eq_filter(C.FIELD_SEGMENT, segment))
    if judicial_body is not None:
        f.append(C.eq_filter(C.FIELD_JUDICIAL_BODY, judicial_body))
    if volume:  # not None AND not "" — see above
        f.append(C.eq_filter(C.FIELD_VOLUME, volume))
    if page is not None:
        # The content request sends the page under SEGMENT_NUMBER, not
        # PAGE_NUMBER — matching the site's own JS.
        f.append(C.eq_filter(C.FIELD_SEGMENT_NUMBER, page))
    return f


# ── the record (live) ────────────────────────────────────────────────────────

def case_content(publication: str, year: Optional[str], segment: str,
                 judicial_body: str, volume: Optional[str], page: str,
                 doc_id: str, citation_text: str,
                 include_html: bool = False, session=None) -> dict:
    """Fetch and parse one citation's record.

    `session` lets a caller reuse one connection across many fetches. Bulk
    callers should pass a per-thread session: without it every judgment pays a
    fresh TCP connect (~253 ms) plus TLS handshake (~521 ms).
    """
    filters = _filters(publication, year, segment, judicial_body,
                       volume, page)
    html = C.fetch_case_content(filters, doc_id, citation_text, session=session)
    records = parse_records(html)
    out: dict[str, Any] = {
        "success": True,
        "citation": citation_text,
        "doc_id": doc_id,
        "count": len(records),
        "records": records,
    }
    if include_html:
        out["html"] = html
    if not records:
        out["success"] = False
        out["error"] = ("upstream returned no parsable record for this "
                        "citation (it may not exist for this combination)")
    return out


def lookup_citation(publication: str, year: Optional[str], segment: str,
                    judicial_body: str, page: str,
                    volume: Optional[str] = None,
                    include_html: bool = False) -> dict:
    """End-to-end: dropdown values in, parsed record out.

    The page number has to be resolved to a document id before the record can
    be fetched — that id is required by the content endpoint and is NOT
    derivable from the citation (2021 (3) AJR 1 -> J_CD_20213AJR1, but
    2020 (1) SCC 1 -> J_Online_AIROnline2019SC1420). So this asks the site for
    the page list, finds the chosen page, then fetches the record.

    When the page is not there, the error carries the pages that ARE available,
    so a caller never has to guess.
    """
    want = str(page)
    pn = page_numbers(publication, year, segment, judicial_body, volume)
    hit = next((r for r in pn["page_numbers"] if str(r["page"]) == want), None)
    if hit is None:
        return _err(
            f"page number {page!r} is not available for this selection",
            selection=pn["selection"],
            available_count=pn["count"],
            available_sample=[r["page"] for r in pn["page_numbers"][:25]],
        )
    return case_content(publication, year, segment, judicial_body, volume,
                        want, hit["doc_id"], hit["citation"] or "",
                        include_html=include_html)
