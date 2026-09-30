"""fetch_itat_casestatus.py — ITAT (Income Tax Appellate Tribunal) case details.

Target: https://itat.gov.in/judicial/casedetails  (== /judicial/casestatus; the
same page hosts FOUR independent search forms / methods, each its own tab):

  f1  by-appeal-number         Bench + Appeal Type + Appeal Number + Filing Year
  f2  by-date-of-filing        Bench + Appeal Type + Date of Filing        (date)
  f3  by-assessee-name         Bench + Appeal Type + Assessee Name    (many rows)
  f4  by-acknowledgement-number Bench + Appeal Type + Acknowledgement Number

The user picks WHICH method to search by; each is exposed as its own REST route.
Assessee-name and date-of-filing searches typically return MANY cases — all rows
are parsed and returned.

FULLY SELF-CONTAINED / DECOUPLED: this + `itat_captcha.py` are the only ITAT
files. Provider surface consumed by the shared case-details REST API:

    SEARCH_METHODS                     -> the 4 methods (id, label, fields)
    list_options()                     -> Bench / Appeal Type / Filing Year choices
    search(method, **query)            -> {found, count, cases, method, query}
    # convenience wrappers:
    by_appeal_number / by_date_of_filing / by_assessee_name /
    by_acknowledgement_number

Protocol (reverse-engineered):
  1. GET the page — seed ci_session; read CSRF (#csrftkn1) and the Bench /
     Appeal-Type / Filing-Year <select>s.
  2. Captcha — itat_captcha.solve() (audio leak + OCR/oracle retry); returns an
     oracle-confirmed value; the oracle does not rotate the captcha.
  3. Submit form fN — POST the page with that form's fields + cN/btN + captcha.
  4. Parse the results table (Appeal No / Assessment Year / Status / Parties /
     Bench / Next Hearing / More-Details link). Multiple rows supported.

Run:
    python fetch_itat_casestatus.py --method by-assessee-name --bench 199 \
        --type ITA --assessee-name "M/S LML"
    python fetch_itat_casestatus.py --list-benches
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Dict, List, Optional

try:
    from curl_cffi import requests as _rq
    _HAS_CFFI = True
except Exception:                                    # pragma: no cover
    import requests as _rq
    _HAS_CFFI = False

import urllib3
urllib3.disable_warnings()

try:
    from . import itat_captcha, itat_pdf_cache   # package import
except Exception:                        # pragma: no cover - direct-run
    import itat_captcha, itat_pdf_cache

_BASE = "https://itat.gov.in"
# The search forms live on /judicial/casedetails and /judicial/casestatus (same
# page), but the form action is empty (posts to the CURRENT url) and ONLY the
# /casestatus handler returns the results table — so search + submit both here.
_PAGE = f"{_BASE}/judicial/casestatus"

_IMP = ["chrome131", "chrome124", "chrome120", "chrome110", "safari17_0"]

COURT = "itat"
COURT_NAME = "Income Tax Appellate Tribunal"

# The four search methods. `form` is the site's form index (1-4); `field` is the
# site's input name for that method's distinctive field; `fields` describes the
# API inputs (Bench + Appeal Type are common to all four).
_BENCH = {"name": "bench", "label": "Bench", "type": "select", "required": True}
_ATYPE = {"name": "appeal_type", "label": "Appeal Type", "type": "select", "required": True}

SEARCH_METHODS = [
    {
        "id": "by-appeal-number",
        "label": "By Appeal Number",
        "form": 1,
        "fields": [_BENCH, _ATYPE,
                   {"name": "appeal_number", "label": "Appeal Number", "type": "text", "required": True},
                   {"name": "filing_year", "label": "Filing Year", "type": "select", "required": True}],
        # site field name -> our field name
        "map": {"app_number": "appeal_number", "app_year_1": "filing_year"},
    },
    {
        "id": "by-date-of-filing",
        "label": "By Date of Filing",
        "form": 2,
        "fields": [_BENCH, _ATYPE,
                   {"name": "date_of_filing", "label": "Date of Filing", "type": "date",
                    "required": True, "format": "dd/mm/yyyy"}],
        "map": {"filed_on": "date_of_filing"},
        "list_mode": True,      # returns MANY cases across pages -> summary + detail link
    },
    {
        "id": "by-assessee-name",
        "label": "By Assessee Name",
        "form": 3,
        "fields": [_BENCH, _ATYPE,
                   {"name": "assessee_name", "label": "Assessee Name", "type": "text", "required": True}],
        "map": {"assessee_name": "assessee_name"},
        "list_mode": True,      # returns MANY cases across pages -> summary + detail link
    },
    {
        "id": "by-acknowledgement-number",
        "label": "By Acknowledgement Number",
        "form": 4,
        "fields": [_BENCH, _ATYPE,
                   {"name": "acknowledgement_number", "label": "Acknowledgement Number",
                    "type": "text", "required": True}],
        "map": {"ack_number": "acknowledgement_number"},
    },
]
_METHOD_BY_ID = {m["id"]: m for m in SEARCH_METHODS}


def _new_session():
    import random
    s = _rq.Session(impersonate=random.choice(_IMP)) if _HAS_CFFI else _rq.Session()
    s.verify = False
    return s


def _to_ddmmyyyy(d: str) -> str:
    """Normalise a date to DD/MM/YYYY (the site's `filed_on` format).
    Accepts dd/mm/yyyy, dd-mm-yyyy, yyyy-mm-dd."""
    d = str(d).strip()
    if re.match(r"^\d{2}/\d{2}/\d{4}$", d):
        return d
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", d)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", d)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return d


# ---------------------------------------------------------------------------
# Page parse: CSRF + dropdown options
# ---------------------------------------------------------------------------

def _csrf(html: str) -> str:
    m = re.search(r'id=["\']?csrftkn1["\']?[^>]*value=["\']([^"\']*)["\']', html) \
        or re.search(r'value=["\']([^"\']*)["\'][^>]*id=["\']?csrftkn1', html)
    return m.group(1) if m else ""


def _options(html: str, select_id: str) -> List[Dict[str, str]]:
    sm = re.search(rf'<select[^>]*id=["\']?{select_id}["\']?[^>]*>(.*?)</select>',
                   html, re.S)
    if not sm:
        return []
    out = []
    for v, t in re.findall(r'<option[^>]*value=["\']?([^"\'>]*)["\']?[^>]*>(.*?)</option>',
                           sm.group(1), re.S):
        if not v:
            continue
        label = re.sub(r"\s+", " ", re.sub(r"&amp;", "&", re.sub(r"<[^>]+>", "", t))).strip()
        out.append({"value": v, "label": label})
    return out


def list_options(session=None) -> Dict[str, List[Dict[str, str]]]:
    """Dropdown choices shared by every method: Bench, Appeal Type, Filing Year."""
    s = session or _new_session()
    try:
        html = s.get(_PAGE, timeout=40).text
        return {
            "bench": _options(html, "bench_name_1"),
            "appeal_type": _options(html, "app_type_1"),
            "filing_year": _options(html, "app_year_1"),
        }
    finally:
        if session is None:
            try:
                s.close()
            except Exception:
                pass


def list_methods() -> List[Dict]:
    """The four search methods (id, label, fields) for the REST API."""
    return [{"id": m["id"], "label": m["label"], "fields": m["fields"]}
            for m in SEARCH_METHODS]


# ---------------------------------------------------------------------------
# Result parse
# ---------------------------------------------------------------------------
#
# The search-results page is only a stepping stone: each result row links to the
# FULL case-details page (casedetails?cid=<token>), which needs NO captcha. We
# therefore DON'T parse the summary columns — we grab the details link(s) and
# fetch each full details page (in parallel), returning that rich data.

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip().strip(",").strip()


def _details_links(html: str) -> List[str]:
    """Return the casedetails?cid=... links from the search results (0..many)."""
    seen, out = set(), []
    for m in re.finditer(r'href=["\']([^"\']*casedetails\?cid=[^"\']+)["\']', html):
        u = m.group(1)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _page_tokens(html: str):
    """Pagination state from a results page: (lq, lqc, csrftkn, total_pages).
    lqc is the TOTAL result count; the results table shows 10 per page and the
    'lq' token + a per-response CSRF drive the `btnPage` paginator."""
    lq = re.search(r'name="lq"\s+value="([^"]*)"', html)
    lqc = re.search(r'name="lqc"\s+value="([^"]*)"', html)
    csrf = (re.search(r'id=["\']?csrftkn1["\']?[^>]*value=["\']([^"\']*)["\']', html)
            or re.search(r'name="csrftkn"\s+value="([^"]*)"', html))
    total = 0
    if lqc and lqc.group(1).isdigit():
        n = int(lqc.group(1))
        total = (n + 9) // 10          # 10 results per page
    return (lq.group(1) if lq else None,
            lqc.group(1) if lqc else None,
            csrf.group(1) if csrf else None, total)


def _parse_summary_rows(html: str) -> List[Dict[str, str]]:
    """Parse the results TABLE rows (the list view) — fast, no detail fetch. Each
    row: td0 = Appeal Number [Assessment Year] Status:X | td1 = Appellant VS
    Respondent | td2 = Bench | td3 = Next Hearing | td4 = More Details link."""
    out: List[Dict[str, str]] = []
    for tbl in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S):
        if "appeal number" not in tbl.lower():
            continue
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if len(cells) < 4:
                continue
            c0 = _clean(cells[0])
            am = re.match(r"([A-Z()][A-Z()\s]*\d+/[A-Z]+/\d+)", c0)
            appeal = am.group(1).strip() if am else ""
            if not appeal:
                continue
            ay = re.search(r"\[([^\]]+)\]", c0)
            st = re.search(r"Status:\s*([A-Za-z /]+)", c0)
            parties = _clean(cells[1])
            app, resp = parties, ""
            ps = re.split(r"\bVS\b\.?", parties, flags=re.I, maxsplit=1)
            if len(ps) == 2:
                app, resp = ps[0].strip(" ,."), ps[1].strip(" ,.")
            dm = re.search(r'href=["\']([^"\']+casedetails[^"\']*)["\']', cells[4]) if len(cells) > 4 else None
            out.append({
                "appeal_number": appeal,
                "assessment_year": ay.group(1).strip() if ay else "",
                "case_status": st.group(1).strip() if st else "",
                "appellant": app, "respondent": resp,
                "bench": _clean(cells[2]) if len(cells) > 2 else "",
                "next_hearing": _clean(cells[3]) if len(cells) > 3 else "",
                "details_url": _abs_url(dm.group(1)) if dm else "",
            })
    return out


def _collect_all_summaries(session, first_html: str, max_pages: int = 500) -> List[Dict[str, str]]:
    """Walk EVERY results page (assessee / date paginate — 10/page) and collect
    all SUMMARY rows. Paging needs no captcha; page 1's `lq` token is stable and
    the CSRF reusable, so pages 2..N are fetched IN PARALLEL. A short retry pass
    re-fetches any page that came back empty so the total matches `lqc`."""
    rows = list(_parse_summary_rows(first_html))
    lq, lqc, csrf, total_pages = _page_tokens(first_html)
    if not lq or not lqc or total_pages <= 1:
        return rows
    total_pages = min(total_pages, max_pages)
    cookies = {k: v for k, v in session.cookies.items()}

    def fetch_page(page: int):
        s = _new_session()
        for k, v in cookies.items():
            try:
                s.cookies.set(k, v)
            except Exception:
                pass
        try:
            r = s.post(_PAGE,
                       data={"csrftkn": csrf or "", "hp": "", "lq": lq,
                             "lqc": lqc, "btnPage": str(page)},
                       headers={"Referer": _PAGE,
                                "Content-Type": "application/x-www-form-urlencoded"},
                       timeout=60)
            return page, _parse_summary_rows(r.text)
        except Exception:
            return page, []
        finally:
            try:
                s.close()
            except Exception:
                pass

    from concurrent.futures import ThreadPoolExecutor
    seen = {r["appeal_number"] for r in rows}
    pending = list(range(2, total_pages + 1))
    for attempt in range(2):                  # initial pass + one retry for empties
        if not pending:
            break
        empty: List[int] = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            for page, prows in pool.map(fetch_page, pending):
                if not prows:
                    empty.append(page)
                for r in prows:
                    if r["appeal_number"] not in seen:
                        seen.add(r["appeal_number"])
                        rows.append(r)
        pending = empty
    return rows


def _abs_url(u: str) -> str:
    if u.startswith("http"):
        return u
    return _BASE + (u if u.startswith("/") else "/" + u)


def _field(region: str, label: str) -> str:
    m = re.search(rf'{re.escape(label)}\s*:?\s*(.*?)'
                  rf'(?=\s+[A-Z][A-Za-z ]{{2,}}\s*:|\s+VS\.|$)', region)
    return m.group(1).strip().strip(",").strip() if m else ""


def _text(s: str) -> str:
    """Clean HTML to readable text, keeping line breaks (<br>, block tags -> \\n)."""
    s = re.sub(r"<\s*br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</\s*(p|div|tr|h\d|li)\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"[ \t]*\n[ \t]*", "\n", s)      # trim spaces around newlines
    s = re.sub(r"\n(?:\s*\n)+", "\n\n", s)      # collapse blank lines
    return s.strip()


def _parse_tribunal_orders(html: str, *, with_token: bool = False) -> List[Dict[str, str]]:
    """The 'Tribunal Orders' table — GENERIC: zero, one, or many order rows. Each
    row: Order Type | Date of Order | Pronounced On | Result | Order Link |
    Status/Remarks. 'View Order' has a raw viewOrder token; rows marked
    'Not Uploaded' have none. With with_token=True the raw token is kept under
    '_token' (internal, so the caller can fetch the PDF in a valid session)."""
    orders: List[Dict[str, str]] = []
    for tbl in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S):
        low = tbl.lower()
        if "order link" not in low and "date of order" not in low:
            continue
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if len(cells) < 4:          # skip the header (uses <th>) / spacers
                continue
            tok = re.search(r"viewOrder\('([^']+)'", row)
            o = {
                "order_type": _clean(cells[0]),
                "date_of_order": _clean(cells[1]),
                "pronounced_on": _clean(cells[2]) if len(cells) > 2 else "",
                "result": _clean(cells[3]) if len(cells) > 3 else "",
                "pdf_url": "",     # filled with OUR proxy link in search(); "" if Not Uploaded
                "status_remarks": _clean(cells[5]) if len(cells) > 5 else "",
            }
            if with_token:
                o["_token"] = tok.group(1) if tok else ""
            else:
                o["_has_pdf"] = bool(tok)
            orders.append(o)
    return orders


def _scrub(text: str) -> str:
    """Strip junk that can leak from the page chrome (modal button, CSS rules,
    inline script) so only the human-readable proceedings text remains."""
    # cut at the first sign of leaked CSS / JS / modal chrome
    for marker in (r"View Order", r"✖", r"/\*", r"\.custom-accordion",
                   r"\bvar \b", r"\bfunction\b", r"document\.", r"<style", r"{"):
        m = re.search(marker, text)
        if m:
            text = text[:m.start()]
    return _text(text)


def _lines(text: str) -> List[str]:
    """Split cleaned text into a list of non-empty lines (no '\\n' chars) so the
    UI can render each line as its own item."""
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def _parse_order_sheets(html: str) -> List[Dict]:
    """The 'Order Sheets' accordion — GENERIC: zero, one, or many dated sheets.
    Each accordion item = a hearing date + the proceedings text (which lives in a
    <table> inside the accordion body). Returns clean [{date, content}, ...] with
    NO HTML tags / CSS / script; `content` is a LIST OF LINES (not a '\\n' string)
    so each line can be displayed separately in the UI."""
    sheets: List[Dict] = []
    i = html.find("Order Sheets")
    if i < 0:
        return sheets
    block = html[i:]
    end = re.search(r"(<script|<footer|</main|</body)", block)
    if end:
        block = block[:end.start()]
    items = re.split(r'<div class="accordion-item[^"]*"', block)[1:]
    for it in items:
        btn = re.search(r"<button[^>]*>(.*?)</button>", it, re.S)
        date = _clean(btn.group(1)) if btn else ""
        # the proceedings text is inside the <table> in the accordion body —
        # extract just that (avoids the trailing modal/CSS chrome entirely).
        tbl = re.search(r"<table[^>]*>(.*?)</table>", it, re.S)
        if tbl:
            content = _text(tbl.group(1))
        else:
            body = re.search(r'class="accordion-body[^"]*"[^>]*>(.*)', it, re.S)
            content = _scrub(body.group(1)) if body else ""
        content = _scrub(content)
        lines = _lines(content)
        if date or lines:
            sheets.append({"date": date, "content": lines})
    return sheets


def fetch_full_details(details_url: str, *, session=None, cookies=None) -> Dict:
    """GET the full case-details page (NO captcha) and parse everything:
    all case fields + the order sheet (0..many) with each order's PDF link.

    The casedetails?cid=<token> link is bound to the SEARCH session's cookies —
    pass either that `session` or its `cookies` (so parallel workers can share
    the session context without sharing one non-thread-safe session object)."""
    s = session or _new_session()
    if cookies and session is None:
        for k, v in cookies.items():
            try:
                s.cookies.set(k, v)
            except Exception:
                pass
    url = _abs_url(details_url)
    try:
        h = s.get(url, headers={"Referer": _PAGE}, timeout=60).text
        lo, hi = h.find("Appeal Number"), h.find("Short Summary")
        region = _clean(h[lo:hi]) if lo >= 0 and hi > lo else _clean(h)
        det: Dict = {
            "appeal_number": _field(region, "Appeal Number"),
            "filed_on": _field(region, "Filed On"),
            "assessment_year": _field(region, "Assessment Year"),
            "bench_alloted": _field(region, "Bench Alloted"),
            "case_status": "", "appellant": "", "respondent": "",
        }
        # everything after "Case Status:" is  <status> <appellant> VS. <respondent>
        after = region.split("Case Status:", 1)[1].strip() if "Case Status:" in region else ""
        mm = re.match(r"(\S+)\s+(.*?)\s+VS\.?\s+(.*)$", after)
        if mm:
            det["case_status"], det["appellant"], det["respondent"] = \
                mm.group(1), mm.group(2).strip(), mm.group(3).strip()
        elif after:
            det["case_status"] = after
        # hearing dates — a date value on pending cases, blank on disposed. Match
        # only a dd-Mon-yyyy / dd-mm-yyyy token right after the label (else "").
        def _date_after(label: str) -> str:
            m = re.search(rf'{re.escape(label)}\s*:?\s*'
                          rf'(\d{{1,2}}[-/][A-Za-z0-9]+[-/]\d{{4}})', _clean(h))
            return m.group(1) if m else ""
        det["date_of_last_hearing"] = _date_after("Date of Last Hearing")
        det["date_of_next_hearing"] = _date_after("Date of Next Hearing")
        # short summary text (if any)
        ss = re.search(r"Short Summary(.*?)(?:Date of Last Hearing|Tribunal Orders|Order Sheets)",
                       h, re.S)
        det["short_summary"] = _text(ss.group(1)) if ss else ""
        # the two distinct sections shown in the UI:
        det["tribunal_orders"] = _parse_tribunal_orders(h)   # orders table + PDF links
        det["order_sheets"] = _parse_order_sheets(h)         # dated proceedings + content
        det["details_url"] = url
        return det
    except Exception as e:
        return {"details_url": url, "error": f"details_fetch_failed: {e}",
                "order_sheet": []}
    finally:
        if session is None:
            try:
                s.close()
            except Exception:
                pass


def _fetch_all_details(urls: List[str], cookies: Dict, workers: int = 8) -> List[Dict]:
    """Fetch many detail pages in parallel (no captcha). Each worker uses its own
    session seeded with the search session's cookies (the cid link is session-
    bound, and curl_cffi sessions are not thread-safe)."""
    if not urls:
        return []
    if len(urls) == 1:
        return [fetch_full_details(urls[0], cookies=cookies)]
    from concurrent.futures import ThreadPoolExecutor
    out: List[Optional[Dict]] = [None] * len(urls)
    with ThreadPoolExecutor(max_workers=min(workers, len(urls))) as pool:
        results = pool.map(lambda u: fetch_full_details(u, cookies=cookies), urls)
        for i, res in enumerate(results):
            out[i] = res
    return [d for d in out if d]


# ---------------------------------------------------------------------------
# Core search — dispatch by method
# ---------------------------------------------------------------------------

def search(method: str, *, session=None, **query) -> Dict:
    """Run one of the four searches. `query` uses the API field names declared in
    that method's `fields` (bench, appeal_type + the method-specific field(s)).
    Returns {found, count, cases:[...], method, query}."""
    m = _METHOD_BY_ID.get(method)
    if not m:
        return {"found": False, "count": 0, "cases": [], "method": method,
                "query": query, "error": f"unknown method '{method}'"}
    n = m["form"]
    s = session or _new_session()
    try:
        html = s.get(_PAGE, timeout=40).text
        csrf = _csrf(html)
        cap = itat_captcha.solve(s, csrf)
        if not cap:
            return {"found": False, "count": 0, "cases": [], "method": method,
                    "query": query, "error": "captcha_unsolved"}
        payload = {
            "csrftkn": csrf, "hp": "", "captcha": cap,
            f"c{n}": cap, f"bt{n}": "true",
            f"bench_name_{n}": str(query.get("bench", "")),
            f"app_type_{n}": str(query.get("appeal_type", "")),
        }
        # method-specific site fields (normalise the date field to DD/MM/YYYY)
        for site_field, our_field in m["map"].items():
            val = str(query.get(our_field, ""))
            if site_field == "filed_on":
                val = _to_ddmmyyyy(val)
            payload[site_field] = val
        r = s.post(_PAGE, data=payload,
                   headers={"Referer": _PAGE,
                            "Content-Type": "application/x-www-form-urlencoded"},
                   timeout=60)
        b = query.get("bench", "")
        if m.get("list_mode"):
            # LIST methods (assessee / date): return ALL cases across ALL pages
            # with their summary fields + a detail link. Full details/order-sheets
            # are fetched on demand via by-appeal-number (fast; avoids hundreds of
            # detail-page fetches in one call).
            rows = _collect_all_summaries(s, r.text)
            for c in rows:
                parts = _appeal_parts(c.get("appeal_number", ""))
                if parts:
                    c["detail_query"] = {
                        "bench": b, "appeal_type": parts["type"],
                        "appeal_number": parts["number"], "filing_year": parts["year"]}
                    c["detail_link"] = (
                        f"/itat/by-appeal-number  (POST "
                        f'{{"bench":"{b}","appeal_type":"{parts["type"]}",'
                        f'"appeal_number":"{parts["number"]}","filing_year":"{parts["year"]}"}})')
            return {"found": bool(rows), "count": len(rows),
                    "cases": rows, "method": method, "query": query}

        # SINGLE-result methods (appeal-number / ack-number): full case details
        # (all fields + order sheets + working PDF links) from the details page.
        links = _details_links(r.text)
        cookies = {k: v for k, v in s.cookies.items()}
        cases = _fetch_all_details(links, cookies, workers=8)
        for c in cases:
            parts = _appeal_parts(c.get("appeal_number", ""))
            atype = (parts or {}).get("type") or query.get("appeal_type", "")
            num = (parts or {}).get("number", "")
            yr = (parts or {}).get("year", "")
            for idx, o in enumerate(c.get("tribunal_orders", [])):
                if o.pop("_has_pdf", False) and num and yr:
                    o["pdf_url"] = (f"/itat/case-details/order-pdf?bench={b}&appeal_type={atype}"
                                    f"&appeal_number={num}&filing_year={yr}"
                                    f"&order_index={idx}")
        return {"found": bool(cases), "count": len(cases),
                "cases": cases, "method": method, "query": query}
    finally:
        if session is None:
            try:
                s.close()
            except Exception:
                pass


# convenience wrappers (also handy from the CLI / other code)
def by_appeal_number(bench, appeal_type, appeal_number, filing_year, **kw):
    return search("by-appeal-number", bench=bench, appeal_type=appeal_type,
                  appeal_number=appeal_number, filing_year=filing_year, **kw)


def by_date_of_filing(bench, appeal_type, date_of_filing, **kw):
    return search("by-date-of-filing", bench=bench, appeal_type=appeal_type,
                  date_of_filing=date_of_filing, **kw)


def by_assessee_name(bench, appeal_type, assessee_name, **kw):
    return search("by-assessee-name", bench=bench, appeal_type=appeal_type,
                  assessee_name=assessee_name, **kw)


def by_acknowledgement_number(bench, appeal_type, acknowledgement_number, **kw):
    return search("by-acknowledgement-number", bench=bench, appeal_type=appeal_type,
                  acknowledgement_number=acknowledgement_number, **kw)


# ---------------------------------------------------------------------------
# Order-PDF download (server-side, so the link works from anywhere)
# ---------------------------------------------------------------------------

def _appeal_parts(appeal_number: str) -> Optional[Dict[str, str]]:
    """'ITA 1/MUM/2024' -> {type:'ITA', number:'1', bench_abbr:'MUM', year:'2024'}."""
    m = re.match(r"\s*([A-Z()][A-Z()]*)\s*(\d+)\s*/\s*([A-Za-z]+)\s*/\s*(\d{4})",
                 appeal_number or "")
    if not m:
        return None
    return {"type": m.group(1), "number": m.group(2),
            "bench_abbr": m.group(3), "year": m.group(4)}


def download_order_pdf(bench: str, appeal_type: str, appeal_number: str,
                       filing_year: str, order_index: int = 0):
    """Fetch ONE Tribunal-Order PDF end-to-end in a single valid session
    (search -> details -> viewOrder), so the token is valid. Returns
    (filename, pdf_bytes) or None. This is what the case-details order-pdf route
    serves — the viewOrder link is session-bound, so it must be fetched
    server-side. Cached: an order fetched once is served instantly thereafter."""
    key = ("cd", str(bench), str(appeal_type), str(appeal_number),
           str(filing_year), int(order_index))
    hit = itat_pdf_cache.get(key)
    if hit:
        return hit
    s = _new_session()
    try:
        html = s.get(_PAGE, timeout=40).text
        csrf = _csrf(html)
        cap = itat_captcha.solve(s, csrf)
        if not cap:
            return None
        payload = {
            "csrftkn": csrf, "hp": "", "captcha": cap, "c1": cap, "bt1": "true",
            "bench_name_1": str(bench), "app_type_1": str(appeal_type),
            "app_number": str(appeal_number), "app_year_1": str(filing_year),
        }
        r = s.post(_PAGE, data=payload,
                   headers={"Referer": _PAGE,
                            "Content-Type": "application/x-www-form-urlencoded"},
                   timeout=60)
        links = _details_links(r.text)
        if not links:
            return None
        dh = s.get(_abs_url(links[0]), headers={"Referer": _PAGE}, timeout=60)
        orders = _parse_tribunal_orders(dh.text, with_token=True)
        if order_index < 0 or order_index >= len(orders):
            return None
        tok = orders[order_index].get("_token")
        if not tok:
            return None                      # this order is 'Not Uploaded'
        pdf = s.get(f"{_BASE}/judicial/viewOrder?data={tok}",
                    headers={"Referer": _abs_url(links[0])}, timeout=90).content
        if pdf[:3] == b"\xef\xbb\xbf":
            pdf = pdf[3:]
        if pdf[:4] != b"%PDF":
            return None
        fname = (f"{appeal_type}_{appeal_number}_{filing_year}"
                 f"_order{order_index + 1}.pdf").replace("/", "-")
        itat_pdf_cache.put(key, (fname, pdf))
        return fname, pdf
    except Exception:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="ITAT case-details lookup (4 methods)")
    p.add_argument("--method", choices=[m["id"] for m in SEARCH_METHODS],
                   default="by-appeal-number")
    p.add_argument("--bench"); p.add_argument("--type", dest="appeal_type")
    p.add_argument("--number", dest="appeal_number"); p.add_argument("--year", dest="filing_year")
    p.add_argument("--date", dest="date_of_filing")
    p.add_argument("--assessee-name", dest="assessee_name")
    p.add_argument("--ack", dest="acknowledgement_number")
    p.add_argument("--list-benches", action="store_true")
    p.add_argument("--list-types", action="store_true")
    p.add_argument("--list-methods", action="store_true")
    args = p.parse_args()

    if args.list_methods:
        print(json.dumps(list_methods(), indent=2)); return
    if args.list_benches or args.list_types:
        opts = list_options()
        if args.list_benches:
            print("BENCHES:")
            for o in opts["bench"]:
                print(f"  {o['value']:>4}  {o['label']}")
        if args.list_types:
            print("APPEAL TYPES:")
            for o in opts["appeal_type"]:
                print(f"  {o['value']:<10} {o['label']}")
        return

    q = {k: v for k, v in {
        "bench": args.bench, "appeal_type": args.appeal_type,
        "appeal_number": args.appeal_number, "filing_year": args.filing_year,
        "date_of_filing": args.date_of_filing, "assessee_name": args.assessee_name,
        "acknowledgement_number": args.acknowledgement_number}.items() if v}
    print(json.dumps(search(args.method, **q), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
