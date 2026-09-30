"""SAT (Securities Appellate Tribunal) provider — satweb.sat.gov.in.

Self-contained scraper for SAT's Case Status and Orders portals.

Captcha bypass
--------------
Both pages show an image captcha, but the search AJAX calls do NOT send the
captcha — the server only validates the CSRF `security_token` + the PHP session
cookie. (The captcha answer is even leaked in a hidden `captcha_word_tab*`
field.) So we just carry the PHPSESSID cookie and the security_token; no OCR.

Flow
----
  GET  /case-status | /orders            -> PHPSESSID cookie + security_token
  POST get-case-status/-al/-partywise    -> case list; each row's View has
                                            data-id = filing_no
  POST get-case-history {filing_no}      -> full case details + order links
  POST get-orders-by-case/-al/-party/-date -> order list; each View link is
                                            view-order/<hash>/<id>
  POST view-order-document {order:<id>}  -> {status, content=<base64 pdf>, token}

Order links (view-order/<hash>/<id>) are SESSION-BOUND: the <hash> is only
valid inside the session that generated it. So we keep the stable numeric <id>
and resolve the PDF on demand via view-order-document (which only needs the id
+ a fresh token).
"""
from __future__ import annotations

import base64
import re

try:
    from curl_cffi import requests as _rq
    _IMP = "chrome"
except Exception:                       # pragma: no cover
    import requests as _rq
    _IMP = None

import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()

BASE = "https://satweb.sat.gov.in"
CASE_PAGE = f"{BASE}/case-status"
ORDERS_PAGE = f"{BASE}/orders"

# Static dropdowns (SAT is a single Mumbai bench; 3 regulator types).
BENCHES = [{"value": "1", "label": "Mumbai"}]
APPEAL_TYPES = [{"value": "1", "label": "SEBI"},
                {"value": "2", "label": "IRDAI"},
                {"value": "3", "label": "PFRDA"}]


# ── session helpers ──────────────────────────────────────────────────────────

def _session():
    return _rq.Session(impersonate=_IMP) if _IMP else _rq.Session()


def _new(page_url: str):
    """Fresh session bound to a page; returns (session, security_token)."""
    s = _session()
    s.verify = False
    html = s.get(page_url, timeout=45).text
    tok = _token(html)
    return s, tok


def _token(html_or_json) -> str:
    if isinstance(html_or_json, dict):
        return html_or_json.get("token") or ""
    m = re.search(r'id="security_token"\s+value="([^"]+)"', html_or_json or "")
    return m.group(1) if m else ""


def _hdr(page_url: str) -> dict:
    return {"Referer": page_url, "Origin": BASE,
            "X-Requested-With": "XMLHttpRequest"}


