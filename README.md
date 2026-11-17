# MCP PaintFE

This repository shows how to use PaintFE as a local MCP server. PaintFE is a graphics editor that can accept commands from an external AI client through the Model Context Protocol (MCP).

![Dog as drawn by MiniMax 3.1 Flash](output/dog_minimax_31flash.png)

## What MCP means in PaintFE

PaintFE can expose a local MCP server for an external agent. The protocol uses JSON-RPC over stdio (stdin/stdout), so this is a local stdio server rather than an HTTP service.

Key points:

- PaintFE runs as a regular executable with the `--mcp` flag.
- The client must run locally and connect from the same environment.
- The PaintFE GUI must remain open and active while the connection is live.
- Local AI clients can inspect document previews, but PaintFE itself does not make network requests to models unless the client chooses to do so.
- Before any edit, fetch the current `document`, `layer`, and `revision` values.

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

- `documents` — lists open documents, their layers, and current revisions
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

- never reuse stale `revision` values; refresh them after each change
- editing works only with supported raster layers
- the active layer should be an 8-bit raster layer
- masks and text layers cannot be edited through this toolset
- each completed drawing operation counts as a separate undo entry
- after launching an asynchronous operation, always wait for `job_status`

## Typical workflow

A standard workflow is:

1. Connect to PaintFE over MCP.
2. Call `documents`.
3. Read the current `document`, `layer`, and `revision` values.
4. Run `stroke`, `shape`, `layer_opacity`, and similar calls.
5. Wait for `job_status`.
6. Refresh document state and continue with the next operation.
7. Save the result manually in PaintFE.

## Example connection check

This project includes a Python script that verifies the MCP connection and draws test points:

```bash
python paintfe_mcp_test.py
```

To point the script at a specific PaintFE installation:

```bash
python paintfe_mcp_test.py --paintfe "C:\Program Files\PaintFE\PaintFE.exe"
```

The script launches `PaintFE.exe --mcp` as a child process. In this mode, the application runs headless and does not open a GUI window. If you do not see PaintFE under the Task Manager **Apps** section, check the **Details** tab instead; the process will appear there with the PID printed by the script.

> Important: PaintFE in `--mcp` mode does not respond to JSON-RPC requests until Local agent connections are enabled in Preferences > AI. If the script times out after "Sending initialize handshake to MCP server", open PaintFE manually, go to Preferences > AI, enable Local agent connections, and then rerun the script.

## Drawing a dog 256x256 image by AI models

The only good result I've received so far is the one below, made by MiniMax 3.1 Flash (18M tokens spent (17.6 cached), took about 3 hours). Other models tested (DeepSeek 4.1 Flash, Qwen 3.8 Flash, GLM 5.3 Flash, ChatGPT 6 Luna) all gave results that were far from good (but they spent way less time and tockens) -- resuls are present in [output\dog.pfe](output\dog.pfe) file.

### MiniMax 3.1 Flash

![Dog as drawn by MiniMax 3.1 Flash](output/dog_minimax_31flash.png)

`paintfe_dog_minimax_31flash.py` uses the same MCP client but paints a stylised dog into a 256x256 document:

```bash
python paintfe_dog_minimax_31flash.py
```

Options:

| Flag | Purpose |
| --- | --- |
| `--paintfe` | Path to the PaintFE executable |
| `--layer` | Name of the new artwork layer (default `MCP dog`) |
| `--doc` | Name of the open document to draw on (default: a 256x256 document) |
| `--no-draw` | Only inspect the document |
| `--no-preview` | Do not save the PNG preview |
| `--preview-path` | Where the PNG preview is written (default `output/dog-preview.png`) |

The artwork is authored in a 256x256 design space and scaled to the open document, so it lands exactly on a 256x256 canvas. It is rasterised in Python and sent as pencil strokes, because the MCP brush in PaintFE 1.5.0 only paints the rim of a stroke — the automation path never rebuilds the brush alpha lookup table for the requested size, so the interior of every dab stays empty. The pencil path uses Bresenham's algorithm and writes exact pixels.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Timeout after the handshake begins | Local agent connections are disabled | Enable Local agent connections in Preferences > AI |
| The process is not visible in the Task Manager Apps list | PaintFE is running headless in `--mcp` mode | Check the Details tab using the PID shown by the script |
| `PaintFE executable not found` | Incorrect EXE path | Pass `--paintfe` with the correct path or update the default value |
| `stale revision` error | The IDs are outdated after a change | Call `documents` again and fetch the fresh values |

## Useful project links

- [docs/automation.md](docs/automation.md) — technical documentation for MCP PaintFE
- [paintfe_mcp_test.py](paintfe_mcp_test.py) — example client for checking the connection and drawing test points
- [paintfe_dog_minimax_31flash.py](paintfe_dog_minimax_31flash.py) — draws a dog into a 256x256 document over the same client
