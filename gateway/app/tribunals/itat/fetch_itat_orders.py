"""fetch_itat_orders.py — ITAT Tribunal ORDERS search.

Target: https://itat.gov.in/judicial/tribunalorders  (separate from case-details;
that page finds ORDERS and gives each order's PDF). It hosts FOUR search methods:

  f1  by-appeal-number       Bench + Appeal Type + Appeal Number + Filing Year
  f2  by-order-date          Bench + Appeal Type + Date of Order        (many)
  f3  by-pronouncement-date  Bench + Appeal Type + Date of Pronouncement (many)
  f4  by-member-name         Bench + Member + Date of Order              (many)

(NB: the ORDERS page's methods differ from CASE-DETAILS — there's no
date-of-filing / assessee / acknowledgement here; instead order-date,
pronouncement-date and member.)

Each result row is ONE order: Appeal Number / Assessment Year / Case Status |
Parties | Bench | Order Link (View Order PDF) | More Details. We return a clean
list (pagination handled — many pages) where every order carries a WORKING PDF
link (proxied through our server, since the viewOrder link is session-bound) plus
a `case_details_query` to pull that appeal's full case details.

Self-contained within the ITAT folder: reuses itat_captcha (captcha) and a few
low-level helpers from fetch_itat_casestatus (same court).
"""

from __future__ import annotations

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

try:                                                 # package / direct-run
    from . import itat_captcha, itat_pdf_cache
    from .fetch_itat_casestatus import (
        _csrf, _page_tokens, _abs_url, _appeal_parts, _clean, _new_session,
        _to_ddmmyyyy, _options as _cd_options)
except Exception:                                    # pragma: no cover
    import itat_captcha, itat_pdf_cache
    from fetch_itat_casestatus import (
        _csrf, _page_tokens, _abs_url, _appeal_parts, _clean, _new_session,
        _to_ddmmyyyy, _options as _cd_options)

_BASE = "https://itat.gov.in"
_PAGE = f"{_BASE}/judicial/tribunalorders"

COURT = "itat"

_BENCH = {"name": "bench", "label": "Bench", "type": "select", "required": True}
_ATYPE = {"name": "appeal_type", "label": "Appeal Type", "type": "select", "required": True}

# Each method: site form index, our->site field map, and whether it lists many.
# form 4 (by-member) uses `member` instead of app_type, and order_date_2.
SEARCH_METHODS = [
    {
        "id": "by-appeal-number",
        "label": "By Appeal Number",
        "form": 1, "second": ("app_type_1", "appeal_type"),
        "fields": [_BENCH, _ATYPE,
                   {"name": "appeal_number", "label": "Appeal Number", "type": "text", "required": True},
                   {"name": "filing_year", "label": "Filing Year", "type": "select", "required": True}],
        "map": {"app_number": "appeal_number", "app_year_1": "filing_year"},
    },
    {
        "id": "by-order-date",
        "label": "By Order Date",
        "form": 2, "second": ("app_type_2", "appeal_type"),
        "fields": [_BENCH, _ATYPE,
                   {"name": "order_date", "label": "Date of Order", "type": "date",
                    "required": True, "format": "dd/mm/yyyy"}],
        "map": {"order_date": "order_date"},
        "list_mode": True,
    },
    {
        "id": "by-pronouncement-date",
        "label": "By Pronouncement Date",
        "form": 3, "second": ("app_type_3", "appeal_type"),
        "fields": [_BENCH, _ATYPE,
                   {"name": "pronouncement_date", "label": "Date of Pronouncement", "type": "date",
                    "required": True, "format": "dd/mm/yyyy"}],
        "map": {"pron_date": "pronouncement_date"},
        "list_mode": True,
    },
    {
        "id": "by-member-name",
        "label": "By Member Name",
        "form": 4, "second": ("member", "member"),
        "fields": [_BENCH,
                   {"name": "member", "label": "Member", "type": "select", "required": True},
                   {"name": "order_date", "label": "Date of Order", "type": "date",
                    "required": True, "format": "dd/mm/yyyy"}],
        "map": {"order_date_2": "order_date"},
        "list_mode": True,
    },
]
_METHOD_BY_ID = {m["id"]: m for m in SEARCH_METHODS}


# ---------------------------------------------------------------------------
# Options (Bench / Appeal Type / Member)
# ---------------------------------------------------------------------------

