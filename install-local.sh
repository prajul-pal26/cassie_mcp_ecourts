#!/bin/sh
# One-time setup. It installs only the small MCP bridge. The heavy gateway is
# downloaded and started later, on the first case lookup.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 -m venv "$ROOT/mcp/.venv"
"$ROOT/mcp/.venv/bin/python" -m pip install --upgrade pip
"$ROOT/mcp/.venv/bin/python" -m pip install "$ROOT/mcp"
printf '%s\n' "Cassie eCourts MCP is installed. Add this folder as a local plugin in Codex."
