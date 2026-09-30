"""E2 — Server-side match confidence + reasons for a search result.

Why server-side: client-side scoring is inconsistent across consumers,
can't be cached, and forces the client to ship the matching logic. The
gateway already has all the inputs (query, variant that produced the hit,
the eCourts row) so the math belongs here.

Public functions:
    score_case(query, case, *, variant_used=None, hint=None) -> ScoredCase
    sort_by_confidence(scored: list[ScoredCase]) -> list[ScoredCase]

Where ScoredCase is the original case dict plus:
    match_confidence: int (0-100)
    match_reasons: list[str]   # ['name_exact', 'year_match', 'type_match', ...]
    primary_variant: str       # the variant that scored highest (debugging)
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Iterable, Optional

from app.search.name_variants import fuzz

log = logging.getLogger("gateway.confidence")


# ── Public API ────────────────────────────────────────────────────────────

def score_case(query: dict, case: dict, *,
               variant_used: Optional[str] = None,
               hint: Optional[dict] = None) -> dict:
    """Score one case against the user's query. Returns the case dict
    *augmented* with match_confidence, match_reasons, primary_variant.

    query shape:
        {
          "party_name"?: "Ram Kumar",
          "advocate_name"?: "Amit Tanwar",
          "year"?: "2022",
          "case_type"?: "criminal" | "civil" | None,
          "status"?: "pending" | "disposed" | None,
        }
    case shape: whatever the v4 search returned (cnr, pet_name, res_name,
    case_year, case_type, ...).

    `variant_used` is the spelling variant that was the SEARCH input (e.g.,
    "Aneel" might have been the variant for a query of "Anil"). We use it
    to record provenance; the name match still scores against the raw
    `query.party_name`.

    `hint` is for future extensibility (e.g., {"court_priority_hint":
    "criminal"}). Currently unused for scoring.
    """
    reasons: list[str] = []
    score = 0
    best_variant = variant_used or query.get("party_name") or query.get("advocate_name") or ""

    # ── Name matching ──
    # Score against both pet and res. Take the max (the user might be
    # searching for a respondent, not a petitioner).
    party_q = (query.get("party_name") or "").strip()
    adv_q = (query.get("advocate_name") or "").strip()

    if party_q:
        pet = str(case.get("pet_name") or "")
        res = str(case.get("res_name") or "")
        s_pet = fuzz(party_q, pet) if pet else 0
        s_res = fuzz(party_q, res) if res else 0
        # Also score the variant that was used (covers the case where the
        # eCourts row matches "Aneel" but the user typed "Anil").
        s_pet_v = fuzz(variant_used, pet) if variant_used and pet else 0
        s_res_v = fuzz(variant_used, res) if variant_used and res else 0
        best_name = max(s_pet, s_res, s_pet_v, s_res_v)
        # Name is the dominant signal: 60% weight so an exact name match
        # plus a year/recency bonus clears the "Strong match" threshold
        # (>=60). Earlier weight of 0.40 capped realistic top scores at
        # ~58, which was misclassified as "Possible match" — confusing
        # citizens whose searches actually matched their case exactly.
        score += round(best_name * 0.60)
        if best_name == 100:
            reasons.append("name_exact")
        elif best_name >= 85:
            reasons.append("name_close")
        elif best_name >= 60:
            reasons.append("name_partial")
        # Record which variant actually produced the hit
        if max(s_pet_v, s_res_v) > max(s_pet, s_res) and variant_used:
            best_variant = variant_used

    if adv_q:
        # Advocate fields in v4 search results aren't always present, but
        # detail responses have pet_adv / res_adv.
        adv_candidates: list[str] = []
        for k in ("pet_adv", "res_adv", "advocate_name", "advocate"):
            v = case.get(k)
            if v:
                adv_candidates.append(str(v))
        best_adv = max((fuzz(adv_q, c) for c in adv_candidates), default=0)
        score += round(best_adv * 0.30)
        if best_adv >= 85:
            reasons.append("advocate_close")
        elif best_adv >= 60:
            reasons.append("advocate_partial")

    # ── Year matching ──
    yr_q = _safe_year(query.get("year"))
    yr_c = _safe_year(case.get("case_year") or case.get("reg_year") or case.get("fil_year"))
    if yr_q and yr_c:
        if yr_q == yr_c:
            score += 12
            reasons.append("year_match")
        elif abs(yr_q - yr_c) == 1:
            score += 6
            reasons.append("year_close")

    # ── Case-type signal ──
    # Map common user-supplied hints to substring matches against the
    # eCourts case_type field (which is itself terse and inconsistent).
    ctype_q = (query.get("case_type") or "").strip().lower()
    # Normalized rows carry the human label in `type_name`; `case_type` is a
    # numeric code (or absent) on district rows. Read type_name first, matching
    # what the case-detail and onboarding surfaces already do. (Today ctype_q is
    # always empty because find never sets a case_type query, so this branch is
    # dead and the change is a no-op — corrected now so it is right when the
    # query side is populated in a later, measured change.)
    ctype_c = str(case.get("type_name") or case.get("case_type") or "").strip().lower()
    if ctype_q and ctype_c:
        if ctype_q == ctype_c:
            score += 8
            reasons.append("type_exact")
        elif _case_type_family(ctype_q) and _case_type_family(ctype_q) == _case_type_family(ctype_c):
            score += 4
            reasons.append("type_family")

    # ── Status (pending/disposed) ──
    status_q = (query.get("status") or "").strip().lower()
    status_c = str(case.get("status") or "").strip().lower()
    if status_q and status_c and status_q == status_c:
        score += 4
        reasons.append("status_match")

    # ── Recency bonus ──
    # Cases filed within the last 5 years get a small bump — citizens
    # searching are usually after their active case, not an old one.
    if yr_c:
        current_year = datetime.now().year
        age = current_year - yr_c
        if 0 <= age <= 5:
            score += 6 - age  # 6, 5, 4, 3, 2, 1
            if age <= 2:
                reasons.append("recent")

    # Clamp
    score = max(0, min(100, score))

    out = dict(case)  # don't mutate input
    out["match_confidence"] = score
    out["match_reasons"] = reasons
    out["primary_variant"] = best_variant
    return out


def sort_by_confidence(cases: Iterable[dict]) -> list[dict]:
    """Descending by match_confidence, then newest, then a stable string
    tie-break so the order is byte-identical across runs.

    Why the extra keys: for a common party name almost every row ties at the
    same confidence AND, in a single-year search, the same year — so the old
    (-confidence, -year) key was a total tie and `sorted` fell back to input
    (task-arrival) order. That is why the result set was non-deterministic and
    why `[:N]` kept "whichever court returned first". `cino_seq` (the CNR's
    6-digit serial, higher = filed later) gives a defensible "newest first"
    within a court+year; `cino` itself is the final total-order fallback.
    """
    def keyer(c: dict) -> tuple:
        return (
            -int(c.get("match_confidence", 0)),
            -(_safe_year(c.get("case_year") or c.get("reg_year")) or -1),
            -_cino_seq(c),
            str(c.get("cino") or c.get("cnr") or ""),
        )
    return sorted(cases, key=keyer)


def _cino_seq(case: dict) -> int:
    """The 6-digit serial inside a CNR (SSDD NNNNNN YYYY). 100% present on
    normalized rows, no I/O. -1 when unparseable so garbage sorts last."""
    cino = str(case.get("cino") or case.get("cnr") or "").strip()
    if len(cino) >= 10:
        mid = cino[-10:-4]
        if mid.isdigit():
            return int(mid)
    return -1


# ── Helpers ───────────────────────────────────────────────────────────────

def _safe_year(v: Any) -> Optional[int]:
    """Parse a year from various shapes ('2022', 2022, '2022/01', '0')."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s == "0":
        return None
    m = re.search(r"(19\d{2}|20\d{2})", s)
    if m:
        return int(m.group(1))
    return None


# Map of user-friendly case-type hints → eCourts substring families.
# Empirically eCourts uses terse codes (CR, CRA, CIL, CC, CT, FAO, CRM, etc.)
# and family names (CT Cases, Civil Suit, Criminal Misc, Family Suit, ...).
_CASE_TYPE_FAMILIES: dict[str, set[str]] = {
    "criminal": {"cr", "crl", "crm", "cra", "criminal", "ct", "cc"},
    "civil": {"cs", "ca", "civil", "suit", "ova", "exec"},
    "matrimonial": {"hma", "hmop", "fam", "div", "marr", "fmcv"},
    "writ": {"wp", "writ", "crwp"},
    "appeal": {"appeal", "rsa", "fao", "ap"},
    "property": {"ts", "rcs", "title", "rent"},
    "consumer": {"cd", "cdc", "consum"},
}


def _case_type_family(ctype: str) -> Optional[str]:
    """Map a (lowercased) case_type string to one of the known families."""
    if not ctype:
        return None
    ct = ctype.lower()
    for family, markers in _CASE_TYPE_FAMILIES.items():
        for m in markers:
            if m in ct:
                return family
    return None
