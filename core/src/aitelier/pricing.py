"""Agent-path cost estimation.

The LLM path (LiteLLM) gets exact cost from the `x-litellm-response-cost`
response header. Agent LLM calls bypass LiteLLM — they run inside the Sandbox
Agent process and go straight to Anthropic/OpenAI — so there is no header to
read. We estimate cost from the token counts the backend reports × a per-model
rate table.

Robustness:
  - **Cache-aware.** Prompt-cache read and write tokens are priced separately
    from fresh input (cache-read ≈ 1/10th of input, cache-write ≈ 1.25× input)
    and dominate warm runs — pricing them as input, or ignoring them, misprices
    by an order of magnitude. The cached_* token columns (migration 008) are
    what make this possible.
  - **Fail-safe.** An unknown model or absent usage returns None — cost_usd
    stays null, honestly. We never emit a guessed number.
  - **Drift-resisted.** Rates mirror LiteLLM's maintained price map; the
    `scripts/check-model-prices.py` drift check (and its test) diff this table
    against LiteLLM's live `/model/info` and fail when upstream prices move, so
    a stale table is caught in CI rather than silently undercounting.

`PRICES_AS_OF` stamps the vintage so a stored cost is auditable.
"""
from __future__ import annotations

# Validated against LiteLLM's model_prices map on this date — bump when the
# drift check flags a change (see scripts/check-model-prices.py).
PRICES_AS_OF = "2026-06-10"

# Per-token USD rates, keyed by the lowercased base model id. Date-suffixed and
# provider-prefixed ids (anthropic/…, …-20250929) resolve via _resolve().
_PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-4-5": {
        "input": 3e-6, "output": 15e-6,
        "cache_read": 0.3e-6, "cache_write": 3.75e-6,
    },
    "claude-haiku-4-5": {
        "input": 1e-6, "output": 5e-6,
        "cache_read": 0.1e-6, "cache_write": 1.25e-6,
    },
    "claude-opus-4": {
        "input": 15e-6, "output": 75e-6,
        "cache_read": 1.5e-6, "cache_write": 18.75e-6,
    },
}


def _resolve(model: str) -> dict[str, float] | None:
    """Map a possibly-decorated model id to its rate row. Strips a provider
    prefix (`anthropic/…`) and matches the longest table key that prefixes the
    id, so date-suffixed ids (`…-4-5-20250929`) and family ids both resolve."""
    m = model.split("/")[-1].strip().lower()
    if m in _PRICES:
        return _PRICES[m]
    for key in sorted(_PRICES, key=len, reverse=True):
        if m.startswith(key):
            return _PRICES[key]
    return None


def compute_cost(model: str | None, usage: dict | None) -> float | None:
    """Estimate USD cost for one agent call, or None when it can't be priced.

    None when the model is unknown/missing or no usage was reported — never a
    guessed number. Missing cache fields price as 0 (a cold run), not an error.
    """
    if not model or not usage:
        return None
    rates = _resolve(model)
    if rates is None:
        return None
    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    cache_read = usage.get("cached_read_tokens") or 0
    cache_write = usage.get("cached_write_tokens") or 0
    return (
        inp * rates["input"]
        + out * rates["output"]
        + cache_read * rates["cache_read"]
        + cache_write * rates["cache_write"]
    )


# Our rate keys → LiteLLM's model_cost field names, for drift checking against
# LiteLLM's continuously-maintained price map.
_LITELLM_FIELDS = {
    "input": "input_cost_per_token",
    "output": "output_cost_per_token",
    "cache_read": "cache_read_input_token_cost",
    "cache_write": "cache_creation_input_token_cost",
}


def find_price_drift(
    litellm_map: dict[str, dict], *, rel_tol: float = 0.02
) -> list[dict]:
    """Diff our rate table against LiteLLM's `model_cost` map.

    Returns one entry per (model, field) whose rate diverges beyond `rel_tol`,
    for models present in both tables — i.e. where upstream prices moved and
    our table is stale. Models LiteLLM doesn't list are skipped (nothing to
    compare against), not treated as drift. `scripts/check-model-prices.py`
    fetches the live map and fails CI when this returns anything."""
    drift: list[dict] = []
    for model, rates in _PRICES.items():
        entry = litellm_map.get(model)
        if not entry:
            continue
        for key, field in _LITELLM_FIELDS.items():
            theirs = entry.get(field)
            ours = rates[key]
            if theirs is None:
                continue
            if ours == 0 and theirs == 0:
                continue
            if abs(ours - theirs) > rel_tol * max(abs(theirs), 1e-12):
                drift.append({"model": model, "field": key,
                              "ours": ours, "theirs": theirs})
    return drift
