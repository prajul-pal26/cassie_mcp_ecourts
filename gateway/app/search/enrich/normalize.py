"""E1 — Normalise the v4 case_history response into a stable CaseDetail shape.

The raw v4 payload (see case_data_*.txt fixtures) is a wide dict with ~80
fields, many empty or HTML-encoded. Clients defending against every shape
variation is the wrong place for that complexity. Move it here:

Public function:
    normalize_case_detail(raw: dict) -> dict

Returns a dict with the shape:
    {
      "cnr": "DLST020142782022",
      "case_no": "1134/2022" | "...",
      "case_type": "CT Cases",
      "filing": {"no": "14267", "year": "2022", "date_iso": "2022-07-08"},
      "registration": {"no": "1134", "year": "2022", "date_iso": "2022-07-13"},
      "parties": {
        "petitioners": [{"name": "...", "advocate": "...", "address": "..."}],
        "respondents": [{"name": "...", "advocate": "...", "address": "..."}],
      },
      "court": {
        "name": "Chief Metropolitan Magistrate, South, Saket",
        "code": "2", "establishment_code": "DLST02",
        "judge": "Chief Judicial Magistrate", "court_no": "20",
        "state_name": "Delhi", "district_name": "South",
        "state_code": "26", "district_code": "10",
        "level": "dc" | "hc",
      },
      "dates": {
        "filed_iso": "2022-07-08",
        "first_listed_iso": "2022-07-13",
        "last_listed_iso": "2025-11-20",
        "next_hearing_iso": "2026-04-13",
        "days_until_next_hearing": 22,
        "decided_iso": null,
      },
      "status": "pending" | "disposed" | "stayed" | "unknown",
      "purpose": "Arguments",
      "acts": [{"name": "Cr. P.C.", "sections": ["156(3)"]}, ...],
      "fir": {"no": "...", "year": "...", "police_station_code": "..."} | null,
      "hearing_history": [{"date_iso": "2025-11-20", "judge": "...",
                           "purpose": "Arguments", "business": "..."}],
      "orders": [{"number": 1, "date_iso": "2022-07-13",
                  "url": "https://...", "label": "COPY OF ORDER"}],
      "sensitive_matter": false,
      "data_completeness_pct": 78,
      "missing_fields": ["case_no_alt", "judge_name"],
      "raw_version": "NC4.0",
    }

Design rules:
  - Every field is null-safe. Missing input -> missing/null output, never KeyError.
  - Dates: parse from the v4 "YYYY-MM-DD" or "DD-MM-YYYY" or "0" forms,
    return ISO-8601 ("YYYY-MM-DD") or None.
  - HTML tables (act, historyOfCaseHearing, interimOrder) are parsed via
    BeautifulSoup since it's already a transitive dep.
  - data_completeness_pct is an honest signal of how much we got — the UI
    uses it to render "Limited information available" rather than blaming us.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any, Optional

from bs4 import BeautifulSoup  # transitive dep via EcourtFetch

log = logging.getLogger("gateway.normalize")


# ── Field weights for data_completeness_pct ──────────────────────────────
# Weighted so that critical fields (parties, court, status, next hearing)
# dominate over nice-to-have fields. Total weight = 100.
_FIELD_WEIGHTS = {
    "cnr": 8,
    "case_no": 6,
    "case_type": 4,
    "filing.date_iso": 4,
    "petitioners": 12,
    "respondents": 12,
    "court.name": 10,
    "court.state_name": 4,
    "court.district_name": 4,
    "status": 6,
    "dates.next_hearing_iso": 10,
    "dates.filed_iso": 4,
    "acts": 6,
    "hearing_history": 5,
    "orders": 5,
}


# ── HC court-name repair ──────────────────────────────────────────────────
# eCourts caseHistoryWebService.php for HC cases returns court_name as a
# database-internal string (e.g. "High Court cisdb_16012018" or
# "High Court cishclko") instead of a human label. We replace it with the
# canonical HC name from a state_code -> HC name lookup. The 25-HC list
# below is sourced from /api/metadata/states?court_type=hc on 2026-05-24.
_HC_STATE_NAMES: dict[str, str] = {
    "13": "Allahabad High Court",
    "1":  "Bombay High Court",
    "16": "Calcutta High Court",
    "6":  "Gauhati High Court",
    "29": "High Court for State of Telangana",
    "2":  "High Court of Andhra Pradesh",
    "18": "High Court of Chhattisgarh",
    "26": "High Court of Delhi",
    "17": "High Court of Gujarat",
    "5":  "High Court of Himachal Pradesh",
    "12": "High Court of Jammu and Kashmir",
    "7":  "High Court of Jharkhand",
    "3":  "High Court of Karnataka",
    "4":  "High Court of Kerala",
    "23": "High Court of Madhya Pradesh",
    "25": "High Court of Manipur",
    "21": "High Court of Meghalaya",
    "11": "High Court of Orissa",
    "22": "High Court of Punjab and Haryana",
    "9":  "High Court of Rajasthan",
    "24": "High Court of Sikkim",
    "20": "High Court of Tripura",
    "15": "High Court of Uttarakhand",
    "10": "Madras High Court",
    "8":  "Patna High Court",
}

# Pattern that distinguishes the broken upstream court_name from a legitimate
# one. Legit values are short proper-case names ("District and Sessions
# Judge", "Allahabad High Court"). Broken ones always start with
# "High Court " followed by an internal-DB identifier with digits or
# underscores.
_HC_BROKEN_COURT_NAME_RE = re.compile(
    r"^high\s+court\s+(cis|courtweb|db)[a-z0-9_]*$", re.IGNORECASE,
)

# UPPERCASE compact state names returned in `caseState` for HC envelopes.
# Mapped to their spaced display form so the court block doesn't show
# "UTTARPRADESH". Anything not listed is left for `_clean()` to title-case.
_CASE_STATE_DISPLAY: dict[str, str] = {
    "UTTARPRADESH": "Uttar Pradesh",
    "MADHYAPRADESH": "Madhya Pradesh",
    "ANDHRAPRADESH": "Andhra Pradesh",
    "ARUNACHALPRADESH": "Arunachal Pradesh",
    "HIMACHALPRADESH": "Himachal Pradesh",
    "TAMILNADU": "Tamil Nadu",
    "WESTBENGAL": "West Bengal",
    "JAMMUKASHMIR": "Jammu and Kashmir",
    "JAMMUANDKASHMIR": "Jammu and Kashmir",
    "PUNJABANDHARYANA": "Punjab and Haryana",
    "ASSAMNORTHEAST": "Assam / NE States",
    "DELHI": "Delhi",
}


def _hc_display_state(value: Optional[str]) -> Optional[str]:
    """Best-effort mapping for `caseState` -> readable state name."""
    if not value:
        return None
    key = str(value).strip().upper().replace(" ", "")
    if key in _CASE_STATE_DISPLAY:
        return _CASE_STATE_DISPLAY[key]
    # Fallback: title-case (works for single-word state names like
    # "MAHARASHTRA" -> "Maharashtra", "KERALA" -> "Kerala").
    return str(value).strip().title() or None


# ── Date parsing ──────────────────────────────────────────────────────────

_DATE_RE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DATE_RE_DMY = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")


def _parse_date(val: Any) -> Optional[str]:
    """Parse a v4 date field into ISO-8601 (YYYY-MM-DD) or None.

    Accepts: "2022-07-08", "08-07-2022", "0", "None", None, "".
    Rejects anything else."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("none", "null", "0"):
        return None
    m = _DATE_RE_ISO.match(s)
    if m:
        y, mo, d = m.groups()
        try:
            return date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            return None
    m = _DATE_RE_DMY.match(s)
    if m:
        d, mo, y = m.groups()
        try:
            return date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            return None
    return None


