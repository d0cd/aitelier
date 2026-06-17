"""Tests for error classification."""

from __future__ import annotations

import json

import httpx
from aitelier.errors import classify_error, scrub_error_text


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


def test_classifies_acp_wrapped_rate_limit_as_rate_limited():
    """When Anthropic 429s the inner agent, sandbox-agent wraps the
    error as a JSON-RPC `-32603 Internal error: API Error: ... Rate
    limited` and aitelier reraises as a plain RuntimeError. Pattern-
    match the surviving prose so SDK consumers' `retry_on=
    ["RateLimited"]` policies actually retry."""
    exc = RuntimeError(
        "ACP error -32603: Internal error: API Error: Server is "
        "temporarily limiting requests (not your usage limit) "
        "· Rate limited | sandbox=local | elapsed=25.3s"
    )
    assert classify_error(exc) == "RateLimited"


def test_classifies_bare_429_message_as_rate_limited():
    """Other downstream wrappers surface plainer text — `HTTP 429`,
    `Too Many Requests`, `rate_limit_exceeded`. All should classify."""
    assert classify_error(RuntimeError("HTTP 429: too many requests")) == "RateLimited"
    assert classify_error(RuntimeError("rate_limit_exceeded")) == "RateLimited"
    assert classify_error(RuntimeError("Too Many Requests")) == "RateLimited"


def test_rate_limit_pattern_does_not_overmatch():
    """Generic mentions of `limit` or `rate` shouldn't trigger. The
    patterns are intentionally specific to actual upstream prose so
    we don't reclassify quota / context-window errors as transient."""
    assert classify_error(RuntimeError("context window exceeded")) != "RateLimited"
    assert classify_error(RuntimeError("max tokens limit reached")) != "RateLimited"
    assert classify_error(RuntimeError("token rate calculation failed")) != "RateLimited"


# --- ACP-boundary classification (consumer convergence: dispatcher #1, deepread #9) ---


def test_read_timeout_classifies_as_timeout():
    """httpx.ReadTimeout was leaking through as the class name. The
    documented `Timeout` is what consumer retry policies key on."""
    assert classify_error(httpx.ReadTimeout("read timed out")) == "Timeout"


def test_remote_protocol_error_classifies_as_provider_unavailable():
    """Mid-call peer disconnect — server was reachable but the exchange
    didn't complete. Surface as transient so consumers retry."""
    assert classify_error(httpx.RemoteProtocolError("peer dropped")) == "ProviderUnavailable"


def test_pool_timeout_classifies_as_provider_unavailable():
    """No connection slot — capacity-side problem on our end or the
    server's. Either way the consumer should retry, not 422."""
    assert classify_error(httpx.PoolTimeout("no slot")) == "ProviderUnavailable"


def test_bad_request_in_tunneled_message_classifies_as_provider_error():
    """Sandbox-agent surfaces upstream HTTP errors as JSON-RPC `-32603
    Internal error: ...400 Bad Request...`. The original status survives
    in the prose; pattern-match it to ProviderError."""
    exc = RuntimeError(
        "Client error '400 Bad Request' for url '<sandbox>/v1/acp/abc'"
    )
    assert classify_error(exc) == "ProviderError"


def test_tunneled_502_classifies_as_provider_unavailable():
    assert classify_error(RuntimeError("HTTP 502 Bad Gateway")) == "ProviderUnavailable"


def test_tunneled_401_classifies_as_auth_error():
    assert classify_error(RuntimeError("HTTP 401 Unauthorized")) == "AuthError"


def test_acp_error_class_classifies_as_provider_error():
    """AcpError is a generic JSON-RPC failure (no surviving status text).
    Consumers shouldn't see the raw class name; map to ProviderError."""
    class AcpError(Exception):
        pass
    assert classify_error(AcpError("ACP error -32603: Internal error")) == "ProviderError"


def test_http_status_pattern_does_not_overmatch():
    """`400 lines of code` mentioned in a message shouldn't reclassify
    to ProviderError. We match canonical status strings (`400 Bad
    Request`, ` 400 `) — not bare digits in any context."""
    assert classify_error(RuntimeError("processed 400 lines")) == "RuntimeError"
    assert classify_error(RuntimeError("got 500 results back")) == "RuntimeError"


# --- scrub_error_text -------------------------------------------------------


def test_scrub_error_text_redacts_bare_bearer_token():
    """The most common leak path: an upstream 401 echoes back the
    Authorization header value. The token is the operator's OAuth bearer
    or API key — must not reach API consumers or persist in `runs.error_msg`."""
    msg = "Anthropic rejected the request: Bearer sk-ant-api03-AbCdEf1234567890XyZ"
    scrubbed = scrub_error_text(msg)
    assert "Bearer [redacted]" in scrubbed
    assert "sk-ant-api03" not in scrubbed


