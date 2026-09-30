"""fetch_cestat_orders.py — CESTAT tribunal ORDERS search.

Target: https://cestat.gov.in/order-status  — a DataTables page whose AJAX
endpoint POST https://cestat.gov.in/ajax/order-status-web returns JSON. The
`tab` param (1-6) picks the search method; `bench` (numeric code) picks the zone.
SIX methods:

  tab 1  by-case-number       Bench + Case Type + Case No + Case Year
  tab 2  by-diary-number      Bench + Diary No + Diary Year
  tab 3  by-member            Bench + Member + From Date + To Date
  tab 4  by-order-date        Bench + From + To
  tab 5  by-party-name        Bench + Party Name
  tab 6  by-brief-description Bench + Description + From Date + To Date

Captcha: NO-OP (server only checks `captcha_code` is exactly 6 chars). We send a
fixed 6-char string; no OCR.

Pagination: the DataTable is CLIENT-SIDE (serverSide is off) — ALL matching
orders come back in ONE response (we request a huge length). So there is no
server pagination to walk; a single call returns everything.

Each result row: [Sl.No, Case/Order No, "Applicant<br>vs<br>Respondent",
Order Date, <a href=./weborders/file/<bench>/<id>>PDF</a>]. The PDF link is
DIRECT and standalone (no captcha, no session) — we return the absolute URL.

FULLY SELF-CONTAINED / DECOUPLED (only curl_cffi). Mirrors the ITAT/CESTAT
provider surface: SEARCH_METHODS, list_options(), list_methods(),
search(method, **query), and by_* wrappers.
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

_BASE = "https://cestat.gov.in"
_PAGE = f"{_BASE}/order-status"
_AJAX = f"{_BASE}/ajax/order-status-web"

_IMP = ["chrome131", "chrome124", "chrome120", "chrome110", "safari17_0"]

COURT = "cestat"
_CAPTCHA = "cestat"          # any 6-char string (value never validated)

# Benches use NUMERIC codes on the orders page (different from casestatus).
BENCHES = [
    ("107079", "Delhi"), ("104044", "Chandigarh"), ("127482", "Mumbai"),
    ("124438", "Ahmedabad"), ("129525", "Bangalore"), ("109120", "Allahabad"),
    ("119315", "Kolkata"), ("133568", "Chennai"), ("136507", "Hyderabad"),
]
CASE_TYPES = [
    ("1", "CUSTOMS"), ("2", "EXCISE"), ("3", "SERVICE TAX"),
    ("4", "ANTIDUMPING"), ("5", "CENTRAL SALES TAX"),
]

_BENCH_FIELD = {"name": "bench", "label": "Bench", "type": "select", "required": True}
_FROMD = {"name": "from_date", "label": "From Date", "type": "date", "required": True, "format": "dd-mm-yyyy"}
_TOD = {"name": "to_date", "label": "To Date", "type": "date", "required": True, "format": "dd-mm-yyyy"}

SEARCH_METHODS = [
    {
        "id": "by-case-number", "tab": "1", "label": "By Case Number",
        "fields": [_BENCH_FIELD,
                   {"name": "case_type", "label": "Case Type", "type": "select", "required": True},
                   {"name": "case_no", "label": "Case Number", "type": "text", "required": True},
                   {"name": "case_year", "label": "Case Year", "type": "text", "required": True}],
        "map": {"case_type": "case_type", "case_no": "case_no", "case_year": "case_year"},
    },
    {
        "id": "by-diary-number", "tab": "2", "label": "By Diary Number",
        "fields": [_BENCH_FIELD,
                   {"name": "diary_no", "label": "Diary Number", "type": "text", "required": True},
                   {"name": "diary_year", "label": "Diary Year", "type": "text", "required": True}],
        "map": {"diary_no": "diary_no", "diary_year": "diary_year"},
    },
    {
        "id": "by-member", "tab": "3", "label": "By Member",
        "fields": [_BENCH_FIELD,
                   {"name": "member_name", "label": "Member", "type": "select", "required": True},
                   _FROMD, _TOD],
        "map": {"member_name": "member_name", "from_date": "from_date", "to_date": "to_date"},
    },
    {
        "id": "by-order-date", "tab": "4", "label": "By Order Date",
        "fields": [_BENCH_FIELD,
                   {"name": "from_date", "label": "From Date", "type": "date", "required": True, "format": "dd-mm-yyyy"},
                   {"name": "to_date", "label": "To Date", "type": "date", "required": True, "format": "dd-mm-yyyy"}],
        # NB: the order-date tab uses site fields `from`/`to` (not from_date/to_date)
        "map": {"from": "from_date", "to": "to_date"},
    },
    {
        "id": "by-party-name", "tab": "5", "label": "By Party Name",
        "fields": [_BENCH_FIELD,
                   {"name": "party_name", "label": "Party Name", "type": "text", "required": True}],
        "map": {"party_name": "party_name"},
    },
    {
        "id": "by-brief-description", "tab": "6", "label": "By Brief Description",
        "fields": [_BENCH_FIELD,
                   {"name": "description", "label": "Brief Description", "type": "text", "required": True},
                   _FROMD, _TOD],
        "map": {"description": "description", "from_date": "from_date", "to_date": "to_date"},
    },
]
_METHOD_BY_ID = {m["id"]: m for m in SEARCH_METHODS}


def _new_session():
    import random
    s = _rq.Session(impersonate=random.choice(_IMP)) if _HAS_CFFI else _rq.Session()
    s.verify = False
    return s


def _csrf(html: str) -> str:
    m = re.search(r'name=["\']?csrf_token["\']?[^>]*value=["\']([^"\']*)["\']', html) \
        or re.search(r'value=["\']([^"\']*)["\'][^>]*name=["\']?csrf_token', html)
    return m.group(1) if m else ""


def _to_ddmmyyyy(d: str) -> str:
    """Normalise a date to DD-MM-YYYY (the order page's format)."""
    d = str(d).strip()
    if re.match(r"^\d{2}-\d{2}-\d{4}$", d):
        return d
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", d)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", d)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return d


