"""Low-level HTTP client for AIROnline's citation-search backend.

Protocol (reverse-engineered from
`https://aol1.aironline.in/webresources/webworld/scripts/citationSearch_visitor.min.js`):

Two endpoints, both on https://aol1.aironline.in:

1. POST /loadVisitorCitationData.html   — the cascading dropdown facets.
   Form-encoded body:
       searchString          JSON: {"facetRequestFields":[{...}], "filters":[...]}
       publicationNameFlag   "true" ONLY for the first (publication name) call
       publicationNumberFlag "true" ONLY for the last (page number) call
   Returns a JSON array of facet rows:
       {"id": <display text>, "value": <option value>, "count": <doc count>,
        "filterParamValue2": <extra>, ...}

   Each dropdown is one call, requesting one `entityFieldEnum` and passing the
   already-chosen values as EQUALTO `filters`. The six levels are:

       PUBLICATION_FULL_NAME          (no filters)
       PUBLICATION_YEAR               filtered by name
       PUBLICATION_SEGMENT_FULL_NAME  + year
       JUDICIAL_BODY_SHORT_NAME       + segment
       PUBLICATION_VOLUME_NUMBER      + judicial body      <- CONDITIONAL
       PUBLICATION_PAGE_NUMBER        + volume (numberFlag) <- the citation itself

2. POST /loadVisitorCitationCaseContent.html — the selected citation's record,
   returned as an HTML fragment (query params: searchString, solrDocumentId,
   citationText).

TLS note: aol1.aironline.in serves a BROKEN certificate chain — it omits the
"Sectigo Public Server Authentication CA DV R36" intermediate that its leaf is
signed by (it sends an unrelated Sectigo CA instead). Python's certifi bundle
therefore cannot build a path and every request fails with
CERTIFICATE_VERIFY_FAILED, even though browsers and curl succeed (they fetch
the missing intermediate via the leaf's AIA extension). Rather than disable
verification, we ship that intermediate in `sectigo_intermediate.pem` and
verify against certifi + that cert — so the connection is still fully
authenticated. Set AIRONLINE_INSECURE=1 to fall back to verify=False.
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from pathlib import Path
from typing import Any, Optional

import certifi
import requests

log = logging.getLogger("app.tribunals.aironline.client")

BASE_URL = "https://aol1.aironline.in"
DATA_URL = f"{BASE_URL}/loadVisitorCitationData.html"
CONTENT_URL = f"{BASE_URL}/loadVisitorCitationCaseContent.html"
REFERER = f"{BASE_URL}/legal-citations.html"

TIMEOUT = int(os.getenv("AIRONLINE_TIMEOUT", "90"))
RETRIES = int(os.getenv("AIRONLINE_RETRIES", "4"))

_HERE = Path(__file__).resolve().parent
_INTERMEDIATE = _HERE / "sectigo_intermediate.pem"
_BUNDLE = _HERE / "data" / "_ca_bundle.pem"

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# A pool of real browser fingerprints. Concurrent workers pick one at random so
# a burst does not look like one client hammering the site — the requests are
# spread across plausible Chrome / Firefox / Safari / Edge users on several
# platforms, which is what a normal audience looks like.
#
# Accept-Language and Accept are varied WITH the User-Agent rather than
# independently: a Chrome UA paired with Firefox's Accept header is a more
# obvious tell than either alone.
_FINGERPRINTS = [
    {   # Chrome / macOS
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 "
                      "Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", '
                     '"Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    },
    {   # Chrome / Windows
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 "
                      "Safari/537.36",
        "Accept-Language": "en-GB,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="123", "Google Chrome";v="123", '
                     '"Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    },
    {   # Firefox / Windows — no sec-ch-ua headers, Firefox does not send them
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
                      "Gecko/20100101 Firefox/125.0",
        "Accept-Language": "en-US,en;q=0.5",
    },
    {   # Firefox / macOS
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) "
                      "Gecko/20100101 Firefox/124.0",
        "Accept-Language": "en-IN,en-US;q=0.7,en;q=0.3",
    },
    {   # Safari / macOS
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
                      "Safari/605.1.15",
        "Accept-Language": "en-IN,en;q=0.9",
    },
    {   # Edge / Windows
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 "
                      "Safari/537.36 Edg/124.0.0.0",
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="124", "Microsoft Edge";v="124", '
                     '"Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    },
]

# The six cascade levels, in order.
FIELD_PUBLICATION = "PUBLICATION_FULL_NAME"
FIELD_YEAR = "PUBLICATION_YEAR"
FIELD_SEGMENT = "PUBLICATION_SEGMENT_FULL_NAME"
FIELD_JUDICIAL_BODY = "JUDICIAL_BODY_SHORT_NAME"
FIELD_VOLUME = "PUBLICATION_VOLUME_NUMBER"
FIELD_PAGE = "PUBLICATION_PAGE_NUMBER"
# Used only when fetching the final case content (the page number is sent back
# under this name, not PUBLICATION_PAGE_NUMBER).
FIELD_SEGMENT_NUMBER = "PUBLICATION_SEGMENT_NUMBER"

# The hidden #input-all template the page posts with every content request.
SEARCH_TEMPLATE: dict[str, Any] = {
    "OrderCriteria": [],
    "queryOperator": "AND",
    "noOfRecordPerPage": 5,
    "recordRecentSearch": False,
    "filters": [],
    "entityType": None,
    "fetchFacet": True,
    "pageNo": 1,
    "highlight": True,
}

_bundle_lock = threading.Lock()

# Backoff between retries: the upstream resets connections when several
# requests land at once, and recovers within a second or two.
BACKOFF_BASE = float(os.getenv("AIRONLINE_BACKOFF", "1.5"))


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter — jitter matters because parallel
    workers fail simultaneously and would otherwise all retry in lockstep."""
    return BACKOFF_BASE * (2 ** attempt) * (0.5 + random.random())


