# MCP PaintFE

This project is intended for working with PaintFE as a local MCP server. PaintFE is a graphics editor that can accept commands from an external AI client through the Model Context Protocol (MCP).

## What is MCP in PaintFE?

PaintFE can act as a local MCP server for an external agent. The protocol uses standard JSON-RPC over stdin/stdout. This is a local stdio server, not an HTTP service.

Key features:

- PaintFE runs as a regular executable with the `--mcp` flag
- the connection must come from a local MCP client running in the same environment
- the PaintFE GUI must remain open and active
- local AI clients can inspect document previews, but PaintFE itself does not make network requests to models
- before editing, you must first fetch the current `document`, `layer`, and `revision`

## Connection setup

In PaintFE, open:

- Preferences > AI
- enable Local agent connections
- enable document editing and script permissions separately if needed

Example client configuration:

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

## Core MCP tools

PaintFE supports tools such as:

- `documents` — lists open documents, layers, and revisions
- `preview` — returns a bounded PNG preview of the document
- `job_status` — reports the status of the current asynchronous operation
- `cancel` — cancels a pending task
- `stroke` — draws strokes or points
- `shape` — draws an outlined rectangle or ellipse
- `script` — runs a restricted Rhai script (requires a separate permission)
- `activate_layer` — selects the active layer
- `new_layer` — creates a new layer
- `layer_opacity` — changes the layer opacity
- `select_rectangle` — selects a rectangular region
- `deselect` — clears the selection
- `undo` / `redo` — undoes or redoes changes

## Safe-work rules

Before editing, keep the following in mind:

- do not use stale `revision` values — after any change, refresh the identifiers
- editing works only with supported raster layers
- the active layer must be an 8-bit raster layer
- masks and text layers cannot be edited through this toolset
- each completed drawing operation counts as a separate undo entry
- after launching an asynchronous operation, always wait for `job_status`

## Workflow

A typical flow is:

1. Connect to PaintFE over MCP
2. Call `documents`
3. Obtain the current `document`, `layer`, and `revision`
4. Run `stroke`, `shape`, `layer_opacity`, and similar calls
5. Wait for `job_status`
6. Refresh document state and continue with the next operation
7. Save the result manually in PaintFE

## Example connection check

This project includes a Python script to verify the MCP connection and draw test points:

```bash
python paintfe_mcp_test.py
```

To specify the executable path explicitly:

```bash
python paintfe_mcp_test.py --command "C:\Program Files\PaintFE\PaintFE.exe" --arg="--mcp"
```

The script launches `PaintFE.exe --mcp` as a child process. This starts a new PaintFE instance in MCP mode, and the GUI window is not opened in this mode. If you do not see PaintFE under the Task Manager **Apps** section, check the **Details** tab instead — the process will appear there using the PID printed by the script.

> Important: PaintFE in `--mcp` mode does not respond to JSON-RPC requests until Local agent connections are enabled in the settings. If the script times out after "Sending initialize handshake to MCP server", open PaintFE manually, go to Preferences > AI, enable Local agent connections, and then restart the script.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| "PaintFE process is running" followed by a timeout | Local agent connections are disabled | Enable Local agent connections in Preferences > AI |
| The process is not visible in the Task Manager Apps list | PaintFE is running headless in `--mcp` mode | Check the Details tab by PID from the script output |
| `PaintFE executable not found` | Incorrect EXE path | Pass `--command` or fix `DEFAULT_CONFIG` |
| `stale revision` error | IDs are outdated after a change | Call `documents` again and fetch the fresh values |

## Useful project links

- [docs/automation.md](docs/automation.md) — technical documentation for MCP PaintFE
- [paintfe_mcp_test.py](paintfe_mcp_test.py) — example client for checking the connection and drawing test points

## Note

This project does not automatically connect PaintFE to external AI services. All data stays local; if a remote model provider is used, its client may receive document previews, but PaintFE itself does not upload the document without your choice of client.
