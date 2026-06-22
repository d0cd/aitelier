"""Unit tests for the OpenAI <-> aitelier translation layer.

Covers the routing + usage/tool/JSON helpers directly — they underlie every
inference response but were previously exercised only indirectly via
test_server / test_sandbox_agent.
"""

from __future__ import annotations

import pytest
from aitelier.openai_compat import (
    _normalize_usage_cache_details,
    agent_usage_to_openai,
    parse_model_route,
    summarize_tool_calls,
    try_parse_json,
)

# --- parse_model_route -----------------------------------------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-sonnet-4-6", ("llm", None, None)),
        ("anthropic/claude-opus-4-7", ("llm", None, None)),
        ("agent:claude/claude-sonnet-4-5", ("agent", "claude", "claude-sonnet-4-5")),
        # Inner LLM may itself contain a slash (provider/model).
        ("agent:claude/anthropic/claude-sonnet-4-5",
         ("agent", "claude", "anthropic/claude-sonnet-4-5")),
    ],
)
def test_parse_model_route(model, expected):
    assert parse_model_route(model) == expected


@pytest.mark.parametrize("model", ["agent:", "agent:/claude-sonnet-4-5"])
def test_parse_model_route_rejects_empty_backend(model):
    """`agent:` with no backend is a client error — raise so the endpoint
    returns a 400 instead of letting an empty backend reach Sandbox Agent."""
    with pytest.raises(ValueError, match="backend"):
        parse_model_route(model)


@pytest.mark.parametrize("model", ["agent:claude", "agent:codex", "agent:claude/"])
def test_parse_model_route_requires_inner_model(model):
    """A bare `agent:<backend>` (no inner model) is rejected: the inner model
    must be named so the run's exact model — and therefore its cost — is known
    rather than left to the backend's silent default."""
    with pytest.raises(ValueError, match="inner model"):
        parse_model_route(model)


# --- agent_usage_to_openai -------------------------------------------------


def test_agent_usage_clamps_negative_overhead():
    """When upstream reports total < prompt+completion, trust the components
    and recompute — never emit a negative aitelier_inner_tokens."""
    out = agent_usage_to_openai(
        {"input_tokens": 10, "output_tokens": 10, "total_tokens": 5}
    )
    assert out["total_tokens"] == 20
    assert "aitelier_inner_tokens" not in out


def test_agent_usage_exposes_inner_overhead():
    out = agent_usage_to_openai(
        {"input_tokens": 10, "output_tokens": 10, "total_tokens": 50}
    )
    assert out["total_tokens"] == 20
    assert out["aitelier_inner_tokens"] == 30


def test_agent_usage_surfaces_cache_details():
    out = agent_usage_to_openai({
        "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
        "cached_tokens": 3, "cache_creation_input_tokens": 2,
    })
    assert out["prompt_tokens_details"] == {
        "cached_tokens": 3, "cache_creation_tokens": 2,
    }


def test_agent_usage_surfaces_native_cache_keys():
    """The agent path normalizes cache counts to cached_read_tokens /
    cached_write_tokens; the OpenAI usage must surface those too, not just the
    LiteLLM-flavored keys — otherwise a client reading prompt_tokens_details
    sees nothing for the agent path despite the data being captured."""
    out = agent_usage_to_openai({
        "input_tokens": 5, "output_tokens": 200, "total_tokens": 48266,
        "cached_read_tokens": 47807, "cached_write_tokens": 254,
    })
    assert out["prompt_tokens_details"] == {
        "cached_tokens": 47807, "cache_creation_tokens": 254,
    }


def test_agent_usage_none_passthrough():
    assert agent_usage_to_openai(None) is None
    assert agent_usage_to_openai({}) is None


# --- summarize_tool_calls --------------------------------------------------


def test_summarize_tool_calls_skips_garbage_and_uses_name_fallback():
    """Non-dict entries are skipped; `name` is the fallback key for `tool`.
    Count reflects the raw list length, not just the named entries."""
    result = {"tool_calls": [
        {"tool": "fs.read"},
        {"name": "fs.write"},
        "garbage",
        {"no_name_here": 1},
        42,
    ]}
    names, count = summarize_tool_calls(result)
    assert names == ["fs.read", "fs.write"]
    assert count == 5


def test_summarize_tool_calls_empty():
    assert summarize_tool_calls({}) == ([], 0)


# --- try_parse_json --------------------------------------------------------


def test_try_parse_json_fenced_object():
    assert try_parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_try_parse_json_prose_prefixed_object():
    assert try_parse_json('Here is the JSON: {"a": 1}') == {"a": 1}


def test_try_parse_json_top_level_array():
    assert try_parse_json("[1, 2, 3]") == [1, 2, 3]


def test_try_parse_json_plain_object():
    assert try_parse_json('{"a": 1}') == {"a": 1}


def test_try_parse_json_unterminated_fence_still_recovers():
    assert try_parse_json('```json\n{"a": 1}') == {"a": 1}


def test_try_parse_json_garbage_returns_none():
    assert try_parse_json("just some prose, no json at all") is None
    assert try_parse_json("") is None


# --- _normalize_usage_cache_details ---------------------------------------


def test_normalize_cache_details_lifts_anthropic_fields():
    usage = {
        "prompt_tokens": 10,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 2,
    }
    _normalize_usage_cache_details(usage)
    assert usage["prompt_tokens_details"] == {
        "cached_tokens": 3, "cache_creation_tokens": 2,
    }


def test_normalize_cache_details_noop_without_cache_fields():
    usage = {"prompt_tokens": 10}
    _normalize_usage_cache_details(usage)
    assert "prompt_tokens_details" not in usage


def test_normalize_cache_details_preserves_existing_values():
    """Idempotency guard: don't clobber a cached_tokens already present in
    prompt_tokens_details."""
    usage = {
        "cache_read_input_tokens": 3,
        "prompt_tokens_details": {"cached_tokens": 99},
    }
    _normalize_usage_cache_details(usage)
    assert usage["prompt_tokens_details"]["cached_tokens"] == 99
