#!/usr/bin/env python3
"""Draw a stylised dog into a 256x256 PaintFE document over the MCP stdio bridge.

Made by MiniMax 3.1 Flash: 18M tokens (17.6 cached) in 3 hours.

The transport, handshake, logging and error handling come from
paintfe_mcp_test.py; only the artwork differs.

Why pencil strokes
------------------
PaintFE's MCP brush only paints the rim of a stroke in this build: the
automation path never rebuilds the brush alpha lookup table for the requested
size, so the interior of every dab stays empty. The pencil path is different -
it uses Bresenham's algorithm and writes exact pixels - so the dog is
rasterised here in Python and sent as a set of pencil strokes. That needs only
the regular document editing permission; the sandboxed script permission is
not required.

The artwork is authored in a 256x256 design space and scaled to the open
document, so it lands exactly on a 256x256 canvas.

Open the PaintFE desktop editor first, enable local agent connections and
document editing in Preferences > AI, and open a 256x256 document.

Usage:
    python paintfe_dog_minimax_31flash.py
    python paintfe_dog_minimax_31flash.py --paintfe "C:\\Program Files\\PaintFE\\PaintFE.exe"
    python paintfe_dog_minimax_31flash.py --layer "MCP dog" --no-preview
"""
import argparse
import base64
import math
from pathlib import Path
import sys

try:
    from paintfe_mcp_test import (
        DEFAULT_PAINTFE_PATH,
        LOG_DIR,
        MCPError,
        RunLog,
        current_document,
        target,
    )
    from paintfe_mcp_test import Client
except ImportError as error:  # pragma: no cover - depends on the repository layout
    raise SystemExit(
        "paintfe_dog_minimax_31flash.py reuses the MCP client from paintfe_mcp_test.py; "
        "keep both scripts in the same folder."
    ) from error


# The artwork is authored in a 256x256 design space and scaled to the open
# document, so the result is exact on a 256x256 canvas.
DESIGN_SIZE = 256
DEFAULT_LAYER_NAME = "MCP dog"
DEFAULT_PREVIEW = Path(__file__).resolve().parent / "output" / "dog-preview.png"
MAX_POINTS_PER_STROKE = 1200  # The MCP limit is 4096 points per stroke.

# Palette (RGBA).
SKY = (176, 214, 236, 255)
SKY_EDGE = (122, 178, 208, 255)
GRASS = (122, 176, 104, 255)
GRASS_EDGE = (86, 138, 74, 255)
SHADOW = (92, 138, 82, 255)
FUR = (198, 137, 82, 255)
FUR_DARK = (150, 96, 52, 255)
CREAM = (243, 218, 184, 255)
DARK = (58, 40, 30, 255)
RED = (198, 62, 62, 255)
GOLD = (240, 190, 80, 255)
TONGUE = (235, 120, 150, 255)
WHITE = (255, 255, 255, 255)
INNER_EAR = (214, 142, 128, 255)


