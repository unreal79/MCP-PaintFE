#!/usr/bin/env python3
"""Draw a completely raster-filled cartoon dog in PaintFE through MCP.

The script is based on paintfe_mcp_test.py (same stdio JSON-RPC client, layer
handling and job polling) but the image is built offline first:

1. A 256x256 RGBA canvas is painted with solid, fully filled shapes.
2. Every horizontal run of identical pixels is extracted from the finished
   canvas, so no pixel and no shape interior is left untouched.
3. Runs are chained into long serpentine pencil strokes (size 1, `pencil`),
   validated against the finished image, and sent one stroke at a time.
4. After every stroke the script waits for `job_status` and refreshes the
   document identities before continuing.

Only Python's standard library is used. PaintFE must be open, preferences >
AI must allow local connections and document editing, and a 256x256 document
must be active.
"""

from __future__ import annotations

import argparse
import base64
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import queue
import struct
import subprocess
import threading
import time
import zlib


LOG_DIR = Path(__file__).resolve().parent / "logs"
DEFAULT_PAINTFE_PATH = r"C:\Program Files\PaintFE\PaintFE.exe"
CANVAS_SIZE = 256
DEFAULT_LAYER_NAME = "DeepSeek V4.1 Flash dog"
MAX_TOOL_POINTS = 4096
DEFAULT_POINTS_PER_STROKE = 1500

Colour = tuple[int, int, int, int]


# ---------------------------------------------------------------------------
# Logging and MCP client (adapted from paintfe_mcp_test.py)
# ---------------------------------------------------------------------------


class RunLog:
    """One log file per run."""

    def __init__(self, folder: Path = LOG_DIR, prefix: str = "dog") -> None:
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = folder / f"{prefix}-{stamp}.log"
        counter = 1
        while path.exists():
            path = folder / f"{prefix}-{stamp}-{counter}.log"
            counter += 1
        self.path = path
        self._file = path.open("w", encoding="utf-8", newline="\n")

    def _write(self, level: str, message: str) -> None:
        for line in str(message).splitlines() or [""]:
            self._file.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {level:<5} {line}\n")
        self._file.flush()

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def detail(self, message: str) -> None:
        self._write("INFO", message)

    def warning(self, message: str) -> None:
        self._write("WARN", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)

    def close(self) -> None:
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class MCPError(RuntimeError):
    """A connection, protocol or tool execution error."""


def _short(text: str, limit: int = 3000) -> str:
    return text if len(text) <= limit else text[:limit] + f"... ({len(text)} chars)"


