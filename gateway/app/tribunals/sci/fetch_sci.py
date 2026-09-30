"""SCI (Supreme Court of India) provider — https://www.sci.gov.in

Self-contained scraper for the court's WordPress "sci-api" services: Case Status
(6 searches), Daily Orders (4 searches) and Judgements (5 searches).

How a search works
------------------
Every form posts to  POST /wp-admin/admin-ajax.php  with an `action` naming the
service (e.g. `get_case_status_diary_no`). The page embeds a set of anti-bot
hidden fields we must echo back:

    scid                 securimage captcha id
    tok_<hash>           per-form CSRF token (name AND value are hashes)
    sci_form_nonce       WordPress nonce
    _form_time           unix time the page was rendered
    _form_signature      server HMAC over the form
    _wp_http_referer     the form's path

So each search = GET the form page (harvest those + cookies) → solve the math
captcha (see sci_captcha) → POST the action with our params → parse the returned
`resultsHtml`. On the rare captcha mis-read the server replies "incorrect"; we
just fetch a fresh page and retry (retry-until-valid).

Captcha:   app.tribunals.sci.sci_captcha  (equation-image OCR + eval).
Proxies:   searches optionally ride the Webshare pool (app.upstream.proxy_pool)
           for speed, exactly like the eCourts path.

Results
-------
Case status  → a case list; each row is enriched (up to DEEP_ENRICH) with the
               full "View" details via get_case_details (which needs NO captcha).
Orders / Judgements → a list where every row carries a DIRECT, session-free PDF
               URL on api.sci.gov.in (verified downloadable with no cookies), so
               no PDF-resolver endpoint is needed.
"""
from __future__ import annotations

import json
import os
import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from curl_cffi import requests as _rq
    _IMP = "chrome"
except Exception:                       # pragma: no cover
    import requests as _rq
    _IMP = None

import urllib3
from bs4 import BeautifulSoup

from app.tribunals.sci import sci_captcha

urllib3.disable_warnings()

BASE = "https://www.sci.gov.in"
AJAX = f"{BASE}/wp-admin/admin-ajax.php"

# Max captcha retries per search before giving up.
MAX_ATTEMPTS = 8
# sci.gov.in occasionally returns success:true with an EMPTY results table even
# for a query that has matches (transient server blank). Re-run the whole search
# this many times when a success parses to zero rows AND the body isn't a genuine
# "no record" page (those short-circuit via _NO_RECORD_RE — no wasted retry).
# Kept small: each retry is a full round-trip, so 2 bounds worst-case latency.
EMPTY_RETRIES = 2
# Case-status rows auto-enriched with full details, fetched CONCURRENTLY (details
# are captcha-free GETs). Cap keeps party/AOR searches with thousands of hits fast.
DEEP_ENRICH = 15
_ENRICH_WORKERS = 10

# Proxies add a hop and (for sci.gov.in) no measured benefit — the captcha bypass
# is what makes us fast. Default DIRECT; set SCI_USE_PROXY=1 to spread the
# remaining captcha'd searches across the Webshare pool under heavy load.
_USE_PROXY = os.getenv("SCI_USE_PROXY", "0") == "1"


# ── session / proxy ──────────────────────────────────────────────────────────

def _pick_proxy():
    """A random Webshare lane (dict for curl_cffi) or None → direct."""
    try:
        from app.upstream import proxy_pool
        lanes = proxy_pool.lane_proxy_urls()
        if lanes:
            u = random.choice(lanes)
            return {"http": u, "https": u}
    except Exception:
        pass
    return None


def _session(use_proxy: bool | None = None):
    s = _rq.Session(impersonate=_IMP) if _IMP else _rq.Session()
    if _USE_PROXY if use_proxy is None else use_proxy:
        p = _pick_proxy()
        if p:
            s.proxies = p
    return s


# ── input sanitising ─────────────────────────────────────────────────────────

