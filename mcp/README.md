# eCourts Case Lookup MCP

This is a provider-neutral, read-only [Model Context Protocol](https://modelcontextprotocol.io/) plugin for the repository's FastAPI gateway. It exposes one tool, `lookup_case_by_cnr`, which calls the gateway's existing `GET /api/case/{cnr}` endpoint.

## What the MCP server guarantees

No agent skill is needed. The MCP server itself validates the CNR, calls the gateway, and always returns:

- `case_summary`: a fixed, ready-to-display case summary.
- `data_quality`: source, age, stale/degraded state, normalization state, and completeness when supplied by the gateway.
- `warnings`: a stale/degraded warning, incomplete-record warning, sensitive-matter warning, or automatic-High-Court-selection warning when applicable.
- `legal_notice`: a consistent notice that this is information, not legal advice.
- `court_type_used`: the District Court (`dc`) or High Court (`hc`) route selected by the gateway.
- `case_details`: the complete normalized gateway response for clients that need it.

An LLM can still reword its final reply. A client that needs the exact format should display `case_summary`, `warnings`, and `legal_notice` unchanged.

## Run locally

Start the existing gateway first:

```sh
cd production/cassie-gateway-fastapi
./run_local.sh
```

In another terminal, install and run the MCP server:

```sh
cd integrations/ecourts-mcp
python -m pip install -e .
ECOURTS_GATEWAY_URL=http://127.0.0.1:9021 ecourts-case-mcp
```

The MCP process uses standard input/output. Do not start it in a terminal for human interaction; let an MCP client start it from its configuration.

The adapter requires `ECOURTS_GATEWAY_URL`. For a protected remote gateway, also set `ECOURTS_GATEWAY_API_KEY`; the adapter sends it as `X-API-Key`. The current FastAPI service does not enforce that header, so add gateway authentication before publishing it on the Internet.

## Connect any MCP client

Install the package above so `ecourts-case-mcp` is on `PATH`, then configure one of these clients:

- **Codex:** merge [`configs/codex.config.toml`](configs/codex.config.toml) into `~/.codex/config.toml`.
- **Claude Code:** run `claude mcp add --scope user ecourts-case-lookup -- ecourts-case-mcp`, or merge [`configs/claude-code.mcp.json`](configs/claude-code.mcp.json) into `.mcp.json`.
- **Google Antigravity:** merge [`configs/antigravity.mcp_config.json`](configs/antigravity.mcp_config.json) into `.agents/mcp_config.json` (or `~/.gemini/config/mcp_config.json`).

Each client must receive its own configuration because their configuration locations differ; the MCP server and tool schema are shared.

## Publishing safely

For team or public use, deploy the FastAPI gateway and MCP server as separate services. Keep the gateway behind TLS and authentication, restrict CORS to intended clients, rate-limit by authenticated tenant, and log only the minimum necessary request metadata. Do not put a permanent API key in a committed MCP config; use each client’s secret/environment-variable mechanism.

The server intentionally has only one read-only tool. Add other endpoints only after defining their input schema, authorization, and privacy behavior.