def list_options(session=None) -> Dict[str, List[Dict[str, str]]]:
    """Dropdown choices for the orders search: Bench, Appeal Type, Member."""
    s = session or _new_session()
    try:
        html = s.get(_PAGE, timeout=40).text
        return {
            "bench": _cd_options(html, "bench_name_1"),
            "appeal_type": _cd_options(html, "app_type_1"),
            "member": _cd_options(html, "member"),
        }
    finally:
        if session is None:
            try:
                s.close()
            except Exception:
                pass


def list_methods() -> List[Dict]:
    return [{"id": m["id"], "label": m["label"], "fields": m["fields"]}
            for m in SEARCH_METHODS]


# ---------------------------------------------------------------------------
# Result parse (each row = one order)
# ---------------------------------------------------------------------------

def _parse_order_rows(html: str, *, with_token: bool = False) -> List[Dict]:
    """Parse the orders results table. Each row:
        td0 = Appeal Number [Assessment Year] Status:X
        td1 = Appellant VS Respondent
        td2 = Bench
        td3 = Order Link  -> viewOrder?data=<token>  (the order PDF)
        td4 = More Details -> casedetails?cid=...
    """
    out: List[Dict] = []
    for tbl in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S):
        if "order link" not in tbl.lower() and "appeal number" not in tbl.lower():
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
            tok = re.search(r"viewOrder\?data=([^\"'&]+)", row) or \
                re.search(r"viewOrder\('([^']+)'", row)
            o = {
                "appeal_number": appeal,
                "assessment_year": ay.group(1).strip() if ay else "",
                "case_status": st.group(1).strip() if st else "",
                "appellant": app, "respondent": resp,
                "bench": _clean(cells[2]) if len(cells) > 2 else "",
                "order_pdf_url": "",           # our proxy link, filled in search()
            }
            if with_token:
                o["_token"] = tok.group(1) if tok else ""
            else:
                o["_has_pdf"] = bool(tok)
            out.append(o)
    return out


# ---------------------------------------------------------------------------
# Pagination (same lq/lqc/btnPage mechanism as case-details, parallel)
# ---------------------------------------------------------------------------

def _collect_all_order_rows(session, first_html: str, max_pages: int = 500) -> List[Dict]:
    rows = _parse_order_rows(first_html)
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
            return page, _parse_order_rows(r.text)
        except Exception:
            return page, []
        finally:
            try:
                s.close()
            except Exception:
                pass

    from concurrent.futures import ThreadPoolExecutor
    seen = {(r["appeal_number"], r.get("order_pdf_url")) for r in rows}
    keyset = {r["appeal_number"] for r in rows}
    pending = list(range(2, total_pages + 1))
    for _ in range(2):                       # initial pass + retry empties
        if not pending:
            break
        empty: List[int] = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            for page, prows in pool.map(fetch_page, pending):
                if not prows:
                    empty.append(page)
                for r in prows:
                    k = r["appeal_number"]
                    if k not in keyset:
                        keyset.add(k)
                        rows.append(r)
        pending = empty
    return rows


# ---------------------------------------------------------------------------
# Search — dispatch by method
# ---------------------------------------------------------------------------

def _build_payload(m: Dict, cap: str, csrf: str, query: Dict) -> Dict:
    n = m["form"]
    site_second, our_second = m["second"]
    payload = {
        "csrftkn": csrf, "hp": "", "captcha": cap,
        f"c{n}": cap, f"bt{n}": "true",
        f"bench_name_{n}": str(query.get("bench", "")),
        site_second: str(query.get(our_second, "")),
    }
    for site_field, our_field in m["map"].items():
        val = str(query.get(our_field, ""))
        if "date" in site_field:
            val = _to_ddmmyyyy(val)
        payload[site_field] = val
    return payload


