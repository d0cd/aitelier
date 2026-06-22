"""Phase 4: agent-path cost estimation from token counts × a rate table.

The LLM path gets cost from LiteLLM's response header; agent LLM calls bypass
LiteLLM (they run inside SA), so cost is estimated here. Cache-aware and
fail-safe (unknown model → None, never a guessed number)."""
import pytest
from aitelier.pricing import compute_cost, find_price_drift


def test_compute_cost_cache_aware():
    """Cost must price fresh input, output, cache-read and cache-write each at
    their own rate — cache-read dominates warm runs and is ~10% of input, so
    ignoring it (or pricing it as input) massively misprices."""
    usage = {
        "input_tokens": 5, "output_tokens": 200, "total_tokens": 48266,
        "cached_read_tokens": 47807, "cached_write_tokens": 254,
    }
    cost = compute_cost("claude-sonnet-4-5", usage)
    # input 5*3e-6 + output 200*15e-6 + cache_read 47807*0.3e-6 + write 254*3.75e-6
    expected = 5 * 3e-6 + 200 * 15e-6 + 47807 * 0.3e-6 + 254 * 3.75e-6
    assert cost == pytest.approx(expected, rel=1e-9)


def test_compute_cost_unknown_model_is_none():
    """Fail-safe: a model not in the table returns None (cost stays null,
    honestly), never a fabricated number."""
    assert compute_cost("some-model-we-dont-price", {"input_tokens": 100}) is None


def test_compute_cost_none_inputs():
    assert compute_cost(None, {"input_tokens": 1}) is None
    assert compute_cost("claude-sonnet-4-5", None) is None


def test_compute_cost_normalizes_model_id():
    """Inner model ids arrive with provider prefixes / date suffixes; the
    table is keyed by base name and must still resolve them."""
    usage = {"input_tokens": 1000, "output_tokens": 0}
    base = compute_cost("claude-sonnet-4-5", usage)
    assert base is not None
    assert compute_cost("anthropic/claude-sonnet-4-5", usage) == base
    assert compute_cost("claude-sonnet-4-5-20250929", usage) == base


def test_compute_cost_missing_cache_fields_treated_zero():
    """A result without cache fields (None) prices them as 0, not an error."""
    cost = compute_cost("claude-sonnet-4-5",
                        {"input_tokens": 1000, "output_tokens": 500,
                         "cached_read_tokens": None, "cached_write_tokens": None})
    assert cost == pytest.approx(1000 * 3e-6 + 500 * 15e-6, rel=1e-9)


def test_find_price_drift_clean_when_rates_match():
    """A LiteLLM map matching our table → no drift reported."""
    litellm = {"claude-sonnet-4-5": {
        "input_cost_per_token": 3e-6, "output_cost_per_token": 15e-6,
        "cache_read_input_token_cost": 0.3e-6,
        "cache_creation_input_token_cost": 3.75e-6,
    }}
    assert find_price_drift(litellm) == []


def test_find_price_drift_detects_changed_rate():
    """When upstream input price moves, drift is reported for that field so CI
    catches a stale table instead of silently undercounting."""
    litellm = {"claude-sonnet-4-5": {
        "input_cost_per_token": 4e-6,  # upstream changed 3 → 4
        "output_cost_per_token": 15e-6,
        "cache_read_input_token_cost": 0.3e-6,
        "cache_creation_input_token_cost": 3.75e-6,
    }}
    drift = find_price_drift(litellm)
    assert any(d["model"] == "claude-sonnet-4-5" and d["field"] == "input"
               for d in drift)


def test_find_price_drift_ignores_models_not_in_map():
    """Models LiteLLM doesn't list are skipped (can't compare) — not drift."""
    assert find_price_drift({}) == []