def dog_shapes():
    """Describe the dog as an ordered list of shapes.

    Later shapes paint over earlier ones, so the order is the layering:
    backdrop, tail, body, legs, ears, head, face and finally the collar.
    Dark "outline" shapes are drawn slightly larger than the shape they frame,
    which gives every part a clean contour.
    """
    return [
        # Backdrop -----------------------------------------------------------
        {"kind": "ellipse", "label": "sky", "cx": 128, "cy": 104,
         "rx": 104, "ry": 94, "colour": SKY_EDGE},
        {"kind": "ellipse", "label": "sky fill", "cx": 128, "cy": 104,
         "rx": 101, "ry": 91, "colour": SKY},
        {"kind": "ellipse", "label": "grass", "cx": 128, "cy": 238,
         "rx": 132, "ry": 34, "colour": GRASS_EDGE},
        {"kind": "ellipse", "label": "grass fill", "cx": 128, "cy": 238,
         "rx": 129, "ry": 31, "colour": GRASS},
        {"kind": "ellipse", "label": "shadow", "cx": 118, "cy": 228,
         "rx": 52, "ry": 9, "colour": SHADOW},

        # Tail, painted first so the body hides its root ----------------------
        {"kind": "line", "label": "tail outline",
         "points": [[150, 168], [174, 152], [190, 126], [195, 98]], "width": 15,
         "colour": DARK},
        {"kind": "line", "label": "tail",
         "points": [[150, 168], [174, 152], [190, 126], [195, 98]], "width": 12,
         "colour": FUR},

        # Body ---------------------------------------------------------------
        {"kind": "ellipse", "label": "torso outline", "cx": 104, "cy": 162,
         "rx": 44, "ry": 50, "colour": DARK},
        {"kind": "ellipse", "label": "torso", "cx": 104, "cy": 162,
         "rx": 41, "ry": 47, "colour": FUR},
        {"kind": "ellipse", "label": "haunch", "cx": 137, "cy": 176,
         "rx": 25, "ry": 27, "colour": FUR_DARK},
        {"kind": "ellipse", "label": "chest", "cx": 91, "cy": 181,
         "rx": 24, "ry": 34, "colour": CREAM},

        # Legs ---------------------------------------------------------------
        {"kind": "line", "label": "far leg outline",
         "points": [[80, 176], [78, 196], [82, 214]], "width": 17, "colour": DARK},
        {"kind": "line", "label": "far leg",
         "points": [[80, 176], [78, 196], [82, 214]], "width": 14, "colour": FUR_DARK},
        {"kind": "line", "label": "near leg outline",
         "points": [[106, 176], [108, 198], [108, 216]], "width": 19, "colour": DARK},
        {"kind": "line", "label": "near leg",
         "points": [[106, 176], [108, 198], [108, 216]], "width": 16, "colour": FUR},
        {"kind": "line", "label": "hind leg outline",
         "points": [[140, 188], [144, 202], [142, 212]], "width": 17, "colour": DARK},
        {"kind": "line", "label": "hind leg",
         "points": [[140, 188], [144, 202], [142, 212]], "width": 14, "colour": FUR},
        {"kind": "ellipse", "label": "far paw outline", "cx": 82, "cy": 216,
         "rx": 14, "ry": 10, "colour": DARK},
        {"kind": "ellipse", "label": "far paw", "cx": 82, "cy": 216,
         "rx": 11, "ry": 7, "colour": CREAM},
        {"kind": "ellipse", "label": "near paw outline", "cx": 108, "cy": 218,
         "rx": 16, "ry": 11, "colour": DARK},
        {"kind": "ellipse", "label": "near paw", "cx": 108, "cy": 218,
         "rx": 13, "ry": 8, "colour": CREAM},
        {"kind": "ellipse", "label": "back paw outline", "cx": 140, "cy": 214,
         "rx": 15, "ry": 10, "colour": DARK},
        {"kind": "ellipse", "label": "back paw", "cx": 140, "cy": 214,
         "rx": 12, "ry": 7, "colour": CREAM},

        # Ears and head ------------------------------------------------------
        {"kind": "ellipse", "label": "far ear outline", "cx": 82, "cy": 44,
         "rx": 17, "ry": 24, "colour": DARK},
        {"kind": "ellipse", "label": "far ear", "cx": 82, "cy": 44,
         "rx": 14, "ry": 21, "colour": FUR_DARK},
        {"kind": "ellipse", "label": "head outline", "cx": 104, "cy": 76,
         "rx": 43, "ry": 39, "colour": DARK},
        {"kind": "ellipse", "label": "head", "cx": 104, "cy": 76,
         "rx": 40, "ry": 36, "colour": FUR},
        {"kind": "ellipse", "label": "near ear outline", "cx": 126, "cy": 42,
         "rx": 17, "ry": 24, "colour": DARK},
        {"kind": "ellipse", "label": "near ear", "cx": 126, "cy": 42,
         "rx": 14, "ry": 21, "colour": FUR},
        {"kind": "ellipse", "label": "inner ear", "cx": 126, "cy": 40,
         "rx": 7, "ry": 12, "colour": INNER_EAR},

        # Face ---------------------------------------------------------------
        {"kind": "ellipse", "label": "muzzle outline", "cx": 130, "cy": 92,
         "rx": 25, "ry": 18, "colour": DARK},
        {"kind": "ellipse", "label": "muzzle", "cx": 130, "cy": 92,
         "rx": 22, "ry": 15, "colour": CREAM},
        {"kind": "ellipse", "label": "tongue", "cx": 136, "cy": 105,
         "rx": 6, "ry": 5, "colour": TONGUE},
        {"kind": "line", "label": "mouth",
         "points": [[127, 98], [136, 104], [143, 97]], "width": 2, "colour": DARK},
        {"kind": "ellipse", "label": "nose", "cx": 142, "cy": 84,
         "rx": 8, "ry": 7, "colour": DARK},
        {"kind": "ellipse", "label": "left eye", "cx": 94, "cy": 66,
         "rx": 5, "ry": 6, "colour": DARK},
        {"kind": "ellipse", "label": "right eye", "cx": 118, "cy": 64,
         "rx": 5, "ry": 6, "colour": DARK},
        {"kind": "ellipse", "label": "left glint", "cx": 92, "cy": 64,
         "rx": 2, "ry": 2, "colour": WHITE},
        {"kind": "ellipse", "label": "right glint", "cx": 116, "cy": 62,
         "rx": 2, "ry": 2, "colour": WHITE},

        # Collar -------------------------------------------------------------
        {"kind": "line", "label": "collar outline",
         "points": [[76, 108], [104, 122], [136, 110]], "width": 13, "colour": DARK},
        {"kind": "line", "label": "collar",
         "points": [[76, 108], [104, 122], [136, 110]], "width": 10, "colour": RED},
        {"kind": "ellipse", "label": "tag", "cx": 104, "cy": 130,
         "rx": 6, "ry": 6, "colour": GOLD},
    ]


