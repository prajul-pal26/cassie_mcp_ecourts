import json
import unittest
from unittest.mock import patch

from ecourts_mcp.client import GatewayClient, GatewayError
from ecourts_mcp.presentation import present_case_result


class _Response:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class GatewayClientTests(unittest.TestCase):
    def test_lookup_normalizes_inputs_and_sends_optional_court_type(self):
        seen = {}

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            return _Response({"success": True, "data": {"cnr": "ABCD123456789012"}})

        client = GatewayClient("https://gateway.example/", api_key="secret", timeout_seconds=12)
        with patch("ecourts_mcp.client.urlopen", fake_urlopen):
            result = client.lookup_case(" abcd123456789012 ", "HC")

        self.assertTrue(result["success"])
        self.assertEqual(
            seen["url"],
            "https://gateway.example/api/case/ABCD123456789012?normalize=1&court_type=hc",
        )
        self.assertEqual(seen["timeout"], 12)

    def test_lookup_rejects_invalid_cnr_before_network(self):
        client = GatewayClient("https://gateway.example")
        for cnr in ("", "ABCDE123456789012", "ABCD12345678901X"):
            with self.subTest(cnr=cnr), self.assertRaisesRegex(GatewayError, "Invalid CNR"):
                client.lookup_case(cnr)

    def test_lookup_rejects_unknown_court_type(self):
        with self.assertRaisesRegex(GatewayError, "court_type"):
            GatewayClient("https://gateway.example").lookup_case("ABCD123456789012", "sc")

    def test_lookup_turns_network_timeout_into_helpful_message(self):
        client = GatewayClient("https://gateway.example", timeout_seconds=1)
        with patch("ecourts_mcp.client.urlopen", side_effect=TimeoutError("slow upstream")):
            with self.assertRaisesRegex(GatewayError, "did not respond in time"):
                client.lookup_case("ABCD123456789012")

    def test_failed_presentation_explains_how_to_check_cnr(self):
        result = present_case_result({"success": False, "error": "case not found"}, "hc")
        self.assertIn("CNR may be incorrect", result["cnr_lookup_guidance"]["message"])
        self.assertIn("16-character CNR", result["cnr_lookup_guidance"]["next_step"])

    def test_presentation_always_includes_summary_quality_and_notice(self):
        result = present_case_result(
            {
                "success": True,
                "court_type": "hc",
                "source": "stale",
                "age_seconds": 300,
                "degraded": True,
                "normalized": True,
                "data": {
                    "cnr": "ABCD123456789012",
                    "case_no": "12/2026",
                    "case_type": "Writ Petition",
                    "status": "pending",
                    "court": {"name": "Example High Court", "level": "hc"},
                    "dates": {"next_hearing_iso": "2026-10-01"},
                    "parties": {"petitioners": [{"name": "A"}], "respondents": [{"name": "B"}]},
                    "orders": [],
                },
            },
            None,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["court_type_used"], "hc")
        self.assertIn("CNR: ABCD123456789012", result["case_summary"])
        self.assertTrue(result["warnings"])
        self.assertIn("not legal advice", result["legal_notice"])
        self.assertEqual(result["cassie_next_steps"]["more_searches"]["url"], "https://cassie.in/")