def _diary_year(diary_no, year):
    """Normalise a diary number + year. Accepts '1', '1/2024', ' 1 ', etc.;
    a 'N/YYYY' diary carries its own year when `year` is blank."""
    dn = str(diary_no or "").strip()
    if "/" in dn:
        head, _, tail = dn.partition("/")
        dn, year = head, (year or tail)
    return re.sub(r"\D", "", dn), re.sub(r"\D", "", str(year or ""))


def _pdf_kind(url: str) -> str:
    return "judgement" if re.search(r"_judgement_", url or "", re.I) else "order"


# ── form-field harvesting ────────────────────────────────────────────────────

def _harvest(html: str, referer: str) -> dict:
    """Pull the anti-bot hidden fields (scid, tok_*, nonce, time, signature)
    out of a freshly-rendered form page."""
    def v(name):
        m = re.search(r'name="' + re.escape(name) + r'"[^>]*value="([^"]*)"', html)
        return m.group(1) if m else None

    fields = {
        "scid": v("scid"),
        "sci_form_nonce": v("sci_form_nonce"),
        "_form_time": v("_form_time"),
        "_form_signature": v("_form_signature"),
        "_wp_http_referer": referer,
    }
    tok = re.search(r'name="(tok_[0-9a-f]+)"[^>]*value="([^"]*)"', html)
    if tok:
        fields[tok.group(1)] = tok.group(2)
    return {k: x for k, x in fields.items() if x is not None}


# ── the search engine (retry-until-valid) ────────────────────────────────────

def _search(action: str, params: dict, page_slug: str) -> dict:
    """GET the form page → solve captcha → POST the action. Retry on a captcha
    mis-read. Returns {"ok", "data"|"message", "attempts"}.

    `data` is the parsed `resultsHtml` string; `message` is set instead when the
    server returned a soft error (no record / bad range / etc.)."""
    referer = f"/{page_slug}/"
    page_url = f"{BASE}/{page_slug}/"
    last_msg = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        s = _session()
        try:
            html = s.get(page_url, timeout=30).text
        except Exception as e:
            last_msg = f"page fetch failed: {e}"
            continue
        fields = _harvest(html, referer)
        if not fields.get("scid"):
            last_msg = "no captcha on page"
            continue
        ans, eq = sci_captcha.solve(s, fields["scid"])
        if ans is None:
            last_msg = f"captcha OCR unparsable ({eq!r})"
            continue
        data = {"action": action, "es_ajax_request": "1", "language": "en",
                "siwp_captcha_value": str(ans), **fields, **params}
        try:
            r = s.post(AJAX, data=data, timeout=45, headers={
                "X-Requested-With": "XMLHttpRequest", "Referer": page_url})
        except Exception as e:
            last_msg = f"post failed: {e}"
            continue
        body = r.text
        if "incorrect" in body.lower() and "captcha" in body.lower():
            last_msg = "captcha rejected"
            continue                     # mis-read → fresh page, retry
        try:
            j = json.loads(body)
        except Exception:
            return {"ok": True, "data": body, "attempts": attempt}
        if j.get("success"):
            d = j.get("data")
            html_out = d.get("resultsHtml") if isinstance(d, dict) else d
            return {"ok": True, "data": html_out or "",
                    "pagination": (d.get("pagination") if isinstance(d, dict) else False),
                    "attempts": attempt}
        # success:false → soft message ({"message": "..."} possibly JSON-in-string)
        msg = j.get("data")
        try:
            msg = json.loads(msg).get("message", msg)
        except Exception:
            pass
        return {"ok": False, "message": (msg or "no result").strip(),
                "attempts": attempt}
    return {"ok": False, "message": last_msg or "search failed", "attempts": MAX_ATTEMPTS}


# ── parsing: case-status list ────────────────────────────────────────────────

def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").replace("\xa0", " ")).strip()


