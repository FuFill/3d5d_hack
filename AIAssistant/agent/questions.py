# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Questions from the agent to the user: one card with 1-6 questions, 2-4 options each plus a free answer.

The ask_user tool blocks its worker thread until the chat window sends all answers
(the card walks through the questions and ends with a review step) or the turn is
cancelled; the answers are returned to the model as the tool result.
"""

import threading

MAX_QUESTIONS = 6


class QuestionCancelled(Exception):
    pass


class QuestionBox:
    def __init__(self, bus):
        self._bus = bus
        self._cond = threading.Condition()
        self._pending = {}  # event seq -> list of answers or None
        self._sizes = {}
        self._cancelled = False

    def ask(self, questions, should_stop=None):
        """questions: [{"text", "options", "allow_custom"}]; returns ([{"text", "choice"}], seq)."""
        with self._cond:
            self._cancelled = False
            # Publish and register under the same lock: an immediate UI answer
            # must not arrive before the question is registered.
            seq = self._bus.emit("question", questions=questions, status="pending", answers=None)
            self._pending[seq] = None
            self._sizes[seq] = len(questions)
            answers = None
            try:
                while self._pending[seq] is None:
                    if self._cancelled or (should_stop and should_stop()):
                        break
                    self._cond.wait(0.5)
                answers = self._pending[seq]
            finally:
                self._pending.pop(seq, None)
                self._sizes.pop(seq, None)
        if answers is None:
            self._bus.update(seq, status="cancelled")
            raise QuestionCancelled()
        self._bus.update(seq, status="answered", answers=answers)
        return answers, seq

    def answer(self, seq, answers):
        """answers: one {"text", "choice"} per question, in order; False if not pending or incomplete."""
        with self._cond:
            if seq not in self._pending or not isinstance(answers, list) or len(answers) != self._sizes.get(seq):
                return False
            clean = []
            for item in answers:
                text = str((item or {}).get("text") or "").strip()
                if not text:
                    return False
                choice = item.get("choice")
                clean.append({"text": text[:2000], "choice": choice if isinstance(choice, int) and
                              not isinstance(choice, bool) else None})
            self._pending[seq] = clean
            self._cond.notify_all()
            return True

    def pending(self):
        with self._cond:
            return sorted(self._pending)

    def cancel_all(self):
        with self._cond:
            self._cancelled = True
            self._cond.notify_all()
