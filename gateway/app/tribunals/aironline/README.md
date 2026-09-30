# AIROnline — Citation Search

Reverse-engineered integration for the Citation Search on
<https://aol1.aironline.in/legal-citations.html>, exposed through the gateway
as the `AIROnline` section in Swagger.

Local only. Nothing here is deployed to Fly.

---

## The site's protocol

Everything below was read out of the page's own JavaScript
(`webresources/webworld/scripts/citationSearch_visitor.min.js`) and then
verified live against the server.

The form is **six dropdowns**, each one a separate request whose options depend
on every choice above it:

| # | Dropdown | `entityFieldEnum` |
|---|----------|-------------------|
| 1 | Publication Name | `PUBLICATION_FULL_NAME` |
| 2 | Publication Year | `PUBLICATION_YEAR` |
| 3 | Publication Segment (Full Report / NOC) | `PUBLICATION_SEGMENT_FULL_NAME` |
| 4 | Judicial Body / Court Name | `JUDICIAL_BODY_SHORT_NAME` |
| 5 | Publication Volume — **conditional** | `PUBLICATION_VOLUME_NUMBER` |
| 6 | Publication Number (the citation) | `PUBLICATION_PAGE_NUMBER` |

### Endpoint 1 — the dropdowns

```
POST /loadVisitorCitationData.html          (form-encoded)
  searchString          {"facetRequestFields":[{"entityFieldEnum":"<FIELD>",
                          "noOfFacets":500,"filterPrefix":null}],
                         "filters":[ <EQUALTO clause per chosen value> ]}
  publicationNameFlag   "true" only for dropdown 1
  publicationNumberFlag "true" only for dropdown 6
```

Answers a JSON array of `{id, value, count, filterParamValue2, …}`. `id` is the
label shown to the user, `value` is what gets sent back as a filter. For
dropdown 6 the payload is richer: `value` is the **Solr document id** and
`filterParamValue2` is the formatted citation (`2021 (3) AJR 1`).

### Endpoint 2 — the record

```
POST /loadVisitorCitationCaseContent.html
  ?searchString=<the same JSON, filters incl. PUBLICATION_SEGMENT_NUMBER=<page>>
  &solrDocumentId=<value from dropdown 6>
  &citationText=<filterParamValue2 from dropdown 6>
```

Answers an **HTML fragment** (not JSON) — the markup the page injects into
`div#fetchedData`. `parse.py` turns it into structured fields.

Note the page number is sent back under `PUBLICATION_SEGMENT_NUMBER`, **not**
`PUBLICATION_PAGE_NUMBER`. That asymmetry is in the site's own JS.

---

## Corner cases (all verified live, all handled)

These are the things that make a naive implementation silently wrong:

1. **The form is 5 or 6 fields, depending on the selection.** When the volume
   facet comes back empty, or every value is `""` / `"0"` / null, the page
   *hides* the Publication Volume dropdown and jumps to page numbers. Real
   example: `All India Reporter / 2020 / Full Report / SC` → hidden (5 fields);
   `SUPREME COURT CASES / 2020 / Full Report / SC` → 17 volumes (6 fields).
   Every relevant response carries `volume_dropdown` and `dropdown_count`.

2. **Two publications get client-side synthetic years.** For
   *All India Reporter (Weekly)* and *AIR Supreme Court Weekly* the page
   **prepends years 2016..current** that the server never returns. Replicated
   and flagged `synthetic: true` — without this those years are missing from
   the mapping but selectable on the real site.

3. **One publication is injected client-side.** *All India Reporter (Weekly)*
   is appended to the dropdown by JavaScript, not returned by the facet call.

4. **Some publications have no years at all** — the page hides the Year field
   and filters segments by publication alone. Reported as
   `year_dropdown: false`, never as an empty list.

5. **Empty string is a REAL selectable value**, for both segment and volume.
   It is preserved, not dropped — dropping it makes valid combinations
   unreachable.

6. **`AIR Supreme Court Weekly` pins `JUDICIAL_BODY_SHORT_NAME=SC`** when
   listing volumes, regardless of what is selected. Replicated.

7. **Volumes genuinely vary per judicial body** — verified, not assumed
   (AJR 2021: `JHA` has 4 volumes, `SC` has 2). So the crawl cannot be
   collapsed to one volume call per segment.

8. **Benches come in two shapes.** Either per-judge designations
   (`A , J , B , J`) or one shared designation for the whole bench
   (`A, B, C, D, E , JJJ` — a five-judge bench). Both are split into one entry
   per judge.

### TLS