def _parse_case_list(html: str) -> list:
    """The case-status results table → [{serial, diary_no, diary_year,
    case_no, petitioner, respondent, status}]."""
    soup = BeautifulSoup(html or "", "html.parser")
    rows = []
    for tr in soup.select("tr[data-diary-no]"):
        tds = tr.find_all("td")
        cells = [_clean(td.get_text(" ")) for td in tds]
        rows.append({
            "diary_no": tr.get("data-diary-no"),
            "diary_year": tr.get("data-diary-year"),
            "serial": cells[0] if len(cells) > 0 else None,
            "diary_number": cells[1] if len(cells) > 1 else None,
            "case_no": cells[2] if len(cells) > 2 else None,
            "petitioner": cells[3] if len(cells) > 3 else None,
            "respondent": cells[4] if len(cells) > 4 else None,
            "status": cells[5] if len(cells) > 5 else None,
        })
    return rows


# ── parsing: full case details (captcha-free) ────────────────────────────────

def _parse_case_details(html: str) -> dict:
    """get_case_details HTML → {title, fields:{label:value}, documents:[pdf...]}"""
    soup = BeautifulSoup(html or "", "html.parser")
    fields: dict = {}
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) == 2:
            k = _clean(cells[0].get_text(" "))
            v = _clean(cells[1].get_text(" "))
            if k and v and k not in fields:
                fields[k] = v
    docs = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" in href.lower() or "api.sci.gov.in" in href.lower():
            docs.append({"label": _clean(a.get_text(" ")) or "PDF",
                         "pdf_url": href})
    # title = first "X vs. Y" line
    text = _clean(soup.get_text(" "))
    m = re.search(r"([A-Z0-9][^\n]{0,120}?\bvs\.?\b[^\n]{0,120})", text, re.I)
    return {
        "title": _clean(m.group(1)) if m else None,
        "fields": fields,
        "documents": docs,
    }


def _tab_html(diary_no, year, tab_name: str):
    """Raw captcha-FREE get_case_details HTML for a tab, or None on failure."""
    diary, year = _diary_year(diary_no, year)
    try:
        r = _session().get(AJAX, params={
            "action": "get_case_details", "es_ajax_request": "1", "language": "en",
            "diary_no": diary, "diary_year": year, "tab_name": tab_name,
        }, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=30)
        j = json.loads(r.text)
    except Exception:
        return None
    return j.get("data") if j.get("success") else None


def case_details(diary_no, diary_year, tab_name: str = "") -> dict:
    """Full 'View' details for a case — a captcha-FREE GET (needs only the diary
    number + year). Used to enrich case-status results and on its own."""
    diary, year = _diary_year(diary_no, diary_year)
    html = _tab_html(diary, year, tab_name)
    if html is None:
        return {"ok": False, "message": "not found"}
    parsed = _parse_case_details(html)
    parsed["ok"] = True
    parsed["diary_no"] = diary
    parsed["diary_year"] = year
    return parsed


def case_documents(diary_no, year) -> dict:
    """CAPTCHA-FREE order + judgement PDFs for one case, read from the
    `judgement_orders` tab of get_case_details. Returns
    {ok, orders:[...], judgements:[...]} where each item is
    {date, pdf_url, type, label}. This is what makes orders/judgements
    *by diary number* skip the captcha entirely (one GET, no OCR)."""
    html = _tab_html(diary_no, year, "judgement_orders")
    if html is None:
        return {"ok": False, "orders": [], "judgements": []}
    soup = BeautifulSoup(html, "html.parser")
    orders, judgements, seen = [], [], set()
    for a in soup.find_all("a", href=True):
        url = a["href"]
        if ".pdf" not in url.lower() or url in seen:
            continue
        seen.add(url)
        fm = _FNAME_DATE_RE.search(url)
        item = {"date": fm.group(1) if fm else None, "pdf_url": url,
                "type": _pdf_kind(url), "label": _clean(a.get_text(" ")) or None}
        (judgements if item["type"] == "judgement" else orders).append(item)
    return {"ok": True, "orders": orders, "judgements": judgements}


