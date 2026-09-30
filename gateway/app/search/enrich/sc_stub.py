"""E5 — Supreme Court CNR detection + deeplink builder.

Until the SC adapter ships (Phase 2 roadmap), the gateway can't fetch SC
cases — the v4 mobile API only covers DC + HC. This module gives the
frontend a clean signal:

    is_sc_cnr("DLST020142782022") -> False
    is_sc_cnr("SCXX0000000002022") -> True
    detect("SCXX0000000002022")    -> {"is_sc": True, "deeplink_url": "..."}

When `is_sc` is True the public-facing /api/case/<cnr> route short-circuits
with a 200 carrying status='not_supported_yet' and the deeplink. The UI
renders the "open in Supreme Court India" panel instead of failing.

The SC CNR format is not strictly published, but cases that originate at
the SC carry state prefix "SC" in the canonical eCourts CNR scheme. This
module's heuristic is conservative: state-prefix "SC" is the only signal
we use. False positives (DC cases that happen to start with "SC") are
near-zero because no Indian state code is "SC".
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode


# Canonical citizen-facing SC case-status URL. The page accepts CNR via
# query string; users can paste it manually if the prefill fails.
_SC_CASE_STATUS_BASE = "https://main.sci.gov.in/case-status"


def is_sc_cnr(cnr: Optional[str]) -> bool:
    """True iff the CNR is for a Supreme Court case (state prefix 'SC')."""
    if not cnr or not isinstance(cnr, str):
        return False
    s = cnr.strip().upper()
    if len(s) < 2:
        return False
    return s[:2] == "SC"


def build_deeplink(cnr: str) -> str:
    """Build the Supreme Court case-status URL with the CNR in the query.

    SC's portal accepts CNRs via the standard search form; the URL we
    return lands the user on the case-status page with the CNR pre-filled
    (where the page supports it) or at least one click away from search.
    """
    cnr_clean = (cnr or "").strip().upper()
    if not cnr_clean:
        return _SC_CASE_STATUS_BASE
    return f"{_SC_CASE_STATUS_BASE}?{urlencode({'cnr': cnr_clean})}"


def detect(cnr: Optional[str]) -> dict:
    """One-call helper for the case-detail route.

    Returns:
        {is_sc: bool, deeplink_url: str | None,
         message: str, waitlist_url: str}
    """
    if not is_sc_cnr(cnr):
        return {"is_sc": False, "deeplink_url": None,
                "message": None, "waitlist_url": None}
    return {
        "is_sc": True,
        "deeplink_url": build_deeplink(cnr),
        "message": ("Supreme Court search is coming to Cassie soon. "
                    "Meanwhile, view your case on the official Supreme Court portal."),
        "waitlist_url": "/find-my-case/sc-waitlist",
    }
