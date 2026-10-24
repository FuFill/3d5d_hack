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
from urllib.parse import parse_qs, urlparse

from .documents import extract_text
from .versions import START_NAME, ModelTree, changes, same_state

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "application/javascript; charset=utf-8"),
          "/presets.js": ("presets.js", "application/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
IMAGE_EXT = (".png", ".jpg", ".jpeg")
MODEL_EXT = (".stl", ".obj", ".3mf")
SPEC_EXT = (".docx", ".txt", ".md")
MAX_UPLOAD = 300 * 1024 * 1024


class ChatServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, service, port=0):
        self.service = service
        HTTPServer.__init__(self, ("127.0.0.1", port), Handler)


class ChatService:
    """State shared by request handlers; created by the extension on the main thread."""

    def __init__(self, bus, agent, mcp, registry, settings, store=None):
        self.bus = bus
        self.agent = agent
        self.store = store  # ChatStore or None (no saved chats)
        self.chat_id = None
        self.tree = ModelTree()
        self.state_io = None  # versions.StateIO: reads and restores the print setup (set by the extension)
        self.on_hover = None  # shows a node card next to the chat in the slicer window: (node view or None, y)
        if store is not None and agent is not None:
            from .chats import new_id
            self.chat_id = new_id()
            agent.on_turn_done = self.save_chat
        if agent is not None:
            agent.before_work = self.before_work
            agent.after_work = self.after_work
        self.mcp = mcp
        self.registry = registry
        self.settings = settings  # object with public(), update(dict), test()
        self.token = uuid.uuid4().hex
        self.restored = None
        self.upload_dir = tempfile.mkdtemp(prefix="ste_ai_assistant_")
        self.uploads = {}
        self._lock = threading.Lock()
        self.server = None

    # ------------------------------------------------------------------ saved chats

    def save_chat(self):
        if self.store is None:
            return False
        events, _seq = self.bus.snapshot()
        files = self._chat_uploads(("model", "spec"), events)
        return self.store.save(self.chat_id, events, self.agent.messages(), files, self.tree.to_dict())

    def chats(self):
        if self.store is None:
            return []
        items = self.store.list()
        for item in items:
            item["current"] = item["id"] == self.chat_id
        return items

    def new_chat(self):
        """Keep the current chat in the list and start an empty one."""
        if self.agent.busy():
            return False
        self.save_chat()
        if not self.agent.reset():
            return False
        if self.store is not None:
            from .chats import new_id
            self.chat_id = new_id()
        self.tree = ModelTree()
        return True

    def open_chat(self, chat_id):
        if self.store is None or self.agent.busy():
            return False
        data = self.store.load(chat_id)
        self.save_chat()
        if not self.agent.reset():
            return False
        self.chat_id = data["id"]
        self._restore_files(self.store.files(data["id"], data.get("files")))
        self.agent.load(data.get("messages") or [])
        self.bus.load(data.get("events") or [])
        self.tree = ModelTree(data.get("tree"))
        self.restored = self._restore_selected()
        return True

    # ------------------------------------------------------------------ model tree

    def _language(self):
        try:
            return self.settings.public().get("language", "ru")
        except Exception:
            return "ru"

    def _restore_selected(self):
        """The slicer goes to the model last chosen in this chat; returns what happened for the window."""
        node = self.tree.current()
        if node is None or self.state_io is None:
            return None
        try:
            result = self.state_io.restore(node["snapshot"])
        except Exception as e:
            return {"error": str(e)}
        return dict(result, node=node["id"])

    def tree_view(self):
        return dict(self.tree.view(self._language()), busy=bool(self.agent and self.agent.busy()))

    def before_work(self):
        """The project as it was before the first work of the chat: the root of its tree."""
        if self.state_io is None or self.tree.nodes or self.tree.start is not None:
            return
        self.tree.start = {"snapshot": self.state_io.capture()}

    def after_work(self, namer, language="ru", should_stop=None):
        """A finished result becomes a node under the selected one, unless nothing changed."""
        if self.state_io is None:
            return None
        snapshot = self.state_io.capture()
        current = self.tree.current()
        base = current["snapshot"] if current else (self.tree.start or {}).get("snapshot")
        if base is not None and same_state(base, snapshot):
            return None
        if not self.tree.nodes and base is not None:
            self.tree.add(base, START_NAME[0 if language == "ru" else 1], kind="start")
        self.tree.start = None
        if self.bus is not None and self.state_io.needs_slice():
            self.bus.emit("message", text="Нарезаю, чтобы сохранить модель" if language == "ru" else
                          "Slicing to save the model", streaming=False, note=True, auto=True)
        stats = self.state_io.stats(should_stop)
        node = self.tree.add(snapshot, namer(changes(base, snapshot, language)), stats)
        return node

    def select_node(self, node_id):
        """Click on a node: the slicer is switched to it; the model hears about it with the next message."""
        if self.agent.busy():
            return None
        node = self.tree.node(node_id)
        if node is None:
            raise ValueError("node")
        result = self.state_io.restore(node["snapshot"]) if self.state_io is not None else {}
        self.tree.selected = node_id
        self.agent.note("[Model tree: the user switched the slicer to the saved variant \"%s\" (%s). The slicer now has "
                        "exactly that setup; continue from it.]" % (node.get("name"), node.get("short")))
        self.save_chat()
        return dict(result or {}, node=node_id)

    def hover(self, node_id, y):
        """Node card in the slicer window next to the chat; False when only the page can show it."""
        if self.on_hover is None:
            return False
        view = next((n for n in self.tree.view(self._language())["nodes"] if n["id"] == node_id), None)
        try:
            return bool(self.on_hover(view, y))
        except Exception:
            return False

    def _restore_files(self, items):
        """Attachments of a reopened chat are available again under their old ids."""
        for item in items:
            if item["kind"] == "spec":
                try:
                    item["text"] = extract_text(item["path"])
                except ValueError:
                    continue
            elif item["kind"] != "model":
                continue
            with self._lock:
                self.uploads.setdefault(item["id"], item)

    def delete_chat(self, chat_id):
        if self.store is None:
            return False
        if chat_id == self.chat_id:
            return self.new_chat() and (self.store.delete(chat_id) or True)
        self.store.delete(chat_id)
        return True

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

    def _chat_uploads(self, kinds, events=None):
        """Uploads sent in the current chat: files of other chats are not offered to the model."""
        if events is None:
            events = self.bus.snapshot()[0]
        ids = set(a.get("id") for e in events if e.get("type") == "user" for a in e.get("attachments") or [])
        with self._lock:
            return [dict(item) for key, item in self.uploads.items() if key in ids and item["kind"] in kinds]

    def model_uploads(self):
        return self._chat_uploads(("model",))

    def spec_uploads(self):
        return self._chat_uploads(("spec",))

    def add_upload(self, name, stream, length):
        base = os.path.basename(name.replace("\\", "/")) or "file"
        ext = os.path.splitext(base)[1].lower()
        if ext in IMAGE_EXT:
            kind = "image"
        elif ext in MODEL_EXT:
            kind = "model"
        elif ext in SPEC_EXT:
            kind = "spec"
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
        if kind == "spec":
            try:
                item["text"] = extract_text(path)
            except ValueError:
                shutil.rmtree(folder, ignore_errors=True)
                raise
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
        if url.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if not self._token_ok():
            return self._send(401, {"error": "token"})
        service = self.service
        if url.path == "/api/state":
            events, seq = service.bus.snapshot()
            return self._send(200, {"events": events, "seq": seq, "busy": service.agent.busy(),
                                    "settings": service.settings.public(), "mcp_url": service.mcp_url(),
                                    "chat_id": service.chat_id, "history": service.store is not None,
                                    "tree": service.state_io is not None, "native_card": service.on_hover is not None})
        if url.path == "/api/chats":
            return self._send(200, {"chats": service.chats(), "current": service.chat_id})
        if url.path == "/api/tree":
            return self._send(200, service.tree_view())
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
                name = (parse_qs(url.query).get("name") or ["file"])[0]  # parse_qs already decodes
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return self._send(413, {"error": "empty"})
                if length > MAX_UPLOAD:
                    return self._send(413, {"error": "too_large"})
                try:
                    item = service.add_upload(name, self.rfile, length)
                except ValueError as e:
                    return self._send(415, {"error": str(e)})
                reply = {"id": item["id"], "name": item["name"], "kind": item["kind"]}
                if item["kind"] == "spec":
                    reply["chars"] = len(item["text"])
                return self._send(200, reply)
            data = self._json()
            if url.path == "/api/message":
                text = str(data.get("text") or "").strip()
                attachments = [service.uploads[i] for i in (data.get("attachments") or []) if i in service.uploads]
                if not text and not attachments:
                    return self._send(400, {"error": "empty"})
                if not service.agent.submit(text, attachments):
                    return self._send(409, {"error": "busy"})
                return self._send(200, {"ok": True})
            if url.path == "/api/answer":
                if not service.registry.questions.answer(int(data.get("seq") or 0), data.get("answers")):
                    return self._send(409, {"error": "no_question"})
                return self._send(200, {"ok": True})
            if url.path == "/api/cancel":
                service.agent.cancel()
                return self._send(200, {"ok": True})
            if url.path == "/api/reset":
                if not service.new_chat():
                    return self._send(409, {"error": "busy"})
                return self._send(200, {"ok": True, "chat_id": service.chat_id})
            if url.path == "/api/chats/open":
                try:
                    opened = service.open_chat(str(data.get("id") or ""))
                except (OSError, ValueError):
                    return self._send(404, {"error": "chat"})
                if not opened:
                    return self._send(409, {"error": "busy"})
                return self._send(200, {"ok": True, "chat_id": service.chat_id, "restored": service.restored})
            if url.path == "/api/tree/select":
                try:
                    result = service.select_node(str(data.get("id") or ""))
                except ValueError:
                    return self._send(404, {"error": "node"})
                except Exception as e:
                    return self._send(422, {"error": str(e)})
                if result is None:
                    return self._send(409, {"error": "busy"})
                return self._send(200, dict(service.tree_view(), restored=result))
            if url.path == "/api/hover":
                y = data.get("y")
                shown = service.hover(data.get("id"), float(y) if isinstance(y, (int, float)) else 0.5)
                return self._send(200, {"native": shown})
            if url.path == "/api/chats/delete":
                if not service.delete_chat(str(data.get("id") or "")):
                    return self._send(409, {"error": "busy"})
                return self._send(200, {"ok": True, "chat_id": service.chat_id})
            if url.path == "/api/settings":
                service.settings.update(data)
                return self._send(200, {"settings": service.settings.public()})
            if url.path == "/api/test":
                ok, error = service.settings.test()
                return self._send(200, {"ok": ok, "error": error})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        return self._send(404, {"error": "not found"})
