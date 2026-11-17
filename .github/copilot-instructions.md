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
- Use `pencil: true` with `size: 1` for anything that must be pixel-exact. The default brush path is unusable for fills (see the bug section below).
- Clip every point to `0 <= x < width` and `0 <= y < height` before sending; `stroke` rejects any point outside the canvas.
- Remember that each applied stroke or shape is its own undo entry, so clean-up after a mistake needs that many `undo` calls.

## Recommended drawing approach (verified on PaintFE 1.5.0)

Drawing a bitmap through MCP one dab at a time is slow and imprecise. The reliable
pattern is to compute the image offline and then replay it as pencil strokes:

1. Build the artwork offline in a plain RGBA array (`list[tuple[int, int, int, int]]`
   per pixel). Fill every shape solidly and keep every pixel fully opaque; assert this.
2. Rasterise shapes with your own primitives (filled ellipse, capsule, polygon,
   even-odd scanline, Bezier sampling). Draw cartoon outlines by filling a slightly
   larger dark shape first and the coloured shape on top.
3. Convert the **finished** image into horizontal runs of identical colour, one list
   of runs per row. Working from the finished pixels, not from the shape list, is
   what guarantees that no interior is left empty.
4. Chain runs of the same colour into long serpentine strokes. Before merging two
   runs, walk the connecting line with integer Bresenham and verify that **every**
   connector pixel already has that colour; otherwise start a new stroke instead of
   leaking paint outside the shape.
5. Cap a stroke at ~1,500 points (the tool limit is 4,096) and split instead of
   overflowing.
6. Verify coverage in Python before touching PaintFE: mark every point of every
   planned stroke and fail if any canvas pixel is unmarked.
7. Create one layer, hide the previous ones, then send the strokes one by one,
   refreshing `document`/`layer`/`revision` before each call and waiting for
   `job_status` after each call.
8. Save a local reference PNG, then call `preview` and diff it against the reference.
   Identical bytes-equivalent pixels is the only proof the drawing really landed.
9. After finishing, restore the opacity of every layer you touched. Layers that were
   hidden with `opacity: 0` stay hidden in the user's document otherwise, so set them
   back to `1` (or their original value) before you report the work as done.

Measured result of this pattern on a 256x256 document: 65,536 pixels, 24 colours,
226 strokes, 73,025 points, and a PaintFE preview bit-identical to the offline PNG.

Practical details that saved time:

- `preview` returns the document at native size (256x256 here), so a pixel-exact diff
  against the local reference is possible with Pillow and numpy.
- A one-point pencil stroke paints exactly one pixel, so isolated pixels (an eye
  highlight, a nose shine) are safe to send as single-point strokes.
- Long pencil strokes are cheap: 226 strokes covering the whole canvas complete in
  well under a minute of `job_status` polling.
- Write the reference PNG with the standard library only (`zlib` + `struct`) if the
  script must stay dependency-free.

## Verified tool behaviour notes

- `new_layer` inserts at `active_layer_index + 1` and becomes the active layer; it is
  undoable.
- `layer_opacity` only accepts the **active** layer, so call `activate_layer` first.
- Setting `opacity: 0` really hides a layer, but `documents` still reports
  `"visible": true` for it. Never use `layers[].visible` to decide whether a layer is
  hidden by opacity.
- There is no layer-delete and no visibility toggle: hide obsolete layers with
  `opacity: 0` instead, and restore every layer's opacity when your work is finished.
- `revision` increments on every operation, including `undo`, so re-read it before
  every single call; a batched loop that caches one revision will fail immediately.
- `undo` is the only way to roll back: one `undo` per stroke, then one more for the
  layer that was created.
- Hiding layers is a temporary convenience, not a finished state: a document left with
  several `opacity: 0` layers looks broken to the user, because `documents` still
  reports them as `"visible": true`.

## Known bug: the MCP brush paints only the stroke rim (issue draft)

**Title:** Automation `stroke` with the brush renders only the outline of the dab; the interior is never filled

**Environment:** PaintFE 1.5.0, Windows, MCP over stdio, document `dog.pfe`
(256x256, 8-bit RGBA raster layer), pencil flag `false`, size `15`.

**Summary:** A brush stroke requested through the MCP `stroke` tool paints a hollow
outline instead of a filled band. Only the outer edge of each dab is written, so the
stroke looks like a 1 px contour of the intended shape.

**Steps to reproduce:**

1. Open a 256x256 document, enable Preferences > AI > local agent connections and
   document editing.
2. Call `documents` and read `document`, `active_layer`, `revision`.
3. `new_layer` with name `ZZ brush probe`.
4. `stroke` with `points: [[60, 40], [140, 40]]`, `colour: [255, 0, 255, 255]`,
   `size: 15`, `pencil: false`.
5. Poll `job_status` until `busy: false`, `status: "Applied"`.
6. Call `preview` and inspect the pixels.

**Expected:** a filled capsule about 15 px tall and 95 px wide, i.e. roughly 1,200
magenta pixels, with rows 33-47 solid between x=60 and x=140.

**Actual:** only 20 magenta pixels exist in the whole area. They sit on the upper and
lower rim of the capsule (3, 3, 2, 1, 1 pixels on rows 32-36 and 1, 1, 2, 3, 3 pixels
on rows 43-47), the end caps are hollow arcs, and rows 37-42 are completely empty.
The sampled interior region (rows 35-45, x=75-125) is 0 of 550 pixels filled, i.e.
**0% of the stroke body is painted**. The saved screenshot is
`logs/probe-brush-stroke.png`.

**Impact:** the brush path cannot be used for any fill, silhouette or thick line. Every
agent that needs a solid image must work around it by rasterising offline and sending
dense `pencil: true`, `size: 1` strokes, which is far slower and pushes more data.

**Suspected cause (needs confirming against the tagged source):** the automation
stroke path builds the dab without rebuilding the brush alpha lookup table for the
requested size, so the LUT stays zeroed and only the outermost fraction of a pixel
band receives alpha. The interactive brush path is unaffected.

**Suggested fix:** initialise or rebuild the brush alpha LUT for the requested size
before `render_automation_stroke` dabs, mirroring the interactive brush code.

**Suggested regression test:** draw one stroke of size 15 over a known rectangle,
read back the pixels, and assert that the interior sample block (for example rows
35-45, x=75-125) is fully covered in the requested colour. A rim-only render must fail
the test.

**Workaround:** use `pencil: true` with `size: 1` and send integer pixel chains, as
described in the drawing approach above.

## Good practice examples

- Inspect the current document before drawing a new object.
- Request a preview before making complex edits.
- Make small targeted changes and confirm them visually.
- Use `job_status` after `stroke` or `shape` calls.
- Save the result manually in PaintFE after review.
- Draw offline and replay as pencil strokes instead of painting dabs one by one.
- Assert full pixel coverage in Python before sending anything to PaintFE.
- Diff the returned `preview` against the local reference image; do not trust the
  absence of an error as proof that the drawing is correct.
- Restore the opacity of all layers after finishing, including layers you set to
  `opacity: 0` while drawing, and leave the artwork layer fully opaque.

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
- [paintfe_dog_DeepSeekV41flash.py](../paintfe_dog_DeepSeekV41flash.py) - reference
  implementation of the offline rasterise plus pencil-stroke replay pattern
