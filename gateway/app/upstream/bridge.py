"""
bridge — internal adapter mapping the gateway's search/detail calls onto the
v4 eCourts upstream (`EcourtFetchV4`), preserving snake_case request field
names and returning flat lists of case dicts.

Only the surface the gateway actually uses is kept here:

  - bridge_party        -> party-name search      (find jobs, mode="party")
  - bridge_advocate     -> advocate-name search   (find jobs, mode="advocate")
  - bridge_case_detail  -> single-CNR case detail  (/api/case/<cnr>, batch)
  - is_valid_cnr / _infer_court_type_from_cnr / _get_v4  (helpers used by routers)

The legacy v3 client (`EcourtFetch`) and the FIR / filing-number / case-number
/ case-type / act bridges were dropped in the FastAPI build — no route or
worker referenced them.

Thread / lifecycle
------------------
- One `EcourtFetchV4` instance per `court_type`, lazy-bootstrapped on first
  use, reused for the lifetime of the process.
- `_call` inside `EcourtFetchV4` already implements WAF (Sucuri 405)
  detection with 60s cool-down + token refresh, plus 5xx retries with
  exponential backoff. The bridge does NOT wrap in another retry layer.
"""
import logging
import os as _os
import re as _re
import threading

from app.upstream.ecourts_v4 import EcourtFetchV4  # v4 upstream

log = logging.getLogger("upstream.bridge")


# ── Module-level state ─────────────────────────────────────────────────

_V4_LANES: dict = {}                    # court_type -> list[EcourtFetchV4] (proxy lanes)
_LANE_IDX: dict = {}                     # court_type -> round-robin counter
_CLIENTS_LOCK = threading.Lock()


_COMPOSITE_CODE_RE = _re.compile(r'[,\-]')

_BAR_CODE_SEP_RE = _re.compile(r'[/\\\-\s]+')


def _normalize_court_type(v) -> str:
    """Case/whitespace-tolerant court_type parse — 'DC', 'Hc', ' hc ' all
    resolve the same as 'dc'/'hc'. Anything else (including missing) -> 'dc'.
    Centralizing this means every bridge_* function and _get_v4 agree."""
    s = str(v or "").strip().lower()
    return s if s in ("dc", "hc") else "dc"


def _normalize_year(v) -> str:
    """Expand a 2-digit year to 4 digits (e.g. '26' -> '2026', '98' -> '1998').
    eCourts case/bar-registration data doesn't predate ~1950, so a 00-79 /
    80-99 pivot unambiguously covers every real record without needing the
    current date. 4-digit (or any other) input passes through unchanged —
    the upstream itself is the source of truth for what's actually valid."""
    s = str(v or "").strip()
    if s.isdigit() and len(s) == 2:
        n = int(s)
        return f"20{s}" if n <= 79 else f"19{s}"
    return s


_STATUS_MAP = {"pending": "Pending", "disposed": "Disposed", "both": "Both"}


def _normalize_status(v) -> str:
    """Case/whitespace-tolerant pendingDisposed parse. The upstream's PHP
    endpoints compare this against exact-case literals ('Pending'/'Disposed'/
    'Both'); a caller-typed 'pending' or 'BOTH' would otherwise silently
    fail to match and get treated as an unrecognized filter. Unrecognized
    input defaults to 'Both' (the existing default), same as before."""
    s = str(v or "").strip().lower()
    return _STATUS_MAP.get(s, "Both")


