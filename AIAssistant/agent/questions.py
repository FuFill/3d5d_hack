# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Questions from the agent to the user: a card with 2-4 options plus a free answer.

The ask_user tool blocks its worker thread until the chat window answers or the
turn is cancelled; the answer is returned to the model as the tool result.
"""

import threading


class QuestionCancelled(Exception):
    pass


class QuestionBox:
    def __init__(self, bus):
        self._bus = bus
        self._cond = threading.Condition()
        self._pending = {}  # event seq -> answer dict or None
        self._cancelled = False

    def ask(self, question, options, allow_custom=True, should_stop=None):
        with self._cond:
            self._cancelled = False
        seq = self._bus.emit("question", text=question, options=options, allow_custom=allow_custom,
                             status="pending", answer=None)
        answer = None
        with self._cond:
            self._pending[seq] = None
            try:
                while self._pending[seq] is None:
                    if self._cancelled or (should_stop and should_stop()):
                        break
                    self._cond.wait(0.5)
                answer = self._pending[seq]
            finally:
                self._pending.pop(seq, None)
        if answer is None:
            self._bus.update(seq, status="cancelled")
            raise QuestionCancelled()
        self._bus.update(seq, status="answered", answer=answer["text"], choice=answer["choice"])
        return answer, seq

    def answer(self, seq, text, choice=None):
        with self._cond:
            if seq not in self._pending:
                return False
            self._pending[seq] = {"text": text, "choice": choice}
            self._cond.notify_all()
            return True

    def pending(self):
        with self._cond:
            return sorted(self._pending)

    def cancel_all(self):
        with self._cond:
            self._cancelled = True
            self._cond.notify_all()
