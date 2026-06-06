"""Tests for error classification."""

from __future__ import annotations

import json

import httpx
from aitelier.errors import classify_error


def test_connect_error():
    assert classify_error(httpx.ConnectError("refused")) == "ProviderUnavailable"


def test_connection_error():
    assert classify_error(ConnectionError("reset")) == "ProviderUnavailable"


def test_os_error():
    assert classify_error(OSError("network down")) == "ProviderUnavailable"


def test_timeout_exception():
    assert classify_error(httpx.TimeoutException("timed out")) == "Timeout"


def test_timeout_error():
    assert classify_error(TimeoutError()) == "Timeout"


def test_json_decode_error():
    try:
        json.loads("not json")
    except json.JSONDecodeError as exc:
        assert classify_error(exc) == "SchemaViolation"


def test_http_status_429():
    resp = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    err = httpx.HTTPStatusError("rate limit", request=resp.request, response=resp)
    assert classify_error(err) == "RateLimited"


def test_http_status_401():
    resp = httpx.Response(401, request=httpx.Request("POST", "http://x"))
    err = httpx.HTTPStatusError("unauth", request=resp.request, response=resp)
    assert classify_error(err) == "AuthError"


def test_http_status_500():
    resp = httpx.Response(500, request=httpx.Request("POST", "http://x"))
    err = httpx.HTTPStatusError("server", request=resp.request, response=resp)
    assert classify_error(err) == "ProviderError"


def test_unknown_passes_through():
    assert classify_error(RuntimeError("oops")) == "RuntimeError"
