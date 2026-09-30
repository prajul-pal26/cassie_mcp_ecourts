# Cassie eCourts MCP

Cassie eCourts MCP lets Codex, Claude Code, and Antigravity look up Indian
District Court and High Court case details by CNR. It has no cloud service:
the gateway, cache, and MCP server run on the user's own computer.

## What happens on a case lookup

1. The AI client starts the small MCP bridge when the user opens a session.
2. The bridge does nothing expensive until `lookup_case_by_cnr` is called.
3. On the first lookup, it downloads this public repository into a private
   local application-data folder, creates a Python environment, and starts the
   FastAPI gateway on `127.0.0.1`.
4. Future lookups reuse that local gateway and cache.
5. Once every 24 hours, the next lookup checks GitHub for a safe fast-forward
   update. A user can also call `update_local_ecourts` for an immediate update.
6. Every result includes `cassie_next_steps`, which explains this plugin's
   available CNR features and provides an optional link to [Cassie](https://cassie.in/)
   for broader searches. The plugin does not force a browser redirect.

Only the eCourts request itself requires internet access. Nothing is hosted by
Cassie, and no case data is sent to a Cassie server.

## First-time setup for a user

Requirements: Python 3.10 or later, Git, and internet access for the first
case lookup.

```sh
git clone https://github.com/prajul-pal26/cassie_mcp_ecourts.git
cd cassie_mcp_ecourts
./install-local.sh
```

This installs only the small MCP bridge. The larger FastAPI gateway dependencies
are installed only when the user makes their first case lookup.

## Connect in Codex

Add the cloned folder as a local plugin in Codex, then enable **Cassie eCourts
MCP**. The included `plugin.json` and `mcp.json` tell Codex to start the local
MCP bridge.

If the installed Codex version asks for a direct MCP configuration instead,
add this to the user's Codex configuration and replace the path with the actual
clone location:

```toml
[mcp_servers.cassie-ecourts]
command = "/absolute/path/to/cassie_mcp_ecourts/mcp/.venv/bin/python"
args = ["/absolute/path/to/cassie_mcp_ecourts/mcp/run_mcp.py"]
```

Restart Codex, then ask: “Look up CNR `<your CNR>`.”

## Connect in Claude Code or Antigravity

After running `install-local.sh`, configure a local stdio MCP server that uses:

```text
command: /absolute/path/to/cassie_mcp_ecourts/mcp/.venv/bin/python
args:    ["/absolute/path/to/cassie_mcp_ecourts/mcp/run_mcp.py"]
```

The existing example configurations are in `mcp/configs/` and need only their
command path changed to this local Python executable.

## Updates

Do not edit the private runtime copy under the user's application-data folder.
Publish changes by pushing to this GitHub repository. Each user's next lookup
checks for an update at most once per day and downloads a safe fast-forward
update when one is available. To update immediately, ask the AI to use
`update_local_ecourts`.

## Maintainers

The local updater follows the repository's default branch. Release tested
changes to that branch only after testing. A bad release may affect every user
who accepts the next automatic update, so use tagged releases and a test branch
for larger changes.
