#!/usr/bin/env python3
"""Draw a dog on a 256x256 PaintFE document over MCP stdio.

Built on top of paintfe_mcp_test.py (same Client / RunLog machinery).
Open PaintFE with a 256x256 document, enable local agent connections and
document editing, then run:

    python paintfe_draw_dog.py

The script creates a separate "Dog" layer, draws the dog with brush/pencil
strokes scaled to the document size, and never saves the document.
"""
import argparse
import math
from pathlib import Path

from paintfe_mcp_test import (
    Client,
    DEFAULT_PAINTFE_PATH,
    LOG_DIR,
    MCPError,
    RunLog,
    current_document,
    target,
)

# Virtual canvas: the dog is designed in a 256x256 coordinate system and
# scaled to the real document size before every stroke.
CANVAS = 256.0

FUR = [188, 124, 62, 255]       # warm brown
FUR_DARK = [140, 88, 44, 255]   # inner ear / shading
CREAM = [236, 206, 164, 255]    # muzzle and chest patch
OUTLINE = [92, 56, 26, 255]     # dark brown
DARK = [24, 24, 24, 255]        # eyes / nose
TONGUE = [224, 96, 110, 255]    # pink
COLLAR = [210, 60, 50, 255]     # red collar
GOLD = [222, 178, 44, 255]      # collar tag


def ellipse_points(cx, cy, rx, ry, steps=96):
    """Closed ellipse outline in virtual coordinates."""
    pts = []
    for i in range(steps + 1):
        a = 2 * math.pi * i / steps
        pts.append([cx + rx * math.cos(a), cy + ry * math.sin(a)])
    return pts


def line_points(x0, y0, x1, y1, steps=24):
    return [[x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps]
            for i in range(steps + 1)]


def quad_bezier_points(p0, p1, p2, steps=32):
    """Curved tail / mouth strokes via a quadratic Bezier."""
    pts = []
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
        pts.append([x, y])
    return pts


def blob_points(cx, cy, rx, ry, tilt=0.0, steps=64):
    """Teardrop-ish blob (floppy ear) with a simple shear for tilt."""
    pts = []
    for i in range(steps + 1):
        a = 2 * math.pi * i / steps
        x, y = rx * math.cos(a), ry * math.sin(a)
        pts.append([cx + x + tilt * y, cy + y])
    return pts


