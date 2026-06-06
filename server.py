#!/usr/bin/env python3
"""Phase 2 local HTTP server — serves screen.html and the screen_state API.

Routes:
    GET /          → screen.html
    GET /state     → LATEST screen_state as JSON (polled by screen.html ~600ms)
    GET /ask?q=...&machine=...
                   → core.answer(q, machine, CosineRetriever()) → set LATEST → JSON

Uses stdlib only: http.server.ThreadingHTTPServer, asyncio, json, urllib.
Port from PORT env var, default 8000.

The module-global LATEST starts as an idle placeholder whose shape is identical
to a real screen_state (every key present, status="idle") so applyState() in the
browser can call it on the first poll without hitting undefined.
"""
import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import core
from retriever import CosineRetriever

HERE = Path(__file__).resolve().parent
SCREEN_HTML = HERE / "screen.html"

# Idle placeholder — every screen_state key present so the browser renders safely
LATEST = {
    "question": "",
    "machine_id": "",
    "status": "idle",
    "answer": "",
    "citations": [],
    "steps_source": None,
    "steps": [],
    "safety_warnings": [],
    "safety_flag": False,
    "top_score": 0.0,
    "threshold": None,
    "source_excerpt": "",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet; only errors to stderr
        pass

    def _send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path, code=200):
        body = path.read_bytes()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global LATEST
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/":
            if not SCREEN_HTML.exists():
                self.send_error(404, "screen.html not found")
                return
            self._send_html(SCREEN_HTML)
            return

        if path == "/state":
            self._send_json(LATEST)
            return

        if path == "/ask":
            q = qs.get("q", [""])[0].strip()
            machine = qs.get("machine", ["labeler-line3"])[0].strip() or "labeler-line3"
            if not q:
                self._send_json({"error": "q parameter required"}, 400)
                return
            try:
                state = asyncio.run(core.answer(q, machine, CosineRetriever()))
                LATEST = state
                self._send_json(state)
            except Exception as exc:
                error_state = {
                    "question": q,
                    "machine_id": machine,
                    "status": "escalated",
                    "answer": f"Server error: {exc}",
                    "citations": [],
                    "steps_source": None,
                    "steps": [],
                    "safety_warnings": [],
                    "safety_flag": False,
                    "top_score": 0.0,
                    "threshold": None,
                    "source_excerpt": "",
                }
                LATEST = error_state
                self._send_json(error_state, 500)
            return

        self.send_error(404)


def main():
    port = int(os.environ.get("PORT", 8000))
    server = ThreadingHTTPServer(("", port), Handler)
    print(f"ManuAI Phase 2 screen  →  http://localhost:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
