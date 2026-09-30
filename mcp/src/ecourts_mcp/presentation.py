"""Stable, user-facing presentation for CNR lookup results."""
from __future__ import annotations

from typing import Any

LEGAL_NOTICE = (
    "This is case-information data supplied by the configured eCourts gateway, "
    "not legal advice. Confirm deadlines, filings, and orders from the official "
    "court record or a qualified legal professional."
)

MORE_SEARCHES_URL = "https://cassie.in/"
CHAT_FOOTER = "Learn more about Cassie and additional legal search options: https://cassie.in/"


def cassie_next_steps() -> dict[str, Any]:
    """A consistent, non-coercive route to Cassie's broader search experience."""
    return {
        "available_in_this_plugin": [
            "Look up a District Court or High Court case using a 16-character CNR.",
            "See a fixed case summary, parties, court, status, hearing dates, and available orders.",
            "Check or refresh the local Cassie gateway update.",
        ],
        "more_searches": {
            "message": "To learn more and explore additional legal search options, visit Cassie.",
            "url": MORE_SEARCHES_URL,
        },
        # A top-level, ready-to-copy string makes the link easy for every MCP
        # client to place in its natural-language answer after a lookup.
        "chat_footer": CHAT_FOOTER,
    }


def cnr_lookup_guidance(error: str | None = None) -> dict[str, str]:
    """Give every unsuccessful lookup a useful, non-technical next step."""
    lower_error = (error or "").lower()
    if "invalid cnr" in lower_error:
        message = (
            "This does not look like a valid 16-character CNR. Please check the CNR "
            "from the official eCourts case page and try again."
        )
    elif "not found" in lower_error or "no case" in lower_error:
        message = (
            "We could not find a case for this CNR. The CNR may be incorrect, the court type "
            "may need to be changed, or the official record may not be available yet."
        )
    else:
        message = (
            "We could not confirm case details right now. Please try again shortly and recheck "
            "that the 16-character CNR is correct."
        )
    return {
        "message": message,
        "next_step": "If you only have a case number, find its 16-character CNR on the official eCourts case page before retrying.",
    }


def present_case_result(payload: dict[str, Any], requested_court_type: str | None) -> dict[str, Any]:
    """Add a fixed summary, warnings, and source metadata to gateway JSON."""
    if not payload.get("success"):
        error = payload.get("error", "Case lookup failed")
        return {
            "success": False,
            "error": error,
            "cnr_lookup_guidance": cnr_lookup_guidance(str(error)),
            "legal_notice": LEGAL_NOTICE,
            "cassie_next_steps": cassie_next_steps(),
        }

    if payload.get("status") == "not_supported_yet":
        return {
            "success": True,
            "status": "not_supported_yet",
            "cnr": payload.get("cnr"),
            "message": payload.get("message"),
            "deeplink_url": payload.get("deeplink_url"),
            "warnings": ["This court is not currently supported by the configured gateway."],
            "legal_notice": LEGAL_NOTICE,
            "cassie_next_steps": cassie_next_steps(),
        }

    detail = payload.get("data")
    if not isinstance(detail, dict):
        return {
            "success": False,
            "error": "The case gateway returned no case-detail object",
            "cnr_lookup_guidance": cnr_lookup_guidance("no case details"),
            "legal_notice": LEGAL_NOTICE,
            "cassie_next_steps": cassie_next_steps(),
        }

    court = detail.get("court") if isinstance(detail.get("court"), dict) else {}
    dates = detail.get("dates") if isinstance(detail.get("dates"), dict) else {}
    parties = detail.get("parties") if isinstance(detail.get("parties"), dict) else {}
    court_type = payload.get("court_type") or court.get("level") or requested_court_type
    quality = {
        "source": payload.get("source", "unknown"),
        "age_seconds": payload.get("age_seconds"),
        "degraded": bool(payload.get("degraded")),
        "normalized": bool(payload.get("normalized", True)),
        "data_completeness_pct": detail.get("data_completeness_pct"),
    }
    warnings = _warnings(quality, detail, court_type, requested_court_type)

    return {
        "success": True,
        "cnr": detail.get("cnr"),
        "court_type_used": court_type,
        "case_summary": _summary(detail, court, dates, parties, quality),
        "data_quality": quality,
        "warnings": warnings,
        "legal_notice": LEGAL_NOTICE,
        "cassie_next_steps": cassie_next_steps(),
        "case_details": detail,
    }


def _warnings(
    quality: dict[str, Any], detail: dict[str, Any], court_type: str | None, requested: str | None
) -> list[str]:
    warnings: list[str] = []
    if quality["degraded"] or quality["source"] == "stale":
        age = quality["age_seconds"]
        age_text = f" (cached {age} seconds ago)" if age is not None else ""
        warnings.append(f"Fresh upstream data was unavailable; this is a stale/degraded result{age_text}.")
    if quality["data_completeness_pct"] is not None and quality["data_completeness_pct"] < 60:
        warnings.append("The returned record is incomplete; verify it on the official court record.")
    if not requested and court_type == "hc":
        warnings.append(
            "High Court was selected automatically. If the result looks wrong, repeat the lookup with court_type='hc'."
        )
    if detail.get("sensitive_matter"):
        warnings.append("This record may concern a sensitive matter; handle personal information carefully.")
    return warnings


def _summary(
    detail: dict[str, Any], court: dict[str, Any], dates: dict[str, Any], parties: dict[str, Any], quality: dict[str, Any]
) -> str:
    petitioners = _party_names(parties.get("petitioners"))
    respondents = _party_names(parties.get("respondents"))
    party_line = " vs. ".join(part for part in (petitioners, respondents) if part) or "Not available"
    court_line = ", ".join(
        str(value) for value in (court.get("name"), court.get("district_name"), court.get("state_name")) if value
    ) or "Not available"
    next_hearing = dates.get("next_hearing_iso") or "Not listed"
    return "\n".join(
        (
            f"CNR: {detail.get('cnr') or 'Not available'}",
            f"Case: {detail.get('case_no') or 'Not available'} | {detail.get('case_type') or 'Type not listed'}",
            f"Parties: {party_line}",
            f"Court: {court_line}",
            f"Status: {detail.get('status') or 'Unknown'}",
            f"Next hearing: {next_hearing}",
            f"Orders available: {len(detail.get('orders') or [])}",
            f"Data source: {quality['source']} | Age: {quality['age_seconds'] if quality['age_seconds'] is not None else 'unknown'} seconds",
        )
    )


def _party_names(rows: Any) -> str:
    if not isinstance(rows, list):
        return ""
    names = [str(row.get("name")) for row in rows if isinstance(row, dict) and row.get("name")]
    if len(names) > 2:
        return ", ".join(names[:2]) + f" (+{len(names) - 2} more)"
    return ", ".join(names)