def _days_between(iso_a: Optional[str], iso_b: Optional[str]) -> Optional[int]:
    """Whole days from iso_a to iso_b (positive if b>a). None if either missing."""
    if not iso_a or not iso_b:
        return None
    try:
        a = date.fromisoformat(iso_a)
        b = date.fromisoformat(iso_b)
        return (b - a).days
    except ValueError:
        return None


# ── Party parsing ─────────────────────────────────────────────────────────

# The v4 payload's `petNameAdd` / `resNameAdd` fields look like:
#   "1) HITACHI PAYMENT SERVICES PVT. LTD.<br />&nbsp;&nbsp;Advocate - AMIT TANWAR"
# Multiple numbered entries separated by "<br />" or numbered "2)" / "3)".
_PARTY_NUM_RE = re.compile(r"^\d+\)\s*", re.MULTILINE)
_ADV_RE = re.compile(r"Advocate\s*-\s*(.+?)(?:<br|$)", re.IGNORECASE)


def _parse_party_block(html: Optional[str], fallback_name: Optional[str],
                       fallback_advocate: Optional[str]) -> list[dict]:
    """Extract a list of {name, advocate, address} from an HTML party block.

    Falls back to the flat fields (pet_name / pet_adv) if the HTML block is
    missing or empty — every search response has *something*, and we'd
    rather show one party than nothing."""
    if not html or not isinstance(html, str):
        if fallback_name:
            return [{"name": _clean(fallback_name),
                     "advocate": _clean(fallback_advocate) or None,
                     "address": None}]
        return []
    # Replace <br/> with newlines, then strip remaining tags.
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"<[^>]+>", "", text).strip()
    if not text:
        if fallback_name:
            return [{"name": _clean(fallback_name),
                     "advocate": _clean(fallback_advocate) or None,
                     "address": None}]
        return []
    # Split by numbered prefix "1) " / "2) " etc.
    parts = _PARTY_NUM_RE.split(text)
    parts = [p.strip() for p in parts if p.strip()]
    out = []
    for p in parts:
        # Take first line as name, look for Advocate marker
        lines = [ln.strip() for ln in p.split("\n") if ln.strip()]
        if not lines:
            continue
        name = lines[0]
        adv = None
        addr_lines = []
        for ln in lines[1:]:
            m = _ADV_RE.search(ln)
            if m:
                adv = m.group(1).strip()
            else:
                addr_lines.append(ln)
        out.append({
            "name": _clean(name),
            "advocate": _clean(adv) if adv else None,
            "address": _clean(" ".join(addr_lines)) if addr_lines else None,
        })
    if not out and fallback_name:
        out = [{"name": _clean(fallback_name),
                "advocate": _clean(fallback_advocate) or None,
                "address": None}]
    return out