def _safe_court_code(v) -> int:
    """Parse a court_code value robustly.

    The eCourts fallback JSON encodes multi-establishment complexes as a
    comma-joined string (e.g. "2,3,4,5,6,7" for Agra District Court Complex).
    The frontend's fallback path can leak that comma-joined original into
    the per-task `court_code` field, which `int(...)` then crashes on,
    failing every task in the fan-out.

    Real district data has ALSO been observed returning hyphen-joined
    establishment-suffix codes (e.g. "1260001-2" for Delhi North-East,
    state_code=26/district_code=1) when `_extract_court_complexes()`
    falls back to `complex_code` because `court_code`/`est_code` are
    absent from the upstream metadata response — confirmed live in
    production: this fed a real circuit-breaker cascading-failure incident
    (see reliability/gateway.py's UpstreamFailure taxonomy), since the
    ValueError this used to raise on a hyphenated code was, at the time,
    indistinguishable from a genuine upstream failure.

    This helper trims, splits on commas AND hyphens, and returns the first
    integer segment. For well-formed inputs ("1", "9", 14) it returns the
    same int. A leading-sign case ("-5") is handled correctly: the empty
    segment before the hyphen is skipped, and "5" parses.

    Raises ValueError ONLY when no segment parses as int.
    """
    s = str(v).strip()
    if not s:
        raise ValueError("empty court_code")
    for segment in _COMPOSITE_CODE_RE.split(s):
        segment = segment.strip()
        if not segment:
            continue
        try:
            return int(segment)
        except ValueError:
            continue
    raise ValueError(f"no integer segment in court_code={v!r}")


def _new_client(court_type: str, proxy_url) -> EcourtFetchV4:
    """Build + bootstrap one v4 client, optionally bound to a proxy URL."""
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    cli = EcourtFetchV4(court_type=court_type, proxies=proxies, timeout=60)
    try:
        cli.bootstrap()
    except Exception as e:
        # bootstrap can partially-fail (one of the 3 calls 500s) but still set
        # a token; also a lane self-heals on first real call. Log and continue.
        log.warning("v4 bootstrap (%s, proxy=%s) warning: %s",
                    court_type, bool(proxy_url), e)
    return cli


def _build_lanes(court_type: str) -> list:
    """One client per proxy lane (each rotates IPs independently). No proxies
    configured → a single direct client (original behaviour). Lanes are
    bootstrapped in parallel to keep first-use latency low."""
    from app.upstream import proxy_pool
    urls = proxy_pool.lane_proxy_urls()
    if not urls:
        return [_new_client(court_type, None)]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(len(urls), 16)) as ex:
        return list(ex.map(lambda u: _new_client(court_type, u), urls))


def _get_v4(court_type: str, proxies=None) -> EcourtFetchV4:
    """Return a bootstrapped v4 client, round-robin across proxy lanes.

    With Webshare proxies configured, there are N independent lanes (each an
    IP-rotating session); calls are spread across them for throughput. Without
    proxies, a single direct client. An explicit `proxies` arg forces a
    one-off direct client (kept for API compatibility; unused in practice)."""
    court_type = _normalize_court_type(court_type)
    if proxies:
        return EcourtFetchV4(court_type=court_type, proxies=proxies, timeout=60)
    with _CLIENTS_LOCK:
        lanes = _V4_LANES.get(court_type)
        if not lanes:
            lanes = _build_lanes(court_type)
            _V4_LANES[court_type] = lanes
            _LANE_IDX[court_type] = 0
        i = _LANE_IDX[court_type] % len(lanes)
        _LANE_IDX[court_type] = i + 1
        return lanes[i]


def reset_lanes(court_type=None) -> None:
    """Drop cached lanes so the next call rebuilds them with the current proxy
    set (call after adding/removing a proxy)."""
    with _CLIENTS_LOCK:
        if court_type:
            _V4_LANES.pop(court_type, None)
            _LANE_IDX.pop(court_type, None)
        else:
            _V4_LANES.clear()
            _LANE_IDX.clear()


# ── Response normaliser (v4 envelope → flat cases) ─────────────────────

def _dbg(msg: str, *args) -> None:
    """Single-place gate for diagnostic logging. Enabled by ECOURTS_DEBUG_LOG=1."""
    if _os.environ.get("ECOURTS_DEBUG_LOG") == "1":
        log.warning("[ECOURTS_DBG] " + msg, *args)