def _ca_bundle() -> Any:
    """certifi + the intermediate the server fails to send.

    Returns a path (str) to a merged PEM bundle, or False when
    AIRONLINE_INSECURE=1 (verification disabled — last resort only).
    """
    if os.getenv("AIRONLINE_INSECURE") == "1":
        log.warning("AIRONLINE_INSECURE=1 — TLS verification DISABLED")
        return False
    if not _INTERMEDIATE.exists():
        log.warning("missing %s — falling back to certifi alone; the upstream's "
                    "broken chain will likely fail verification", _INTERMEDIATE)
        return certifi.where()
    with _bundle_lock:
        # Rebuild if absent or older than either input.
        if (not _BUNDLE.exists()
                or _BUNDLE.stat().st_mtime < _INTERMEDIATE.stat().st_mtime):
            _BUNDLE.parent.mkdir(parents=True, exist_ok=True)
            _BUNDLE.write_text(
                Path(certifi.where()).read_text() + "\n"
                + _INTERMEDIATE.read_text())
            log.info("built CA bundle at %s", _BUNDLE)
    return str(_BUNDLE)


def make_session(fingerprint: Optional[dict] = None,
                 pool_size: int = 32) -> requests.Session:
    """A session with browser headers and a CA bundle that can verify the
    upstream's incomplete chain.

    Each session picks a random browser fingerprint (see _FINGERPRINTS) unless
    one is passed in, so concurrent workers do not all present as the same
    client. The connection pool is sized to the worker count — urllib3's
    default is 10, and exceeding it silently discards and reopens connections,
    which shows up as extra latency under high concurrency.
    """
    fp = fingerprint or random.choice(_FINGERPRINTS)
    s = requests.Session()
    s.verify = _ca_bundle()
    s.headers.update({
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": REFERER,
        "Origin": BASE_URL,
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        **fp,
    })
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def eq_filter(field: str, value: Any) -> dict:
    """One EQUALTO filter clause, shaped exactly like the site's JS builds it."""
    return {
        "fieldEnum": field,
        "filterParamValue1": value,
        "filterParamValue2": None,
        "filterOperator": "EQUALTO",
        "searchTypeEnum": None,
        "queryStringNew": "",
        "queryString": "",
    }