def _clean(s: Optional[str]) -> Optional[str]:
    """Tidy whitespace + decode common HTML entities."""
    if not s:
        return None
    s = str(s).strip()
    s = re.sub(r"\s+", " ", s)
    if not s:
        return None
    return s


# ── Acts/Sections from HTML table ─────────────────────────────────────────

def _parse_acts_table(html: Optional[str], flat_acts: list[str],
                      flat_sections: list[str]) -> list[dict]:
    """Extract [{name, sections: [...]}] from the v4 `act` HTML table.

    Falls back to the flat under_act{1..4} / under_sec{1..4} fields if the
    HTML is unparseable. Returns empty list if both sources empty."""
    rows: list[tuple[str, str]] = []
    if html and isinstance(html, str):
        try:
            soup = BeautifulSoup(html, "html.parser")
            for tr in soup.select("tbody tr"):
                tds = tr.find_all("td")
                if len(tds) >= 2:
                    act = _clean(tds[0].get_text(" ", strip=True))
                    sec = _clean(tds[1].get_text(" ", strip=True))
                    if act:
                        rows.append((act, sec or ""))
        except Exception as e:  # defensive — never let HTML parse crash
            log.debug("acts table parse failed: %r", e)

    # Merge in flat fields if HTML didn't yield anything
    if not rows:
        for a, s in zip(flat_acts, flat_sections):
            a_c = _clean(str(a)) if a and str(a) not in ("0", "None") else None
            s_c = _clean(str(s)) if s else None
            if a_c:
                rows.append((a_c, s_c or ""))

    # Collapse by act name → list of sections
    collapsed: dict[str, list[str]] = {}
    for act, sec in rows:
        bucket = collapsed.setdefault(act, [])
        if sec and sec not in bucket:
            bucket.append(sec)
    return [{"name": k, "sections": v} for k, v in collapsed.items()]


