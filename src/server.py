#!/usr/bin/env python3
"""Phase 2/3 local HTTP server — serves the screens, the screen_state API, the
locally-bundled livekit-client JS, and LiveKit access tokens.

Routes:
    GET /              → screen.html (Phase 2 HTTP-poll screen — unchanged)
    GET /state         → LATEST screen_state as JSON (polled by screen.html ~600ms)
    GET /ask?q=...&machine=...
                       → core.answer(q, machine, CosineRetriever()) → set LATEST → JSON
    GET /operator.html → operator.html (Phase 3 unified voice + live screen)   [NEW]
    GET /operator      → alias for /operator.html                              [NEW]
    GET /console(.html)→ alias for /operator.html (same operator console UI)   [NEW]
    GET /static/<file> → the locally-bundled livekit-client UMD + operator.js  [NEW]
    GET /token?identity=...
                       → {"url", "token", "room"} — LiveKit JWT for the browser [NEW]
    GET /tts?text=...  → Kokoro-synthesized WAV for operator demo / typed fallback [NEW]

Uses stdlib only for the core (http.server.ThreadingHTTPServer, asyncio, json,
urllib). /token additionally imports livekit.api (already a project dep); /tts
lazy-imports kokoro_onnx + soundfile (same stack as voice_smoke.py).
Port from PORT env var, default 8000.

The module-global LATEST starts as an idle placeholder whose shape is identical
to a real screen_state (every key present, status="idle") so applyState() in the
browser can call it on the first poll without hitting undefined.

WIFI-OFF (G5): the livekit-client JS is served from static/ on localhost — no CDN
at runtime. operator.html is served from localhost too, so ws://localhost works
with no mixed-content block.
"""
import asyncio
import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import core
import paths
from retriever import CosineRetriever, load_env

# Load .env so LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET are available to
# /token (reuses retriever's stdlib-only os.environ.setdefault loader; harmless if
# already loaded). Falls back to local-dev defaults below.
try:
    load_env()
except Exception:
    pass

SCREEN_HTML = paths.WEB / "screen.html"
OPERATOR_HTML = paths.WEB / "operator.html"
STATIC_DIR = paths.WEB / "static"

# LiveKit config — local self-hosted defaults (G1: never a cloud URL).
LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "secret")
ROOM_NAME = os.environ.get("ROOM_NAME", "manuai")
# Must match agent.py's @server.rtc_session(agent_name=...) for explicit dispatch.
AGENT_NAME = os.environ.get("AGENT_NAME", "manuai")

# Content types for the few static asset kinds we serve.
_STATIC_TYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".wasm": "application/wasm",
}

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

TTS_VOICE = os.environ.get("TTS_VOICE", "af_heart")
KOKORO_MODEL_PATH = os.environ.get("KOKORO_MODEL_PATH", str(paths.MODELS / "kokoro-v1.0.onnx"))
KOKORO_VOICES_PATH = os.environ.get("KOKORO_VOICES_PATH", str(paths.MODELS / "voices-v1.0.bin"))
_kokoro = None


