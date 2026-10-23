# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Bounded OpenAI-compatible Timeweb transport, without an SDK; Python 3.5."""
import json
import math
import os
import socket
import ssl
import sys
from http.client import HTTPException
from urllib import request, error, parse

DEFAULT_BASE_URL = "https://api.timeweb.ai/v1"
DEFAULT_MODEL = "dashscope/qwen3.8-flash"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class QwenError(Exception):
    def __init__(self, code, message, http_status=None):
        super().__init__(message)
        self.code, self.http_status = code, http_status


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a bearer token to a redirect destination.


def tls_context(ca_file=None):
    context = ssl.create_default_context(cafile=ca_file)
    if not ca_file and sys.platform == "win32":
        # Some bundled OpenSSL builds omit Windows roots due to purpose filtering.
        # Import only the OS trusted ROOT store; chain/hostname verification stays on.
        roots = [ssl.DER_cert_to_PEM_cert(cert) for cert, encoding, trust in ssl.enum_certificates("ROOT")
                 if encoding == "x509_asn"]
        if roots:
            context.load_verify_locations(cadata="\n".join(roots))
    return context


def load_env_file(path, environ=None):
    """Read only our configuration keys. No shell expansion or execution."""
    env = os.environ if environ is None else environ
    allowed = {"TIMEWEB_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL", "QWEN_TIMEOUT",
               "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "QWEN_CA_FILE"}
    with open(path, encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if sep and key.strip() in allowed and key.strip() not in env:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                env[key.strip()] = value
    return env


class QwenClient(object):
    def __init__(self, api_key, base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL,
                 timeout=60, opener=None, ca_file=None):
        url = parse.urlsplit(base_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise QwenError("configuration", "QWEN_BASE_URL must be an HTTPS API URL without credentials/query")
        if not isinstance(api_key, str) or not api_key.strip() or "\n" in api_key or "\r" in api_key:
            raise QwenError("configuration", "Set TIMEWEB_API_KEY locally")
        if not isinstance(model, str) or not model.strip():
            raise QwenError("configuration", "QWEN_MODEL is required")
        try:
            timeout = float(timeout)
        except (ValueError, TypeError):
            raise QwenError("configuration", "Invalid QWEN_TIMEOUT")
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise QwenError("configuration", "QWEN_TIMEOUT must be in (0, 300] seconds")
        self._api_key = api_key.strip()
        self.base_url, self.model, self.timeout = base_url.rstrip("/"), model, timeout
        handlers = [_NoRedirect(), request.HTTPSHandler(context=tls_context(ca_file))]
        self._opener = opener or request.build_opener(*handlers).open

    @classmethod
    def from_env(cls, env_file=None, environ=None):
        env = dict(os.environ if environ is None else environ)
        if env_file:
            load_env_file(env_file, env)
        return cls(env.get("TIMEWEB_API_KEY") or env.get("LLM_API_KEY"),
                   env.get("QWEN_BASE_URL") or env.get("LLM_BASE_URL") or DEFAULT_BASE_URL,
                   env.get("QWEN_MODEL") or env.get("LLM_MODEL") or DEFAULT_MODEL,
                   env.get("QWEN_TIMEOUT", "60"), ca_file=env.get("QWEN_CA_FILE"))

    def __repr__(self):
        return "QwenClient(model=%r, credentials=<redacted>)" % self.model

    def complete(self, messages, tools=None, max_tokens=2048, tool_choice=None):
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "stream": False,
                "enable_thinking": False}
        if tools:
            body.update({"tools": tools, "tool_choice": tool_choice or "auto"})
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        req = request.Request(self.base_url + "/chat/completions", data=data,
                              headers={"Authorization": "Bearer " + self._api_key,
                                       "Content-Type": "application/json"})
        try:
            response = self._opener(req, timeout=self.timeout)
            try:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            finally:
                response.close()
        except error.HTTPError as exc:
            code = "authentication" if exc.code in (401, 403) else ("rate_limit" if exc.code == 429 else "http_error")
            exc.close()
            raise QwenError(code, "Timeweb request failed (HTTP %s)" % exc.code, exc.code) from None
        except (error.URLError, socket.timeout, OSError, HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            # Report only exception class/verification code, never URL/body/header text.
            detail = type(reason).__name__
            verify_code = getattr(reason, "verify_code", None)
            if isinstance(verify_code, int):
                detail += "; TLS verification code %d" % verify_code
            raise QwenError("network", "Timeweb connection failed (%s)" % detail) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise QwenError("response_limit", "Timeweb response exceeded size limit")
        try:
            # A provider response must never leak the credential through content/errors.
            decoded = raw.decode("utf-8").replace(self._api_key, "<redacted>")
            payload = json.loads(decoded)
            choice = payload["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict) or choice.get("finish_reason") in ("length", "content_filter"):
                raise ValueError()
            if message.get("content") is not None and not isinstance(message["content"], str):
                raise ValueError()
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list):
                raise ValueError()
            return {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
        except (ValueError, KeyError, IndexError, TypeError, UnicodeError):
            raise QwenError("protocol", "Invalid or incomplete Chat Completions response") from None
