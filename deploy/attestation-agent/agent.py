"""Minimal TDX quote agent.

Runs privileged on TDX confidential nodes and exposes one operation: turn a
caller-supplied 64-byte report_data into a raw TDX quote via the kernel's
configfs-tsm interface. The agent performs no binding logic and never sees
prompts, keys, or customer data; the gateway constructs report_data and the
client verifies the returned quote against Intel's PCS chain.
"""

from __future__ import annotations

import base64
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TSM_REPORT_ROOT = Path("/sys/kernel/config/tsm/report")
MAX_BODY_BYTES = 4096

_generation_lock = threading.Lock()


def generate_quote(report_data: bytes) -> dict[str, str]:
    if len(report_data) != 64:
        raise ValueError("report_data must be exactly 64 bytes")
    report_dir = TSM_REPORT_ROOT / f"agent-{uuid.uuid4().hex}"
    with _generation_lock:
        report_dir.mkdir()
        try:
            (report_dir / "inblob").write_bytes(report_data)
            quote = (report_dir / "outblob").read_bytes()
            provider = (report_dir / "provider").read_text().strip()
            generation = (report_dir / "generation").read_text().strip()
        finally:
            report_dir.rmdir()
    return {
        "quote": base64.b64encode(quote).decode(),
        "provider": provider,
        "generation": generation,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "tdx-quote-agent"

    def _respond(self, status: int, payload: dict[str, str]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == "/healthz":
            if TSM_REPORT_ROOT.is_dir():
                self._respond(200, {"status": "ok"})
            else:
                self._respond(503, {"status": "tsm_unavailable"})
            return
        self._respond(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path != "/quote":
            self._respond(404, {"error": "not_found"})
            return
        length = int(self.headers.get("content-length", "0"))
        if length <= 0 or length > MAX_BODY_BYTES:
            self._respond(400, {"error": "invalid_body_length"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            report_data = bytes.fromhex(payload["report_data"])
            result = generate_quote(report_data)
        except (KeyError, ValueError, TypeError):
            self._respond(400, {"error": "report_data must be 128 hex characters"})
            return
        except OSError:
            self._respond(503, {"error": "quote_generation_failed"})
            return
        self._respond(200, result)

    def log_message(self, fmt: str, *args: object) -> None:
        print(json.dumps({"event": "agent_request", "detail": fmt % args}), flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8100), Handler).serve_forever()
