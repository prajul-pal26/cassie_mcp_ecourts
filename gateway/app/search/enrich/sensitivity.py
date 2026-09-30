"""E6 — Sensitive matter classifier.

Marks cases involving POCSO, juvenile justice, sexual offences, or
matrimonial disputes as `sensitive_matter: true` in the normalised
response. The frontend uses this signal to render a default-redacted view
with a "View full details" disclosure, mirroring the redaction many High
Courts already apply at publication time.

This is conservative by design — false positives (extra caution) are
acceptable; false negatives (showing sensitive party names by default)
are not. The classifier looks at three signals:

  1. Act codes / act names — POCSO Act, JJ Act, IPC 376, etc.
  2. Case type codes / labels — matrimonial-only registries.
  3. The case_type free-text field (e.g., "Matrimonial Petition").

If ANY of the three signals matches a sensitive category, the case is
flagged. The category itself is returned for UI nuance (POCSO gets
stronger redaction than matrimonial, for instance).

Public functions:
    classify(case_or_detail) -> {is_sensitive, category, reasons}
    apply(detail) -> detail  (mutates in place + returns it)
"""
from __future__ import annotations

import re
from typing import Any, Optional


# Sensitive-category signatures. Order matters: pocso/juvenile take priority
# over matrimonial. Each entry maps a category to substring markers.
_SENSITIVE_ACT_MARKERS: dict[str, tuple[str, ...]] = {
    "pocso": ("pocso", "protection of children", "sexual offences against children"),
    "juvenile": ("juvenile justice", "jj act", "juvenile"),
    "sexual_offence": (
        # IPC 376 series + new BNS equivalents. Substring check on the act+section combo.
        "376", "rape", "sexual assault", "outrag", "sect 354", "354a", "354b", "354c", "354d",
    ),
    "ndps": ("narcotic drugs", "ndps", "psychotropic substances"),
    "matrimonial": (
        "matrimonial", "divorce", "hindu marriage act", "hma",
        "special marriage", "marriage act", "family suit", "fmcv",
        "guardianship", "custody", "maintenance act",
    ),
    "domestic_violence": ("domestic violence", "dv act", "pwdva", "498a"),
    "official_secrets": ("official secrets", "ufa", "uapa", "unlawful activities"),
}

# Sensitive case-type code markers (terse eCourts registry codes).
_SENSITIVE_TYPE_MARKERS: dict[str, tuple[str, ...]] = {
    "matrimonial": ("hma", "hmop", "fmcv", "matrimonial", "div"),
    "pocso": ("pocso",),
    "juvenile": ("jjb", "jcl", "juv"),
}


def classify(case: dict) -> dict:
    """Return {is_sensitive: bool, category: str|None, reasons: [str]}.

    Accepts either:
      - a raw v4 search-result case dict (has under_act1, case_type)
      - a normalised CaseDetail (has acts: [{name, sections}], case_type)
    """
    if not isinstance(case, dict):
        return {"is_sensitive": False, "category": None, "reasons": []}

    reasons: list[str] = []
    matched_categories: list[str] = []

    # 1) Check acts. Try the normalised shape first, then fall back to raw.
    act_texts: list[str] = []
    acts_norm = case.get("acts")
    if isinstance(acts_norm, list) and acts_norm:
        for a in acts_norm:
            if isinstance(a, dict):
                name = str(a.get("name") or "")
                secs = a.get("sections") or []
                if isinstance(secs, list) and secs:
                    act_texts.append(f"{name} {' '.join(str(s) for s in secs)}")
                else:
                    act_texts.append(name)
    else:
        # Raw shape: under_act1..4 + under_sec1..4
        for i in range(1, 5):
            a = case.get(f"under_act{i}")
            s = case.get(f"under_sec{i}")
            if a and str(a) not in ("0", "None", ""):
                act_texts.append(f"{a} {s or ''}")

    act_blob = " | ".join(act_texts).lower()

    for category, markers in _SENSITIVE_ACT_MARKERS.items():
        for m in markers:
            if m in act_blob:
                matched_categories.append(category)
                reasons.append(f"act:{m}")
                break  # one marker per category is enough

    # 2) Check case_type field.
    ctype = str(case.get("case_type") or case.get("type_name") or "").lower()
    if ctype:
        for category, markers in _SENSITIVE_TYPE_MARKERS.items():
            for m in markers:
                # Use word-boundary-ish match for short codes like 'hma' so we
                # don't false-positive on 'aham' or 'mathama' substrings.
                if _short_code_match(m, ctype):
                    if category not in matched_categories:
                        matched_categories.append(category)
                    reasons.append(f"type:{m}")
                    break

    if not matched_categories:
        return {"is_sensitive": False, "category": None, "reasons": []}

    # Pick highest-priority category. POCSO/juvenile/sexual offences > matrimonial.
    priority = ("pocso", "juvenile", "sexual_offence", "ndps",
                "official_secrets", "domestic_violence", "matrimonial")
    primary = next((c for c in priority if c in matched_categories),
                   matched_categories[0])

    return {
        "is_sensitive": True,
        "category": primary,
        "reasons": reasons,
    }


def apply(detail: dict) -> dict:
    """Mutate a normalised CaseDetail in place: set sensitive_matter +
    sensitivity_category, return the same dict for chaining.
    """
    if not isinstance(detail, dict):
        return detail
    result = classify(detail)
    detail["sensitive_matter"] = result["is_sensitive"]
    detail["sensitivity_category"] = result["category"]
    detail["sensitivity_reasons"] = result["reasons"]
    return detail


# ── helpers ───────────────────────────────────────────────────────────────

_SHORT_CODE_BOUND_RE = re.compile(r"[a-z0-9]")


def _short_code_match(marker: str, haystack: str) -> bool:
    """Match `marker` in `haystack` only when bounded by non-alphanumerics.

    For short codes like 'hma' (3 chars) we need boundaries to avoid
    false-positive substring hits. For longer markers ("matrimonial") a
    plain substring is fine."""
    if len(marker) >= 6:
        return marker in haystack
    # Find positions; check both neighbours.
    i = 0
    while True:
        j = haystack.find(marker, i)
        if j == -1:
            return False
        left = haystack[j - 1] if j > 0 else " "
        right = haystack[j + len(marker)] if j + len(marker) < len(haystack) else " "
        if not _SHORT_CODE_BOUND_RE.match(left) and not _SHORT_CODE_BOUND_RE.match(right):
            return True
        i = j + 1