# ── Hearing history (HTML table OR v4 JSON array) ─────────────────────────

# YYYYMMDD parser used by the v4-array branch of _parse_hearings. The
# `nextdate` / `n_dt` fields arrive as compact strings like "20240808".
_YYYYMMDD_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def _parse_compact_date(val: Any) -> Optional[str]:
    """Convert YYYYMMDD -> ISO YYYY-MM-DD, or fall through to _parse_date."""
    if val is None:
        return None
    s = str(val).strip()
    m = _YYYYMMDD_RE.match(s)
    if m:
        y, mo, d = m.groups()
        try:
            return date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            return None
    return _parse_date(s)


def _parse_hearings(payload: Any) -> list[dict]:
    """Parse the historyOfCaseHearing field.

    eCourts v4 returns this in two shapes depending on which endpoint
    served the case:
      1. **HTML table** — historic shape, columns: Judge | Business on
         Date | Hearing Date | Purpose.
      2. **JSON array** — current mobile-API shape (both DC and HC),
         e.g. `[{judge_name, todays_date, nextdate, purpose,
         businessStatus, ...}]`.

    The HTML branch was the only one normalised before, which silently
    dropped every hearing on JSON-array responses (most live cases) —
    leaving the case-detail page's Hearing History section empty even
    when the upstream had 12+ hearings to show.
    """
    if isinstance(payload, list):
        return _parse_hearings_array(payload)
    if isinstance(payload, str) and payload:
        return _parse_hearings_html(payload)
    return []


def _parse_hearings_array(rows: list) -> list[dict]:
    """Map v4 JSON-array hearing entries to the normalised shape.

    Per-entry fields used (from observed payloads):
      judge_name   -> judge (HTML-decode `&amp;` -> `&` etc.)
      todays_date  -> hearing_date_iso (date the hearing took place)
      nextdate     -> business_date_iso (next-listing date, YYYYMMDD)
      purpose      -> purpose (e.g. "Hearing", "Arguments", "Disposed")

    `businessStatus` is intentionally ignored — observed values include
    cryptic compounds like "DisposedP" alongside the real `purpose`, and
    surfacing them produces noise like "Hearing · DisposedP". The
    `purpose` field already carries the meaningful per-hearing label.
    """
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        judge = _decode_html_entities(_clean(row.get("judge_name")))
        hearing_iso = _parse_date(row.get("todays_date"))
        business_iso = _parse_compact_date(row.get("nextdate") or row.get("n_dt"))
        purpose = _clean(row.get("purpose")) or "Hearing"
        # Skip entirely-empty rows (no judge, no date, generic purpose).
        if not (judge or hearing_iso or business_iso):
            continue
        out.append({
            "judge": judge,
            "business_date_iso": business_iso,
            "hearing_date_iso": hearing_iso,
            "purpose": purpose,
        })
    return out


# Lightweight HTML-entity decoder — the hearings array often returns
# `judge_name` with raw entities like "District &amp; Sessions Judge"
# (upstream serialises HTML-safe). We only handle the common entities;
# anything unusual passes through unchanged.
_HTML_ENTITY_MAP = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
    "&nbsp;": " ",
}