def _normalize_cases(raw) -> list:
    """Flatten v4's keyed envelope into a flat list of case dicts.

    v4 returns:
      {
        "0": {"court_code": "1", "establishment_name": "...", "caseNos": [...]},
        "1": {...},
        "token": "...",
        "no_of_establishments": 1
      }

    OR for "no results"-style server errors:
      {"status": "N", "msg": "..."}

    Callers expect a flat array. We copy each numeric-keyed block's
    `establishment_name`/`court_code` onto every case in its `caseNos[]`
    so downstream UI can group results.
    """
    if not isinstance(raw, dict):
        _dbg("normalize: raw is not dict (type=%s) → returning []", type(raw).__name__)
        return []
    if raw.get("status") == "N":
        _dbg("normalize: status=N msg=%r → returning []", raw.get("msg"))
        return []
    flat = []
    numeric_buckets = 0
    for k, v in raw.items():
        if not (k.isdigit() and isinstance(v, dict)):
            continue
        numeric_buckets += 1
        est_name = v.get("establishment_name")
        est_code = v.get("court_code")
        cases = v.get("caseNos") or []
        for c in cases:
            if not isinstance(c, dict):
                continue
            if est_name and "establishment_name" not in c:
                c["establishment_name"] = est_name
            if est_code and "establishment_code" not in c:
                c["establishment_code"] = est_code
            flat.append(c)
    _dbg(
        "normalize: buckets=%d cases_returned=%d top_keys=%s",
        numeric_buckets, len(flat), list(raw.keys())[:20],
    )
    return flat


# ── Bridge handlers ────────────────────────────────────────────────────

def bridge_party(body: dict, proxies=None) -> list:
    """Translate a party-search body, call v4, return a flat case list.

    Body fields:
      state_code, district_code, court_code   (required)
      party_name                              (required, min 3 chars)
      year                                    (optional, default "")
      pending_disposed                        (optional, default "Both")
      court_type                              (optional, default "dc")
    """
    court_type = body.get("court_type", "dc")
    _dbg(
        "bridge_party IN: state=%r dist=%r court=%r party=%r year=%r status=%r ct=%s",
        body.get("state_code"), body.get("district_code"),
        body.get("court_code"), body.get("party_name"),
        body.get("year"), body.get("pending_disposed"), court_type,
    )
    cli = _get_v4(court_type, proxies=proxies)
    raw = cli.search_by_party_name(
        state_code=int(body["state_code"]),
        dist_code=int(body["district_code"]),
        court_code=_safe_court_code(body["court_code"]),
        pet_name=str(body["party_name"]),
        year=_normalize_year(body.get("year", "") or ""),
        pendingDisposed=_normalize_status(body.get("pending_disposed", "Both")),
    )
    cases = _normalize_cases(raw)
    _dbg("bridge_party OUT: %d cases for party=%r court=%r",
         len(cases), body.get("party_name"), body.get("court_code"))
    return cases


def bridge_advocate(body: dict, proxies=None, mode: str = "name") -> list:
    """Bridge advocate searches (by name or by bar registration / barcode).

    mode="name":
      Body fields: state_code, district_code, court_code, advocate_name,
                   year?, pending_disposed?, court_type?
    mode="barcode":
      Body fields: state_code, district_code, court_code, barcode,
                   pending_disposed?, court_type?
      (v4 uses a single endpoint with `checkedSearchByRadioValue: "2"` to
      denote barcode-mode; the value is passed as `advocateName`.)
    """
    court_type = body.get("court_type", "dc")
    cli = _get_v4(court_type, proxies=proxies)
    if mode == "barcode":
        adv_value = str(body.get("barcode") or "").strip()
        radio = "2"
    else:
        adv_value = str(body.get("advocate_name") or "").strip()
        radio = "1"
    raw = cli.search_by_advocate(
        state_code=int(body["state_code"]),
        dist_code=int(body["district_code"]),
        court_code=_safe_court_code(body["court_code"]),
        advocateName=adv_value,
        checkedSearchByRadioValue=radio,
        pendingDisposed=_normalize_status(body.get("pending_disposed", "Both")),
    )
    return _normalize_cases(raw)