def test_scrub_error_text_redacts_authorization_header_form():
    """When the upstream echoes the full header line, keep the `Authorization:`
    prefix so operators reading logs can still see WHICH header leaked
    without seeing the value."""
    msg = "request failed: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
    scrubbed = scrub_error_text(msg)
    assert "Authorization: Bearer [redacted]" in scrubbed
    assert "eyJhbGciOiJIUzI1NiJ9" not in scrubbed


def test_scrub_error_text_redacts_url_credentials():
    """OAuth tokens and API keys sometimes appear in query strings inside
    upstream error envelopes (legacy auth methods, callback URLs)."""
    msg = "GET https://api.example.com/v1/things?api_key=secret123&filter=foo failed"
    scrubbed = scrub_error_text(msg)
    assert "api_key=[redacted]" in scrubbed
    assert "secret123" not in scrubbed
    assert "filter=foo" in scrubbed  # non-credential params preserved


def test_scrub_error_text_redacts_json_token_field():
    """Stringified dicts can carry credential fields by name. Match
    `'token': 'value'` / `"token": "value"` patterns and redact the value."""
    msg = "ACP error: {'token': 'sk-abc123', 'session_id': 'xyz'}"
    scrubbed = scrub_error_text(msg)
    assert "'token': '[redacted]'" in scrubbed
    assert "sk-abc123" not in scrubbed
    assert "session_id" in scrubbed  # only credential-named fields hit


def test_scrub_error_text_leaves_non_credential_content_alone():
    """Conservative: the scrubber must not mangle ordinary error prose."""
    msg = "Connection refused to api.anthropic.com:443 after 30s timeout"
    assert scrub_error_text(msg) == msg


def test_scrub_error_text_handles_empty_and_none_safe():
    """Defensive — `error_msg` is sometimes empty or never set."""
    assert scrub_error_text("") == ""
    assert scrub_error_text(None) is None


def test_scrub_error_text_does_not_match_short_words_after_bearer():
    """The `\\bBearer\\s+...{16,}` pattern requires at least 16 chars after
    `Bearer` to avoid false positives like 'Bearer of bad news' (where
    `of` is too short to look like a token)."""
    msg = "Server is bearer of bad news"
    assert scrub_error_text(msg) == msg


def test_scrub_error_text_redacts_basic_auth_url():
    """Upstream proxy / DSN errors sometimes echo full URLs with
    embedded basic-auth — `postgres://user:pass@host`, `https://api:key@…`.
    Keep the scheme + host for context; redact the userinfo block."""
    msg = "connection failed: https://admin:s3cret@api.example.com/v1/foo timed out"
    scrubbed = scrub_error_text(msg)
    assert "https://[redacted]:[redacted]@api.example.com" in scrubbed
    assert "admin" not in scrubbed
    assert "s3cret" not in scrubbed
    # Path + rest of message preserved
    assert "/v1/foo timed out" in scrubbed


def test_scrub_error_text_basic_auth_handles_http_and_https():
    """Same pattern, both schemes."""
    for scheme in ("http", "https"):
        msg = f"{scheme}://u:p@host/path failed"
        scrubbed = scrub_error_text(msg)
        assert f"{scheme}://[redacted]:[redacted]@host" in scrubbed


def test_scrub_error_text_redacts_non_http_dsn_schemes():
    """Database / cache driver errors echo non-http DSNs. The catch-all
    scrubber must redact userinfo regardless of scheme."""
    for dsn in (
        "postgresql://aitelier:hunter2@localhost:5433/aitelier",
        "postgres://u:p@db/app",
        "redis://default:s3cret@cache:6379/0",
    ):
        scrubbed = scrub_error_text(f"connect failed: {dsn}")
        assert "[redacted]:[redacted]@" in scrubbed
        assert "hunter2" not in scrubbed
        assert "s3cret" not in scrubbed


def test_scrub_error_text_basic_auth_leaves_non_credential_urls_alone():
    """URLs without userinfo (most URLs) must not be touched. The pattern
    only fires when there's a `user:pass@` block before the host."""
    msg = "GET https://api.example.com/v1/things failed (500)"
    assert scrub_error_text(msg) == msg


def test_classify_network_errors_as_provider_unavailable():
    """httpx mid-call NetworkError leaves (ReadError/WriteError) classify as
    ProviderUnavailable, matching the documented mapping."""
    import httpx
    from aitelier.errors import classify_error
    assert classify_error(httpx.ReadError("peer reset")) == "ProviderUnavailable"
    assert classify_error(httpx.WriteError("broken pipe")) == "ProviderUnavailable"