def rasterize(shape, width, height, scale_x, scale_y):
    """Return a coverage mask for one shape as a list of bytearray rows."""
    mask = [bytearray(width) for _ in range(height)]
    kind = shape["kind"]

    if kind == "ellipse":
        cx = shape["cx"] * scale_x
        cy = shape["cy"] * scale_y
        rx = max(0.5, shape["rx"] * scale_x)
        ry = max(0.5, shape["ry"] * scale_y)
        top = max(0, int(math.floor(cy - ry)))
        bottom = min(height - 1, int(math.ceil(cy + ry)))
        for y in range(top, bottom + 1):
            dy = (y + 0.5 - cy) / ry
            if abs(dy) > 1.0:
                continue
            half = rx * math.sqrt(1.0 - dy * dy)
            left = int(math.ceil(cx - half - 0.5))
            right = int(math.floor(cx + half - 0.5))
            for x in range(max(0, left), min(width - 1, right) + 1):
                mask[y][x] = 1
    elif kind == "line":
        points = [(px * scale_x, py * scale_y) for px, py in shape["points"]]
        radius = max(0.5, shape["width"] * min(scale_x, scale_y) / 2.0)
        radius_sq = radius * radius
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            left = max(0, int(math.floor(min(x0, x1) - radius)))
            right = min(width - 1, int(math.ceil(max(x0, x1) + radius)))
            top = max(0, int(math.floor(min(y0, y1) - radius)))
            bottom = min(height - 1, int(math.ceil(max(y0, y1) + radius)))
            dx = x1 - x0
            dy = y1 - y0
            length_sq = dx * dx + dy * dy
            for y in range(top, bottom + 1):
                for x in range(left, right + 1):
                    px = x + 0.5
                    py = y + 0.5
                    if length_sq <= 1e-9:
                        dist_sq = (px - x0) ** 2 + (py - y0) ** 2
                    else:
                        t = ((px - x0) * dx + (py - y0) * dy) / length_sq
                        t = max(0.0, min(1.0, t))
                        dist_sq = (px - x0 - t * dx) ** 2 + (py - y0 - t * dy) ** 2
                    if dist_sq <= radius_sq:
                        mask[y][x] = 1
    else:
        raise MCPError(f"Unknown shape kind: {kind}")
    return mask


def mask_runs(mask, width):
    """Return the horizontal pixel runs of every row of a mask."""
    runs_by_row = {}
    for y, row in enumerate(mask):
        runs = []
        start = None
        for x in range(width):
            if row[x]:
                if start is None:
                    start = x
            elif start is not None:
                runs.append((start, x - 1))
                start = None
        if start is not None:
            runs.append((start, width - 1))
        if runs:
            runs_by_row[y] = runs
    return runs_by_row


def segment_pixels(x0, y0, x1, y1):
    """Bresenham's line, matching PaintFE's pencil rasterizer."""
    x0, y0 = int(math.floor(x0)), int(math.floor(y0))
    x1, y1 = int(math.floor(x1)), int(math.floor(y1))
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    step_x = 1 if x0 < x1 else -1
    step_y = 1 if y0 < y1 else -1
    error = dx + dy
    pixels = []
    while True:
        pixels.append((x0, y0))
        if x0 == x1 and y0 == y1:
            return pixels
        doubled = 2 * error
        if doubled >= dy:
            error += dy
            x0 += step_x
        if doubled <= dx:
            error += dx
            y0 += step_y


