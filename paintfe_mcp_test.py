#!/usr/bin/env python3
"""Minimal MCP client for PaintFE.

It connects to PaintFE over stdio using the MCP JSON-RPC protocol, checks the
handshake, lists available tools, then draws three colored points on the active
raster layer.

Example config:
{
  "mcpServers": {
    "paintfe": {
      "command": "C:\\Program Files\\PaintFE\\PaintFE.exe",
      "args": ["--mcp"]
    }
  }
}
"""

from __future__ import annotations

import argparse
import json
import os
import queue as _queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

WORKSPACE = Path(__file__).resolve().parent
LOG_DIR = WORKSPACE / "logs"
TIMEOUT = 5.0 # Default timeout for PaintFE startup in seconds
DEFAULT_CONFIG = {
    "mcpServers": {
        "paintfe": {
            "command": "C:\\Program Files\\PaintFE\\PaintFE.exe",
            "args": ["--mcp"],
        }
    }
}


class RunLog:
    """One log file per run, mirroring the helper used in the Rigby2D MCP utilities."""

    def __init__(self, folder: Path = LOG_DIR, prefix: str = "run") -> None:
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
        print(message)
        sys.stdout.flush()

    def detail(self, message: str) -> None:
        self._write("INFO", message)

    def warning(self, message: str) -> None:
        self._write("WARN", message)
        print(message, file=sys.stderr)
        sys.stderr.flush()

    def error(self, message: str) -> None:
        self._write("ERROR", message)
        print(message, file=sys.stderr)
        sys.stderr.flush()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "RunLog":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class PaintFEMCPError(RuntimeError):
    """Raised when the MCP connection or a PaintFE tool call fails."""


