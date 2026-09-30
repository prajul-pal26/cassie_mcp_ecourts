"""fetch_cestat_casestatus.py — CESTAT (Customs, Excise & Service Tax Appellate
Tribunal) case-status / case-details.

Target: https://cestat.gov.in/casestatus  — ONE form whose `app_type` selector
picks the search method (and `schema_type` picks the bench/zone). FOUR methods:

  dno  by-diary-number      Bench + Diary No + Year                      (4 fields)
  cno  by-case-number       Bench + Case Type + Case No + Year           (5 fields)
  pno  by-party-name        Bench + Party Name                     (many, cap ~50)
  ino  by-impugned-order    Bench + Impugned Order No (O-I-A / O-I-O)

Captcha: the site's captcha is a NO-OP — the server only checks that
`captcha_code` is exactly 6 characters, never the value. So we send a fixed
6-char string; no OCR, no image fetch. (Verified: 'ABCDEF'/'123456'/'000000' all
succeed; 5- or 7-char fail.)

Flow: submit the form -> results table (Applicant | Respondent | Diary No |
Case No | Action) where Action links to
    https://cestat.gov.in/casedetailreport/<report_id>/<bench>
That report page (NO captcha, NOT session-bound) holds the FULL case details:
Case Status, Petitioner, Respondent, Case-Proceeding rows, Application rows —
all parsed here into a clean structure.

FULLY SELF-CONTAINED / DECOUPLED (only curl_cffi). Mirrors the ITAT provider
surface so the REST API plugs in the same way:

    SEARCH_METHODS, list_options(), list_methods(),
    search(method, **query), by_diary_number / by_case_number /
    by_party_name / by_impugned_order, fetch_full_details(report_id, bench)
"""

from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
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
_PAGE = f"{_BASE}/casestatus"
_REPORT = f"{_BASE}/casedetailreport"

_IMP = ["chrome131", "chrome124", "chrome120", "chrome110", "safari17_0"]

COURT = "cestat"
COURT_NAME = "Customs, Excise & Service Tax Appellate Tribunal"

# captcha bypass — any exactly-6-char string is accepted (value never checked)
_CAPTCHA = "cestat"

# Benches (schema_type)
BENCHES = [
    ("delhi", "Delhi"), ("chandigarh", "Chandigarh"), ("mumbai", "Mumbai"),
    ("ahmedabad", "Ahmedabad"), ("bangalore", "Bangalore"),
    ("allahabad", "Allahabad"), ("kolkata", "Kolkata"), ("chennai", "Chennai"),
    ("hyderabad", "Hyderabad"),
]
# Case types (case_type, only for by-case-number)
CASE_TYPES = [
    ("1", "CUSTOMS"), ("2", "EXCISE"), ("3", "SERVICE TAX"),
    ("4", "ANTIDUMPING"), ("5", "CENTRAL SALE TAX"),
]

_BENCH_FIELD = {"name": "bench", "label": "Bench", "type": "select", "required": True}