class DogDrawer:
    def __init__(self, client, document_id):
        self.client = client
        self.document_id = document_id
        doc = current_document(client, document_id)
        self.sx = doc["width"] / CANVAS
        self.sy = doc["height"] / CANVAS
        self.w = doc["width"]
        self.h = doc["height"]

    def _scale(self, points):
        out = []
        for x, y in points:
            xi = max(0, min(self.w - 1, int(round(x * self.sx))))
            yi = max(0, min(self.h - 1, int(round(y * self.sy))))
            out.append([xi, yi])
        return out

    def stroke(self, points, colour, size=3):
        scaled = self._scale(points)
        # Refresh the document identities before every edit (stale revisions
        # are rejected by the server).
        document = current_document(self.client, self.document_id)
        result = self.client.tool("stroke", {**target(document), "points": scaled,
                                             "colour": colour, "size": size,
                                             "pencil": True})
        if result.get("queued") is not True:
            raise MCPError("stroke: expected a queued operation.")
        self.client.wait_for_job(timeout=120)

    def outline(self, points, colour=OUTLINE, size=3):
        self.stroke(points, colour, size)

    def dot(self, cx, cy, colour, size=5):
        self.stroke([[cx, cy]], colour, size)

    def fill_ellipse(self, cx, cy, rx, ry, colour, rings=10):
        """Solid ellipse: concentric rings, dense enough to cover the disc."""
        for k in range(rings):
            f = 1.0 - k / rings
            self.stroke(ellipse_points(cx, cy, max(rx * f, 1), max(ry * f, 1)),
                        colour, 4)

    def fill_blob(self, cx, cy, rx, ry, tilt, colour, rings=8):
        for k in range(rings):
            f = 1.0 - k / rings
            self.stroke(blob_points(cx, cy, max(rx * f, 1), max(ry * f, 1), tilt),
                        colour, 4)

    def draw(self):
        document = current_document(self.client, self.document_id)
        if self.client.log:
            self.client.log.info("Creating the Dog layer")
        self.client.tool("new_layer", {**target(document), "name": "Dog v2"})

        if self.client.log:
            self.client.log.info("Drawing tail and hind leg (behind the body)")
        # Curled fluffy tail rising from the right hip, drawn first so the
        # body overlaps its root.
        self.stroke(quad_bezier_points([172, 172], [214, 168], [212, 116]),
                    FUR, 14)
        self.stroke(quad_bezier_points([172, 172], [214, 168], [212, 116]),
                    OUTLINE, 2)
        self.dot(212, 114, FUR, 14)  # rounded tail tip
        # Hind leg haunch (behind the body silhouette).
        self.fill_ellipse(162, 196, 26, 24, FUR, 8)
        self.outline(ellipse_points(162, 196, 26, 24), OUTLINE, 2)

        if self.client.log:
            self.client.log.info("Drawing the body and front legs")
        # Sitting body: rounded, wider at the bottom.
        self.fill_ellipse(126, 184, 44, 48, FUR, 14)
        self.outline(ellipse_points(126, 184, 44, 48), OUTLINE, 3)
        # Light chest patch.
        self.fill_ellipse(118, 196, 17, 24, CREAM, 8)
        # Front legs: short, connected to the body, with rounded paws.
        for x in (104, 134):
            self.stroke(line_points(x, 208, x, 238), FUR, 12)
            self.outline(line_points(x - 6, 210, x - 6, 236), OUTLINE, 2)
            self.outline(line_points(x + 6, 210, x + 6, 236), OUTLINE, 2)
            self.fill_ellipse(x, 240, 9, 5, FUR, 4)
            self.outline(ellipse_points(x, 241, 9, 5), OUTLINE, 2)
            # Paw toes.
            self.stroke(line_points(x - 3, 243, x - 3, 239), OUTLINE, 1)
            self.stroke(line_points(x + 3, 243, x + 3, 239), OUTLINE, 1)
        # Back paw peeking under the haunch.
        self.fill_ellipse(168, 240, 10, 5, FUR, 4)
        self.outline(ellipse_points(168, 241, 10, 5), OUTLINE, 2)
        # Red collar between head and body.
        self.stroke(quad_bezier_points([98, 136], [126, 148], [154, 136]),
                    COLLAR, 8)
        self.dot(126, 147, GOLD, 6)  # tag

        if self.client.log:
            self.client.log.info("Drawing floppy ears behind the head")
        # Big floppy ears attached to the skull sides (drawn before the head
        # so the head outline overlaps their inner edge).
        for cx, tilt in ((84, 0.28), (170, -0.28)):
            self.fill_blob(cx, 88, 17, 33, tilt, FUR_DARK, 8)
            self.stroke(blob_points(cx, 88, 17, 33, tilt), OUTLINE, 3)
        # Rounded ear tips.
        self.dot(75, 118, FUR_DARK, 12)
        self.dot(179, 118, FUR_DARK, 12)

        if self.client.log:
            self.client.log.info("Drawing the head")
        # Head: solid round skull on top of the body.
        self.fill_ellipse(127, 84, 44, 40, FUR, 14)
        self.outline(ellipse_points(127, 84, 44, 40), OUTLINE, 3)
        # Cute hair tuft on the forehead.
        self.stroke(quad_bezier_points([114, 48], [122, 38], [128, 46]),
                    FUR, 5)
        self.stroke(quad_bezier_points([128, 46], [134, 36], [142, 48]),
                    FUR, 5)

        if self.client.log:
            self.client.log.info("Drawing the face")
        # Light muzzle low on the face.
        self.fill_ellipse(127, 106, 24, 15, CREAM, 8)
        self.outline(ellipse_points(127, 106, 24, 15), OUTLINE, 2)
        # Nose: rounded triangle.
        self.fill_ellipse(127, 99, 7, 5, DARK, 3)
        # Philtrum and smiling mouth hanging below the nose.
        self.stroke(line_points(127, 104, 127, 111), DARK, 2)
        self.stroke(quad_bezier_points([127, 111], [116, 118], [108, 110]),
                    DARK, 2)
        self.stroke(quad_bezier_points([127, 111], [138, 118], [146, 110]),
                    DARK, 2)
        # Tongue peeking from the left smile.
        self.fill_ellipse(119, 117, 4, 5, TONGUE, 3)
        self.outline(ellipse_points(119, 117, 4, 5), [170, 60, 80, 255], 1)
        # Big round eyes with highlights, placed above the muzzle.
        for ex in (110, 144):
            self.fill_ellipse(ex, 80, 6, 7, DARK, 4)
            self.dot(ex - 2, 77, [255, 255, 255, 255], 3)
            self.dot(ex + 2, 83, [255, 255, 255, 255], 1)
        # Soft eyebrows.
        self.stroke(quad_bezier_points([102, 66], [110, 62], [118, 66]),
                    OUTLINE, 2)
        self.stroke(quad_bezier_points([136, 66], [144, 62], [152, 66]),
                    OUTLINE, 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paintfe", default=DEFAULT_PAINTFE_PATH, type=Path,
                        help=f"Path to the PaintFE executable (default: {DEFAULT_PAINTFE_PATH})")
    parser.add_argument("--timeout", type=float, default=20,
                        help="Response timeout in seconds (default: 20)")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
        parser.error("--timeout must be greater than zero and at most 120 seconds")

    with RunLog(LOG_DIR, prefix="draw-dog") as log:
        try:
            log.info(f"Starting PaintFE MCP dog drawing with executable: {args.paintfe}")
            with Client(args.paintfe, timeout=args.timeout, log=log) as client:
                tools = client.connect()
                print(f"Connected. {len(tools['tools'])} tools available.")
                document = current_document(client)
                print(f"Document: {document['name']} "
                      f"({document['width']} x {document['height']})")
                if (document["width"], document["height"]) != (256, 256):
                    print("Warning: the document is not 256x256; "
                          "the drawing will be scaled to fit anyway.")
                    log.warning("Document is not 256x256; scaling the drawing "
                                f"to {document['width']}x{document['height']}.")
                drawer = DogDrawer(client, document["document"])
                drawer.draw()
                print("Dog added to the 'Dog' layer. "
                      "Review in PaintFE; nothing was saved.")
                log.info("Dog drawing finished successfully.")
            return 0
        except (OSError, MCPError) as error:
            message = f"PaintFE dog drawing: {error}"
            log.error(message)
            print(f"ERROR: {error}")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