def _synth_wav_bytes(text: str) -> bytes:
    """Kokoro TTS → in-memory WAV (same voice stack as agent.py / voice_smoke)."""
    global _kokoro
    import io

    import soundfile as sf
    from kokoro_onnx import Kokoro

    if _kokoro is None:
        if not Path(KOKORO_MODEL_PATH).is_file() or not Path(KOKORO_VOICES_PATH).is_file():
            raise FileNotFoundError(
                "Kokoro weights missing — place kokoro-v1.0.onnx + voices-v1.0.bin in models/"
            )
        _kokoro = Kokoro(KOKORO_MODEL_PATH, KOKORO_VOICES_PATH)
    samples, sample_rate = _kokoro.create(text, voice=TTS_VOICE, speed=1.0, lang="en-us")
    buf = io.BytesIO()
    sf.write(buf, samples, sample_rate, format="WAV")
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet; only errors to stderr
        pass

    # Set by do_HEAD: emit headers (incl. Content-Length) but no body.
    _head_only = False

    def _write_body(self, body: bytes):
        if not self._head_only:
            self.wfile.write(body)

    def _send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self._write_body(body)

    def _send_html(self, path: Path, code=200):
        body = path.read_bytes()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write_body(body)

    def _send_bytes(self, body: bytes, content_type: str, code=200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self._write_body(body)

    def _serve_static(self, rel_path: str):
        """Serve a file from static/ (the bundled livekit-client JS + operator.js).

        Guards against path traversal: the resolved path must stay inside STATIC_DIR.
        """
        # Normalize and confine to STATIC_DIR.
        candidate = (STATIC_DIR / rel_path).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(403, "forbidden")
            return
        if not candidate.is_file():
            self.send_error(404, "static file not found")
            return
        ctype = _STATIC_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        self._send_bytes(candidate.read_bytes(), ctype)

    def _mint_token(self, identity: str) -> dict:
        """Mint a LiveKit join token for ``identity`` into room ROOM_NAME.

        Grants: room_join + can_publish (mic) + can_subscribe (agent audio) +
        can_publish_data (so the browser could publish too). Imported lazily so a
        missing/broken livekit.api never takes down the Phase 2 routes.

        EXPLICIT AGENT DISPATCH: agent.py registers with ``agent_name="manuai"``,
        which DISABLES LiveKit automatic dispatch — the worker joins no room until
        explicitly told to. We embed a RoomConfiguration with a RoomAgentDispatch in
        the token so that the operator *joining* the room auto-dispatches the manuai
        agent. Without this the browser sits at "waiting for agent" forever. The
        room-config step is best-effort: if this livekit build lacks the API the
        token is still minted (the agent can be dispatched via `lk dispatch create`).
        """
        from livekit.api import AccessToken, VideoGrants

        # Unique identity per load — avoid two tabs colliding on the same identity.
        unique = identity or "operator"
        unique = f"{unique}-{uuid.uuid4().hex[:8]}"

        grant = VideoGrants(
            room_join=True,
            room=ROOM_NAME,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
        )
        builder = (
            AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            .with_identity(unique)
            .with_grants(grant)
        )
        dispatched = False
        try:
            from livekit.api import RoomAgentDispatch, RoomConfiguration

            builder = builder.with_room_config(
                RoomConfiguration(
                    agents=[RoomAgentDispatch(agent_name=AGENT_NAME)]
                )
            )
            dispatched = True
        except Exception:  # noqa: BLE001
            # Older/newer livekit without the room-config API → fall back to manual
            # `lk dispatch create --room manuai --agent-name manuai`.
            dispatched = False

        return {
            "url": LIVEKIT_URL,
            "token": builder.to_jwt(),
            "room": ROOM_NAME,
            "agent_dispatch": dispatched,
        }

    def do_HEAD(self):
        # Share do_GET's routing but suppress bodies (curl -I uses HEAD). For /ask
        # we avoid running the brain on a HEAD: just acknowledge with headers.
        self._head_only = True
        try:
            if urlparse(self.path).path == "/ask":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                return
            self.do_GET()
        finally:
            self._head_only = False

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

        if path in ("/operator", "/operator.html"):
            if not OPERATOR_HTML.exists():
                self.send_error(404, "operator.html not found")
                return
            self._send_html(OPERATOR_HTML)
            return

        if path in ("/console", "/console.html", "/operator-console", "/operator_console.html"):
            if not OPERATOR_HTML.exists():
                self.send_error(404, "operator.html not found")
                return
            self._send_html(OPERATOR_HTML)
            return

        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return

        if path == "/tts":
            text = qs.get("text", [""])[0].strip()
            if not text:
                self.send_error(400, "text query param required")
                return
            try:
                self._send_bytes(_synth_wav_bytes(text), "audio/wav")
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"tts failed: {exc}"}, 500)
            return

        if path == "/token":
            identity = qs.get("identity", ["operator"])[0].strip() or "operator"
            try:
                self._send_json(self._mint_token(identity))
            except Exception as exc:  # noqa: BLE001
                self._send_json(
                    {"error": f"token mint failed: {exc}"}, 500
                )
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
    print(f"ManuAI screen (HTTP poll)  →  http://localhost:{port}/")
    print(f"ManuAI operator (voice)    →  http://localhost:{port}/operator.html")
    print(f"ManuAI operator console    →  http://localhost:{port}/console")
    print(f"  LiveKit: url={LIVEKIT_URL} room={ROOM_NAME} key={LIVEKIT_API_KEY}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
