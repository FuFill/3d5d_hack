# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Saved chats: the chat feed (events) and the model history (messages) as one JSON file per chat.

Images are not stored (only a note that one was shown); everything else is kept so an
old chat can be reopened and continued after restarting the slicer. Model and specification
files attached in the chat are kept next to it (<id>.files/<attachment id>/<name>), so the
model can still plan from them after a restart.
"""

import json
import os
import re
import shutil
import threading
import time
import uuid

TITLE_CHARS = 48


def new_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def title_of(events):
    for event in events:
        if event.get("type") == "user":
            text = " ".join(str(event.get("text") or "").split())
            if not text and event.get("attachments"):
                text = event["attachments"][0].get("name", "")
            if text:
                return text if len(text) <= TITLE_CHARS else text[:TITLE_CHARS - 1].rstrip() + "…"
    return ""


def _without_images(messages):
    out = []
    for message in messages:
        if isinstance(message.get("content"), list):
            message = dict(message, content=[p if p.get("type") != "image_url" else
                                             {"type": "text", "text": "[image shown earlier]"}
                                             for p in message["content"]])
        out.append(message)
    return out


class ChatStore:
    def __init__(self, folder):
        self.folder = folder
        self._lock = threading.Lock()
        if not os.path.isdir(folder):
            os.makedirs(folder)

    def _path(self, chat_id):
        if not re.match(r"^[0-9A-Za-z_-]{1,64}$", str(chat_id)):
            raise ValueError("chat id")
        return os.path.join(self.folder, chat_id + ".json")

    def _files(self, chat_id):
        return self._path(chat_id)[:-len(".json")] + ".files"

    def _keep_files(self, chat_id, files):
        """Copies (hard links where possible) of the chat's attachments; returns their records."""
        kept = []
        for item in files or []:
            if not re.match(r"^[0-9A-Za-z_-]{1,64}$", str(item.get("id"))) or not os.path.isfile(item.get("path") or ""):
                continue
            name = os.path.basename(item["name"]) or "file"
            folder = os.path.join(self._files(chat_id), item["id"])
            target = os.path.join(folder, name)
            if os.path.abspath(target) != os.path.abspath(item["path"]) and not os.path.isfile(target):
                if not os.path.isdir(folder):
                    os.makedirs(folder)
                try:
                    os.link(item["path"], target)
                except (OSError, AttributeError, NotImplementedError):
                    shutil.copyfile(item["path"], target)
            kept.append({"id": item["id"], "name": name, "kind": item["kind"]})
        return kept

    def files(self, chat_id, records):
        """Attachment records of a loaded chat whose files still exist, with their paths."""
        out = []
        for record in records or []:
            if not re.match(r"^[0-9A-Za-z_-]{1,64}$", str(record.get("id"))):
                continue
            path = os.path.join(self._files(chat_id), record["id"], os.path.basename(record.get("name") or ""))
            if os.path.isfile(path):
                out.append({"id": record["id"], "name": record["name"], "kind": record.get("kind"), "path": path,
                            "size": os.path.getsize(path)})
        return out

    def save(self, chat_id, events, messages, files=None, tree=None, turns=None, head=None):
        """turns/head: the chat as a tree of turns (each with its feed events and model messages)
        and the last turn of the branch on screen; events/messages are then not stored twice."""
        if turns is not None:
            ordered = sorted(turns, key=lambda t: t.get("created", 0))
            events = [e for t in ordered for e in t.get("events") or []]
        title = title_of(events)
        if not title:
            return False  # an empty chat is not worth keeping
        path = self._path(chat_id)
        try:
            kept = self._keep_files(chat_id, files)
        except OSError:
            kept = []  # the chat itself is still saved
        data = {"id": chat_id, "title": title, "updated": time.time(), "files": kept, "tree": tree or {}}
        if turns is not None:
            data.update(turns=[dict(t, messages=_without_images(t.get("messages") or [])) for t in turns],
                        head=head, events=[], messages=[])
        else:
            data.update(events=[e for e in events if e.get("type") != "reset"], messages=_without_images(messages))
        with self._lock:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(path + ".tmp", path)
        return True

    def load(self, chat_id):
        with self._lock, open(self._path(chat_id), encoding="utf-8") as f:
            return json.load(f)

    def delete(self, chat_id):
        with self._lock:
            path = self._path(chat_id)
            if os.path.isfile(path):
                os.remove(path)
            shutil.rmtree(self._files(chat_id), ignore_errors=True)

    def list(self, limit=100):
        items = []
        with self._lock:
            names = [n for n in os.listdir(self.folder) if n.endswith(".json")]
        for name in names:
            try:
                with open(os.path.join(self.folder, name), encoding="utf-8") as f:
                    data = json.load(f)
                items.append({"id": data["id"], "title": data.get("title") or "", "updated": data.get("updated", 0)})
            except (OSError, ValueError, KeyError):
                continue
        items.sort(key=lambda i: -i["updated"])
        return items[:limit]
