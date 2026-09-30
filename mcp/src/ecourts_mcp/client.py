"""Small, dependency-free client for the gateway's CNR endpoint."""
from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .local_gateway import LocalGatewayError, ensure_gateway

_CNR_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{2}\d{12}$")
_ALLOWED_COURT_TYPES = {"dc", "hc"}


class GatewayError(RuntimeError):
    """A request to the case gateway could not be completed."""


@dataclass(frozen=True)
class GatewayClient:
    """Client for a gateway instance, configured only through explicit inputs."""

    base_url: str
    api_key: str | None = None
    timeout_seconds: float = 30.0

    @classmethod
    def from_environment(cls) -> "GatewayClient":
        """Create the client from MCP-specific environment variables.

        ECOURTS_GATEWAY_URL is deliberately required: silently defaulting to a
        public endpoint would make it too easy to send case data to an
        unintended deployment.
        """
        try:
            base_url = ensure_gateway()
        except LocalGatewayError as error:
            raise GatewayError(str(error)) from error
        raw_timeout = os.environ.get("ECOURTS_GATEWAY_TIMEOUT_SECONDS", "30")
        try:
            timeout = float(raw_timeout)
        except ValueError as error:
            raise GatewayError("ECOURTS_GATEWAY_TIMEOUT_SECONDS must be a number") from error
        if timeout <= 0:
            raise GatewayError("ECOURTS_GATEWAY_TIMEOUT_SECONDS must be positive")
        return cls(
            base_url=base_url,
            api_key=os.environ.get("ECOURTS_GATEWAY_API_KEY") or None,
            timeout_seconds=timeout,
        )

    def lookup_case(
        self, cnr: str, court_type: str | None = None, normalize: bool = True
    ) -> dict[str, Any]:
        """Fetch one case, validating the stable client-side contract first."""
        cnr_clean = (cnr or "").strip().upper()
        if not _CNR_RE.fullmatch(cnr_clean):
            raise GatewayError(
                "Invalid CNR. It must contain 16 characters: 2 letters, 2 alphanumeric characters, and 12 digits."
            )

        court_type_clean = court_type.strip().lower() if court_type else None
        if court_type_clean and court_type_clean not in _ALLOWED_COURT_TYPES:
            raise GatewayError("court_type must be 'dc' or 'hc' when supplied")

        params: dict[str, str] = {"normalize": "1" if normalize else "0"}
        if court_type_clean:
            params["court_type"] = court_type_clean
        url = (
            f"{self.base_url.rstrip('/')}/api/case/{quote(cnr_clean, safe='')}?"
            f"{urlencode(params)}"
        )
        headers = {"Accept": "application/json", "User-Agent": "ecourts-case-mcp/0.1"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise GatewayError(_gateway_error_message(error.code, body)) from error
        except URLError as error:
            raise GatewayError(f"Could not reach the case gateway: {error.reason}") from error
        except (TimeoutError, socket.timeout) as error:
            raise GatewayError(
                "The official eCourts service did not respond in time. "
                "Please try again shortly and recheck that the 16-character CNR is correct."
            ) from error

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise GatewayError("The case gateway returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise GatewayError("The case gateway returned an unexpected response")
        return payload


def _gateway_error_message(status: int, body: str) -> str:
    """Convert a gateway failure to a concise, safe tool error."""
    try:
        payload: Mapping[str, Any] = json.loads(body)
    except json.JSONDecodeError:
        payload = {}
    detail = payload.get("error") or payload.get("detail") or "request failed"
    return f"Case gateway returned HTTP {status}: {str(detail)[:300]}"