def search(method: str, *, session=None, **query) -> Dict:
    """Run one of the four ORDERS searches. Returns
    {found, count, orders:[...], method, query}. Every order has a working
    `order_pdf_url` (proxied) and a `case_details_query`."""
    m = _METHOD_BY_ID.get(method)
    if not m:
        return {"found": False, "count": 0, "orders": [], "method": method,
                "query": query, "error": f"unknown method '{method}'"}
    s = session or _new_session()
    try:
        html = s.get(_PAGE, timeout=40).text
        csrf = _csrf(html)
        cap = itat_captcha.solve(s, csrf)
        if not cap:
            return {"found": False, "count": 0, "orders": [], "method": method,
                    "query": query, "error": "captcha_unsolved"}
        r = s.post(_PAGE, data=_build_payload(m, cap, csrf, query),
                   headers={"Referer": _PAGE,
                            "Content-Type": "application/x-www-form-urlencoded"},
                   timeout=60)
        if m.get("list_mode"):
            rows = _collect_all_order_rows(s, r.text)
        else:
            rows = _parse_order_rows(r.text)
        b = query.get("bench", "")
        for o in rows:
            parts = _appeal_parts(o.get("appeal_number", ""))
            has_pdf = o.pop("_has_pdf", False)
            if parts:
                o["case_details_query"] = {
                    "bench": b, "appeal_type": parts["type"],
                    "appeal_number": parts["number"], "filing_year": parts["year"]}
                if has_pdf:
                    o["order_pdf_url"] = (
                        f"/itat/orders/order-pdf?bench={b}&appeal_type={parts['type']}"
                        f"&appeal_number={parts['number']}&filing_year={parts['year']}")
        return {"found": bool(rows), "count": len(rows),
                "orders": rows, "method": method, "query": query}
    finally:
        if session is None:
            try:
                s.close()
            except Exception:
                pass


def by_appeal_number(bench, appeal_type, appeal_number, filing_year, **kw):
    return search("by-appeal-number", bench=bench, appeal_type=appeal_type,
                  appeal_number=appeal_number, filing_year=filing_year, **kw)


def by_order_date(bench, appeal_type, order_date, **kw):
    return search("by-order-date", bench=bench, appeal_type=appeal_type,
                  order_date=order_date, **kw)


def by_pronouncement_date(bench, appeal_type, pronouncement_date, **kw):
    return search("by-pronouncement-date", bench=bench, appeal_type=appeal_type,
                  pronouncement_date=pronouncement_date, **kw)


def by_member_name(bench, member, order_date, **kw):
    return search("by-member-name", bench=bench, member=member,
                  order_date=order_date, **kw)


# ---------------------------------------------------------------------------
# Order-PDF download (server-side, so the link works from anywhere)
# ---------------------------------------------------------------------------

def download_order_pdf(bench: str, appeal_type: str, appeal_number: str,
                       filing_year: str, order_index: int = 0):
    """Fetch ONE order PDF end-to-end in a single valid session via the ORDERS
    by-appeal-number search (the viewOrder link is session-bound). Returns
    (filename, pdf_bytes) or None. Cached: an order fetched once is served
    instantly on every later view (no re-solve of the captcha)."""
    key = ("ord", str(bench), str(appeal_type), str(appeal_number),
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
        m = _METHOD_BY_ID["by-appeal-number"]
        r = s.post(_PAGE, data=_build_payload(m, cap, csrf, {
            "bench": bench, "appeal_type": appeal_type,
            "appeal_number": appeal_number, "filing_year": filing_year}),
            headers={"Referer": _PAGE,
                     "Content-Type": "application/x-www-form-urlencoded"}, timeout=60)
        rows = _parse_order_rows(r.text, with_token=True)
        if order_index < 0 or order_index >= len(rows):
            return None
        tok = rows[order_index].get("_token")
        if not tok:
            return None
        pdf = s.get(f"{_BASE}/judicial/viewOrder?data={tok}",
                    headers={"Referer": _PAGE}, timeout=90).content
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


if __name__ == "__main__":
    import json, argparse
    p = argparse.ArgumentParser(description="ITAT tribunal-orders search")
    p.add_argument("--method", default="by-appeal-number",
                   choices=[m["id"] for m in SEARCH_METHODS])
    p.add_argument("--bench"); p.add_argument("--type", dest="appeal_type")
    p.add_argument("--number", dest="appeal_number"); p.add_argument("--year", dest="filing_year")
    p.add_argument("--order-date", dest="order_date")
    p.add_argument("--pron-date", dest="pronouncement_date")
    p.add_argument("--member")
    p.add_argument("--list-options", action="store_true")
    a = p.parse_args()
    if a.list_options:
        print(json.dumps(list_options(), indent=2, ensure_ascii=False)); raise SystemExit
    q = {k: v for k, v in vars(a).items()
         if v and k not in ("method", "list_options")}
    print(json.dumps(search(a.method, **q), indent=2, ensure_ascii=False))
