#!/usr/bin/env python3
"""Draw a simple cartoon dog on an open 256 x 256 PaintFE canvas over MCP."""
import argparse
import math
from pathlib import Path
import sys

from paintfe_mcp_test import (
    Client,
    DEFAULT_PAINTFE_PATH,
    LOG_DIR,
    MCPError,
    RunLog,
    current_document,
    target,
)


CANVAS_SIZE = 256
LAYER_NAME = "Собака MCP"
OUTLINE = [76, 48, 32, 255]
TAN = [190, 125, 73, 255]
MUZZLE = [133, 91, 59, 255]
COLLAR = [178, 54, 49, 255]
TAG = [244, 184, 65, 255]

# Each entry is one supported PaintFE shape or stroke operation. Coordinates
# are deliberately kept inside the 256 x 256 canvas.
DOG_STEPS = (
    ("stroke", {
        "points": [[75, 155], [61, 155], [49, 147], [45, 136], [50, 127], [59, 132]],
        "colour": TAN, "size": 12,
    }),
    ("stroke", {
        "points": [[101, 181], [100, 197], [95, 205], [94, 211], [98, 214],
                   [108, 214], [112, 210], [111, 197], [110, 181]],
        "colour": OUTLINE, "size": 5,
    }),
    ("stroke", {
        "points": [[145, 181], [146, 197], [151, 205], [152, 211], [148, 214],
                   [138, 214], [134, 210], [135, 197], [136, 181]],
        "colour": OUTLINE, "size": 5,
    }),
    ("shape", {
        "shape": "ellipse", "x": 75, "y": 131, "width": 106, "height": 86,
        "colour": OUTLINE, "size": 5,
    }),
    ("stroke", {
        "points": [[85, 73], [77, 78], [73, 88], [75, 100], [82, 106], [91, 101]],
        "colour": TAN, "size": 14,
    }),
    ("stroke", {
        "points": [[171, 73], [179, 78], [183, 88], [181, 100], [174, 106], [165, 101]],
        "colour": TAN, "size": 14,
    }),
    ("shape", {
        "shape": "ellipse", "x": 61, "y": 43, "width": 134, "height": 118,
        "colour": OUTLINE, "size": 5,
    }),
    ("shape", {
        "shape": "ellipse", "x": 95, "y": 99, "width": 66, "height": 46,
        "colour": MUZZLE, "size": 4,
    }),
    ("stroke", {
        "points": [[101, 91]], "colour": OUTLINE, "size": 7,
    }),
    ("stroke", {
        "points": [[155, 91]], "colour": OUTLINE, "size": 7,
    }),
    ("stroke", {
        "points": [[128, 116]], "colour": OUTLINE, "size": 12,
    }),
    ("stroke", {
        "points": [[128, 122], [128, 132], [122, 138], [116, 138]],
        "colour": OUTLINE, "size": 3,
    }),
    ("stroke", {
        "points": [[128, 132], [134, 138], [140, 138]],
        "colour": OUTLINE, "size": 3,
    }),
    ("stroke", {
        "points": [[105, 119], [88, 115]], "colour": MUZZLE, "size": 2,
    }),
    ("stroke", {
        "points": [[105, 128], [87, 131]], "colour": MUZZLE, "size": 2,
    }),
    ("stroke", {
        "points": [[151, 119], [168, 115]], "colour": MUZZLE, "size": 2,
    }),
    ("stroke", {
        "points": [[151, 128], [169, 131]], "colour": MUZZLE, "size": 2,
    }),
    ("stroke", {
        "points": [[94, 153], [110, 159], [128, 161], [146, 159], [162, 153]],
        "colour": COLLAR, "size": 8,
    }),
    ("stroke", {
        "points": [[128, 162], [128, 169]], "colour": TAG, "size": 3,
    }),
    ("stroke", {
        "points": [[128, 171]], "colour": TAG, "size": 8,
    }),
)


def find_canvas(client, document_id=None):
    """Find the requested open 256 x 256 document, rejecting ambiguity."""
    documents = client.tool("documents")
    if not isinstance(documents, list):
        raise MCPError("documents: expected a list.")

    if document_id is not None:
        for document in documents:
            if str(document.get("document")) == document_id:
                if document.get("width") != CANVAS_SIZE or document.get("height") != CANVAS_SIZE:
                    raise MCPError(f"Document {document_id} is not 256 x 256 pixels.")
                return document
        raise MCPError(f"No open PaintFE document has ID {document_id!r}.")

    canvases = [
        document for document in documents
        if document.get("width") == CANVAS_SIZE and document.get("height") == CANVAS_SIZE
    ]
    if not canvases:
        raise MCPError("Open a 256 x 256 pixel document in PaintFE before running this script.")
    if len(canvases) > 1:
        ids = ", ".join(str(document.get("document")) for document in canvases)
        raise MCPError(f"Multiple 256 x 256 documents are open ({ids}); specify --document.")
    return canvases[0]


def draw_step(client, document_id, tool_name, arguments):
    """Apply one edit using a fresh revision, then wait for its async job."""
    document = current_document(client, document_id)
    if document.get("width") != CANVAS_SIZE or document.get("height") != CANVAS_SIZE:
        raise MCPError("The selected document is no longer 256 x 256 pixels.")

    result = client.tool(tool_name, {**target(document), **arguments})
    if result.get("queued") is not True:
        raise MCPError(f"{tool_name}: expected a queued operation.")
    client.wait_for_job()


def draw_dog(client, document):
    document_id = document["document"]
    client.tool("new_layer", {**target(document), "name": LAYER_NAME})

    # Refresh after creating the layer; each following operation refreshes again
    # so stale revisions are never reused.
    current_document(client, document_id)
    for tool_name, arguments in DOG_STEPS:
        draw_step(client, document_id, tool_name, arguments)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paintfe", default=DEFAULT_PAINTFE_PATH, type=Path,
        help=f"Path to the PaintFE executable (default: {DEFAULT_PAINTFE_PATH})",
    )
    parser.add_argument(
        "--document", help="Document ID to use when multiple 256 x 256 documents are open",
    )
    parser.add_argument(
        "--timeout", type=float, default=20,
        help="MCP response timeout in seconds (default: 20)",
    )
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
        parser.error("--timeout must be greater than zero and at most 120 seconds")

    with RunLog(LOG_DIR, prefix="draw-dog") as log:
        try:
            log.info(f"Starting PaintFE dog drawing with executable: {args.paintfe}")
            with Client(args.paintfe, timeout=args.timeout, log=log) as client:
                tools = client.connect()
                print(f"Connected. {len(tools['tools'])} tools available.")
                document = find_canvas(client, args.document)
                print(f"Document: {document['name']} ({document['width']} x {document['height']})")
                draw_dog(client, document)
                print(f"Dog drawn on the '{LAYER_NAME}' layer. Review in PaintFE; nothing was saved.")
                log.info(f"Dog drawn on the '{LAYER_NAME}' layer; document was not saved.")
            return 0
        except (OSError, MCPError) as error:
            message = f"PaintFE dog drawing: {error}"
            log.error(message)
            print(message, file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