def fetch_facets(field: str, filters: Optional[list[dict]] = None, *,
                 publication_name_flag: bool = False,
                 publication_number_flag: bool = False,
                 session: Optional[requests.Session] = None,
                 no_of_facets: int = 1_000_000) -> list[dict]:
    """One dropdown's options.

    Retries transient failures: under concurrency the upstream intermittently
    drops connections / returns non-JSON, and a bare failure here would silently
    truncate the mapping (a court/year appearing to have "no options" when it
    has many). Raises only when every attempt fails, so a caller can record the
    gap instead of writing a wrong-but-plausible empty list.

    `no_of_facets` is sent absurdly high on purpose. The site's own JS sends
    500, which is fine for its six dropdowns but NOT for page numbers — AIR
    Online 2026 / GUJ alone has 2,275. Measured, this server ignores the
    parameter and returns the full set either way (500 / 2000 / 5000 / 100000
    all returned 2,275), but relying on that would make silent truncation one
    server-side change away, and a truncated page list means permanently
    missing judgments with nothing to flag it.
    """
    body = {
        "facetRequestFields": [
            {"entityFieldEnum": field, "noOfFacets": no_of_facets,
             "filterPrefix": None}
        ],
        "filters": filters if filters else None,
    }
    payload = {
        "searchString": json.dumps(body),
        "publicationNumberFlag": "true" if publication_number_flag else "false",
        "publicationNameFlag": "true" if publication_name_flag else "false",
    }
    own = session is None
    s = session or make_session()
    last: Exception | None = None
    try:
        for attempt in range(RETRIES):
            try:
                r = s.post(DATA_URL, data=payload, timeout=TIMEOUT)
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, list):
                    raise ValueError(f"expected a JSON array, got {type(data).__name__}")
                return data
            except Exception as e:  # noqa: BLE001 - retried below
                last = e
                if attempt < RETRIES - 1:
                    # Exponential backoff with jitter. The upstream resets
                    # connections under concurrent load; retrying instantly just
                    # burns all attempts inside the same overloaded moment, so
                    # every attempt fails together and a real combination gets
                    # recorded as a permanent gap.
                    time.sleep(_backoff(attempt))
                    log.debug("facet %s attempt %d failed: %r", field, attempt + 1, e)
        raise RuntimeError(f"fetch_facets({field}) failed after {RETRIES} "
                           f"attempts: {last!r}") from last
    finally:
        if own:
            s.close()


def fetch_case_content(filters: list[dict], solr_document_id: str,
                       citation_text: str, *,
                       session: Optional[requests.Session] = None) -> str:
    """The selected citation's record as the raw HTML fragment the page renders."""
    body = dict(SEARCH_TEMPLATE)
    body["filters"] = filters
    own = session is None
    s = session or make_session()
    last: Exception | None = None
    try:
        for attempt in range(RETRIES):
            try:
                r = s.post(
                    CONTENT_URL,
                    params={"searchString": json.dumps(body),
                            "solrDocumentId": solr_document_id,
                            "citationText": citation_text},
                    headers={"Content-Type": "application/json",
                             "Accept": "text/html, */*; q=0.01"},
                    timeout=TIMEOUT,
                )
                r.raise_for_status()
                return r.text
            except Exception as e:  # noqa: BLE001 - retried below
                last = e
                if attempt < RETRIES - 1:
                    time.sleep(_backoff(attempt))
                log.debug("content attempt %d failed: %r", attempt + 1, e)
        raise RuntimeError(f"fetch_case_content failed after {RETRIES} "
                           f"attempts: {last!r}") from last
    finally:
        if own:
            s.close()