# ── parsing: orders / judgements list (direct PDF links) ─────────────────────

_DATE_RE = re.compile(r"\d{2}-\d{2}-\d{4}")
_FNAME_DATE_RE = re.compile(r"_(?:Order|Judgement)_(\d{2}-[A-Za-z]{3}-\d{4})\.pdf", re.I)


def _row_title_date(text: str):
    """A display row reads 'PETITIONER VS. RESPONDENT / DD-MM-YYYY [View]'."""
    t = _clean(re.sub(r"\bView\b", "", text)).strip(" /")
    dm = re.search(r"(\d{2}-\d{2}-\d{4})\s*$", t)
    date = dm.group(1) if dm else None
    title = _clean(re.sub(r"/\s*\d{2}-\d{2}-\d{4}\s*$", "", t)) or None
    return title, date


def _first_pdf(el):
    a = el.find("a", href=lambda h: h and ".pdf" in h.lower())
    return a["href"] if a else None


def _parse_structured(soup, headers, kind) -> list:
    """The by-diary/case/judge/date layout: a full table (Serial, Diary No,
    Case No, Parties, Advocate, Bench, Judge, Judgment/ROP). The last column
    holds the PDF link + date. Returns rich rows so the 'small details' show."""
    hl = [h.lower() for h in headers]

    def idx(*needles, exact=None):
        for i, h in enumerate(hl):
            if exact is not None:
                if h == exact:
                    return i
            elif any(n in h for n in needles):
                return i
        return None

    i_parties = idx(exact="petitioner / respondent")
    if i_parties is None:
        i_parties = idx("petitioner / respondent")
    i_diary = idx("diary number")
    i_case = idx("case number")
    i_adv = idx("advocate")
    i_bench = idx("bench")
    i_judge = idx("judgment by", "judge by")
    out = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        cells = [_clean(td.get_text(" ")) for td in tds]
        pdf = _first_pdf(tr)
        last = cells[-1]
        dm = _DATE_RE.search(last)
        if dm:
            date = dm.group(0)
        else:
            fm = _FNAME_DATE_RE.search(pdf or "")
            date = fm.group(1) if fm else None
        rec = {
            "title": cells[i_parties] if i_parties is not None and i_parties < len(cells) else None,
            "date": date,
            "pdf_url": pdf,
            "type": kind,
            "diary_number": cells[i_diary] if i_diary is not None and i_diary < len(cells) else None,
            "case_no": cells[i_case] if i_case is not None and i_case < len(cells) else None,
            "advocate": cells[i_adv] if i_adv is not None and i_adv < len(cells) else None,
            "bench": cells[i_bench] if i_bench is not None and i_bench < len(cells) else None,
            "judgment_by": cells[i_judge] if i_judge is not None and i_judge < len(cells) else None,
        }
        if rec["title"] or rec["pdf_url"]:
            out.append(rec)
    return out


def _parse_doc_list(html: str, kind: str) -> list:
    """Orders/Judgements results → [{title, date, pdf_url, ...}].

    SCI serves two layouts: the free-text search uses **paired** rows (a visible
    display row with title+date+View, then a hidden row with the DIRECT PDF link
    + record-of-proceedings preview); the by-diary/case/judge/date searches use a
    **structured** full-column table. We detect which and parse accordingly. The
    date falls back to the one encoded in the PDF filename."""
    soup = BeautifulSoup(html or "", "html.parser")
    headers = [_clean(th.get_text(" ")) for th in soup.find_all("th")]
    if any("serial number" in h.lower() for h in headers):
        return _parse_structured(soup, headers, kind)
    out = []
    pending = None
    for tr in soup.find_all("tr"):
        pdf = next((a["href"] for a in tr.find_all("a", href=True)
                    if ".pdf" in a["href"].lower()), None)
        if pdf:
            if pending:
                title, date = pending
            else:                        # title lives in the same row
                title, date = _row_title_date(tr.get_text(" "))
            if not date:
                m = _FNAME_DATE_RE.search(pdf)
                date = m.group(1) if m else None
            preview = _clean(re.sub(r"\bPDF\b", "", tr.get_text(" ")))[:600] or None
            out.append({"title": title, "date": date, "pdf_url": pdf,
                        "preview": preview, "type": kind})
            pending = None
            continue
        # a non-PDF row: header/logo → skip; otherwise it's a display row
        txt = _clean(tr.get_text(" "))
        if not txt or "Petitioner vs" in txt or txt == "View" \
           or "SUPREME COURT OF INDIA" in txt:
            continue
        title, date = _row_title_date(txt)
        if title:
            pending = (title, date)
    return out


