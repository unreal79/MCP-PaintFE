#!/usr/bin/env python3
"""Draw a cartoon dog on a 256x256 document in PaintFE over the MCP stdio bridge.

Based on paintfe_mcp_test.py. Uses only Python's standard library.

Before running:
1. Open PaintFE normally and open a 256x256 document (for example _INFO/dog.pfe).
2. In Preferences > AI enable local agent connections and document editing.
3. Keep the desktop editor open, then run: python paintfe_draw_dog.py

The script lists the open documents, picks the 256x256 one (preferring a
document whose name contains "dog"), creates or reuses a "Dog" layer, paints
a side-view cartoon dog with stroke calls and outlined ellipses, waits for
every job and finally saves a PNG preview under output/. It never saves,
closes or overwrites the PaintFE document itself.
"""
import argparse
import base64
from collections import deque
from datetime import datetime
import json
import math
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time


LOG_DIR = Path(__file__).resolve().parent / "logs"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
DEFAULT_PAINTFE_PATH = r"C:\Program Files\PaintFE\PaintFE.exe"
LAYER_NAME = "Dog"

# Palette (RGBA).
TAN = (222, 184, 135, 255)         # body, head, legs and tail
OUTLINE = (124, 82, 48, 255)       # chunky cartoon outline
EAR_BROWN = (110, 70, 40, 255)     # floppy ear
MUZZLE = (245, 219, 178, 255)      # lighter snout fill
MUZZLE_OUTLINE = (140, 95, 55, 255)
INK = (45, 38, 32, 255)            # eye, nose and mouth
PINK = (240, 112, 140, 255)        # tongue
RED = (212, 74, 74, 255)           # collar

FILL_SIZE = 8    # scanline stroke size for the big fills
FILL_STEP = 6    # vertical distance between scanlines
SMALL_FILL_SIZE = 6
SMALL_FILL_STEP = 4


class RunLog:
    """One log file per run, mirroring the helper used in the PaintFE MCP probe."""

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


