# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Minimal OpenAI-compatible Chat Completions client with streaming and tool calls.

Only the standard library is used, so it runs on the Python 3.5 bundled with STE Slicer.
"""

import json
import ssl
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


class Cancelled(Exception):
    pass


def _ssl_context():
    # The same verified Windows ROOT-store handling as the existing core client.
    try:
        from .core_worker import core_path
        import sys
        if core_path() not in sys.path:
            sys.path.insert(0, core_path())
        from ste_planner.qwen_client import tls_context
        return tls_context()
    except (ImportError, RuntimeError):
        pass
    context = ssl.create_default_context()
    try:  # Frozen builds may lack a CA store; certifi is used when it ships with the slicer.
        import certifi
        context.load_verify_locations(certifi.where())
    except Exception:
        pass
    return context


class LLMClient:
    def __init__(self, base_url, api_key, model, timeout=180):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.timeout = timeout

    def _request(self, body, timeout=None):
        if not self.base_url or not self.api_key or not self.model:
            raise LLMError("not_configured")
        url = self.base_url + "/chat/completions"
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if body.get("stream") else "application/json",
            "Authorization": "Bearer " + self.api_key,
        })
        try:
            return urllib.request.urlopen(request, timeout=timeout or self.timeout, context=_ssl_context())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            raise LLMError("HTTP %d %s" % (e.code, detail))
        except (urllib.error.URLError, OSError) as e:
            raise LLMError(str(getattr(e, "reason", e)))

    def chat(self, messages, tools=None, on_reasoning=None, on_content=None, should_stop=None):
        """Stream one completion; returns {"content", "reasoning", "tool_calls"}."""
        body = {"model": self.model, "messages": messages, "stream": True}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        else:
            body["tool_choice"] = "none"
        response = self._request(body)
        content, reasoning, calls = [], [], {}
        try:
            for raw in response:
                if should_stop and should_stop():
                    raise Cancelled()
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise LLMError(json.dumps(chunk["error"], ensure_ascii=False)[:500])
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    think = delta.get("reasoning_content") or delta.get("reasoning")
                    if think:
                        reasoning.append(think)
                        if on_reasoning:
                            on_reasoning(think)
                    if delta.get("content"):
                        content.append(delta["content"])
                        if on_content:
                            on_content(delta["content"])
                    for part in delta.get("tool_calls") or []:
                        slot = calls.setdefault(part.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        function = part.get("function") or {}
                        if part.get("id"):
                            slot["id"] = part["id"]
                        if function.get("name"):
                            slot["name"] += function["name"]
                        if function.get("arguments"):
                            slot["arguments"] += function["arguments"]
        except OSError as e:  # timeout or dropped connection while streaming
            raise LLMError(str(e) or type(e).__name__)
        finally:
            response.close()
        tool_calls = []
        for index in sorted(calls):
            call = calls[index]
            tool_calls.append({
                "id": call["id"] or "call_%d" % index,
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"] or "{}"},
            })
        return {"content": "".join(content), "reasoning": "".join(reasoning), "tool_calls": tool_calls}

    def text(self, messages, max_tokens=None, temperature=0, timeout=None):
        """One short non-streamed completion without tools (classification, proofreading)."""
        body = {"model": self.model, "messages": messages, "temperature": temperature, "enable_thinking": False}
        if max_tokens:
            body["max_tokens"] = max_tokens
        response = self._request(body, timeout)
        try:
            data = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as e:  # timeout, dropped connection, not JSON
            raise LLMError(str(e) or type(e).__name__)
        finally:
            response.close()
        if data.get("error"):
            raise LLMError(json.dumps(data["error"], ensure_ascii=False)[:500])
        choices = data.get("choices") or [{}]
        return ((choices[0].get("message") or {}).get("content") or "").strip()

    def ping(self):
        body = {"model": self.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
        response = self._request(body)
        try:
            json.loads(response.read().decode("utf-8"))
        finally:
            response.close()
        return True