# ── public: CASE STATUS (6) ──────────────────────────────────────────────────

_NO_RECORD_RE = re.compile(r"no record|not found|nothing found|no data found|no result", re.I)


def _run_list(action, params, slug, parse):
    """Run a search and parse its rows, retrying only on a *transient* empty
    table. A genuine "no record" body short-circuits (no wasted retries).
    Returns (items, res); items is None on a hard soft-error."""
    res = None
    for _ in range(EMPTY_RETRIES):
        res = _search(action, params, slug)
        if not res["ok"]:
            return None, res            # soft error (bad range / etc.)
        items = parse(res["data"])
        if items:
            return items, res
        if _NO_RECORD_RE.search(res["data"] or ""):
            return [], res              # genuinely empty — don't retry
    return [], res                      # persistently blank


def _enrich(cases):
    """Attach full captcha-free details to the first DEEP_ENRICH cases,
    fetched CONCURRENTLY so a many-hit search stays fast."""
    targets = cases[:DEEP_ENRICH]
    if not targets:
        return
    with ThreadPoolExecutor(max_workers=min(_ENRICH_WORKERS, len(targets))) as ex:
        futs = {ex.submit(case_details, c["diary_no"], c["diary_year"]): c
                for c in targets}
        for fut in as_completed(futs):
            try:
                det = fut.result()
                if det.get("ok"):
                    futs[fut]["details"] = det
            except Exception:
                pass


def _case_status(action, params, slug):
    cases, res = _run_list(action, params, slug, _parse_case_list)
    if cases is None:
        return {"success": False, "captcha": "ddddocr", "message": res.get("message"),
                "attempts": res.get("attempts"), "cases": []}
    _enrich(cases)
    return {"success": True, "captcha": "ddddocr", "count": len(cases),
            "attempts": res["attempts"], "cases": cases}


def _case_entry_from_details(det: dict, diary_no, year) -> dict:
    """Shape a captcha-free get_case_details result like a case-status list row,
    so the diary-no / CNR bypass returns the same structure as a real search."""
    fields = det.get("fields") or {}
    title = det.get("title") or ""
    pet = resp = None
    m = re.search(r"\d+/\d{4}\s+(.+?)\s+vs\.?\s+(.+?)\s+Case Details", title, re.I)
    if m:
        pet, resp = _clean(m.group(1)), _clean(m.group(2))
    status = fields.get("Status/Stage") or fields.get("Status")
    if status:                          # keep just the headline status word(s)
        status = _clean(status.split("(")[0])
    return {
        "diary_no": str(diary_no), "diary_year": str(year),
        "diary_number": f"{diary_no}/{year}",
        "case_no": fields.get("Case Number"),
        "petitioner": pet, "respondent": resp,
        "status": status, "details": det,
    }


def case_status_diary_no(diary_no, year):
    """CAPTCHA-FREE. The diary number + year is exactly the key that the
    captcha-free get_case_details wants, so we skip the captcha'd search entirely
    and fetch the full details directly (same result the search would enrich)."""
    diary, yr = _diary_year(diary_no, year)
    det = case_details(diary, yr)
    if det.get("ok"):
        return {"success": True, "count": 1, "captcha": "bypassed",
                "cases": [_case_entry_from_details(det, diary, yr)]}
    # not found via the direct path → fall back to the real (captcha'd) search
    return _case_status("get_case_status_diary_no",
                        {"diary_no": diary, "year": yr},
                        "case-status-diary-no")


