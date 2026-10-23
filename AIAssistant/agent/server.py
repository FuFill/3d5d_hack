# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Local HTTP server: serves the chat SPA, its JSON API with long polling and the MCP endpoint.

Only 127.0.0.1 is bound. Every API call needs the session token, sent by the SPA as the
X-Token header; that also stops other web pages from calling the API (custom headers
need a CORS preflight, which this server never grants).
"""

import json
import os
import shutil
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote, urlparse

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "application/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
IMAGE_EXT = (".png", ".jpg", ".jpeg")
MODEL_EXT = (".stl", ".obj", ".3mf")
MAX_UPLOAD = 300 * 1024 * 1024


class ChatServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, service, port=0):
        self.service = service
        HTTPServer.__init__(self, ("127.0.0.1", port), Handler)


class ChatService:
    """State shared by request handlers; created by the extension on the main thread."""

    def __init__(self, bus, agent, mcp, registry, settings):
        self.bus = bus
        self.agent = agent
        self.mcp = mcp
        self.registry = registry
        self.settings = settings  # object with public(), update(dict), test()
        self.token = uuid.uuid4().hex
        self.upload_dir = tempfile.mkdtemp(prefix="ste_ai_assistant_")
        self.uploads = {}
        self._lock = threading.Lock()
        self.server = None

    def start(self, port=0):
        self.server = ChatServer(self, port)
        thread = threading.Thread(target=self.server.serve_forever, name="AIAssistantHTTP", daemon=True)
        thread.start()
        return self.server.server_port

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        shutil.rmtree(self.upload_dir, ignore_errors=True)

    @property
    def port(self):
        return self.server.server_port if self.server else 0

    def url(self):
        return "http://127.0.0.1:%d/#t=%s" % (self.port, self.token)

    def mcp_url(self):
        return "http://127.0.0.1:%d/mcp" % self.port

    def add_upload(self, name, stream, length):
        base = os.path.basename(name.replace("\\", "/")) or "file"
        ext = os.path.splitext(base)[1].lower()
        if ext in IMAGE_EXT:
            kind = "image"
        elif ext in MODEL_EXT:
            kind = "model"
        else:
            raise ValueError("unsupported")
        upload_id = uuid.uuid4().hex[:12]
        folder = os.path.join(self.upload_dir, upload_id)
        os.makedirs(folder)
        path = os.path.join(folder, base)
        remaining = length
        with open(path, "wb") as f:
            while remaining > 0:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining:
            raise ValueError("incomplete")
        item = {"id": upload_id, "name": base, "kind": kind, "path": path, "size": length}
        with self._lock:
            self.uploads[upload_id] = item
        return item

    def call_mcp(self, body):
        """MCP over HTTP; tool calls of external clients are shown in the chat like the agent's."""
        try:
            message = json.loads(body)
        except ValueError:
            return self.mcp.handle_text(body)
        if isinstance(message, dict) and message.get("method") == "tools/call" and "id" in message:
            params = message.get("params") or {}
            seq = self.bus.emit("tool", call_id=str(message.get("id")), name=params.get("name"),
                                args=params.get("arguments") or {}, status="running", source="mcp")
            reply = self.mcp.handle(message, context={"event_seq": seq})
            if "error" in reply:
                self.bus.update(seq, status="error", result={"error": reply["error"]["message"]})
            else:
                result = reply["result"]
                self.bus.update(seq, status="error" if result.get("isError") else "ok",
                                result=result.get("structuredContent"))
            return json.dumps(reply, ensure_ascii=False)
        return self.mcp.handle_text(body)


class Handler(BaseHTTPRequestHandler):
    server_version = "AIAssistant/1.0"

    def log_message(self, format, *args):
        pass

    @property
    def service(self):
        return self.server.service

    # ------------------------------------------------------------------ plumbing

    def _send(self, code, body, content_type="application/json; charset=utf-8", extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_port
        return host in ("127.0.0.1:%d" % port, "localhost:%d" % port)

    def _token_ok(self):
        token = self.headers.get("X-Token") or ""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = token or auth[7:].strip()
        return token == self.service.token

    def _body(self, limit=4 * 1024 * 1024):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError("too large")
        return self.rfile.read(length).decode("utf-8") if length else ""

    def _json(self):
        text = self._body()
        return json.loads(text) if text else {}

    # ------------------------------------------------------------------ routes

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "host"})
        url = urlparse(self.path)
        if url.path in STATIC:
            name, content_type = STATIC[url.path]
            with open(os.path.join(WEB_DIR, name), "rb") as f:
                return self._send(200, f.read(), content_type,
                                  {"Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; "
                                                              "style-src 'self'; connect-src 'self'"})
        if not self._token_ok():
            return self._send(401, {"error": "token"})
        service = self.service
        if url.path == "/api/state":
            events, seq = service.bus.snapshot()
            return self._send(200, {"events": events, "seq": seq, "busy": service.agent.busy(),
                                    "settings": service.settings.public(), "mcp_url": service.mcp_url()})
        if url.path == "/api/events":
            query = parse_qs(url.query)
            since = int((query.get("since") or ["0"])[0])
            events, seq = service.bus.since(since, timeout=20)
            return self._send(200, {"events": events, "seq": seq, "busy": service.agent.busy()})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "host"})
        if not self._token_ok():
            return self._send(401, {"error": "token"})
        url = urlparse(self.path)
        service = self.service
        try:
            if url.path == "/mcp":
                reply = service.call_mcp(self._body())
                if reply is None:
                    return self._send(202, b"", "text/plain")
                return self._send(200, reply.encode("utf-8"))
            if url.path == "/api/upload":
                name = unquote((parse_qs(url.query).get("name") or ["file"])[0])
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_UPLOAD:
                    return self._send(413, {"error": "size"})
                try:
                    item = service.add_upload(name, self.rfile, length)
                except ValueError as e:
                    return self._send(415, {"error": str(e)})
                return self._send(200, {"id": item["id"], "name": item["name"], "kind": item["kind"]})
            data = self._json()
            if url.path == "/api/message":
                text = str(data.get("text") or "").strip()
                attachments = [service.uploads[i] for i in (data.get("attachments") or []) if i in service.uploads]
                if not text and not attachments:
                    return self._send(400, {"error": "empty"})
                if not service.agent.submit(text, attachments):
                    return self._send(409, {"error": "busy"})
                return self._send(200, {"ok": True})
            if url.path == "/api/cancel":
                service.agent.cancel()
                return self._send(200, {"ok": True})
            if url.path == "/api/reset":
                service.agent.reset()
                return self._send(200, {"ok": True})
            if url.path == "/api/settings":
                service.settings.update(data)
                return self._send(200, {"settings": service.settings.public()})
            if url.path == "/api/test":
                ok, error = service.settings.test()
                return self._send(200, {"ok": ok, "error": error})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        return self._send(404, {"error": "not found"})
