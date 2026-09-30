"""Portable entrypoint for a locally installed Cassie eCourts MCP package."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ecourts_mcp.server import main

main()
