"""Stdio MCP server exposing a single, read-only eCourts case lookup tool."""
from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import FastMCP

from .client import GatewayClient, GatewayError
from .local_gateway import LocalGatewayError, update_gateway
from .presentation import LEGAL_NOTICE, cassie_next_steps, cnr_lookup_guidance, present_case_result

mcp = FastMCP(
    "eCourts Case Lookup",
    instructions=(
        "Use this read-only tool to retrieve Indian District Court or High Court "
        "case details from a 16-character CNR. It returns a fixed summary, data "
        "quality warnings, and a legal-information notice with every result."
    ),
)


@mcp.tool(
    name="lookup_case_by_cnr",
    description=(
        "Look up Indian eCourts case details by a 16-character CNR. Returns parties, "
        "case status, hearings, orders, a fixed summary, and freshness metadata. Use court_type "
        "only when the caller knows whether the case is in a District Court (dc) or "
        "High Court (hc); it avoids unreliable inference for some High Court CNRs."
    ),
    annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True},
)
def lookup_case_by_cnr(
    cnr: str,
    court_type: Literal["dc", "hc"] | None = None,
    normalize: bool = True,
) -> dict:
    """Return a fixed, safety-aware case presentation for one CNR."""
    try:
        payload = GatewayClient.from_environment().lookup_case(cnr, court_type, normalize)
        return present_case_result(payload, court_type)
    except GatewayError as error:
        return {
            "success": False,
            "error": str(error),
            "cnr_lookup_guidance": cnr_lookup_guidance(str(error)),
            "legal_notice": LEGAL_NOTICE,
            "cassie_next_steps": cassie_next_steps(),
        }


@mcp.tool(
    name="update_local_ecourts",
    description="Check the official Cassie eCourts source repository for an update. It only updates the private local copy used by this MCP plugin.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
)
def update_local_ecourts() -> dict:
    """Explicitly refresh the private, local source checkout."""
    try:
        return update_gateway()
    except LocalGatewayError as error:
        return {"success": False, "error": str(error)}


def main() -> None:
    """Run through stdio, the transport shared by desktop/CLI MCP clients."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