# ---------------------------------------------------------------------------
# Options (Bench / Case Type / Member) — member list is fetched live
# ---------------------------------------------------------------------------

def list_options(session=None) -> Dict[str, List[Dict[str, str]]]:
    s = session or _new_session()
    members: List[Dict[str, str]] = []
    try:
        html = s.get(_PAGE, timeout=40).text
        mm = re.search(r'<select[^>]*name=["\']?member_name["\']?[^>]*>(.*?)</select>', html, re.S)
        if mm:
            for v, t in re.findall(r'<option[^>]*value=["\']?([^"\'>]*)["\']?[^>]*>(.*?)</option>', mm.group(1), re.S):
                if v:
                    members.append({"value": v, "label": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", t)).strip()})
    except Exception:
        pass
    finally:
        if session is None:
            try:
                s.close()
            except Exception:
                pass
    return {
        "bench": [{"value": v, "label": t} for v, t in BENCHES],
        "case_type": [{"value": v, "label": t} for v, t in CASE_TYPES],
        "member_name": members,
    }


def list_methods() -> List[Dict]:
    return [{"id": m["id"], "label": m["label"], "fields": m["fields"]}
            for m in SEARCH_METHODS]


# ---------------------------------------------------------------------------
# Result parse (JSON data rows)
# ---------------------------------------------------------------------------

_PDF_RE = re.compile(r'href=["\']\.?/?(weborders/file/[\w/]+)["\']', re.I)


def _clean(x: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x or "")).strip()


def _parse_row(cols: List[str]) -> Dict[str, str]:
    """cols = [Sl.No, Case/Order No, 'Applicant<br>vs<br>Respondent', Order Date,
    <a ...PDF>]. -> structured order dict with a DIRECT pdf_url."""
    case_no = _clean(cols[1]) if len(cols) > 1 else ""
    parties = cols[2] if len(cols) > 2 else ""
    app, resp = _clean(parties), ""
    ps = re.split(r"<br\s*/?>\s*vs\s*<br\s*/?>", parties, flags=re.I, maxsplit=1)
    if len(ps) == 2:
        app, resp = _clean(ps[0]), _clean(ps[1])
    order_date = _clean(cols[3]) if len(cols) > 3 else ""
    pdf_url = ""
    if len(cols) > 4:
        pm = _PDF_RE.search(cols[4])
        if pm:
            pdf_url = f"{_BASE}/{pm.group(1)}"
    return {"case_no": case_no, "applicant": app, "respondent": resp,
            "order_date": order_date, "pdf_url": pdf_url}


# ---------------------------------------------------------------------------
# Search — dispatch by method (tab). Every search runs BOTH the daily and the
# final order forms and MERGES the results into one list (each order tagged with
# its `order_type`: "daily" | "final"). The daily/final split on cestat.gov.in
# is just a hidden `order_type` field on the SAME endpoint/tabs/fields, so we
# hit it twice and combine — the caller gets all orders in one go.
# ---------------------------------------------------------------------------

_ORDER_TYPES = [("D", "daily", "order-status"), ("F", "final", "final-order-status")]


def _search_one(method: str, ot_code: str, ot_label: str, ref: str,
                query: Dict) -> List[Dict]:
    """One search against the daily OR final form. Returns a list of order dicts
    (each tagged with order_type=ot_label); [] on no-data / error."""
    m = _METHOD_BY_ID[method]
    s = _new_session()
    try:
        html = s.get(f"{_BASE}/{ref}", timeout=40).text
        csrf = _csrf(html)
        payload = {
            "csrf_token": csrf, "s": "s", "order_type": ot_code,
            "draw": "1", "start": "0",
            "length": "100000",            # client-side table -> ask for everything
            "tab": m["tab"], "bench": str(query.get("bench", "")),
            "captcha_code": _CAPTCHA,
        }
        for site_field, our_field in m["map"].items():
            val = str(query.get(our_field, ""))
            if site_field in ("from", "to", "from_date", "to_date"):
                val = _to_ddmmyyyy(val)
            payload[site_field] = val
        r = s.post(_AJAX, data=payload,
                   headers={"Referer": f"{_BASE}/{ref}", "X-Requested-With": "XMLHttpRequest",
                            "Content-Type": "application/x-www-form-urlencoded"},
                   timeout=120)
        try:
            data = r.json().get("data")
        except Exception:
            return []
        if not isinstance(data, list):     # {messages:'data not Found'} / {errors}
            return []
        out = []
        for row in data:
            if isinstance(row, list):
                o = _parse_row(row)
                o["order_type"] = ot_label
                out.append(o)
        return out
    except Exception:
        return []
    finally:
        try:
            s.close()
        except Exception:
            pass


def search(method: str, *, session=None, **query) -> Dict:
    """Run the method against BOTH daily and final forms (in parallel) and merge
    all orders into one list. Each order carries order_type: 'daily' | 'final'."""
    m = _METHOD_BY_ID.get(method)
    if not m:
        return {"found": False, "count": 0, "orders": [], "method": method,
                "query": query, "error": f"unknown method '{method}'"}
    query.pop("order_type", None)          # flag removed — we always do both
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda ot: _search_one(method, ot[0], ot[1], ot[2], query),
            _ORDER_TYPES))
    orders: List[Dict] = []
    for part in results:
        orders.extend(part)
    counts = {label: len(part) for (_, label, _), part in zip(_ORDER_TYPES, results)}
    return {"found": bool(orders), "count": len(orders),
            "counts": counts, "orders": orders, "method": method, "query": query}