_CNR_RE = re.compile(r"^([A-Z]{4}\d{2})(\d{6})(\d{4})$")


def _decode_cnr(cnr: str):
    """SCI CNR = SCIN01 + diary(6) + year(4) → (diary_no, year) or None."""
    m = _CNR_RE.match((cnr or "").strip().upper())
    if not m:
        return None
    return int(m.group(2)), m.group(3)


def case_status_case_no(case_type, case_no, year):
    return _case_status("get_case_status_case_no",
                        {"case_type": str(case_type), "case_no": str(case_no),
                         "year": str(year)}, "case-status-case-no")


def case_status_cnr(cnr_no):
    """CAPTCHA-FREE when the CNR decodes. An SCI CNR encodes the diary number +
    year (SCIN01 + diary + year), so we decode it and fetch details directly —
    no captcha. Falls back to the captcha'd search for any non-standard CNR."""
    decoded = _decode_cnr(str(cnr_no))
    if decoded:
        diary, year = decoded
        det = case_details(diary, year)
        if det.get("ok"):
            return {"success": True, "count": 1, "captcha": "bypassed",
                    "cases": [_case_entry_from_details(det, diary, year)]}
    return _case_status("get_case_status_cnr_no",
                        {"cnr_no": str(cnr_no)}, "case-status-cnr-number")


def case_status_aor_code(party_type, aor_code, year, case_status):
    return _case_status("get_case_status_aor_code",
                        {"party_type": str(party_type), "aor_code": str(aor_code),
                         "year": str(year), "case_status": str(case_status)},
                        "case-status-aor-code")


def case_status_party_name(party_type, party_name, year, party_status):
    return _case_status("get_case_status_party_name",
                        {"party_type": str(party_type), "party_name": str(party_name),
                         "year": str(year), "party_status": str(party_status)},
                        "case-status-party-name")


def case_status_court(court, state, bench, case_type, case_no, year, listing_date):
    return _case_status("get_case_status_court", {
        "case_status_court": str(court), "case_status_state": str(state),
        "case_status_bench": str(bench or ""), "case_status_case_type": str(case_type or ""),
        "case_no": str(case_no or ""), "year": str(year),
        "listing_date": str(listing_date),
    }, "case-status-court")


# ── public: DAILY ORDERS (4) ─────────────────────────────────────────────────

def _orders(action, params, slug):
    orders, res = _run_list(action, params, slug,
                            lambda h: _parse_doc_list(h, "order"))
    if orders is None:
        return {"success": False, "captcha": "ddddocr", "message": res.get("message"),
                "attempts": res.get("attempts"), "orders": []}
    return {"success": True, "captcha": "ddddocr", "count": len(orders),
            "attempts": res["attempts"], "orders": orders}


def orders_diary_no(diary_no, year):
    """CAPTCHA-FREE: order PDFs come straight from the case's judgement_orders
    tab (no search, no captcha). Falls back to the captcha'd search only if the
    direct lookup is unavailable."""
    docs = case_documents(diary_no, year)
    if docs.get("ok"):
        return {"success": True, "count": len(docs["orders"]),
                "captcha": "bypassed", "orders": docs["orders"]}
    return _orders("get_daily_order_diary_no",
                   {"diary_no": str(diary_no), "year": str(year)},
                   "daily-order-diary-no")


def orders_case_no(case_type, case_no, year):
    return _orders("get_daily_order_case_no",
                   {"case_type": str(case_type), "case_no": str(case_no),
                    "year": str(year)}, "daily-order-case-no")


def orders_rop_date(from_date, to_date):
    return _orders("get_daily_order_rop_date",
                   {"from_date": from_date, "to_date": to_date},
                   "daily-order-rop-date")


