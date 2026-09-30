"""Parse the HTML fragment AIROnline returns for a selected citation.

`/loadVisitorCitationCaseContent.html` answers with the same markup the page
injects into `div#fetchedData`. The visible text is only part of it — the
fragment also carries hidden inputs and spans (the Solr document id, the record
nature, equivalent citations) that never render but are useful to a caller. We
extract both.

Fields returned per record:

    solr_document_id  hidden input — the stable document key (e.g. J_CD_20213AJR1)
    nature_of_record  hidden input — "J" for judgment
    citation          the "Citations:" value (e.g. "2021 (3) AJR 1")
    equal_citations   the hidden equivalent-citation span (parallel citations)
    document_type     "Judgment" / "Order" badge
    court             the judicial body heading (e.g. "Jharkhand High Court")
    judges            Hon'ble Judge(s), split into a list
    petitioner        left side of the " v. " line
    respondent        right side of the " v. " line
    parties           the raw "A v. B" line
    case_number       e.g. "Cr. M.P. No. 1865 of 2020"
    decided_on        e.g. "18/03/2021"
    case_details_raw  the whole case-number/decided-on line, unsplit

Nothing here raises on unexpected markup: a missing field comes back as None so
one odd record cannot break a batch.
"""
from __future__ import annotations

import html
import re
from typing import Any, Optional

# Record blocks. Each result is a <div class="caseResultDiv" id="<doc id>">.
_RE_BLOCK = re.compile(
    r'<div class="caseResultDiv"[^>]*>(.*?)(?=<div class="caseResultDiv"|\Z)',
    re.S | re.I)
_RE_HIDDEN = re.compile(
    r'<input[^>]*\bid="([^"]+)"[^>]*\bvalue="([^"]*)"', re.I)
_RE_CITATION = re.compile(
    r'Citations:\s*(?:</[^>]+>\s*)*<span[^>]*>(.*?)</span>', re.S | re.I)
_RE_EQUAL = re.compile(
    r'<span[^>]*id\s*=\s*"equalCitation_[^"]*"[^>]*>(.*?)</span>', re.S | re.I)
_RE_DOCTYPE = re.compile(
    r'<span class="searchResultJudgementhiglight-result">(.*?)</span>', re.S | re.I)
_RE_COURT = re.compile(
    r'<strong class="searchResultJudicialBody[^"]*">(.*?)</strong>', re.S | re.I)
_RE_JUDGES = re.compile(
    r"Hon'ble Judge\(s\):\s*</b>(.*?)</span>", re.S | re.I)
_RE_PARTIES = re.compile(
    r'<p class="searchResultVs">(.*?)</p>', re.S | re.I)
_RE_CASENO = re.compile(
    r'<p class="searchResultJudgementCaseNumber[^"]*">(.*?)</p>', re.S | re.I)
_RE_DECIDED = re.compile(r'decided\s+on\s+(.+)$', re.I)
_RE_TAG = re.compile(r'<[^>]+>')


def _text(fragment: Optional[str]) -> Optional[str]:
    """Strip tags/entities and collapse whitespace."""
    if fragment is None:
        return None
    t = _RE_TAG.sub(" ", fragment)
    t = html.unescape(t).replace("\xa0", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t or None


def _first(rx: re.Pattern[str], s: str) -> Optional[str]:
    m = rx.search(s)
    return _text(m.group(1)) if m else None


# The site emits benches in two different shapes:
#   (a) per-judge designation:  "A K Sikri , J , N V Ramana , J"
#   (b) one shared designation: "Ranjan Gogoi, S A Bobde, ... , JJJ"
#       (a five-judge bench gets "JJJ" — the count of J's is not meaningful)
# A naive comma split breaks (a) — it cuts every judge away from their
# designation. Handling only (a) breaks (b) — the whole bench stays one string.
# So: strip a trailing shared designation, then split the remainder on commas
# that are NOT part of a per-judge designation.
_DESIG = r"(?:ACJ|CJI|CJ|JJJ+|JJ|J)"
_RE_TRAILING_DESIG = re.compile(rf",\s*({_DESIG})\s*\.?\s*$", re.I)
_RE_JUDGE_WITH_DESIG = re.compile(rf"(.+?)\s*,\s*({_DESIG})\b\.?", re.I)


def _normalise_desig(d: str) -> str:
    """"JJJ"/"JJ" are just repeated J's for a multi-judge bench; each judge is
    a "J". CJ/CJI/ACJ are real distinct designations."""
    u = d.upper().rstrip(".")
    return "J" if set(u) == {"J"} else u


def _split_judges(raw: Optional[str]) -> list[str]:
    """One entry per judge, each as "<name>, <designation>"."""
    if not raw:
        return []
    s = re.sub(r"\s+", " ", raw).replace(" ,", ",").strip(" ,;&")
    if not s:
        return []

    # Shape (b): a single designation at the very end covering the whole bench.
    m = _RE_TRAILING_DESIG.search(s)
    if m and not _RE_JUDGE_WITH_DESIG.search(s[: m.start()]):
        desig = _normalise_desig(m.group(1))
        names = [n.strip(" ,;&") for n in re.split(r",|\band\b|&", s[: m.start()])]
        out = [f"{n}, {desig}" for n in names if n]
        return out or [s]

    # Shape (a): each judge carries their own designation.
    out: list[str] = []
    pos = 0
    for m in _RE_JUDGE_WITH_DESIG.finditer(s):
        name = re.sub(r"^(?:and|&)\s+", "", m.group(1).strip(" ,;&"),
                      flags=re.I).strip()
        if name:
            out.append(f"{name}, {_normalise_desig(m.group(2))}")
        pos = m.end()
    tail = s[pos:].strip(" ,;&")
    if tail:
        out.append(tail)
    return out or [s]


def _split_parties(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not raw:
        return None, None
    m = re.split(r"\s+v\.?s?\.?\s+", raw, maxsplit=1, flags=re.I)
    if len(m) == 2:
        return m[0].strip() or None, m[1].strip() or None
    return raw.strip() or None, None


def parse_records(fragment: str) -> list[dict[str, Any]]:
    """Every record in the returned HTML fragment."""
    out: list[dict[str, Any]] = []
    for m in _RE_BLOCK.finditer(fragment or ""):
        block = m.group(1)
        hidden = {k: v for k, v in _RE_HIDDEN.findall(block)}

        case_details = _first(_RE_CASENO, block)
        decided_on = None
        case_number = case_details
        if case_details:
            dm = _RE_DECIDED.search(case_details)
            if dm:
                decided_on = dm.group(1).strip(" .,")
                case_number = case_details[: dm.start()].strip(" ,")
        case_number = (case_number or "").strip(" ,") or None

        parties_raw = _first(_RE_PARTIES, block)
        pet, res = _split_parties(parties_raw)

        out.append({
            "solr_document_id": hidden.get("Solr_Document_ID"),
            "nature_of_record": hidden.get("NATURE_OF_CASE_ID"),
            "citation": _first(_RE_CITATION, block),
            "equal_citations": _first(_RE_EQUAL, block),
            "document_type": _first(_RE_DOCTYPE, block),
            "court": _first(_RE_COURT, block),
            "judges": _split_judges(_first(_RE_JUDGES, block)),
            "petitioner": pet,
            "respondent": res,
            "parties": parties_raw,
            "case_number": case_number,
            "decided_on": decided_on,
            "case_details_raw": case_details,
        })
    return out