def bridge_advocate_barcode(body: dict, proxies=None) -> list:
    """Search cases by advocate Bar Registration number (e.g. MP/687/2012).

    Body: state_code, district_code, court_code  (the court to search)
          bar_code = "MP/687/2012"   (split into bar_state/number/year)
            OR bar_state + bar_number + bar_year
          status?  (Pending/Disposed/Both), court_type?
    """
    court_type = body.get("court_type", "dc")
    bar_state = body.get("bar_state")
    bar_number = body.get("bar_number")
    bar_year = body.get("bar_year")
    if body.get("bar_code") and not (bar_state and bar_number and bar_year):
        raw_code = str(body["bar_code"]).strip()
        # Accept '/', '\', '-' or whitespace as the state/number/year separator
        # (real-world input is inconsistent: "MP/687/2012", "MP\687\2012",
        # "MP-687-2012", "MP 687 2012" have all been observed). A fully
        # separator-free code (e.g. "MP6872012") is deliberately NOT
        # guessed at — splitting digits into number+year without a
        # delimiter is ambiguous (the same digit run can parse as more
        # than one valid number/year split), and a silently wrong split
        # would fetch the wrong advocate's cases. Fail clearly instead.
        parts = [p for p in _BAR_CODE_SEP_RE.split(raw_code) if p]
        if len(parts) >= 3:
            bar_state, bar_number, bar_year = parts[0], parts[1], parts[2]
    if not (bar_state and bar_number and bar_year):
        raise ValueError(
            "bar_code must be 'STATE/NUMBER/YEAR' with a separator "
            "(e.g. MP/687/2012, MP-687-2012, MP 687 2012)")
    cli = _get_v4(court_type, proxies=proxies)
    raw = cli.search_by_advocate_barcode(
        state_code=int(body["state_code"]),
        dist_code=int(body["district_code"]),
        court_code=_safe_court_code(body["court_code"]),
        barstatecode=str(bar_state).strip().upper(),
        barcode=str(bar_number).strip(),
        year=_normalize_year(bar_year),
        pendingDisposed=_normalize_status(body.get("status", "Both")))
    return _normalize_cases(raw)


# ── Additional case-status searches (filing / FIR / act / case-type / case-no) ──
# Each maps a snake_case request body onto the matching v4 upstream method and
# returns a flat list of case dicts (same shape as party/advocate).

def bridge_filing_number(body: dict, proxies=None) -> list:
    """Search by filing number + year.

    Body: state_code, district_code, court_code, filing_no, filing_year, court_type?
    """
    court_type = body.get("court_type", "dc")
    cli = _get_v4(court_type, proxies=proxies)
    raw = cli.search_by_filing_number(
        state_code=int(body["state_code"]),
        dist_code=int(body["district_code"]),
        court_code=_safe_court_code(body["court_code"]),
        filingNumber=str(body["filing_no"]),
        year=_normalize_year(body["filing_year"]),
    )
    return _normalize_cases(raw)


def bridge_fir(body: dict, proxies=None) -> list:
    """Search by FIR number. DC only — HC has no FIR index.

    Body: state_code, district_code, court_code, police_station?, fir_no,
          fir_year, status?, uniform_code?
    """
    if _normalize_court_type(body.get("court_type")) == "hc":
        raise ValueError("FIR search is not available for High Court")
    cli = _get_v4("dc", proxies=proxies)
    try:
        raw = cli.search_by_fir(
            state_code=int(body["state_code"]),
            dist_code=int(body["district_code"]),
            court_code=_safe_court_code(body["court_code"]),
            police_stationcode=str(body.get("police_station", "")),
            uniform_code=body.get("uniform_code", 0),
            firNumber=str(body["fir_no"]),
            year=_normalize_year(body["fir_year"]),
            pendingDisposed=_normalize_status(body.get("status", "Both")),
        )
    except NotImplementedError as e:
        raise ValueError(str(e))
    return _normalize_cases(raw)


def bridge_case_number(body: dict, proxies=None) -> list:
    """Search by case type + case number + year (one exact case).

    Body: state_code, district_code, court_code, case_type, case_no, year, court_type?
    """
    court_type = body.get("court_type", "dc")
    cli = _get_v4(court_type, proxies=proxies)
    raw = cli.search_by_case_number(
        state_code=int(body["state_code"]),
        dist_code=int(body["district_code"]),
        court_code=_safe_court_code(body["court_code"]),
        case_type=str(body["case_type"]),
        case_number=str(body["case_no"]),
        year=_normalize_year(body["year"]),
    )
    return _normalize_cases(raw)


