# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Ordered event log for the chat window, read by long polling."""

import threading
import time


class EventBus:
    def __init__(self):
        self._cond = threading.Condition()
        self._events = []
        self._seq = 0

    def emit(self, event_type, **data):
        with self._cond:
            self._seq += 1
            data["type"] = event_type
            data["seq"] = self._seq
            data["ts"] = time.time()
            self._events.append(data)
            self._cond.notify_all()
            return self._seq

    def update(self, seq, **data):
        """Replace fields of an earlier event (used for live progress) and wake readers."""
        with self._cond:
            for event in reversed(self._events):
                if event["seq"] == seq:
                    event.update(data)
                    break
            self._seq += 1
            self._events.append({"type": "patch", "seq": self._seq, "target": seq, "data": data})
            self._cond.notify_all()

    def append(self, seq, field, text):
        """Append streamed text to a field of an earlier event."""
        with self._cond:
            for event in reversed(self._events):
                if event["seq"] == seq:
                    event[field] = event.get(field, "") + text
                    break
            self._seq += 1
            self._events.append({"type": "patch", "seq": self._seq, "target": seq, "append": {field: text}})
            self._cond.notify_all()

    def since(self, seq, timeout=25.0):
        deadline = time.time() + timeout
        with self._cond:
            while self._seq <= seq:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return [e for e in self._events if e["seq"] > seq], self._seq

    def snapshot(self):
        """Current history without patch records (patches are already folded in)."""
        with self._cond:
            return [e for e in self._events if e["type"] != "patch"], self._seq

    def clear(self):
        with self._cond:
            self._events = []
            self._seq += 1
            self._events.append({"type": "reset", "seq": self._seq, "ts": time.time()})
            self._cond.notify_all()