class MCPStdioClient:
    def __init__(self, command: str, args: Optional[Sequence[str]] = None, log: Optional[RunLog] = None, startup_timeout: float = TIMEOUT):
        self.command = command
        self.args = list(args or [])
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.next_id = 1
        self.stderr_lines: List[str] = []
        self._stderr_lock = threading.Lock()
        self._message_queue: "_queue.Queue[Any]" = _queue.Queue()
        self.log = log
        self.startup_timeout = startup_timeout

    def start(self) -> None:
        if not self.command:
            raise PaintFEMCPError("PaintFE command is empty.")

        resolved = self.command
        if not os.path.exists(resolved):
            found = shutil.which(resolved)
            if found:
                resolved = found
            else:
                raise PaintFEMCPError(f"PaintFE executable not found: {self.command}")

        self.command = resolved
        self.process = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()
        self._stdout_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        self._stdout_thread.start()
        self.wait_for_startup(self.startup_timeout)

    def wait_for_startup(self, timeout_seconds: float = TIMEOUT) -> None:
        if self.process is None:
            raise PaintFEMCPError("PaintFE process was not created.")

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            exit_code = self.process.poll()
            if exit_code is not None:
                stderr_text = "\n".join(self.stderr_lines[-20:])
                raise PaintFEMCPError(
                    f"PaintFE exited before the MCP server became ready. "
                    f"Exit code: {exit_code}. "
                    f"Check that PaintFE is installed and that the command is valid: "
                    f"{self.command} {' '.join(self.args)}. "
                    f"stderr: {stderr_text or 'no stderr output'}"
                )
            time.sleep(0.2)

        if self.process.poll() is None:
            if self.log:
                self.log.info(f"PaintFE process is running (PID {self.process.pid})")
            return

        stderr_text = "\n".join(self.stderr_lines[-20:])
        raise PaintFEMCPError(
            f"PaintFE did not start within {timeout_seconds} seconds. "
            f"Make sure the window is open, the app is installed, and the local AI connection is enabled. "
            f"stderr: {stderr_text or 'no stderr output'}"
        )

    def _read_stderr(self) -> None:
        if self.process is None or self.process.stderr is None:
            return

        while True:
            line = self.process.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                with self._stderr_lock:
                    self.stderr_lines.append(text)

    def _pump_stdout(self) -> None:
        """Parse complete MCP messages from stdout and push them onto the queue."""
        if self.process is None or self.process.stdout is None:
            return
        try:
            while True:
                header_lines: List[bytes] = []
                while True:
                    line = self.process.stdout.readline()
                    if not line:
                        self._message_queue.put(PaintFEMCPError("PaintFE closed the MCP stdio connection."))
                        return
                    header_lines.append(line)
                    if line in (b"\r\n", b"\n", b""):
                        break

                headers: Dict[str, str] = {}
                for raw in header_lines:
                    if raw in (b"\r\n", b"\n", b""):
                        continue
                    text = raw.decode("utf-8", errors="replace")
                    if ":" not in text:
                        continue
                    name, value = text.split(":", 1)
                    headers[name.strip().lower()] = value.strip()

                content_length = headers.get("content-length")
                if content_length is None:
                    raw_all = b"".join(header_lines)
                    try:
                        self._message_queue.put(json.loads(raw_all.decode("utf-8", errors="replace")))
                    except json.JSONDecodeError as exc:
                        self._message_queue.put(PaintFEMCPError(f"Could not decode MCP JSON message: {raw_all!r}"))
                    continue

                size = int(content_length)
                body = self.process.stdout.read(size)
                if len(body) != size:
                    self._message_queue.put(PaintFEMCPError("Unexpected end of MCP message body from PaintFE."))
                    return
                try:
                    self._message_queue.put(json.loads(body.decode("utf-8", errors="replace")))
                except json.JSONDecodeError as exc:
                    self._message_queue.put(PaintFEMCPError(f"Could not decode MCP JSON payload: {body!r}"))
        except Exception as exc:
            self._message_queue.put(exc)

    def _send_message(self, message: Dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise PaintFEMCPError("MCP client is not connected to PaintFE.")

        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if self.log:
            self.log.detail(f"SEND {json.dumps(message, ensure_ascii=False)}")
        header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
        self.process.stdin.write(header + payload)
        self.process.stdin.flush()

    def _read_message(self, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        if self.process is None:
            raise PaintFEMCPError("PaintFE stdio stream is not available.")
        try:
            item = self._message_queue.get(timeout=timeout_seconds)
        except _queue.Empty:
            if self.process.poll() is not None:
                stderr_text = "\n".join(self.stderr_lines[-5:])
                raise PaintFEMCPError(
                    f"PaintFE exited unexpectedly while waiting for a response. "
                    f"stderr: {stderr_text or 'no stderr output'}"
                )
            raise TimeoutError(
                f"PaintFE is running (PID {self.process.pid}), but the MCP server did not respond "
                f"within {timeout_seconds:.0f} seconds. "
                "Open Preferences > AI and enable Local agent connections. "
                "If connections are already enabled, check that the PaintFE window is open and not minimised."
            )
        if isinstance(item, BaseException):
            raise item
        if self.log:
            self.log.detail(f"RECV {json.dumps(item, ensure_ascii=False)}")
        return item

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, *, expect_response: bool = True, timeout_seconds: Optional[float] = TIMEOUT) -> Optional[Dict[str, Any]]:
        request_id = self.next_id
        self.next_id += 1

        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        self._send_message(message)

        if not expect_response:
            return None

        deadline = time.monotonic() + (timeout_seconds or TIMEOUT)
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"PaintFE is running, but MCP method '{method}' did not respond within {timeout_seconds} seconds. "
                    "Check Preferences > AI and enable Local agent connections, then ensure document editing is allowed."
                )
            response = self._read_message(timeout_seconds=max(0.1, deadline - time.monotonic()))
            if response.get("id") == request_id:
                if "error" in response:
                    error = response["error"]
                    message_text = error.get("message", "Unknown MCP error")
                    raise PaintFEMCPError(f"PaintFE MCP error for {method}: {message_text}")
                return response.get("result")

            # Ignore unrelated notifications triggered by the server.

    def notification(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._send_message({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        })

    def close(self) -> None:
        if self.process is None:
            return
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except Exception:
            pass
        try:
            self.process.terminate()
            self.process.wait(timeout=3)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        finally:
            self.process = None


def parse_config(config_text: str) -> Tuple[str, List[str]]:
    data = json.loads(config_text)
    server = data.get("mcpServers", {}).get("paintfe", {})
    command = server.get("command") or server.get("executable")
    args = server.get("args") or []
    if not command:
        raise PaintFEMCPError("No `command` found in the PaintFE MCP config.")
    return command, list(args)


def normalize_tool_result(payload: Any) -> Any:
    if isinstance(payload, dict):
        if "structuredContent" in payload:
            return payload["structuredContent"]
        if "content" in payload:
            content = payload["content"]
            if isinstance(content, list):
                parts: List[str] = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text = item.get("text")
                            if isinstance(text, str):
                                try:
                                    return json.loads(text)
                                except json.JSONDecodeError:
                                    parts.append(text)
                        else:
                            return item
                if parts:
                    return parts[0]
            return content
        return payload
    return payload


def get_documents(client: MCPStdioClient) -> List[Dict[str, Any]]:
    raw = client.request("tools/call", {"name": "documents", "arguments": {}})
    result = normalize_tool_result(raw)

    if isinstance(result, dict) and "documents" in result:
        docs = result["documents"]
    elif isinstance(result, list):
        docs = result
    elif isinstance(result, dict):
        docs = [result]
    else:
        raise PaintFEMCPError(f"Unexpected documents response: {result!r}")

    if not isinstance(docs, list):
        raise PaintFEMCPError(f"The documents tool did not return a list: {docs!r}")
    return docs


def pick_document_and_layer(doc: Dict[str, Any]) -> Tuple[str, str, int]:
    document_id = doc.get("document") or doc.get("id") or doc.get("document_id")
    if not document_id:
        raise PaintFEMCPError(f"Could not find the document identifier in: {doc!r}")

    layers = doc.get("layers") or doc.get("items") or []
    layer = None

    for entry in layers:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type") or entry.get("kind") or entry.get("layerType") or "").lower()
        if kind in {"raster", "bitmap", "image", "layer"} or "raster" in kind:
            layer = entry
            break
        if entry.get("active") or entry.get("selected"):
            layer = entry
            break

    if layer is None and layers:
        layer = layers[0]

    if layer is None:
        raise PaintFEMCPError(f"No raster layer found in document {document_id!r}.")

    layer_id = layer.get("id") or layer.get("layer") or layer.get("layer_id")
    if not layer_id:
        raise PaintFEMCPError(f"No layer identifier was found for document {document_id!r}.")

    revision = doc.get("revision")
    if revision is None:
        revision = doc.get("current_revision") or doc.get("currentRevision") or doc.get("version")
    if revision is None:
        raise PaintFEMCPError(f"No revision number found for document {document_id!r}.")

    return str(document_id), str(layer_id), int(revision)


def wait_for_job_completion(client: MCPStdioClient, timeout_seconds: float = 15.0) -> Any:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        raw = client.request("tools/call", {"name": "job_status", "arguments": {}})
        status = normalize_tool_result(raw)

        if isinstance(status, dict):
            state = str(status.get("status") or status.get("state") or status.get("result") or "").lower()
            if not state or "idle" in state or "done" in state or "completed" in state or "finished" in state:
                return status
            if "error" in state or "cancel" in state:
                return status
        elif isinstance(status, str):
            state = status.lower()
            if not state or "idle" in state or "done" in state or "completed" in state or "finished" in state:
                return status
            if "error" in state or "cancel" in state:
                return status

        time.sleep(0.25)

    raise TimeoutError("PaintFE async job did not complete within the allowed time.")


def connect_and_test_paintfe(config_text: str, log: Optional[RunLog] = None) -> int:
    command, args = parse_config(config_text)
    client = MCPStdioClient(command, args, log=log)

    try:
        if log:
            log.info(f"Launching PaintFE: {command} {' '.join(args)}")
        client.start()

        if log:
            log.info("Sending initialize handshake to MCP server")
        client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "paintfe-python-probe", "version": "1.0.0"},
            },
            timeout_seconds=TIMEOUT,
        )
        client.notification("notifications/initialized")

        tool_list_result = client.request("tools/list", {})
        tools = normalize_tool_result(tool_list_result)
        if isinstance(tools, dict):
            tool_names = [tool.get("name") for tool in tools.get("tools", []) if isinstance(tool, dict)]
        elif isinstance(tools, list):
            tool_names = [tool.get("name") if isinstance(tool, dict) else str(tool) for tool in tools]
        else:
            raise PaintFEMCPError(f"Unexpected result from tools/list: {tools!r}")

        if not tool_names:
            raise PaintFEMCPError("PaintFE returned no MCP tools. Enable local agent connections and try again.")

        print("MCP connection OK.")
        print(f"Available tools: {', '.join(tool_names)}") # pyright: ignore[reportArgumentType, reportCallIssue]
        if log:
            log.info(f"MCP connection OK. Available tools: {', '.join(tool_names)}") # pyright: ignore[reportCallIssue, reportArgumentType]

        documents = get_documents(client)
        if not documents:
            raise PaintFEMCPError("There are no open PaintFE documents to edit.")

        doc = documents[0]
        document_id, layer_id, revision = pick_document_and_layer(doc)
        print(f"Using document={document_id}, layer={layer_id}, revision={revision}")
        if log:
            log.info(f"Using document={document_id}, layer={layer_id}, revision={revision}")

        if "activate_layer" in tool_names:
            response = client.request(
                "tools/call",
                {"name": "activate_layer", "arguments": {"document": document_id, "layer": layer_id}},
            )
            print(f"activate_layer response: {normalize_tool_result(response)}")
            if log:
                log.info(f"activate_layer response: {normalize_tool_result(response)}")

        width = int(doc.get("width") or doc.get("image_width") or 1024)
        height = int(doc.get("height") or doc.get("image_height") or 768)

        points = [
            [(width * 0.15), (height * 0.20)],
            [(width * 0.50), (height * 0.50)],
            [(width * 0.80), (height * 0.80)],
        ]
        colors = [
            [255, 0, 0, 255],
            [0, 255, 0, 255],
            [0, 0, 255, 255],
        ]

        for idx, (point, colour) in enumerate(zip(points, colors), start=1):
            x, y = int(point[0]), int(point[1])
            payload = {
                "document": document_id,
                "layer": layer_id,
                "revision": revision,
                "points": [[x, y]],
                "colour": colour,
                "size": 8,
                "pencil": True,
            }
            print(f"Drawing dot #{idx} at ({x}, {y}) using colour {colour}...")
            if log:
                log.info(f"Drawing dot #{idx} at ({x}, {y}) using colour {colour}")
            client.request("tools/call", {"name": "stroke", "arguments": payload})
            wait_for_job_completion(client)

            refreshed = get_documents(client)
            if refreshed:
                doc = refreshed[0]
                document_id, layer_id, revision = pick_document_and_layer(doc)

        print("Three test dots have been sent to PaintFE.")
        print("Open the PaintFE window and it should show three colored dots in different positions.")
        if log:
            log.info("Three test dots have been sent to PaintFE.")
        return 0

    except Exception as exc:  # pragma: no cover - top-level CLI error path
        stderr_text = "\n".join(client.stderr_lines[-20:])
        if stderr_text and log:
            log.error(f"PaintFE stderr: {stderr_text}")
        elif stderr_text:
            print(f"PaintFE stderr: {stderr_text}", file=sys.stderr)

        if log:
            log.error(f"Connection test failed: {exc}")
        else:
            print(f"Connection test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the PaintFE MCP stdio server and draw three test dots.")
    parser.add_argument(
        "--config",
        default=json.dumps(DEFAULT_CONFIG, separators=(",", ":")),
        help="JSON string with the PaintFE MCP server config (default: the example from the docs).",
    )
    parser.add_argument(
        "--command",
        help="Override the PaintFE executable path. Use only if you do not want to rely on the JSON config.",
    )
    parser.add_argument(
        "--arg",
        action="append",
        dest="args",
        default=[],
        help="Extra argument to pass to PaintFE (for example --mcp). Can be used more than once.",
    )
    args = parser.parse_args()

    config_text = args.config
    if args.command:
        if not args.args:
            args.args = ["--mcp"]
        config_text = json.dumps({"mcpServers": {"paintfe": {"command": args.command, "args": args.args}}}, separators=(",", ":"))

    with RunLog(LOG_DIR) as log:
        log.info(f"Starting PaintFE MCP probe with config: {config_text}")
        return connect_and_test_paintfe(config_text, log=log)


if __name__ == "__main__":
    raise SystemExit(main())