def connector_inside(mask, previous, run, y):
    """Check that the connector between two runs stays inside the mask."""
    conn = min(max(previous[1], run[0]), run[1])
    for x, yy in segment_pixels(previous[1], previous[2], conn, y):
        if not (0 <= yy < len(mask)) or not mask[yy][x]:
            return False
    return True


def chain_to_strokes(chain):
    """Turn a chain of runs into pencil strokes of horizontal lines."""
    strokes = []
    points = []
    pending = None
    for y, (x0, x1) in chain:
        if pending is None:
            points = [[x0, y], [x1, y]]
            pending = (x0, x1, y)
            continue
        conn = min(max(pending[1], x0), x1)
        points.append([conn, y])
        points.append([x0, y])
        points.append([x1, y])
        pending = (x0, x1, y)
        if len(points) >= MAX_POINTS_PER_STROKE:
            strokes.append(points)
            points = []
            pending = None
    if points:
        strokes.append(points)
    return strokes


def mask_to_strokes(mask, width):
    """Convert a coverage mask into the fewest possible pencil strokes."""
    runs_by_row = mask_runs(mask, width)
    used = set()
    strokes = []
    for y in sorted(runs_by_row):
        for index, run in enumerate(runs_by_row[y]):
            if (y, index) in used:
                continue
            chain = [(y, run)]
            used.add((y, index))
            current = run
            next_y = y + 1
            while next_y in runs_by_row:
                candidate = None
                for other_index, other in enumerate(runs_by_row[next_y]):
                    if (next_y, other_index) in used:
                        continue
                    if other[0] <= current[1] and current[0] <= other[1]:
                        candidate = (other_index, other)
                        break
                if candidate is None:
                    break
                other_index, other = candidate
                if not connector_inside(mask, (current[0], current[1], next_y - 1),
                                       other, next_y):
                    break
                chain.append((next_y, other))
                used.add((next_y, other_index))
                current = other
                next_y += 1
            strokes.extend(chain_to_strokes(chain))
    return strokes


def build_strokes(shapes, width, height, scale_x, scale_y):
    """Rasterise every shape and return the pencil strokes to send."""
    strokes = []
    for shape in shapes:
        mask = rasterize(shape, width, height, scale_x, scale_y)
        for index, points in enumerate(mask_to_strokes(mask, width)):
            strokes.append({
                "label": shape["label"] if index == 0 else f"{shape['label']} {index + 1}",
                "points": points,
                "colour": list(shape["colour"]),
            })
    return strokes


def hide_previous_layers(client, document_id, base_name):
    """Hide earlier layers with the same name so repeated runs stay clean.

    The MCP tool set cannot delete a layer, so the previous artwork is made
    invisible by setting its opacity to zero.
    """
    document = current_document(client, document_id)
    previous = [layer for layer in document["layers"]
                if layer["name"].startswith(base_name)]
    if not previous:
        return 0
    restore = document["active_layer"]
    if client.log:
        client.log.info(f"Hiding {len(previous)} earlier {base_name!r} layer(s)")
    for layer in previous:
        document = current_document(client, document_id)
        client.tool("activate_layer", {**target(document), "layer": layer["layer"]})
        document = current_document(client, document_id)
        client.tool("layer_opacity", {**target(document), "layer": layer["layer"],
                                      "opacity": 0})
    document = current_document(client, document_id)
    client.tool("activate_layer", {**target(document), "layer": restore})
    return len(previous)


def paint_strokes(client, document_id, strokes):
    """Paint every pencil stroke, refreshing the identities before each edit."""
    total = len(strokes)
    for index, stroke in enumerate(strokes, start=1):
        document = current_document(client, document_id)
        result = client.tool("stroke", {**target(document),
                                         "points": stroke["points"],
                                         "colour": stroke["colour"],
                                         "size": 1,
                                         "pencil": True})
        if result.get("queued") is not True:
            raise MCPError(f"{stroke['label']}: expected a queued operation.")
        client.wait_for_job()
        if client.log:
            client.log.info(f"Painted {index}/{total}: {stroke['label']}")


def save_preview(client, document_id, destination):
    """Save the bounded PNG preview that the preview tool returns."""
    result = client.request("tools/call", {
        "name": "preview", "arguments": {"document": document_id}})
    if result.get("isError"):
        text = "\n".join(item.get("text", "") for item in result.get("content", [])
                         if item.get("type") == "text")
        raise MCPError(f"preview: {text or 'tool execution failed'}")
    for item in result.get("content", []):
        data = item.get("data")
        if item.get("type") == "image" and data:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(base64.b64decode(data))
            return destination
    raise MCPError("preview: the response carries no image data.")