SEARCH_METHODS = [
    {
        "id": "by-diary-number", "app_type": "dno", "button": "button1",
        "label": "By Diary Number",
        "fields": [_BENCH_FIELD,
                   {"name": "diary_no", "label": "Diary Number", "type": "text", "required": True},
                   {"name": "year", "label": "Year", "type": "text", "required": True}],
        "map": {"token_no": "diary_no", "token_year": "year"},
    },
    {
        "id": "by-case-number", "app_type": "cno", "button": "button2",
        "label": "By Case Number",
        "fields": [_BENCH_FIELD,
                   {"name": "case_type", "label": "Case Type", "type": "select", "required": True},
                   {"name": "case_no", "label": "Case Number", "type": "text", "required": True},
                   {"name": "year", "label": "Year", "type": "text", "required": True}],
        "map": {"case_type": "case_type", "token_no": "case_no", "token_year": "year"},
    },
    {
        "id": "by-party-name", "app_type": "pno", "button": "button3",
        "label": "By Party Name",
        "fields": [_BENCH_FIELD,
                   {"name": "party_name", "label": "Party Name", "type": "text", "required": True}],
        "map": {"token_no": "party_name"},
        "list_mode": True,        # returns many rows (site caps at ~50)
    },
    {
        "id": "by-impugned-order", "app_type": "ino", "button": "button4",
        "label": "By Impugned Order (O-I-A / O-I-O)",
        "fields": [_BENCH_FIELD,
                   {"name": "impugned_order", "label": "Impugned Order No", "type": "text", "required": True}],
        "map": {"token_no": "impugned_order"},
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


def _clean(s: str) -> str:
    s = re.sub(r"&nbsp;", " ", s)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip().strip(".").strip()


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

def list_options(session=None) -> Dict[str, List[Dict[str, str]]]:
    """Dropdown choices: Bench (schema_type) and Case Type (for by-case-number)."""
    return {
        "bench": [{"value": v, "label": t} for v, t in BENCHES],
        "case_type": [{"value": v, "label": t} for v, t in CASE_TYPES],
    }


def list_methods() -> List[Dict]:
    return [{"id": m["id"], "label": m["label"], "fields": m["fields"]}
            for m in SEARCH_METHODS]


# ---------------------------------------------------------------------------
# Search results parse (Applicant | Respondent | Diary No | Case No | Action)
# ---------------------------------------------------------------------------

_REPORT_RE = re.compile(r"casedetailreport/([\w]+)/([\w]+)")


def _parse_results(html: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen = set()
    for tbl in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S):
        if "casedetailreport" not in tbl:
            continue
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S):
            lk = _REPORT_RE.search(row)
            if not lk:
                continue
            cells = [_clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            report_id, bench = lk.group(1), lk.group(2)
            if report_id in seen:
                continue
            seen.add(report_id)
            out.append({
                "applicant_name": cells[0] if len(cells) > 0 else "",
                "respondent_name": cells[1] if len(cells) > 1 else "",
                "diary_no": cells[2] if len(cells) > 2 else "",
                "case_no": cells[3] if len(cells) > 3 else "",
                "report_id": report_id, "bench": bench,
                "report_url": f"{_REPORT}/{report_id}/{bench}",
            })
    return out


# ---------------------------------------------------------------------------
# Full case-detail report parse (NO captcha, not session-bound)
# ---------------------------------------------------------------------------

def _kv_table(tbl: str) -> Dict[str, str]:
    """Two-column label|value table -> dict (snake_case keys)."""
    out: Dict[str, str] = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S):
        cells = [_clean(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        cells = [c for c in cells if c]
        if len(cells) == 2:
            key = re.sub(r"[^a-z0-9]+", "_", cells[0].lower()).strip("_")
            if key and key not in ("print",):
                out[key] = cells[1]
    return out


def _rows_table(tbl: str) -> List[Dict[str, str]]:
    """Header-row + data-rows table -> list of dicts."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S)
    header = None
    out: List[Dict[str, str]] = []
    for row in rows:
        ths = [_clean(c) for c in re.findall(r"<th[^>]*>(.*?)</th>", row, re.S)]
        tds = [_clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        cells = ths or tds
        cells = [c for c in cells if c is not None]
        if not any(cells):
            continue
        # a heading row (single cell) -> skip
        if len([c for c in cells if c]) <= 1:
            continue
        if header is None:
            header = [re.sub(r"[^a-z0-9]+", "_", c.lower()).strip("_") for c in cells]
            continue
        rec = {header[i] if i < len(header) else f"col{i}": cells[i]
               for i in range(len(cells))}
        if any(v for v in rec.values()):
            out.append(rec)
    return out


def fetch_full_details(report_id: str, bench: str, *, session=None) -> Dict:
    """GET the case-detail report and parse every section into a clean structure.
    No captcha; the report URL works standalone."""
    s = session or _new_session()
    url = f"{_REPORT}/{report_id}/{bench}"
    try:
        h = s.get(url, headers={"Referer": _PAGE}, timeout=60).text
        det: Dict = {"report_id": report_id, "bench": bench, "report_url": url}
        for tbl in re.findall(r"<table[^>]*>(.*?)</table>", h, re.S):
            head = _clean(tbl[:400]).upper()
            if "CASE STATUS" in head or "CASE CURRENT STAGE" in head:
                det["case_status"] = _kv_table(tbl)
            elif "PETITIONER" in head:
                det["petitioner"] = _kv_table(tbl)
            elif "RESPONDENT" in head:
                det["respondent"] = _kv_table(tbl)
            elif "CASE PROCEEDING" in head:
                det["proceedings"] = _rows_table(tbl)
            elif "APPLICATION DETAILS" in head:
                det["applications"] = _rows_table(tbl)
        return det
    except Exception as e:
        return {"report_id": report_id, "bench": bench, "report_url": url,
                "error": f"report_fetch_failed: {e}"}
    finally:
        if session is None:
            try:
                s.close()
            except Exception:
                pass


def _fetch_all_details(rows: List[Dict], workers: int = 8) -> List[Dict]:
    if not rows:
        return []
    if len(rows) == 1:
        return [fetch_full_details(rows[0]["report_id"], rows[0]["bench"])]
    with ThreadPoolExecutor(max_workers=min(workers, len(rows))) as pool:
        return list(pool.map(
            lambda r: fetch_full_details(r["report_id"], r["bench"]), rows))


# ---------------------------------------------------------------------------
# Search — dispatch by method
# ---------------------------------------------------------------------------

def search(method: str, *, session=None, **query) -> Dict:
    m = _METHOD_BY_ID.get(method)
    if not m:
        return {"found": False, "count": 0, "cases": [], "method": method,
                "query": query, "error": f"unknown method '{method}'"}
    s = session or _new_session()
    try:
        html = s.get(_PAGE, timeout=40).text
        csrf = _csrf(html)
        payload = {
            "csrf_token": csrf, "schema_type": str(query.get("bench", "delhi")),
            "app_type": m["app_type"], "captcha_code": _CAPTCHA,
            m["button"]: "SEARCH",
        }
        for site_field, our_field in m["map"].items():
            payload[site_field] = str(query.get(our_field, ""))
        r = s.post(_PAGE, data=payload,
                   headers={"Referer": _PAGE,
                            "Content-Type": "application/x-www-form-urlencoded"},
                   timeout=60)
        rows = _parse_results(r.text)
        if m.get("list_mode"):
            # party search: many rows -> summary + report link (full details on
            # demand via the report_url / by report_id+bench).
            return {"found": bool(rows), "count": len(rows),
                    "cases": rows, "method": method, "query": query,
                    "note": "party search is capped at ~50 rows by the site"}
        # single-result methods -> full parsed case details
        cases = _fetch_all_details(rows)
        return {"found": bool(cases), "count": len(cases),
                "cases": cases, "method": method, "query": query}
    finally:
        if session is None:
            try:
                s.close()
            except Exception:
                pass


def by_diary_number(bench, diary_no, year, **kw):
    return search("by-diary-number", bench=bench, diary_no=diary_no, year=year, **kw)


def by_case_number(bench, case_type, case_no, year, **kw):
    return search("by-case-number", bench=bench, case_type=case_type,
                  case_no=case_no, year=year, **kw)


def by_party_name(bench, party_name, **kw):
    return search("by-party-name", bench=bench, party_name=party_name, **kw)


def by_impugned_order(bench, impugned_order, **kw):
    return search("by-impugned-order", bench=bench, impugned_order=impugned_order, **kw)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="CESTAT case-status lookup (4 methods)")
    p.add_argument("--method", choices=[m["id"] for m in SEARCH_METHODS],
                   default="by-diary-number")
    p.add_argument("--bench", default="delhi")
    p.add_argument("--diary-no", dest="diary_no"); p.add_argument("--year")
    p.add_argument("--case-type", dest="case_type"); p.add_argument("--case-no", dest="case_no")
    p.add_argument("--party-name", dest="party_name")
    p.add_argument("--impugned-order", dest="impugned_order")
    p.add_argument("--report"); p.add_argument("--report-bench", default="delhi")
    p.add_argument("--list-options", action="store_true")
    a = p.parse_args()
    if a.list_options:
        print(json.dumps(list_options(), indent=2)); return
    if a.report:
        print(json.dumps(fetch_full_details(a.report, a.report_bench), indent=2, ensure_ascii=False)); return
    q = {k: v for k, v in vars(a).items()
         if v and k not in ("method", "list_options", "report", "report_bench")}
    print(json.dumps(search(a.method, **q), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
