"""Shared helpers for the routers.

The gateway's public JSON contract predates FastAPI and returns bespoke
`{"error": ...}` / `{"success": false, ...}` bodies with specific status
codes. To preserve that contract byte-for-byte we:

  - parse request bodies leniently (mirroring Flask's request.get_json(silent=True)),
  - validate manually and return the exact legacy shapes via `json_response`.

That's why the POST handlers take a raw `Request` instead of a Pydantic body
model — a strict model would emit FastAPI's 422 `{"detail": [...]}` shape and
break existing clients. The endpoints are still fully documented in Swagger
via per-route `openapi_extra` examples.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import Request
from fastapi.responses import JSONResponse


def json_response(payload: Any, status: int = 200) -> JSONResponse:
    """Return the exact dict as JSON with the given status (no reshaping)."""
    return JSONResponse(content=payload, status_code=status)


async def parse_json_body(request: Request) -> Optional[Any]:
    """Equivalent of Flask's request.get_json(silent=True) — returns the
    parsed body or None on any parse error (never raises)."""
    try:
        return await request.json()
    except Exception:
        return None