class Client:
    def __init__(self, command, args=("--mcp",), timeout=20, log: RunLog | None = None):
        self.timeout = timeout
        self.next_id = 0
        self.messages = queue.Queue()
        self.errors = deque(maxlen=16)
        self.log = log
        self.process = subprocess.Popen(
            [str(command), *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        try:
            while True:
                if self.process.stdout is None:
                    raise MCPError("The MCP bridge has no stdout stream.")
                line = self.process.stdout.readline(8 * 1024 * 1024 + 1)
                if not line:
                    raise MCPError("The MCP bridge closed its output stream.")
                if len(line) > 8 * 1024 * 1024:
                    raise MCPError("The MCP response exceeds the example's size limit.")
                message = json.loads(line.decode("utf-8"))
                if not isinstance(message, dict):
                    raise MCPError("Expected a JSON-RPC message object.")
                if self.log:
                    self.log.detail("RECV " + _short(json.dumps(message, ensure_ascii=False)))
                self.messages.put(message)
        except (ValueError, OSError, MCPError) as error:
            error_message = str(error)
            if self.log:
                self.log.error(f"STDOUT error: {error_message}")
            self.messages.put(MCPError(error_message))

    def _read_stderr(self):
        if self.process.stderr is None:
            self.errors.append("The MCP bridge has no stderr stream.")
            if self.log:
                self.log.warning("The MCP bridge has no stderr stream.")
            return
        try:
            for line in self.process.stderr:
                text = line.decode("utf-8", errors="replace").strip()[:2048]
                if text:
                    self.errors.append(text)
                    if self.log:
                        self.log.detail(f"STDERR {text}")
        except OSError:
            pass

    def send(self, message):
        # MCP stdio is one UTF-8 JSON message per line, without headers.
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        if self.process.stdin is None:
            raise MCPError("The MCP bridge has no stdin stream.")
        if self.log:
            self.log.detail("SEND " + _short(payload))
        try:
            self.process.stdin.write(payload.encode("utf-8") + b"\n")
            self.process.stdin.flush()
        except OSError as error:
            raise MCPError("Could not write to the MCP bridge.") from error

    def request(self, method, params=None):
        self.next_id += 1
        request_id = self.next_id
        self.send({"jsonrpc": "2.0", "id": request_id,
                   "method": method, "params": params or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError(
                    f"Timed out waiting for {method}; check the client framing and bridge process."
                )
            try:
                response = self.messages.get(timeout=remaining)
            except queue.Empty as error:
                raise MCPError(
                    f"Timed out waiting for {method}; check the client framing and bridge process."
                ) from error
            if isinstance(response, Exception):
                detail = "\n".join(self.errors)
                raise MCPError(str(response) + ("\n" + detail if detail else ""))
            if response.get("id") != request_id:
                continue  # Notifications do not complete a pending request.
            if "error" in response:
                raise MCPError(str(response["error"].get("message", response["error"])))
            if "result" not in response:
                raise MCPError("The JSON-RPC response has no result.")
            return response["result"]

    def connect(self):
        if self.log:
            self.log.info("Initializing PaintFE MCP connection")
        result = self.request("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "paintfe-deepseek-dog", "version": "1.0.0"},
        })
        if result.get("protocolVersion") != "2025-11-25":
            raise MCPError("The server did not accept the requested protocol version.")
        if self.log:
            self.log.info("Sending notifications/initialized")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self.request("tools/list")

    def tool(self, name, arguments=None):
        if self.log:
            self.log.info(
                f"Calling tool {name} with arguments "
                + _short(json.dumps(arguments or {}, ensure_ascii=False))
            )
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        text = "\n".join(item["text"] for item in result.get("content", [])
                         if item.get("type") == "text")
        if result.get("isError"):
            raise MCPError(f"{name}: {text or 'tool execution failed'}")
        if "structuredContent" in result:
            if self.log:
                self.log.detail(
                    f"tool {name} structurally returned: "
                    + _short(json.dumps(result["structuredContent"], ensure_ascii=False))
                )
            return result["structuredContent"]
        try:
            parsed = json.loads(text)
            if self.log:
                self.log.detail(
                    f"tool {name} returned: " + _short(json.dumps(parsed, ensure_ascii=False))
                )
            return parsed
        except ValueError as error:
            raise MCPError(f"{name}: expected a JSON text result.") from error

    def wait_for_job(self, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.tool("job_status")
            if not isinstance(result, dict) or not isinstance(result.get("busy"), bool):
                raise MCPError("job_status: missing boolean busy field.")
            if not result["busy"]:
                if result.get("status") != "Applied":
                    raise MCPError(
                        "Drawing did not complete: "
                        + str(result.get("status") or "no job result")
                    )
                if self.log:
                    self.log.info("Job completed successfully.")
                return result
            time.sleep(0.1)
        raise MCPError("Drawing is still running. Inspect job_status or use cancel before continuing.")

    def close(self):
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=3)
        finally:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()
            if self.log:
                self.log.info("PaintFE MCP client closed.")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def current_document(client: Client, document_id=None):
    documents = client.tool("documents")
    if not isinstance(documents, list):
        raise MCPError("documents: expected a list.")
    for document in documents:
        if document_id is None or document.get("document") == document_id:
            return document
    raise MCPError("Open the 256x256 dog document in PaintFE before continuing.")


def target(document):
    return {
        "document": document["document"],
        "layer": document["active_layer"],
        "revision": document["revision"],
    }


def layer_key(entry):
    for key in ("layer", "id", "layer_id"):
        if key in entry:
            return entry[key]
    return None


# ---------------------------------------------------------------------------
# Tiny offline raster canvas
# ---------------------------------------------------------------------------


class Canvas:
    """A flat RGBA canvas with filled primitive shapes."""

    __slots__ = ("width", "height", "pixels")

    def __init__(self, width: int, height: int, colour: Colour) -> None:
        self.width = width
        self.height = height
        self.pixels: list[Colour] = [colour] * (width * height)

    def set(self, x: int, y: int, colour: Colour) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            self.pixels[y * self.width + x] = colour

    def get(self, x: int, y: int) -> Colour:
        return self.pixels[y * self.width + x]

    def fill_rect(self, x0: int, y0: int, x1: int, y1: int, colour: Colour) -> None:
        x0 = max(0, x0)
        y0 = max(0, y0)
        x1 = min(self.width - 1, x1)
        y1 = min(self.height - 1, y1)
        if x1 < x0 or y1 < y0:
            return
        for y in range(y0, y1 + 1):
            start = y * self.width
            for x in range(x0, x1 + 1):
                self.pixels[start + x] = colour

    def fill_ellipse(
        self,
        cx: float,
        cy: float,
        rx: float,
        ry: float,
        colour: Colour,
        angle: float = 0.0,
    ) -> None:
        if rx <= 0 or ry <= 0:
            return
        ca = math.cos(angle)
        sa = math.sin(angle)
        extent_x = math.hypot(rx * ca, ry * sa)
        extent_y = math.hypot(rx * sa, ry * ca)
        min_x = max(0, math.floor(cx - extent_x))
        max_x = min(self.width - 1, math.ceil(cx + extent_x))
        min_y = max(0, math.floor(cy - extent_y))
        max_y = min(self.height - 1, math.ceil(cy + extent_y))
        inv_rx2 = 1.0 / (rx * rx)
        inv_ry2 = 1.0 / (ry * ry)
        for y in range(min_y, max_y + 1):
            dy = y + 0.5 - cy
            row = y * self.width
            for x in range(min_x, max_x + 1):
                dx = x + 0.5 - cx
                u = dx * ca + dy * sa
                v = -dx * sa + dy * ca
                if u * u * inv_rx2 + v * v * inv_ry2 <= 1.0:
                    self.pixels[row + x] = colour

    def fill_capsule(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        radius: float,
        colour: Colour,
    ) -> None:
        if radius <= 0:
            return
        min_x = max(0, math.floor(min(x0, x1) - radius))
        max_x = min(self.width - 1, math.ceil(max(x0, x1) + radius))
        min_y = max(0, math.floor(min(y0, y1) - radius))
        max_y = min(self.height - 1, math.ceil(max(y0, y1) + radius))
        abx = x1 - x0
        aby = y1 - y0
        length_sq = abx * abx + aby * aby
        r_sq = radius * radius
        for y in range(min_y, max_y + 1):
            py = y + 0.5
            row = y * self.width
            for x in range(min_x, max_x + 1):
                px = x + 0.5
                apx = px - x0
                apy = py - y0
                if length_sq == 0:
                    t = 0.0
                else:
                    t = (apx * abx + apy * aby) / length_sq
                    t = max(0.0, min(1.0, t))
                dx = apx - t * abx
                dy = apy - t * aby
                if dx * dx + dy * dy <= r_sq:
                    self.pixels[row + x] = colour

    def fill_polygon(self, points: list[tuple[float, float]], colour: Colour) -> None:
        if len(points) < 3:
            return
        y_values = [point[1] for point in points]
        min_y = max(0, math.floor(min(y_values)))
        max_y = min(self.height - 1, math.ceil(max(y_values)))
        count = len(points)
        for y in range(min_y, max_y + 1):
            scan_y = y + 0.5
            intersections: list[float] = []
            for index in range(count):
                x1, y1 = points[index]
                x2, y2 = points[(index + 1) % count]
                if (y1 <= scan_y < y2) or (y2 <= scan_y < y1):
                    intersections.append(x1 + (scan_y - y1) * (x2 - x1) / (y2 - y1))
            intersections.sort()
            row = y * self.width
            for index in range(0, len(intersections) - 1, 2):
                start = max(0, math.ceil(intersections[index] - 0.5))
                end = min(self.width - 1, math.ceil(intersections[index + 1] - 0.5) - 1)
                for x in range(start, end + 1):
                    self.pixels[row + x] = colour

    def outlined_ellipse(
        self,
        cx: float,
        cy: float,
        rx: float,
        ry: float,
        colour: Colour,
        outline_colour: Colour,
        outline: float = 3.0,
        angle: float = 0.0,
    ) -> None:
        self.fill_ellipse(cx, cy, rx + outline, ry + outline, outline_colour, angle)
        self.fill_ellipse(cx, cy, rx, ry, colour, angle)

    def outlined_capsule(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        radius: float,
        colour: Colour,
        outline_colour: Colour,
        outline: float = 3.0,
    ) -> None:
        self.fill_capsule(x0, y0, x1, y1, radius + outline, outline_colour)
        self.fill_capsule(x0, y0, x1, y1, radius, colour)


def cubic_bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    samples: int,
) -> list[tuple[float, float]]:
    points = []
    for index in range(samples + 1):
        t = index / samples
        mt = 1.0 - t
        x = (mt ** 3) * p0[0] + 3 * (mt ** 2) * t * p1[0] \
            + 3 * mt * (t ** 2) * p2[0] + (t ** 3) * p3[0]
        y = (mt ** 3) * p0[1] + 3 * (mt ** 2) * t * p1[1] \
            + 3 * mt * (t ** 2) * p2[1] + (t ** 3) * p3[1]
        points.append((x, y))
    return points


def stamp_curve(
    canvas: Canvas,
    curve: list[tuple[float, float]],
    radius_start: float,
    radius_end: float,
    colour: Colour,
) -> None:
    last = len(curve) - 1
    for index, (x, y) in enumerate(curve):
        t = index / last if last else 0.0
        radius = radius_start + (radius_end - radius_start) * t
        canvas.fill_ellipse(x, y, radius, radius, colour)


# ---------------------------------------------------------------------------
# The dog artwork. Everything is an opaque, solid filled shape.
# ---------------------------------------------------------------------------


OUTLINE = (52, 36, 30, 255)
BODY = (198, 136, 78, 255)
BODY_DARK = (152, 96, 54, 255)
CREAM = (248, 228, 194, 255)
NOSE = (48, 36, 34, 255)
EYE_WHITE = (252, 250, 244, 255)
PUPIL = (42, 32, 30, 255)
HIGHLIGHT = (255, 255, 255, 255)

COLLAR = (204, 62, 64, 255)
TAG = (248, 200, 76, 255)
TAG_DARK = (198, 148, 36, 255)
TONGUE = (238, 132, 150, 255)
TONGUE_DARK = (198, 92, 112, 255)

SKY_TOP = (150, 207, 240, 255)
SKY_BOTTOM = (176, 220, 246, 255)
SUN = (255, 208, 78, 255)
SUN_HALO = (255, 232, 158, 255)
CLOUD = (250, 253, 255, 255)
CLOUD_SHADE = (208, 231, 246, 255)

GROUND = (126, 186, 108, 255)
HORIZON = (98, 150, 82, 255)
GRASS = (88, 140, 76, 255)
SHADOW = (100, 154, 86, 255)
BONE = (250, 240, 218, 255)


def render_dog() -> Canvas:
    """Paint the whole 256x256 scene into an offline RGBA canvas."""
    canvas = Canvas(CANVAS_SIZE, CANVAS_SIZE, SKY_TOP)

    # Sky, horizon and ground bands.
    canvas.fill_rect(0, 0, 255, 139, SKY_TOP)
    canvas.fill_rect(0, 140, 255, 195, SKY_BOTTOM)
    canvas.fill_rect(0, 196, 255, 255, GROUND)
    canvas.fill_rect(0, 196, 255, 199, HORIZON)

    # Sun.
    canvas.fill_ellipse(38, 36, 25, 25, SUN_HALO)
    canvas.fill_ellipse(38, 36, 17, 17, SUN)

    # Two fluffy clouds with a soft underside.
    clouds = ((178, 50, 22, 11), (204, 41, 28, 13), (228, 50, 18, 9))
    for cx, cy, rx, ry in clouds:
        canvas.fill_ellipse(cx, cy + 3, rx, ry, CLOUD_SHADE)
    for cx, cy, rx, ry in clouds:
        canvas.fill_ellipse(cx, cy, rx, ry, CLOUD)

    # Grass tufts on the horizon.
    for x, base in ((22, 210), (58, 208), (198, 210), (232, 208), (14, 222)):
        canvas.fill_capsule(x, base, x - 4, base - 12, 1.5, GRASS)
        canvas.fill_capsule(x, base, x, base - 15, 1.5, GRASS)
        canvas.fill_capsule(x, base, x + 4, base - 11, 1.5, GRASS)

    # Ground shadow.
    canvas.fill_ellipse(128, 242, 76, 11, SHADOW)

    # A little bone on the left.
    canvas.fill_capsule(22, 236, 46, 236, 9, OUTLINE)
    for cx in (20, 48):
        canvas.fill_ellipse(cx, 230, 10, 10, OUTLINE)
        canvas.fill_ellipse(cx, 242, 10, 10, OUTLINE)
    canvas.fill_capsule(22, 236, 46, 236, 6, BONE)
    for cx in (20, 48):
        canvas.fill_ellipse(cx, 230, 6.5, 6.5, BONE)
        canvas.fill_ellipse(cx, 242, 6.5, 6.5, BONE)

    # Tail behind the body.
    tail = cubic_bezier((164, 200), (230, 206), (224, 158), (204, 126), 90)
    stamp_curve(canvas, tail, 12.0, 7.0, OUTLINE)
    stamp_curve(canvas, tail, 8.5, 3.8, BODY)

    # Torso and flank patches.
    canvas.outlined_ellipse(128, 174, 50, 46, BODY, OUTLINE, 3)
    canvas.fill_ellipse(154, 172, 17, 12, BODY_DARK, angle=-0.35)
    canvas.fill_ellipse(92, 166, 10, 8, BODY_DARK, angle=0.40)

    # Haunches, haunch spots and hind paws.
    canvas.outlined_ellipse(86, 206, 30, 30, BODY, OUTLINE, 3)
    canvas.outlined_ellipse(170, 206, 30, 30, BODY, OUTLINE, 3)
    canvas.fill_ellipse(74, 198, 9, 7, BODY_DARK, angle=-0.25)
    canvas.fill_ellipse(181, 197, 11, 8, BODY_DARK, angle=0.20)
    canvas.outlined_ellipse(82, 232, 26, 13, CREAM, OUTLINE, 3)
    canvas.outlined_ellipse(174, 232, 26, 13, CREAM, OUTLINE, 3)

    # Cream chest, front legs and front paws.
    canvas.fill_ellipse(128, 184, 28, 32, CREAM)
    canvas.outlined_capsule(111, 190, 107, 230, 9, BODY, OUTLINE, 3)
    canvas.outlined_capsule(145, 190, 149, 230, 9, BODY, OUTLINE, 3)
    canvas.outlined_ellipse(104, 238, 16, 10, CREAM, OUTLINE, 3)
    canvas.outlined_ellipse(152, 238, 16, 10, CREAM, OUTLINE, 3)

    # Toe lines.
    for paw_x, paw_y in ((82, 232), (174, 232)):
        for delta in (-8, 0, 8):
            canvas.fill_capsule(paw_x + delta, paw_y + 5, paw_x + delta, paw_y + 10, 1.1, BODY_DARK)
    for paw_x in (104, 152):
        for delta in (-7, 0, 7):
            canvas.fill_capsule(paw_x + delta, 241, paw_x + delta, 245, 1.1, BODY_DARK)

    # Floppy ears sit behind the head.
    canvas.outlined_ellipse(80, 126, 17, 40, BODY_DARK, OUTLINE, 3, angle=-0.18)
    canvas.outlined_ellipse(176, 126, 17, 40, BODY_DARK, OUTLINE, 3, angle=0.18)

    # Head and the dark patch around the right eye.
    canvas.outlined_ellipse(128, 100, 52, 46, BODY, OUTLINE, 3)
    canvas.fill_ellipse(150, 96, 17, 20, BODY_DARK, angle=-0.15)

    # Eyebrows.
    canvas.fill_capsule(101, 79, 115, 75, 2.6, OUTLINE)
    canvas.fill_capsule(139, 75, 153, 79, 2.6, OUTLINE)

    # Collar and tag (the muzzle will overlap the collar centre).
    canvas.outlined_capsule(97, 154, 159, 154, 8, COLLAR, OUTLINE, 3)
    canvas.outlined_ellipse(128, 169, 8, 8, TAG, OUTLINE, 2)
    canvas.fill_ellipse(128, 169, 2.4, 2.4, TAG_DARK)

    # Muzzle.
    canvas.outlined_ellipse(128, 127, 30, 21, CREAM, OUTLINE, 2)

    # Nose with a shine.
    canvas.fill_ellipse(128, 115, 11, 8, NOSE)
    canvas.fill_ellipse(124, 111, 3, 2.2, HIGHLIGHT)

    # Mouth and tongue.
    canvas.fill_capsule(128, 122, 128, 133, 1.7, NOSE)
    canvas.fill_capsule(128, 133, 117, 139, 1.7, NOSE)
    canvas.fill_capsule(128, 133, 139, 139, 1.7, NOSE)
    canvas.fill_ellipse(128, 143, 9, 8, TONGUE)
    canvas.fill_capsule(128, 137, 128, 150, 1.2, TONGUE_DARK)

    # Eyes, pupils and highlights.
    canvas.outlined_ellipse(109, 93, 10, 11, EYE_WHITE, OUTLINE, 2)
    canvas.outlined_ellipse(147, 93, 10, 11, EYE_WHITE, OUTLINE, 2)
    canvas.fill_ellipse(111, 95, 5, 5, PUPIL)
    canvas.fill_ellipse(145, 95, 5, 5, PUPIL)
    canvas.fill_ellipse(108, 90, 2.6, 2.6, HIGHLIGHT)
    canvas.fill_ellipse(142, 90, 2.6, 2.6, HIGHLIGHT)

    for index, colour in enumerate(canvas.pixels):
        if len(colour) != 4 or colour[3] != 255:
            x = index % canvas.width
            y = index // canvas.width
            raise MCPError(f"Internal render error: pixel ({x}, {y}) is not opaque.")
    return canvas


# ---------------------------------------------------------------------------
# Finished canvas -> pixel-exact pencil strokes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    y: int
    x0: int
    x1: int
    colour: Colour


@dataclass
class Stroke:
    colour: Colour
    points: list[tuple[int, int]]


@dataclass
class Chain:
    colour: Colour
    points: list[tuple[int, int]]
    closed: bool = False


def horizontal_runs(canvas: Canvas) -> list[list[Run]]:
    rows: list[list[Run]] = []
    for y in range(canvas.height):
        offset = y * canvas.width
        row_runs: list[Run] = []
        start = 0
        current = canvas.pixels[offset]
        for x in range(1, canvas.width):
            colour = canvas.pixels[offset + x]
            if colour != current:
                row_runs.append(Run(y, start, x - 1, current))
                start = x
                current = colour
        row_runs.append(Run(y, start, canvas.width - 1, current))
        rows.append(row_runs)
    return rows


def line_pixels(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    """Standard integer Bresenham line, endpoints included."""
    points: list[tuple[int, int]] = []
    dx = abs(x1 - x0)
    sx = 1 if x0 < x1 else -1
    dy = -abs(y1 - y0)
    sy = 1 if y0 < y1 else -1
    error = dx + dy
    while True:
        points.append((x0, y0))
        if x0 == x1 and y0 == y1:
            break
        doubled = 2 * error
        if doubled >= dy:
            error += dy
            x0 += sx
        if doubled <= dx:
            error += dx
            y0 += sy
    return points


def run_pixels(run: Run, forward: bool) -> list[tuple[int, int]]:
    if forward:
        return [(x, run.y) for x in range(run.x0, run.x1 + 1)]
    return [(x, run.y) for x in range(run.x1, run.x0 - 1, -1)]


def connector_is_safe(canvas: Canvas, connector: list[tuple[int, int]], colour: Colour) -> bool:
    for x, y in connector:
        if not (0 <= x < canvas.width and 0 <= y < canvas.height):
            return False
        if canvas.get(x, y) != colour:
            return False
    return True


def plan_strokes(canvas: Canvas, max_points: int) -> list[Stroke]:
    """Cover every painted pixel with chained, same-colour pencil strokes."""
    rows = horizontal_runs(canvas)
    strokes: list[Stroke] = []
    open_chains: list[Chain] = []

    def finish(chain: Chain) -> None:
        if not chain.closed:
            chain.closed = True
            strokes.append(Stroke(chain.colour, chain.points))

    for y in range(canvas.height):
        used: set[int] = set()
        row_chains: list[Chain] = []

        for run in rows[y]:
            best_index = -1
            best_extension: list[tuple[int, int]] | None = None
            best_length = 0

            for index, chain in enumerate(open_chains):
                if (index in used or chain.closed
                        or chain.colour != run.colour):
                    continue
                last_x, last_y = chain.points[-1]
                for forward in (True, False):
                    first_x = run.x0 if forward else run.x1
                    connector = line_pixels(last_x, last_y, first_x, y)
                    if not connector_is_safe(canvas, connector, run.colour):
                        continue
                    extension = connector[1:] + run_pixels(run, forward)
                    if best_extension is None or len(extension) < best_length:
                        best_index = index
                        best_extension = extension
                        best_length = len(extension)

            if best_index >= 0 and best_extension is not None:
                chain = open_chains[best_index]
                if len(chain.points) + len(best_extension) <= max_points:
                    chain.points.extend(best_extension)
                    used.add(best_index)
                    row_chains.append(chain)
                    continue
                # Too long: finish the current chain and start a new stroke.
                finish(chain)
                used.add(best_index)

            new_chain = Chain(run.colour, run_pixels(run, True))
            row_chains.append(new_chain)

        # Chains from older rows that were not extended end here.
        continued = {id(chain) for chain in row_chains}
        for chain in open_chains:
            if id(chain) not in continued:
                finish(chain)
        open_chains = row_chains

    for chain in open_chains:
        finish(chain)

    cleaned: list[Stroke] = []
    for stroke in strokes:
        points: list[tuple[int, int]] = []
        for point in stroke.points:
            if not points or point != points[-1]:
                points.append(point)
        if points:
            cleaned.append(Stroke(stroke.colour, points))
    return cleaned


def verify_coverage(canvas: Canvas, strokes: list[Stroke]) -> None:
    """Fail loudly if the stroke plan misses a single canvas pixel."""
    covered = bytearray(canvas.width * canvas.height)
    for stroke in strokes:
        for x, y in stroke.points:
            covered[y * canvas.width + x] = 1
    missing = covered.count(0)
    if missing:
        raise MCPError(
            f"Internal planner error: {missing} of "
            f"{canvas.width * canvas.height} pixels are not in any stroke."
        )


# ---------------------------------------------------------------------------
# PNG output
# ---------------------------------------------------------------------------


def write_png(path: Path, canvas: Canvas) -> None:
    raw = bytearray()
    for y in range(canvas.height):
        raw.append(0)  # filter type: none
        offset = y * canvas.width
        for colour in canvas.pixels[offset:offset + canvas.width]:
            raw.extend(colour)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", canvas.width, canvas.height, 8, 6, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def save_preview_png(client: Client, document_id, path: Path) -> Path:
    if client.log:
        client.log.info(f"Requesting preview for document {document_id}")
    result = client.request(
        "tools/call", {"name": "preview", "arguments": {"document": document_id}}
    )
    if result.get("isError"):
        text = "\n".join(
            item.get("text", "") for item in result.get("content", [])
            if item.get("type") == "text"
        )
        raise MCPError("preview: " + (text or "tool execution failed"))
    for item in result.get("content", []):
        if item.get("type") == "image" and item.get("data"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(item["data"]))
            return path
    raise MCPError("preview: the response contained no PNG image.")


# ---------------------------------------------------------------------------
# PaintFE edits
# ---------------------------------------------------------------------------


def ensure_active_layer(client: Client, document_id, layer_id) -> dict:
    document = current_document(client, document_id)
    if document["active_layer"] != layer_id:
        client.tool("activate_layer", {
            "document": document_id,
            "layer": layer_id,
            "revision": document["revision"],
        })
        document = current_document(client, document_id)
    return document


def hide_other_layers(client: Client, document_id, keep_layer) -> None:
    document = current_document(client, document_id)
    others: list = []
    seen = set()
    for entry in document.get("layers", []):
        identifier = layer_key(entry)
        if identifier is None or identifier == keep_layer or identifier in seen:
            continue
        seen.add(identifier)
        others.append(identifier)

    for identifier in others:
        try:
            document = current_document(client, document_id)
            if document["active_layer"] != identifier:
                client.tool("activate_layer", {
                    "document": document_id,
                    "layer": identifier,
                    "revision": document["revision"],
                })
                document = current_document(client, document_id)
            client.tool("layer_opacity", {**target(document), "opacity": 0})
            if client.log:
                client.log.info(f"Layer {identifier} set to opacity 0.")
        except MCPError as error:
            # A non-raster layer may not support opacity; keep going.
            if client.log:
                client.log.warning(f"Could not hide layer {identifier}: {error}")
            print(f"Warning: could not hide layer {identifier}: {error}")


def draw_strokes(client: Client, document_id, layer_id, strokes: list[Stroke]) -> None:
    total = len(strokes)
    for index, stroke in enumerate(strokes, 1):
        document = ensure_active_layer(client, document_id, layer_id)
        arguments = {
            **target(document),
            "points": [[x, y] for x, y in stroke.points],
            "colour": list(stroke.colour),
            "size": 1,
            "pencil": True,
        }
        result = client.tool("stroke", arguments)
        if result.get("queued") is not True:
            raise MCPError("stroke: expected a queued operation.")
        client.wait_for_job()
        if index == 1 or index == total or index % 5 == 0:
            percent = index * 100 // total
            print(
                f"  stroke {index}/{total} ({percent}%): "
                f"{len(stroke.points)} points, colour rgba{stroke.colour}"
            )
        if client.log:
            client.log.info(
                f"Stroke {index}/{total} applied: {len(stroke.points)} points, "
                f"colour rgba{stroke.colour}"
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paintfe", default=DEFAULT_PAINTFE_PATH, type=Path,
        help=f"Path to the PaintFE executable (default: {DEFAULT_PAINTFE_PATH})",
    )
    parser.add_argument(
        "--timeout", type=float, default=30,
        help="MCP response timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--layer-name", default=DEFAULT_LAYER_NAME,
        help=f"New layer name (default: {DEFAULT_LAYER_NAME})",
    )
    parser.add_argument(
        "--keep-other-layers", action="store_true",
        help="Do not set the other layers' opacity to 0",
    )
    parser.add_argument(
        "--no-preview", action="store_true",
        help="Do not request a PaintFE preview after drawing",
    )
    parser.add_argument(
        "--render-only", action="store_true",
        help="Only render the local PNG and print the stroke plan; do not start PaintFE",
    )
    parser.add_argument(
        "--render-out", type=Path, default=None,
        help="Path for the local reference PNG (default: logs/dog-render-<stamp>.png)",
    )
    parser.add_argument(
        "--max-points", type=int, default=DEFAULT_POINTS_PER_STROKE,
        help=f"Maximum points per MCP stroke (default: {DEFAULT_POINTS_PER_STROKE}, max: {MAX_TOOL_POINTS})",
    )
    args = parser.parse_args()

    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
        parser.error("--timeout must be greater than zero and at most 120 seconds")
    if not 32 <= args.max_points <= MAX_TOOL_POINTS:
        parser.error(f"--max-points must be between 32 and {MAX_TOOL_POINTS}")
    if len(args.layer_name) > 80:
        parser.error("--layer-name must be at most 80 characters")

    print("Rendering the dog offline...")
    canvas = render_dog()
    strokes = plan_strokes(canvas, args.max_points)
    verify_coverage(canvas, strokes)
    total_points = sum(len(stroke.points) for stroke in strokes)
    colours = len({stroke.colour for stroke in strokes})
    print(
        f"Canvas {canvas.width}x{canvas.height}: {canvas.width * canvas.height} pixels, "
        f"{colours} colours, {len(strokes)} strokes, {total_points} points."
    )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    render_path = args.render_out or (LOG_DIR / f"dog-render-{stamp}.png")
    write_png(render_path, canvas)
    print(f"Local reference image: {render_path}")
    if args.render_only:
        print("Render-only mode: PaintFE was not started.")
        return 0

    with RunLog(LOG_DIR) as log:
        try:
            log.info(f"Starting dog drawing with executable: {args.paintfe}")
            log.info(f"Stroke plan: {len(strokes)} strokes, {total_points} points, {colours} colours")
            with Client(args.paintfe, timeout=args.timeout, log=log) as client:
                tools = client.connect()
                print(f"Connected. {len(tools['tools'])} tools available.")
                log.info(f"Connected. {len(tools['tools'])} tools available.")

                document = current_document(client)
                document_id = document["document"]
                print(
                    f"Document: {document['name']} "
                    f"({document['width']} x {document['height']}, revision {document['revision']})"
                )
                if document["width"] != CANVAS_SIZE or document["height"] != CANVAS_SIZE:
                    raise MCPError(
                        f"Open a 256x256 document; {document['name']} is "
                        f"{document['width']}x{document['height']}."
                    )

                client.tool("new_layer", {**target(document), "name": args.layer_name})
                document = current_document(client, document_id)
                layer_id = document["active_layer"]
                print(f"Created layer: {args.layer_name}")
                log.info(f"Created layer {layer_id} ({args.layer_name})")

                if not args.keep_other_layers:
                    print("Hiding the other layers with opacity 0...")
                    hide_other_layers(client, document_id, layer_id)

                document = ensure_active_layer(client, document_id, layer_id)
                client.tool("layer_opacity", {**target(document), "opacity": 1})
                document = ensure_active_layer(client, document_id, layer_id)

                print(f"Drawing {len(strokes)} pixel-exact pencil strokes...")
                draw_strokes(client, document_id, layer_id, strokes)

                print("All strokes applied. Nothing was saved; review the image in PaintFE.")
                log.info("All strokes applied. The document was not saved.")

                if not args.no_preview:
                    preview_path = LOG_DIR / f"dog-preview-{stamp}.png"
                    try:
                        save_preview_png(client, document_id, preview_path)
                        print(f"PaintFE preview saved to {preview_path}")
                        log.info(f"PaintFE preview saved to {preview_path}")
                    except MCPError as error:
                        print(f"Preview unavailable: {error}")
                        log.warning(f"Preview unavailable: {error}")
            return 0
        except (OSError, MCPError) as error:
            message = f"PaintFE dog drawing: {error}"
            log.error(message)
            print(message)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