def bridge_case_type(body: dict, proxies=None) -> list:
    """Search by case type (+ year, + pending/disposed) — all cases of a type.

    Body: state_code, district_code, court_code, case_type, year?, status?, court_type?
    """
    court_type = body.get("court_type", "dc")
    cli = _get_v4(court_type, proxies=proxies)
    raw = cli.search_by_case_type(
        state_code=int(body["state_code"]),
        dist_code=int(body["district_code"]),
        court_code=_safe_court_code(body["court_code"]),
        case_type=str(body["case_type"]),
        year=_normalize_year(body.get("year", "") or ""),
        pendingDisposed=_normalize_status(body.get("status", "Both")),
    )
    return _normalize_cases(raw)


def bridge_act(body: dict, proxies=None) -> list:
    """Search cases by Act type.

    Body: state_code, district_code, court_code, act_type, status?, court_type?

    Only `act_type` (the act code, from /api/metadata/acts) is required — the
    upstream reads it from `selectActTypeText`; act_name and under_section are
    NOT sent.

    `status` selects ONE pool — 'Pending' or 'Disposed' — which is exactly how
    the upstream works (there is no combined 'Both'; querying both is a separate
    call each). Each returned case is tagged `pending_disposed` with the status
    it was fetched under. One upstream call (~10-17s cold; instant when cached).
    """
    court_type = body.get("court_type", "dc")
    act_type = str(body["act_type"])
    sc = int(body["state_code"])
    dc = int(body["district_code"])
    cc = _safe_court_code(body["court_code"])
    # exactly one pool: anything starting 'd' → Disposed, else Pending.
    status = "Disposed" if str(body.get("status") or "").strip().lower().startswith("d") else "Pending"

    cli = _get_v4(court_type, proxies=proxies)
    raw = cli.search_by_act(state_code=sc, dist_code=dc, court_code=cc,
                            act_type=act_type, pendingDisposed=status)
    cases = _normalize_cases(raw)
    for c in cases:
        if isinstance(c, dict):
            c["pending_disposed"] = status
    return cases


# ── CNR case-detail lookup ─────────────────────────────────────────────

_CNR_RE = _re.compile(r"^[A-Z]{2}[A-Z0-9]{2}[0-9]{12}$")


def _infer_court_type_from_cnr(cnr: str) -> str:
    """Infer court_type from CNR positions 2-3.

    eCourts CNR format: 2 chars state + 2 chars court-type + 12 chars
    sequence. Position 2-3 is 'HC' for high court, otherwise district.
    """
    if not cnr or len(cnr) < 4:
        return "dc"
    return "hc" if cnr[2:4].upper() == "HC" else "dc"


def is_valid_cnr(cnr: str) -> bool:
    """Strict-but-tolerant CNR format check.

    Real CNRs are 16 chars: 2 letters (state) + 2 alnum (court-type) +
    12 digits (sequence). We uppercase before checking. Reject anything
    else early so we never spend a rate token on garbage input."""
    if not isinstance(cnr, str):
        return False
    return bool(_CNR_RE.match(cnr.strip().upper()))


def bridge_case_detail(cnr: str, court_type=None, proxies=None) -> dict:
    """Single-CNR case-detail lookup via v4.

    `court_type` is auto-inferred from the CNR if not provided. Returns the
    raw `case_history` response from EcourtFetchV4 as-is.

    Raises:
        ValueError if cnr fails format validation (caller maps to 400).
        Other exceptions propagate; the gateway layer catches them and
        surfaces honest 502 / breaker behaviour.
    """
    if not is_valid_cnr(cnr):
        raise ValueError(f"invalid CNR format: {cnr!r}")
    cnr_norm = cnr.strip().upper()
    if court_type is None:
        court_type = _infer_court_type_from_cnr(cnr_norm)
    cli = _get_v4(court_type, proxies=proxies)
    return cli.case_history(cino=cnr_norm)