def by_case_number(bench, case_type, case_no, case_year, **kw):
    return search("by-case-number", bench=bench, case_type=case_type,
                  case_no=case_no, case_year=case_year, **kw)


def by_diary_number(bench, diary_no, diary_year, **kw):
    return search("by-diary-number", bench=bench, diary_no=diary_no,
                  diary_year=diary_year, **kw)


def by_member(bench, member_name, from_date, to_date, **kw):
    return search("by-member", bench=bench, member_name=member_name,
                  from_date=from_date, to_date=to_date, **kw)


def by_order_date(bench, from_date, to_date, **kw):
    return search("by-order-date", bench=bench, from_date=from_date, to_date=to_date, **kw)


def by_party_name(bench, party_name, **kw):
    return search("by-party-name", bench=bench, party_name=party_name, **kw)


def by_brief_description(bench, description, from_date, to_date, **kw):
    return search("by-brief-description", bench=bench, description=description,
                  from_date=from_date, to_date=to_date, **kw)


def main():
    p = argparse.ArgumentParser(description="CESTAT orders search (6 methods)")
    p.add_argument("--method", choices=[m["id"] for m in SEARCH_METHODS], default="by-order-date")
    p.add_argument("--bench", default="107079")
    p.add_argument("--case-type", dest="case_type"); p.add_argument("--case-no", dest="case_no")
    p.add_argument("--case-year", dest="case_year")
    p.add_argument("--diary-no", dest="diary_no"); p.add_argument("--diary-year", dest="diary_year")
    p.add_argument("--member", dest="member_name")
    p.add_argument("--from-date", dest="from_date"); p.add_argument("--to-date", dest="to_date")
    p.add_argument("--party-name", dest="party_name"); p.add_argument("--description")
    p.add_argument("--list-options", action="store_true")
    a = p.parse_args()
    if a.list_options:
        print(json.dumps(list_options(), indent=2, ensure_ascii=False)); return
    q = {k: v for k, v in vars(a).items() if v and k not in ("method", "list_options")}
    print(json.dumps(search(a.method, **q), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