def _txt(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


# ── options ──────────────────────────────────────────────────────────────────

def list_options() -> dict:
    """Dropdown choices for the SAT search forms."""
    return {
        "bench": BENCHES,
        "appeal_type": APPEAL_TYPES,       # case/appeal type (SEBI/IRDAI/PFRDA)
        "order_date_type": APPEAL_TYPES,   # dtapl_type for the date-range order search
    }


# ── case-status: search + history ────────────────────────────────────────────

def _parse_case_rows(content: str) -> list:
    out = []
    soup = BeautifulSoup(content or "", "html.parser")
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 8:
            continue
        view = tr.find("a", class_="view-case-info")
        parties = _txt(tds[4])
        pet, res = (parties.split(" vs ", 1) + [""])[:2] if " vs " in parties.lower() \
            else (parties, "")
        out.append({
            "sr": _txt(tds[0]),
            "appeal_type": _txt(tds[1]),
            "al_no": _txt(tds[2]),
            "appeal_no": _txt(tds[3]),
            "parties": parties,
            "petitioner": pet.strip(),
            "respondent": res.strip(),
            "date": _txt(tds[5]),
            "status": _txt(tds[6]),
            "filing_no": (view.get("data-id") if view else None),
        })
    return out


def _parse_history(content: str) -> dict:
    """Parse the case-history modal HTML into structured details + order links."""
    soup = BeautifulSoup(content or "", "html.parser")
    detail = {"parties": {}, "counsel": {}, "ma_details": [], "ra_details": [],
              "next_listing": None, "listing_history": [], "orders": []}

    def table_after(label_regex):
        for el in soup.find_all(string=re.compile(label_regex, re.I)):
            tbl = el.find_parent().find_next("table")
            if tbl:
                return tbl
        return None

    def rows_of(tbl):
        rows = []
        if not tbl:
            return rows
        heads = [_txt(th) for th in tbl.select("tr th")]
        for tr in tbl.select("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            vals = [_txt(td) for td in tds]
            rows.append(dict(zip(heads, vals)) if heads and len(heads) == len(vals)
                        else {"cols": vals})
        return rows

    detail["ma_details"] = rows_of(table_after(r"MA Details"))
    detail["ra_details"] = rows_of(table_after(r"RA Details"))

    # Listing history (has the Order "View" links)
    lt = table_after(r"Listing History")
    for tr in (lt.select("tr") if lt else []):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        a = tr.find("a", href=re.compile(r"/view-order/"))
        oid = None
        link = None
        if a:
            link = a.get("href")
            m = re.search(r"/view-order/[a-f0-9]+/(\d+)", link)
            oid = m.group(1) if m else None
        entry = {"listing_date": _txt(tds[0]), "bench": _txt(tds[1]),
                 "presiding_officer": _txt(tds[2]) if len(tds) > 2 else "",
                 "order_id": oid, "order_link": link}
        detail["listing_history"].append(entry)
        if oid:
            detail["orders"].append({"order_id": oid, "order_link": link,
                                     "listing_date": entry["listing_date"]})

    # Parties (best-effort from the first two-column party table)
    pt = table_after(r"^\s*Party") or soup.find("table")
    for tr in (pt.select("tr") if pt else []):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            k = _txt(tds[0]).lower()
            if "applicant" in k or "petitioner" in k:
                detail["parties"]["applicant"] = _txt(tds[1])
            elif "respondent" in k:
                detail["parties"]["respondent"] = _txt(tds[1])
    return detail


def get_case_history(session, page_url, token, filing_no):
    """POST get-case-history for one filing_no; returns (detail, new_token)."""
    r = session.post(f"{BASE}/get-case-history", headers=_hdr(page_url),
                     data={"filing_no": filing_no, "token": token}, timeout=60)
    try:
        j = r.json()
    except Exception:
        return {"error": "bad response"}, token
    return _parse_history(j.get("content", "")), j.get("token", token)


def _search_cases(endpoint: str, data: dict, *, deep: int = 25) -> dict:
    """Run a case-status search, then fetch full history for up to `deep` cases."""
    s, tok = _new(CASE_PAGE)
    payload = dict(data); payload["token"] = tok
    r = s.post(f"{BASE}/{endpoint}", headers=_hdr(CASE_PAGE), data=payload, timeout=90)
    try:
        j = r.json()
    except Exception:
        return {"found": False, "count": 0, "cases": [], "error": "search failed"}
    tok = j.get("token", tok)
    rows = _parse_case_rows(j.get("content", ""))
    for row in rows[:deep]:
        if row.get("filing_no"):
            row["details"], tok = get_case_history(s, CASE_PAGE, tok, row["filing_no"])
    return {"found": bool(rows), "count": len(rows),
            "deep_fetched": min(len(rows), deep), "cases": rows}


def case_by_case_number(case_type, case_no, filing_year, bench="1"):
    return _search_cases("get-case-status", {
        "bench": bench, "case_type": case_type, "case_no": case_no,
        "filing_year": filing_year})


def case_by_appeal_number(al_number, filing_year, bench="1"):
    return _search_cases("get-al-status", {
        "bench": bench, "al_number": al_number, "filing_year": filing_year})


def case_by_party_name(prty_name, filing_year, bench="1"):
    return _search_cases("get-partywise-status", {
        "bench": bench, "prty_name": prty_name, "filing_year": filing_year})


# ── orders: 4 searches ───────────────────────────────────────────────────────

def _parse_order_rows(content: str) -> list:
    out = []
    soup = BeautifulSoup(content or "", "html.parser")
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        a = tr.find("a", href=re.compile(r"/view-order/"))
        oid, link = None, None
        if a:
            link = a.get("href")
            m = re.search(r"/view-order/[a-f0-9]+/(\d+)", link)
            oid = m.group(1) if m else None
        parties = _txt(tds[3])
        out.append({
            "sr": _txt(tds[0]),
            "al_no": _txt(tds[1]),
            "appeal_no": _txt(tds[2]),
            "parties": parties,
            "court": _txt(tds[4]),
            "order_date": _txt(tds[5]),
            "order_id": oid,
            "order_link": link,     # session-bound; resolve via /sat/order_link
        })
    return out


def _search_orders(endpoint: str, data: dict) -> dict:
    s, tok = _new(ORDERS_PAGE)
    payload = dict(data); payload["security_token"] = tok
    r = s.post(f"{BASE}/{endpoint}", headers=_hdr(ORDERS_PAGE), data=payload, timeout=90)
    try:
        j = r.json()
    except Exception:
        return {"found": False, "count": 0, "orders": [], "error": "search failed"}
    rows = _parse_order_rows(j.get("content", ""))
    return {"found": bool(rows), "count": len(rows), "orders": rows}


def orders_by_case_number(case_type, case_no, filing_year, bench="1"):
    return _search_orders("get-orders-by-case", {
        "bench": bench, "case_type": case_type, "case_no": case_no,
        "filing_year": filing_year})


def orders_by_appeal_number(al_number, filing_year, bench="1"):
    return _search_orders("get-orders-by-al", {
        "bench": bench, "al_number": al_number, "filing_year": filing_year})


def orders_by_party_name(prty_name, filing_year, bench="1"):
    return _search_orders("get-orders-by-party", {
        "bench": bench, "prty_name": prty_name, "filing_year": filing_year})


def orders_by_date(apl_type, start_date, end_date):
    """Date-range order search. apl_type: 1=SEBI,2=IRDA,3=PFRDA. Dates dd-mm-yyyy."""
    return _search_orders("get-orders-by-date", {
        "apl_type": apl_type, "startDate": start_date, "endDate": end_date})


# ── order-link resolver (view-order-document) ────────────────────────────────

_ID_RE = re.compile(r"/view-order/[a-f0-9]+/(\d+)")


def _order_id_from(link_or_id: str) -> str:
    s = str(link_or_id or "").strip()
    m = _ID_RE.search(s)
    if m:
        return m.group(1)
    if s.isdigit():
        return s
    # last numeric path segment fallback
    m = re.search(r"(\d+)\D*$", s)
    if m:
        return m.group(1)
    raise ValueError(f"could not extract order id from {link_or_id!r}")


def resolve_order(link_or_id: str):
    """Turn a (possibly stale) order link or id into the live PDF.

    Returns (filename, pdf_bytes) or None. Only the numeric order id matters;
    view-order-document re-mints the session token server-side.
    """
    order_id = _order_id_from(link_or_id)
    s, tok = _new(ORDERS_PAGE)
    r = s.post(f"{BASE}/view-order-document", headers=_hdr(ORDERS_PAGE),
               data={"order": order_id, "security_token": tok}, timeout=90)
    try:
        j = r.json()
    except Exception:
        return None
    if j.get("status") != "success":
        return None
    b64 = j.get("content") or j.get("c") or ""
    if not b64:
        return None
    try:
        pdf = base64.b64decode(b64.split(",")[-1])
    except Exception:
        return None
    if not pdf.startswith(b"%PDF"):
        return None
    return f"SAT_order_{order_id}.pdf", pdf
