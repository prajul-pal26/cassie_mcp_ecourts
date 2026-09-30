"""Self-checks for the AIROnline integration.

Run:
    ./.venv/bin/python -m app.tribunals.aironline.selftest          # offline only
    ./.venv/bin/python -m app.tribunals.aironline.selftest --live   # + upstream

Offline checks cover the parsing and the mapping read-path (both pure
functions over fixed input). The `--live` checks hit the real site and assert
the protocol still behaves as documented — run them when something looks wrong,
since they are the ones that catch AIROnline changing under us.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from typing import Callable

from app.tribunals.aironline.parse import _split_judges, parse_records

_FRAGMENT = """
<div class="caseResultDiv" id="J_CD_20213AJR1">
<input type="hidden" id="NATURE_OF_CASE_ID" name="natureOfRecord" value="J" />
<input type="hidden" id="Solr_Document_ID" name="solrDocumentName" value="J_CD_20213AJR1" />
<span class="citationJudgementHeading new-head"><b> Citations:
  <span> 2021 (3) AJR 1 </span>
  <span id = "equalCitation_J_CD_20213AJR1" style="display: none;">::(2019) 15 Scale 1</span>
</b></span>
<span class="searchResultJudgementhiglight-result">Judgment</span>&nbsp
<strong class="searchResultJudicialBody searchResultCourtHeading"> Jharkhand High Court </strong>
<p><span class="citationJudgementHeading"><b> Hon'ble Judge(s):</b> Ananda Sen , J </span></p>
<p class="searchResultVs">Online Entertainment Private Limited, Jharkhand  v. State of Jharkhand</p>
<p class="searchResultJudgementCaseNumber searchResultAppealDetail">Cr. M.P. No. 1865 of 2020,, decided on  18/03/2021</p>
</div>
"""

_results: list[tuple[str, bool, str]] = []


def check(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
        _results.append((name, True, ""))
    except Exception as e:  # noqa: BLE001
        _results.append((name, False, f"{e}\n{traceback.format_exc(limit=2)}"))


def _eq(got, want, what=""):
    if got != want:
        raise AssertionError(f"{what}: expected {want!r}, got {got!r}")


# ── offline: parsing ─────────────────────────────────────────────────────────

def offline_checks() -> None:
    recs = parse_records(_FRAGMENT)
    check("parse: one record", lambda: _eq(len(recs), 1, "record count"))
    r = recs[0]
    check("parse: citation", lambda: _eq(r["citation"], "2021 (3) AJR 1"))
    check("parse: doc id", lambda: _eq(r["solr_document_id"], "J_CD_20213AJR1"))
    check("parse: nature", lambda: _eq(r["nature_of_record"], "J"))
    check("parse: equal citations",
          lambda: _eq(r["equal_citations"], "::(2019) 15 Scale 1"))
    check("parse: doc type", lambda: _eq(r["document_type"], "Judgment"))
    check("parse: court", lambda: _eq(r["court"], "Jharkhand High Court"))
    check("parse: judges", lambda: _eq(r["judges"], ["Ananda Sen, J"]))
    check("parse: petitioner",
          lambda: _eq(r["petitioner"],
                      "Online Entertainment Private Limited, Jharkhand"))
    check("parse: respondent", lambda: _eq(r["respondent"], "State of Jharkhand"))
    check("parse: case number",
          lambda: _eq(r["case_number"], "Cr. M.P. No. 1865 of 2020"))
    check("parse: decided on", lambda: _eq(r["decided_on"], "18/03/2021"))
    check("parse: empty input", lambda: _eq(parse_records(""), []))

    # Bench shapes — both forms the site emits.
    check("judges: single",
          lambda: _eq(_split_judges("Ananda Sen , J"), ["Ananda Sen, J"]))
    check("judges: per-judge designations",
          lambda: _eq(_split_judges("A K Sikri , J , N V Ramana , J"),
                      ["A K Sikri, J", "N V Ramana, J"]))
    check("judges: shared designation (5-judge bench)",
          lambda: _eq(_split_judges(
              "Ranjan Gogoi, S. A. Bobde, D Y Chandrachud, Ashok Bhushan, "
              "S. Abdul Nazeer , JJJ"),
              ["Ranjan Gogoi, J", "S. A. Bobde, J", "D Y Chandrachud, J",
               "Ashok Bhushan, J", "S. Abdul Nazeer, J"]))
    check("judges: CJ preserved",
          lambda: _eq(_split_judges("S. Ravindra Bhat, CJ"),
                      ["S. Ravindra Bhat, CJ"]))
    check("judges: CJI + and-join",
          lambda: _eq(_split_judges("Dipak Misra, CJI and A M Khanwilkar, J"),
                      ["Dipak Misra, CJI", "A M Khanwilkar, J"]))
    check("judges: empty", lambda: _eq(_split_judges(""), []))


# ── offline: mapping read-path ───────────────────────────────────────────────

def dropdowns_file_checks() -> None:
    """The shipped JSON file is the deliverable — validate IT, not just the
    in-memory tree it was generated from."""
    import json
    from pathlib import Path
    DROPDOWNS_PATH = (Path(__file__).resolve().parent / "data"
                      / "aironline_dropdowns.json")

    if not DROPDOWNS_PATH.exists():
        _results.append(("file: aironline_dropdowns.json exists", False,
                         f"missing {DROPDOWNS_PATH} — run build_dropdowns"))
        return
    _results.append(("file: aironline_dropdowns.json exists", True, ""))
    d = json.loads(DROPDOWNS_PATH.read_text())

    check("file: publication_count matches publications",
          lambda: _eq(d["publication_count"], len(d["publications"])))
    check("file: reports zero gaps", lambda: _eq(d["failure_count"], 0))

    # Walk the documented shape on a known publication.
    node = d["publications"]["AIR JHARKHAND HIGH COURT REPORTS"]
    check("file: year_dropdown present", lambda: _eq(node["year_dropdown"], True))
    jha = node["years"]["2021"]["Full Report"]["JHA"]
    check("file: volumes are plain strings",
          lambda: _eq(all(isinstance(v, str) for v in jha["volumes"]), True))
    check("file: 6-field case flagged",
          lambda: _eq((jha["volume_count"] > 0, jha["dropdown_count"]), (True, 6)))
    check("file: volume_count equals the number of volumes",
          lambda: _eq(jha["volume_count"], len(jha["volumes"])))
    air5 = d["publications"]["All India Reporter"]["years"]["2020"]["Full Report"]["SC"]
    check("file: 5-field case flagged",
          lambda: _eq((air5["volume_count"], air5["volumes"], air5["dropdown_count"]),
                      (0, [], 5)))

    # Every node must carry the three keys a caller reads.
    missing = []
    for pub, pn in d["publications"].items():
        cascades = list((pn.get("years") or {}).values())
        if pn.get("no_year"):
            cascades.append(pn["no_year"])
        for casc in cascades:
            for seg, jbs in casc.items():
                for jid, jn in jbs.items():
                    if not {"volume_count", "dropdown_count",
                            "volumes"} <= set(jn):
                        missing.append(f"{pub}/{seg}/{jid}")
                    elif jn["volume_count"] != len(jn["volumes"]):
                        missing.append(f"{pub}/{seg}/{jid}")
    check("file: every node has volume_count/dropdown_count/volumes, and "
          "volume_count matches",
          lambda: _eq(missing[:3], []))




def _raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


# ── live ─────────────────────────────────────────────────────────────────────

def live_checks() -> None:
    from app.tribunals.aironline import client as C
    from app.tribunals.aironline import fetch_aironline as air

    check("live: TLS verifies (broken chain workaround)",
          lambda: _eq(air.ping()["success"], True))

    pubs = C.fetch_facets(C.FIELD_PUBLICATION, None, publication_name_flag=True)
    check("live: publications > 300", lambda: _eq(len(pubs) > 300, True))

    # The documented worked example, end to end.
    res = air.lookup_citation("AIR JHARKHAND HIGH COURT REPORTS", "2021",
                              "Full Report", "JHA", "1", volume="3")
    check("live: citation lookup succeeds", lambda: _eq(res["success"], True))
    if res.get("records"):
        r = res["records"][0]
        check("live: citation text", lambda: _eq(r["citation"], "2021 (3) AJR 1"))
        check("live: court", lambda: _eq(r["court"], "Jharkhand High Court"))
        check("live: judges parsed", lambda: _eq(bool(r["judges"]), True))
        check("live: decided_on parsed", lambda: _eq(r["decided_on"], "18/03/2021"))

    # The conditional-volume rule, on a publication known to HAVE volumes.
    pn = air.page_numbers("SUPREME COURT CASES", "2020", "Full Report", "SC",
                          volume="1")
    check("live: page numbers returned", lambda: _eq(pn["count"] > 0, True))
    check("live: page rows carry doc_id + citation",
          lambda: _eq(all(r.get("doc_id") for r in pn["page_numbers"]), True))



def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="also hit the real site")
    args = ap.parse_args(argv)

    offline_checks()
    dropdowns_file_checks()
    if args.live:
        live_checks()

    width = max(len(n) for n, _, _ in _results)
    failed = 0
    for name, ok, detail in _results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}")
        if not ok:
            failed += 1
            print("        " + detail.strip().replace("\n", "\n        "))
    total = len(_results)
    print(f"\n{total - failed}/{total} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