def _decode_html_entities(s: Optional[str]) -> Optional[str]:
    if not s:
        return s
    for ent, ch in _HTML_ENTITY_MAP.items():
        if ent in s:
            s = s.replace(ent, ch)
    return s


def _parse_hearings_html(html: str) -> list[dict]:
    """Parse the legacy HTML-table shape (kept for backward compat)."""
    out: list[dict] = []
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.select("tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            judge = _clean(tds[0].get_text(" ", strip=True))
            # Second column has an <a> with the business date as link text
            business_link = tds[1].find("a")
            business_date_text = _clean(business_link.get_text(strip=True)) \
                if business_link else _clean(tds[1].get_text(" ", strip=True))
            hearing_date_text = _clean(tds[2].get_text(" ", strip=True))
            purpose = _clean(tds[3].get_text(" ", strip=True))
            out.append({
                "judge": judge,
                "business_date_iso": _parse_date(business_date_text),
                "hearing_date_iso": _parse_date(hearing_date_text),
                "purpose": purpose,
            })
    except Exception as e:
        log.debug("hearing history HTML parse failed: %r", e)
    return out


# ── Orders / interim orders ───────────────────────────────────────────────

def _parse_orders(interim_payload: Any, final_payload: Any) -> list[dict]:
    """Extract order rows from interimOrder + finalOrder.

    eCourts v4 returns these in two shapes:
      1. **HTML table** — historic shape. Row columns:
         number | date | <a>label</a> with a public PDF URL.
      2. **JSON array** — current mobile-API shape, e.g.
         `[{appFlag, bilingual_flag, cCode, caseno, court_code, dist_cd,
            filename, order_date1f, order_details, order_id, state_cd}]`.
         There is no public URL here — `filename` is a relative eCourts
         path that has to be resolved via the gateway's PDF endpoint
         (`display_pdf_new.php`) into a tokenised download URL on click.

    The HTML branch was the only one normalised before, which silently
    dropped every order on JSON-array responses (most live cases) — the
    case-detail page's Orders section was empty even when the upstream
    had a final judgment to show.

    Each output dict carries the resolution coords (cCode, court_code,
    state_cd, dist_cd, caseno, filename, appFlag, order_id) so a
    follow-up gateway endpoint can resolve `filename` -> pdf_url on
    demand. `url` is the direct PDF URL when the HTML branch produced
    one; null when the array branch needs deferred resolution.
    """
    out: list[dict] = []
    for kind, payload in (("interim", interim_payload), ("final", final_payload)):
        if isinstance(payload, list):
            out.extend(_parse_orders_array(kind, payload))
        elif isinstance(payload, str) and payload:
            out.extend(_parse_orders_html(kind, payload))
    return out


def _parse_orders_array(kind: str, rows: list) -> list[dict]:
    """Map v4 JSON-array order entries into the normalised shape.

    `filename` is the eCourts-internal path (e.g.
    "/orders/2026/260400007212026_1.pdf"). It is NOT a fetchable URL —
    callers wanting the PDF must POST the resolution coords to the
    gateway's `display_pdf` endpoint to get a tokenised pdf_url first.
    """
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # order_id is the upstream's per-case sequence (string "1", "2", ...).
        order_id_raw = row.get("order_id")
        try:
            number = int(str(order_id_raw)) if order_id_raw not in (None, "") else None
        except ValueError:
            number = None
        # Final orders use `order_date1f`, interim orders use `order_date1`
        # (observed across UPKN / DLST envelopes — same upstream, two
        # field names depending on `kind`). Read whichever is present.
        date_iso = _parse_date(
            row.get("order_date1f") or row.get("order_date1")
        )
        label = _clean(row.get("order_details")) or f"{kind.title()} order"
        filename = _clean(row.get("filename"))
        out.append({
            "kind": kind,
            "number": number,
            "date_iso": date_iso,
            "url": None,  # deferred — resolve via gateway PDF endpoint
            "label": label,
            # Coords needed to resolve `filename` -> pdf_url. The
            # frontend hands these back to /api/case/<cnr>/order/<id>/url
            # when the user clicks "View PDF".
            "filename": filename,
            "order_id": str(order_id_raw) if order_id_raw not in (None, "") else None,
            "caseno": _clean(row.get("caseno")),
            "court_code": _to_str_or_none(row.get("court_code")),
            "cCode": _to_str_or_none(row.get("cCode")),
            "state_cd": _to_str_or_none(row.get("state_cd")),
            "dist_cd": _to_str_or_none(row.get("dist_cd")),
            "appFlag": _to_str_or_none(row.get("appFlag")) or "1",
        })
    return out


def _parse_orders_html(kind: str, html: str) -> list[dict]:
    """Parse the legacy HTML-table shape (URL already populated)."""
    out: list[dict] = []
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.select("tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            num = _clean(tds[0].get_text(" ", strip=True))
            try:
                num_int: Optional[int] = int(re.sub(r"\D", "", num or ""))
            except (TypeError, ValueError):
                num_int = None
            date_iso = _parse_date(_clean(tds[1].get_text(" ", strip=True)))
            link = tds[2].find("a")
            url = link.get("href") if link else None
            label = _clean(link.get_text(" ", strip=True)) if link else \
                _clean(tds[2].get_text(" ", strip=True))
            out.append({
                "kind": kind,
                "number": num_int,
                "date_iso": date_iso,
                "url": url,
                "label": label,
                # HTML branch carries the URL inline; the resolver
                # fields below stay null. (Callers must tolerate them.)
                "filename": None,
                "order_id": None,
                "caseno": None,
                "court_code": None,
                "cCode": None,
                "state_cd": None,
                "dist_cd": None,
                "appFlag": None,
            })
    except Exception as e:
        log.debug("orders HTML parse failed (%s): %r", kind, e)
    return out


def _to_str_or_none(v: Any) -> Optional[str]:
    """Coerce a v4 numeric/string scalar into a clean string, or None."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# ── Status inference ──────────────────────────────────────────────────────

def _infer_status(raw: dict, decided_iso: Optional[str]) -> str:
    """Return 'pending' | 'disposed' | 'stayed' | 'unknown'.

    The v4 payload doesn't carry an explicit status string for case_history,
    but `date_of_decision` being populated is a reliable disposed signal.
    `disp_nature` of 0 means undecided. We also peek at the hearing-history
    HTML for a 'Pending' / 'Disposed' marker since the v4 search results
    embed that in the URL onclick handlers."""
    if decided_iso:
        return "disposed"
    disp = str(raw.get("disp_nature") or "0").strip()
    if disp and disp != "0":
        return "disposed"
    # Peek at hearing-history HTML for an explicit 'Disposed' / 'Pending'
    hist = raw.get("historyOfCaseHearing") or ""
    if isinstance(hist, str):
        if "'Disposed'" in hist or '"Disposed"' in hist:
            return "disposed"
        if "'Stayed'" in hist or '"Stayed"' in hist:
            return "stayed"
        if "'Pending'" in hist or '"Pending"' in hist:
            return "pending"
    # If we have a future next-list date, treat as pending.
    next_iso = _parse_date(raw.get("date_next_list"))
    if next_iso:
        return "pending"
    return "unknown"


# ── Completeness scoring ──────────────────────────────────────────────────

def _completeness(out: dict) -> tuple[int, list[str]]:
    """Return (pct, missing_field_names). Pure function of the normalised shape."""
    earned = 0
    missing: list[str] = []

    def has(path: str) -> bool:
        # Resolve dotted paths against `out`. Returns True iff value is truthy.
        cur: Any = out
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return False
        if isinstance(cur, list):
            return len(cur) > 0
        return bool(cur)

    for path, weight in _FIELD_WEIGHTS.items():
        if path == "petitioners":
            ok = bool(out.get("parties", {}).get("petitioners"))
        elif path == "respondents":
            ok = bool(out.get("parties", {}).get("respondents"))
        else:
            ok = has(path)
        if ok:
            earned += weight
        else:
            missing.append(path)
    return earned, missing


# ── Public entrypoint ─────────────────────────────────────────────────────

def normalize_case_detail(raw: dict, *,
                          court_level: Optional[str] = None,
                          today: Optional[date] = None) -> dict:
    """Convert a raw v4 case_history dict into the stable CaseDetail shape.

    `today` is an injectable hook for deterministic days_until_next_hearing
    in tests.
    """
    if not isinstance(raw, dict):
        return _empty_detail()

    # eCourts v4 caseHistoryWebService.php wraps the actual case fields
    # inside a "history" sub-object alongside a fresh JWT token. Unwrap so
    # the rest of this function can treat the case fields as top-level.
    # Idempotent: if the inner dict is passed directly (no "history" key
    # or it's not a dict), this is a no-op.
    if isinstance(raw.get("history"), dict):
        raw = raw["history"]

    today = today or date.today()

    # ── identifiers ──
    cnr = _clean(raw.get("cino")) or _clean(raw.get("cnr"))
    case_no = _clean(raw.get("case_no")) or _clean(raw.get("reg_no"))
    case_type = _clean(raw.get("type_name")) or _clean(raw.get("regcase_type"))

    # ── dates ──
    filed_iso = _parse_date(raw.get("date_of_filing"))
    reg_iso = _parse_date(raw.get("dt_regis"))
    first_listed_iso = _parse_date(raw.get("date_first_list"))
    last_listed_iso = _parse_date(raw.get("date_last_list"))
    next_hearing_iso = _parse_date(raw.get("date_next_list"))
    decided_iso = _parse_date(raw.get("date_of_decision"))

    days_until = _days_between(today.isoformat(), next_hearing_iso) \
        if next_hearing_iso else None

    # ── parties ──
    petitioners = _parse_party_block(
        raw.get("petNameAdd"),
        fallback_name=raw.get("pet_name") or raw.get("petparty_name"),
        fallback_advocate=raw.get("pet_adv"),
    )
    respondents = _parse_party_block(
        raw.get("resNameAdd"),
        fallback_name=raw.get("res_name") or raw.get("resparty_name"),
        fallback_advocate=raw.get("res_adv"),
    )

    # ── court ──
    court_name = _clean(raw.get("court_name"))
    court_code = _clean(str(raw.get("court_code"))) if raw.get("court_code") is not None else None
    est_code = _clean(raw.get("est_code"))
    judge = _clean(raw.get("desgname"))
    court_no = _clean(str(raw.get("court_no") or raw.get("courtno") or ""))
    state_name = _clean(raw.get("state_name"))
    district_name = _clean(raw.get("district_name"))
    state_code = _clean(str(raw.get("state_code"))) if raw.get("state_code") is not None else None
    district_code = _clean(str(raw.get("district_code"))) if raw.get("district_code") is not None else None

    # Court level: explicit override, then est_code prefix heuristic, then default
    inferred_level = court_level
    if not inferred_level and est_code:
        # Establishment codes like 'DLST02' are DC; HC codes typically use 'HC'.
        inferred_level = "hc" if "HC" in est_code.upper() else "dc"
    inferred_level = inferred_level or "dc"

    # HC envelopes need additional repair: the upstream returns
    # court_name as a DB-internal label ("High Court cisdb_16012018",
    # "High Court cishclko"), state_name/district_name as empty strings,
    # but does provide `state_code` and `caseState`. Replace the broken
    # court_name with the canonical HC name from the state_code lookup,
    # and infer state_name from `caseState` where available.
    if inferred_level == "hc":
        if (
            court_name is not None
            and _HC_BROKEN_COURT_NAME_RE.match(court_name)
            and state_code in _HC_STATE_NAMES
        ):
            court_name = _HC_STATE_NAMES[state_code]
        if not state_name:
            state_name = _hc_display_state(raw.get("caseState"))

    # ── acts ──
    flat_acts = [raw.get(f"under_act{i}") for i in range(1, 5)]
    flat_secs = [raw.get(f"under_sec{i}") for i in range(1, 5)]
    acts = _parse_acts_table(raw.get("act"), flat_acts, flat_secs)

    # ── hearings + orders ──
    hearings = _parse_hearings(raw.get("historyOfCaseHearing"))
    orders = _parse_orders(raw.get("interimOrder"), raw.get("finalOrder"))

    # ── FIR (optional) ──
    fir_no = _clean(str(raw.get("fir_no") or "").strip())
    fir_year = _clean(str(raw.get("fir_year") or "").strip())
    fir = None
    if fir_no and fir_no not in ("0", ""):
        fir = {
            "no": fir_no,
            "year": fir_year if fir_year and fir_year != "0" else None,
            "police_station_code": _clean(str(raw.get("police_st_code") or "")) or None,
        }

    # ── status ──
    status = _infer_status(raw, decided_iso)

    out = {
        "cnr": cnr,
        "case_no": case_no,
        "case_type": case_type,
        "filing": {
            "no": _clean(raw.get("fil_no")),
            "year": _clean(raw.get("fil_year")),
            "date_iso": filed_iso,
        },
        "registration": {
            "no": _clean(raw.get("reg_no")),
            "year": _clean(raw.get("reg_year")),
            "date_iso": reg_iso,
        },
        "parties": {
            "petitioners": petitioners,
            "respondents": respondents,
        },
        "court": {
            "name": court_name,
            "code": court_code,
            "establishment_code": est_code,
            "judge": judge,
            "court_no": court_no or None,
            "state_name": state_name,
            "district_name": district_name,
            "state_code": state_code,
            "district_code": district_code,
            "level": inferred_level,
        },
        "dates": {
            "filed_iso": filed_iso,
            "first_listed_iso": first_listed_iso,
            "last_listed_iso": last_listed_iso,
            "next_hearing_iso": next_hearing_iso,
            "days_until_next_hearing": days_until,
            "decided_iso": decided_iso,
        },
        "status": status,
        "purpose": _clean(raw.get("purpose_name")),
        "acts": acts,
        "fir": fir,
        "hearing_history": hearings,
        "orders": orders,
        "sensitive_matter": False,  # set by gateway.sensitivity afterward
        "raw_version": _clean(raw.get("version")),
    }

    pct, missing = _completeness(out)
    out["data_completeness_pct"] = pct
    out["missing_fields"] = missing
    return out


def _empty_detail() -> dict:
    """Shape stub returned when raw input is not a dict — keeps the contract
    stable even on adversarial inputs."""
    return {
        "cnr": None, "case_no": None, "case_type": None,
        "filing": {"no": None, "year": None, "date_iso": None},
        "registration": {"no": None, "year": None, "date_iso": None},
        "parties": {"petitioners": [], "respondents": []},
        "court": {"name": None, "code": None, "establishment_code": None,
                  "judge": None, "court_no": None,
                  "state_name": None, "district_name": None,
                  "state_code": None, "district_code": None, "level": "dc"},
        "dates": {"filed_iso": None, "first_listed_iso": None,
                  "last_listed_iso": None, "next_hearing_iso": None,
                  "days_until_next_hearing": None, "decided_iso": None},
        "status": "unknown", "purpose": None, "acts": [], "fir": None,
        "hearing_history": [], "orders": [],
        "sensitive_matter": False, "data_completeness_pct": 0,
        "missing_fields": list(_FIELD_WEIGHTS.keys()),
        "raw_version": None,
    }