def orders_free_text(search_text, from_date, to_date):
    return _orders("get_daily_order_free_text",
                   {"search_text": search_text, "from_date": from_date,
                    "to_date": to_date}, "free-text-orders")


# ── public: JUDGEMENTS (5) ───────────────────────────────────────────────────

def _judgements(action, params, slug):
    js, res = _run_list(action, params, slug,
                        lambda h: _parse_doc_list(h, "judgement"))
    if js is None:
        return {"success": False, "captcha": "ddddocr", "message": res.get("message"),
                "attempts": res.get("attempts"), "judgements": []}
    return {"success": True, "captcha": "ddddocr", "count": len(js),
            "attempts": res["attempts"], "judgements": js}


def judgements_diary_no(diary_no, year):
    """CAPTCHA-FREE: judgement PDFs come straight from the case's
    judgement_orders tab (no search, no captcha)."""
    docs = case_documents(diary_no, year)
    if docs.get("ok"):
        return {"success": True, "count": len(docs["judgements"]),
                "captcha": "bypassed", "judgements": docs["judgements"]}
    return _judgements("get_judgements_diary_no",
                       {"diary_no": str(diary_no), "year": str(year)},
                       "judgements-diary-no")


def judgements_case_no(case_type, case_no, year):
    return _judgements("get_judgements_case_no",
                       {"case_type": str(case_type), "case_no": str(case_no),
                        "year": str(year)}, "judgements-case-no")


def judgements_judge(judge, from_date, to_date):
    return _judgements("get_judgements_judge",
                       {"judge": str(judge), "from_date": from_date,
                        "to_date": to_date}, "judgements-judge")


def judgements_judgement_date(from_date, to_date):
    return _judgements("get_judgements_judgement_date",
                       {"from_date": from_date, "to_date": to_date},
                       "judgements-judgement-date")


def judgements_free_text(search_text, from_date, to_date):
    return _judgements("get_judgements_free_text",
                       {"search_text": search_text, "from_date": from_date,
                        "to_date": to_date}, "free-text-judgements")


# ── dropdown options (for building searches) ─────────────────────────────────

_STATIC_OPTIONS = {
    "party_type": [{"value": "any", "label": "Any"},
                   {"value": "P", "label": "Petitioner"},
                   {"value": "R", "label": "Respondent"}],
    "status": [{"value": "P", "label": "Pending"},
               {"value": "D", "label": "Disposed"}],
    "court_type": [{"value": "4", "label": "Supreme Court"},
                   {"value": "1", "label": "High Court"},
                   {"value": "3", "label": "District Court"}],
}


def _select_options(slug: str, name: str) -> list:
    try:
        s = _session()
        html = s.get(f"{BASE}/{slug}/", timeout=25).text
        blk = re.search(r'<select[^>]*name="' + re.escape(name) + r'"[^>]*>(.*?)</select>',
                        html, re.S)
        if not blk:
            return []
        opts = re.findall(r'<option[^>]*value=[\'"]([^\'"]*)[\'"][^>]*>(.*?)</option>',
                          blk.group(1), re.S)
        return [{"value": v, "label": _clean(t)} for v, t in opts if v.strip()]
    except Exception:
        return []


def list_options() -> dict:
    """Dropdown choices needed to build a search (case types, judges, years,
    party types, statuses). Case types + judges are fetched live; the rest are
    static/derived."""
    return {
        "case_types": _select_options("case-status-case-no", "case_type"),
        "judges": _select_options("judgements-judge", "judge"),
        "years": [str(y) for y in range(2026, 1950, -1)],
        "party_type": _STATIC_OPTIONS["party_type"],
        "status": _STATIC_OPTIONS["status"],
        "court_type": _STATIC_OPTIONS["court_type"],
        "note": "aor_code has ~3648 values (use the numeric id); court search "
                "state/bench are dependent dropdowns loaded per court.",
    }
