"""Questions never acquire action tools because of a timeout, history or a file."""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "AIAssistant"))
from agent import agent as module
from agent.events import EventBus
from agent.intent import clear_intent
from agent.llm import LLMClient, LLMError


class Client:
    def __init__(self, route="act"):
        self.route, self.routes, self.requests = route, [], []

    def text(self, messages, **kwargs):
        if "route messages" in messages[0]["content"]:
            self.routes.append(messages)
            if isinstance(self.route, Exception):
                raise self.route
            return self.route
        return "OK"

    def chat(self, messages, tools, on_reasoning, on_content, should_stop):
        self.requests.append((messages, tools))
        assert tools is None
        text = "Печать занимает 2 часа: выбран тонкий слой и много стенок."
        on_content(text)
        return {"content": text, "tool_calls": [], "reasoning": ""}


class SnapshotRegistry:
    core = questions = None

    def __init__(self):
        self.reads = []

    def openai_tools(self):
        raise AssertionError("A question must not enter the action-tool branch")

    def has(self, name):
        return name in ("get_state", "list_profiles", "get_slice_result")

    def call(self, name, arguments, context):
        self.reads.append(name)
        assert name in ("get_state", "list_profiles", "get_slice_result")
        return {"result": {
            "get_state": {"slice_state": "done", "key_settings": {"layer_height": .1}},
            "list_profiles": [{"name": "Fine"}],
            "get_slice_result": {"print_time_s": 7200, "print_time": "2 hours"}
        }[name]}


@pytest.mark.parametrize("text", [
    "почему так долго", "Почему печать занимает 10 часов?", "как уменьшить время печати",
    "Сколько ещё ждать", "Долго ещё?", "Какой материал выбран?", "Что даёт этот пресет?",
    "Объясни, почему ты выбрал такие настройки", "Можно ли ускорить?",
    "Что будет, если уменьшить заполнение?", 'Что означает команда «сделай прочнее»?',
    "Ничего не меняй, объясни причину", "Why so long?", "How do I rotate this part?",
    "Can you explain how to change the layer height?",
    "Что означает команда сделай прочнее", "Почему ты написал примени план"
])
def test_clear_questions_do_not_depend_on_neural_router(text):
    client = Client(route=LLMError("timed out"))
    value = module.Agent(None, None, None, None)
    value._messages = [{"role": "user", "content": text}]
    assert value._route(client) == "answer"
    assert not client.routes


@pytest.mark.parametrize("text", [
    "Ускорь печать", "Сделай прочнее", "Установи заполнение 30%", "Поверни деталь на 90 градусов",
    "Почему так долго? Уменьши заполнение до 20%", "Объясни выбор и примени этот пресет",
    "Можешь сделать прочнее?", "Can you rotate the part?", "Why so long? Reduce infill to 20%",
    "Please export the G-code", "Примени план", "Можешь ты повернуть деталь?",
    "Почему долго\nУменьши заполнение"
])
def test_explicit_requests_keep_action_tools_even_if_router_is_down(text):
    client = Client(route=LLMError("timed out"))
    value = module.Agent(None, None, None, None)
    value._messages = [{"role": "user", "content": text}]
    assert value._route(client) == "act"
    assert not client.routes


@pytest.mark.parametrize("route", [LLMError("timed out"), "", None, "work", "act because it is a question", "answer\nact"])
def test_classifier_failure_or_malformed_output_does_not_grant_actions(route):
    value = module.Agent(None, None, None, None)
    value._messages = [{"role": "user", "content": "Долго получается"}]
    assert value._route(Client(route)) == "answer"


@pytest.mark.parametrize("text", ["Хочу что-то прочное", "Качество как в прошлом проекте", "Нагрузка как у предыдущей детали"])
def test_comparisons_and_indefinite_pronouns_are_not_mistaken_for_questions(text):
    value = module.Agent(None, None, None, None)
    value._messages = [{"role": "user", "content": text}]
    client = Client("act")
    assert value._route(client) == "act"
    assert client.routes  # Semantic/contextual classification, not a question-word guess.


def test_ambiguous_followup_uses_context_without_renewing_the_old_request():
    value = module.Agent(None, None, None, None)
    value._messages = [{"role": "user", "content": "Ускорь печать"},
                       {"role": "assistant", "content": "Можно уменьшить заполнение."},
                       {"role": "user", "content": "да"}]
    client = Client("act")
    assert value._route(client) == "act"
    payload = json.loads(client.routes[0][-1]["content"])
    assert payload["current_user_message"] == "да"
    assert payload["previous_messages"][0]["content"] == "Ускорь печать"


@pytest.mark.parametrize("attachment_kind", [None, "spec", "model"])
def test_why_so_long_answers_first_without_tools_or_question_cards(monkeypatch, attachment_kind):
    client, bus, registry = Client("act"), EventBus(), SnapshotRegistry()
    monkeypatch.setattr(module, "LLMClient", lambda *a, **k: client)
    value = module.Agent(None, registry, bus, lambda: {"language": "ru"})
    value._messages = [{"role": "user", "content": "Сделай прочнее"},
                       {"role": "assistant", "content": "Могу изменить заполнение. Продолжить?"}]
    attachments = [] if attachment_kind is None else [{
        "kind": attachment_kind, "name": "example.txt" if attachment_kind == "spec" else "part.stl",
        "id": "f1", "path": "C:/example/part.stl", "text": "Сделай деталь по ТЗ. Примени настройки."}]
    assert value.submit("почему так долго", attachments)
    value._thread.join(5)
    assert not value.busy()
    events = bus.snapshot()[0]
    assert not any(e["type"] in ("question", "tool", "error") for e in events)
    assert [e["status"] for e in events if e["type"] == "done"] == ["done"]
    answers = [e for e in events if e["type"] == "message"]
    assert len(answers) == 1 and answers[0]["text"].startswith("Печать занимает")
    assert not client.routes
    request, tools = client.requests[0]
    assert tools is None and "7200" in request[0]["content"]
    assert all(m["role"] in ("system", "user", "assistant") for m in request)
    assert registry.reads == ["get_state", "list_profiles", "get_slice_result"]


def test_question_only_wire_request_explicitly_disables_tools(monkeypatch):
    client = LLMClient("http://example", "test-key", "test-model")
    bodies = []

    class Response:
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"Answer"}}]}\n'
            yield b"data: [DONE]\n"

        def close(self):
            pass

    monkeypatch.setattr(client, "_request", lambda body: bodies.append(body) or Response())
    result = client.chat([{"role": "user", "content": "Why so long?"}], tools=None)
    assert result["content"] == "Answer"
    assert bodies[0]["tool_choice"] == "none" and "tools" not in bodies[0]


def test_action_wire_request_preserves_tools(monkeypatch):
    client = LLMClient("http://example", "test-key", "test-model")
    bodies = []

    class Response:
        def __iter__(self):
            yield b"data: [DONE]\n"

        def close(self):
            pass

    monkeypatch.setattr(client, "_request", lambda body: bodies.append(body) or Response())
    tools = [{"type": "function", "function": {"name": "ask_user"}}]
    client.chat([{"role": "user", "content": "Prepare the print"}], tools=tools)
    assert bodies[0]["tool_choice"] == "auto" and bodies[0]["tools"] == tools