`aol1.aironline.in` serves a **broken certificate chain**: its leaf is signed
by *Sectigo Public Server Authentication CA DV R36*, but the server sends an
unrelated Sectigo intermediate instead. Browsers and curl recover by fetching
the missing cert via the leaf's AIA extension; Python does not, so every
request fails `CERTIFICATE_VERIFY_FAILED`.

Rather than disable verification, the correct intermediate ships here as
`sectigo_intermediate.pem` and is merged with certifi at runtime — the
connection stays **fully verified**. `AIRONLINE_INSECURE=1` disables
verification as a last resort.

---

## The mapping

One file, `data/aironline_dropdowns.json`, holds the whole cascade. It is
self-contained: plain values, no indirection, nothing else to load.

### Depth

The file covers the **five selection levels**
(publication → year → segment → judicial body → volume). The sixth level —
Publication Number (page) — is fetched **live** by the endpoint.

That split is deliberate. Levels 1–5 are 60,281 combinations and stable. Level 6
is 120,357 separate lists that change whenever AIROnline publishes, and it
cannot be shortened: the facet **deduplicates by page number**, so asking for
several volumes at once silently returns one document id per page — and 228 of
AJR 2021's pages exist in more than one volume, each a different judgment. A
merged crawl would look complete while returning the wrong judgment for those.

### How it was built

The file is the product of a one-off crawl of the site's facet endpoint: 351
publications -> 8,428 (publication, year) units -> 78,932 requests in 1h58m,
followed by a repair pass that re-fetched the 27 nodes which had failed under
concurrency (all 27 succeeded on retry; one alone restored 11 judicial bodies).
The result has **0 gaps**.

The crawl scripts have been removed now that the file is complete and
self-contained — it holds plain values and depends on nothing else. If AIROnline
adds publications or years and the file needs refreshing, the protocol is fully
documented above and in `client.py`.

### What the mapping says about the form

Of the 60,281 fully-specified combinations:

| Form shape | Combinations |
|------------|--------------|
| 6 fields (volume shown) | 40,030 |
| 5 fields (volume hidden) | 20,230 |

plus one publication (*MANUPATRA LAW REPORTS (DEL)*) with no years at all — a
4-field form. So all three shapes are real, and the count is not guessable from
the publication alone.

---

## API

**The dropdown values are a FILE, not an endpoint.** There is exactly one
route:

| Endpoint | Purpose |
|----------|---------|
| `GET /aironline/citation` | the record: citation, court, judges, parties, case no, decision date |

Everything needed to build the form is in
[`data/aironline_dropdowns.json`](data/aironline_dropdowns.json) — read it off
disk. Nothing is served over HTTP to populate a dropdown.

### The dropdowns file

`data/aironline_dropdowns.json` — **8.6 MB, 351 publications, 0 gaps.** Every
valid combination, plain values, no index indirection:

```jsonc
{
  "generated_at": "...", "publication_count": 351, "failure_count": 0,
  "publications": {
    "AIR JHARKHAND HIGH COURT REPORTS": {
      "year_dropdown": true,
      "years": {
        "2021": {
          "Full Report": {                       // segment
            "JHA": {                             // judicial body
              "volume_dropdown": true,
              "dropdown_count": 6,
              "volumes": ["1", "2", "3", "4", "1.0", ""]
            } } } } } }
}
```

A publication whose Year field the site hides carries `year_dropdown: false`
and puts its cascade under `no_year` instead of `years`.

With this file alone you can answer, offline: which publications exist, which
years each has, which segments and courts follow, which volumes are valid, and
whether the form has 4, 5, or 6 fields.


### Example (the worked case)

```bash
curl "http://127.0.0.1:9021/aironline/citation?\
publication=AIR%20JHARKHAND%20HIGH%20COURT%20REPORTS&year=2021&\
segment=Full%20Report&judicial_body=JHA&volume=3&page=1"
```

```json
{"success": true, "citation": "2021 (3) AJR 1", "doc_id": "J_CD_20213AJR1",
 "records": [{
   "citation": "2021 (3) AJR 1",
   "court": "Jharkhand High Court",
   "judges": ["Ananda Sen, J"],
   "petitioner": "Online Entertainment Private Limited, Jharkhand",
   "respondent": "State of Jharkhand",
   "case_number": "Cr. M.P. No. 1865 of 2020",
   "decided_on": "18/03/2021",
   "solr_document_id": "J_CD_20213AJR1",
   "nature_of_record": "J",
   "equal_citations": null,
   "document_type": "Judgment"}]}
```

`solr_document_id`, `nature_of_record` and `equal_citations` come from hidden
inputs/spans in the fragment — they never render on the site's own page.
