# Copilot instructions for MCP PaintFE

## Project goal

This repository is for working with PaintFE over the local MCP protocol. The goal is to connect an external AI client to the PaintFE desktop app and use the supported tools to inspect and edit documents.

## Key facts

- PaintFE exposes an MCP server over stdio JSON-RPC, not HTTP.
- The script launches `PaintFE.exe --mcp` as a child process; in this mode, PaintFE runs without a visible GUI window.
- Because it runs headless, the process does not appear in the Windows Task Manager **Apps** tab; look in the **Details** tab by PID instead.
- Local AI connections are disabled by default and must be enabled in Preferences > AI before the MCP server responds.
- Editing and scripting permissions are separate and must be granted explicitly.
- If the script prints "Sending initialize handshake to MCP server" and then times out, Local agent connections are not enabled in Preferences > AI.

## Example MCP configuration

```json
{
  "mcpServers": {
    "paintfe": {
      "command": "C:\\Program Files\\PaintFE\\PaintFE.exe",
      "args": ["--mcp"]
    }
  }
}
```

## Required workflow for edits

Before editing, always:

1. Call `documents`.
2. Read the current `document`, `layer`, and `revision` values.
3. Use the newest identifiers for the next tool call.
4. Avoid reusing stale revision values.
5. After each change, wait for `job_status` to report completion.
6. Refresh the document state before continuing to the next edit.

## Editing rules

- Use only supported tools: `documents`, `preview`, `stroke`, `shape`, `select_rectangle`, `deselect`, `activate_layer`, `new_layer`, `layer_opacity`, `undo`, `redo`, `job_status`, and `cancel`.
- Prefer active raster layers that are 8-bit.
- Do not attempt mask editing or text-layer operations through this toolset.
- Respect the supported image limits and coordinate system.
- Keep commands limited to valid document pixels and supported parameter values.

## Good practice examples

- Inspect the current document before drawing a new object.
- Request a preview before making complex edits.
- Make small targeted changes and confirm them visually.
- Use `job_status` after `stroke` or `shape` calls.
- Save the result manually in PaintFE after review.

## Testing script

Use the script in the repository root to validate the MCP connection and test drawing:

```bash
python paintfe_mcp_test.py
```

This script performs:

- the MCP handshake (`initialize` and `notifications/initialized`)
- `tools/list` to confirm the server is reachable
- a document inspection
- drawing three colored points on the active image layer

## Troubleshooting

If the connection fails:

- confirm that PaintFE is installed at the configured path
- open PaintFE manually, go to Preferences > AI, enable Local agent connections, and then run the script again
- the process will not appear in the Windows Task Manager Apps tab; check the Details tab using the PID printed by the script
- use the same Windows user profile and storage profile
- check that document editing is enabled

If the script hangs after "Sending initialize handshake to MCP server":

- Local agent connections are not enabled in Preferences > AI; enable them and retry
- the script will now time out cleanly because stdout is read in a background thread with `queue.get(timeout=...)` instead of blocking forever

If a tool call fails because of a stale revision:

- call `documents` again
- read the current `document`, `layer`, and `revision` values again
- retry with only the refreshed values

## Notes for AI agents

- Do not pretend that there is a remote HTTP API when the actual transport is stdio.
- Do not assume that arbitrary GUI operations are possible beyond the MCP tool set.
- Keep the workflow bounded to the supported PaintFE tool surface.
- Do not save or overwrite documents automatically; the user should save via the PaintFE UI.

## Related files

- [docs/automation.md](../docs/automation.md)
- [paintfe_mcp_test.py](../paintfe_mcp_test.py)
