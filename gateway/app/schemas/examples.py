"""Example request bodies surfaced in the Swagger UI via each route's
`openapi_extra`. Kept out of the router modules so the handlers stay lean.

These are documentation only — they never affect validation, which the
handlers perform manually to preserve the legacy JSON error contract.
"""
from __future__ import annotations


def _body(example: dict) -> dict:
    """Wrap an example dict into the OpenAPI requestBody fragment shape."""
    return {
        "requestBody": {
            "content": {"application/json": {"example": example}},
        }
    }


FIND_EXAMPLE = _body({
    "search_term": "Ram Kumar",
    "court_type": "dc",
    "state_code": "26",
    "district_code": "1",
    "courts": [{"court_code": "1"}],
    "modes": ["party", "advocate"],
})

FIND_SMART_EXAMPLE = _body({
    "court_level": "dc",
    "where": {"state_code": "26", "district_code": "1"},
    "who": {"party_name": "Ram Kumar"},
    "refine": {"year": "2024", "status": "both", "expand_strategy": "single"},
    "courts": [{"court_code": "1"}],
})

CASE_BATCH_EXAMPLE = _body({
    "cnrs": ["DLST020142782022", "MHPU010104452016"],
    "normalize": True,
})

ORDER_URL_EXAMPLE = _body({
    "state_cd": "26",
    "dist_cd": "1",
    "court_code": "1",
    "caseno": "0",
    "filename": "example.pdf",
    "cCode": 1,
    "appFlag": "1",
})