def pick_document(client, wanted):
    """Return the open document to draw on, preferring a 256x256 canvas."""
    documents = client.tool("documents")
    if not isinstance(documents, list) or not documents:
        raise MCPError("Open a document in PaintFE before continuing.")
    if wanted:
        for document in documents:
            if document.get("name") == wanted:
                return document
        raise MCPError(f"No open document is named {wanted!r}.")
    for document in documents:
        if document.get("width") == DESIGN_SIZE and document.get("height") == DESIGN_SIZE:
            return document
    return documents[0]


def draw_dog(client, document, layer_name):
    """Create the artwork layer and paint the dog into it."""
    document_id = document["document"]
    hidden = hide_previous_layers(client, document_id, layer_name)
    document = current_document(client, document_id)
    if client.log:
        client.log.info(f"Creating layer {layer_name!r} in document {document_id}")
    client.tool("new_layer", {**target(document), "name": layer_name})
    document = current_document(client, document_id)
    width = document["width"]
    height = document["height"]
    scale_x = width / DESIGN_SIZE
    scale_y = height / DESIGN_SIZE
    if client.log:
        client.log.info(
            f"Canvas {width}x{height}; scaling the 256x256 artwork by "
            f"{scale_x:.3f} / {scale_y:.3f}")
    strokes = build_strokes(dog_shapes(), width, height, scale_x, scale_y)
    if client.log:
        client.log.info(f"Painting {len(strokes)} pencil strokes")
    paint_strokes(client, document_id, strokes)
    return len(strokes), hidden


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paintfe", default=DEFAULT_PAINTFE_PATH, type=Path,
                        help=f"Path to the PaintFE executable (default: {DEFAULT_PAINTFE_PATH})")
    parser.add_argument("--layer", default=DEFAULT_LAYER_NAME,
                        help=f"Name of the new artwork layer (default: {DEFAULT_LAYER_NAME})")
    parser.add_argument("--doc", default=None,
                        help="Name of the open document to draw on (default: a 256x256 document)")
    parser.add_argument("--no-draw", dest="draw", action="store_false",
                        help="Skip drawing and only inspect the document")
    parser.add_argument("--no-preview", dest="preview", action="store_false",
                        help="Skip saving the PNG preview")
    parser.add_argument("--preview-path", default=DEFAULT_PREVIEW, type=Path,
                        help=f"Where to save the PNG preview (default: {DEFAULT_PREVIEW})")
    parser.add_argument("--timeout", type=float, default=20,
                        help="Response timeout in seconds (default: 20)")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
        parser.error("--timeout must be greater than zero and at most 120 seconds")
    with RunLog(LOG_DIR, "dog") as log:
        try:
            log.info(f"Starting the PaintFE dog drawing with executable: {args.paintfe}")
            with Client(args.paintfe, timeout=args.timeout, log=log) as client:
                tools = client.connect()
                print(f"Connected. {len(tools['tools'])} tools available.")
                log.info(f"Connected. {len(tools['tools'])} tools available.")
                document = pick_document(client, args.doc)
                print(f"Document: {document['name']} ({document['width']} x {document['height']})")
                log.info(f"Document: {document['name']} ({document['width']} x {document['height']})")
                if not args.draw:
                    print("Read-only check complete. Drop --no-draw to paint the dog.")
                    log.info("Read-only check complete.")
                    return 0
                strokes, hidden = draw_dog(client, document, args.layer)
                print(f"Painted {strokes} pencil strokes into the {args.layer!r} layer.")
                log.info(f"Painted {strokes} pencil strokes into the {args.layer!r} layer.")
                if hidden:
                    print(f"Hid {hidden} earlier {args.layer!r} layer(s).")
                    log.info(f"Hid {hidden} earlier {args.layer!r} layer(s).")
                if args.preview:
                    saved = save_preview(client, document["document"], args.preview_path)
                    print(f"Preview saved to {saved}.")
                    log.info(f"Preview saved to {saved}.")
                print("Review the result in PaintFE and save it there; nothing was saved by this script.")
                log.info("Review the result in PaintFE and save it there; nothing was saved by this script.")
            return 0
        except (OSError, MCPError) as error:
            message = f"PaintFE dog drawing: {error}"
            log.error(message)
            print(message, file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