class Client:
    def __init__(self, command, args=("--mcp",), timeout=20, log: RunLog | None = None):
        self.timeout = timeout
        self.next_id = 0
        self.messages = queue.Queue()
        self.errors = deque(maxlen=16)
        self.log = log
        self.process = subprocess.Popen(
            [str(command), *args], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
                    self.log.detail(f"RECV {json.dumps(message, ensure_ascii=False)}")
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
            self.log.detail(f"SEND {payload}")
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
                raise MCPError(f"Timed out waiting for {method}; check the client framing and bridge process.")
            try:
                response = self.messages.get(timeout=remaining)
            except queue.Empty as error:
                raise MCPError(f"Timed out waiting for {method}; check the client framing and bridge process.") from error
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
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "paintfe-dog-example", "version": "1.0.0"},
        })
        if result.get("protocolVersion") != "2025-11-25":
            raise MCPError("The server did not accept the requested protocol version.")
        if self.log:
            self.log.info("Sending notifications/initialized")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self.request("tools/list")

    def tool(self, name, arguments=None):
        if self.log:
            self.log.info(f"Calling tool {name} with arguments {json.dumps(arguments or {}, ensure_ascii=False)}")
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        text = "\n".join(item["text"] for item in result.get("content", [])
                         if item.get("type") == "text")
        if result.get("isError"):
            raise MCPError(f"{name}: {text or 'tool execution failed'}")
        if "structuredContent" in result:
            if self.log:
                self.log.detail(f"tool {name} structurally returned: {json.dumps(result['structuredContent'], ensure_ascii=False)}")
            return result["structuredContent"]
        try:
            parsed = json.loads(text)
            if self.log:
                self.log.detail(f"tool {name} returned: {json.dumps(parsed, ensure_ascii=False)}")
            return parsed
        except ValueError as error:
            raise MCPError(f"{name}: expected a JSON text result.") from error

    def wait_for_job(self, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.tool("job_status")
            if not isinstance(result, dict) or not isinstance(result.get("busy"), bool):
                raise MCPError("job_status: missing boolean busy field.")
            if not result["busy"]:
                if result.get("status") != "Applied":
                    raise MCPError("Drawing did not complete: " + str(result.get("status") or "no job result"))
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


def current_document(client, document_id=None):
    documents = client.tool("documents")
    if not isinstance(documents, list):
        raise MCPError("documents: expected a list.")
    for document in documents:
        if document_id is None or document.get("document") == document_id:
            return document
    raise MCPError("The dog document is no longer open in PaintFE.")


def target(document):
    return {"document": document["document"], "layer": document["active_layer"],
            "revision": document["revision"]}


def pick_document(documents, size):
    """Choose the open square document to draw on, preferring a dog-named one."""
    if not isinstance(documents, list) or not documents:
        raise MCPError("Open a document in PaintFE before continuing.")
    matching = [document for document in documents
                if document.get("width") == size and document.get("height") == size]
    if not matching:
        available = "; ".join(
            f"{document.get('name')!r} {document.get('width')}x{document.get('height')}"
            for document in documents) or "none"
        raise MCPError(
            f"No open {size}x{size} document in PaintFE. Open one (for example _INFO/dog.pfe) "
            f"and run the script again. Open documents: {available}.")
    named = [document for document in matching
             if "dog" in str(document.get("name", "")).lower()]
    return named[0] if named else matching[0]


def layer_identity(document, name):
    """Return the id of the named layer, when documents exposes a layer list."""
    layers = document.get("layers")
    if not isinstance(layers, list):
        return None
    for entry in layers:
        if isinstance(entry, dict) and entry.get("name") == name:
            for key in ("layer", "id", "layer_id"):
                if entry.get(key) is not None:
                    return entry[key]
    return None


def ensure_layer(client, document, name):
    """Create (or reuse) the dog layer and make it the active layer."""
    document_id = document["document"]
    layer_id = layer_identity(document, name)
    if layer_id is None:
        if client.log:
            client.log.info(f"Creating layer {name!r} for document {document_id}")
        result = client.tool("new_layer", {**target(document), "name": name})
        if result.get("queued") is True:
            client.wait_for_job()
        document = current_document(client, document_id)
        layer_id = layer_identity(document, name)
    if layer_id is not None and document.get("active_layer") != layer_id:
        if client.log:
            client.log.info(f"Activating layer {name!r}; an earlier run may have left content on it")
        result = client.tool("activate_layer", {**target(document), "layer": layer_id})
        if result.get("queued") is True:
            client.wait_for_job()
        document = current_document(client, document_id)
    elif layer_id is None:
        if client.log:
            client.log.warning(f"Could not resolve the id of layer {name!r}; "
                               "drawing on the layer that documents reports as active.")
    return document


def ellipse_fill_rows(cx, cy, rx, ry, size, step):
    """Yield horizontal scanline point lists that fill an axis-aligned ellipse.

    Every row is inset by half the stroke size so the rounded caps stay inside
    the shape; the chunky outline drawn afterwards covers the remaining slivers
    along the boundary. An ellipse is convex, so nothing pokes outside.
    """
    inset = size / 2.0
    y = int(math.ceil(cy - ry))
    last = int(math.floor(cy + ry))
    while y <= last:
        dy = (y - cy) / ry
        width = rx * math.sqrt(max(0.0, 1.0 - dy * dy))
        if width > inset + 0.5:
            left = int(round(cx - width + inset))
            right = int(round(cx + width - inset))
            yield [[left, y], [right, y]]
        elif width > 1.0:
            yield [[int(round(cx)), y]]
        y += step


def build_dog_operations():
    """Ordered (tool, arguments, label) operations that paint the cartoon dog.

    Side view facing left on a 256x256 canvas: oval body, round head in front,
    lighter muzzle, floppy ear, collar, four legs, curled tail and a face.
    """
    head = {"cx": 88, "cy": 100, "rx": 40, "ry": 40}
    body = {"cx": 150, "cy": 150, "rx": 56, "ry": 32}
    muzzle = {"cx": 58, "cy": 114, "rx": 20, "ry": 12}
    operations = []

    def fill(geometry, colour, size, step, label):
        for points in ellipse_fill_rows(geometry["cx"], geometry["cy"],
                                        geometry["rx"], geometry["ry"], size, step):
            operations.append(("stroke", {"points": points, "colour": list(colour),
                                          "size": size, "pencil": True}, label))

    def outline(geometry, colour, size, label):
        operations.append(("shape", {
            "shape": "ellipse",
            "x": geometry["cx"] - geometry["rx"],
            "y": geometry["cy"] - geometry["ry"],
            "width": geometry["rx"] * 2, "height": geometry["ry"] * 2,
            "colour": list(colour), "size": size}, label))

    def line(points, colour, size, label):
        operations.append(("stroke", {"points": [list(point) for point in points],
                                      "colour": list(colour), "size": size,
                                      "pencil": True}, label))

    # Body first, then the head painted in front of it, then the muzzle.
    fill(body, TAN, FILL_SIZE, FILL_STEP, "body fill")
    outline(body, OUTLINE, 8, "body outline")
    fill(head, TAN, FILL_SIZE, FILL_STEP, "head fill")
    outline(head, OUTLINE, 8, "head outline")
    fill(muzzle, MUZZLE, SMALL_FILL_SIZE, SMALL_FILL_STEP, "muzzle fill")
    outline(muzzle, MUZZLE_OUTLINE, 4, "muzzle outline")
    line([(98, 68), (86, 66), (72, 78), (64, 96), (62, 114)], EAR_BROWN, 14, "floppy ear")
    line([(102, 126), (118, 140)], RED, 9, "collar")
    line([(106, 166), (106, 221)], TAN, 11, "front leg 1")
    line([(124, 172), (124, 221)], TAN, 11, "front leg 2")
    line([(184, 172), (184, 221)], TAN, 11, "back leg 1")
    line([(200, 162), (200, 221)], TAN, 11, "back leg 2")
    line([(202, 140), (218, 124), (224, 104)], TAN, 10, "tail")
    line([(80, 92)], INK, 10, "eye")
    line([(42, 110)], INK, 10, "nose")
    line([(48, 118), (52, 126), (64, 127)], INK, 3, "mouth")
    line([(52, 128), (50, 137)], PINK, 6, "tongue")
    return operations


def apply_operation(client, document_id, tool_name, arguments):
    """Refresh identities, submit one edit and wait until its job is applied."""
    document = current_document(client, document_id)
    result = client.tool(tool_name, {**target(document), **arguments})
    if result.get("queued") is True:
        client.wait_for_job()
    return result


def save_preview(client, document_id, log):
    """Save the bounded PNG preview that PaintFE returns for the document."""
    result = client.request("tools/call", {"name": "preview",
                                           "arguments": {"document": document_id}})
    if result.get("isError"):
        text = "\n".join(item.get("text", "") for item in result.get("content", [])
                         if item.get("type") == "text")
        raise MCPError(f"preview: {text or 'tool execution failed'}")
    for item in result.get("content", []):
        if item.get("type") == "image" and item.get("data"):
            mime = str(item.get("mimeType") or "image/png")
            extension = "png" if "png" in mime else ("jpg" if "jpe" in mime else "bin")
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = OUTPUT_DIR / f"dog-preview-{stamp}.{extension}"
            path.write_bytes(base64.b64decode(item["data"]))
            return path
    log.warning("preview returned no image content; skipping the preview file.")
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paintfe", default=DEFAULT_PAINTFE_PATH, type=Path,
                        help=f"Path to the PaintFE executable (default: {DEFAULT_PAINTFE_PATH})")
    parser.add_argument("--size", type=int, default=256,
                        help="Required square canvas size in pixels (default: 256)")
    parser.add_argument("--timeout", type=float, default=20,
                        help="Response timeout in seconds (default: 20)")
    parser.add_argument("--no-preview", dest="preview", action="store_false",
                        help="Skip saving the PNG preview under output/")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
        parser.error("--timeout must be greater than zero and at most 120 seconds")
    if not 16 <= args.size <= 4096:
        parser.error("--size must be between 16 and 4096 pixels")
    with RunLog(LOG_DIR) as log:
        try:
            log.info(f"Starting PaintFE dog drawing with executable: {args.paintfe}")
            with Client(args.paintfe, timeout=args.timeout, log=log) as client:
                tools = client.connect()
                print(f"Connected. {len(tools['tools'])} tools available.")
                log.info(f"Connected. {len(tools['tools'])} tools available.")
                document = pick_document(client.tool("documents"), args.size)
                message = (f"Document: {document['name']} "
                           f"({document['width']} x {document['height']})")
                print(message)
                log.info(message)
                document = ensure_layer(client, document, LAYER_NAME)
                operations = build_dog_operations()
                document_id = document["document"]
                total = len(operations)
                message = f"Drawing a dog with {total} operations on layer {LAYER_NAME!r}..."
                print(message)
                log.info(message)
                for index, (tool_name, arguments, label) in enumerate(operations, 1):
                    apply_operation(client, document_id, tool_name, arguments)
                    progress = f"  [{index:>2}/{total}] {label}"
                    print(progress)
                    log.info(progress.strip())
                print("Dog drawing finished. Review it in PaintFE; nothing was saved.")
                log.info("Dog drawing finished; nothing was saved.")
                if args.preview:
                    path = save_preview(client, document_id, log)
                    if path is not None:
                        message = f"Preview saved to {path}"
                        print(message)
                        log.info(message)
            return 0
        except (OSError, MCPError) as error:
            message = (f"PaintFE dog drawing: {error}\n"
                       "Check that the PaintFE editor is open, that Preferences > AI "
                       "enables local agent connections and document editing, and that "
                       "no dialog is blocking the editor. The run log has the details.")
            log.error(message)
            print(message, file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            message = "PaintFE dog drawing: interrupted by the user."
            log.error(message)
            print(message, file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
