#!/usr/bin/env python3
"""Drift check for aitelier's agent-path price table (core/src/aitelier/pricing.py).

Agent LLM calls bypass LiteLLM, so aitelier estimates cost from a built-in
per-model rate table. Those rates go stale when Anthropic/OpenAI change prices.
This script diffs our table against LiteLLM's continuously-maintained price map
and exits non-zero on drift — wire it into CI (and run after a price change) so
a stale table is caught instead of silently undercounting.

  uv run --project core python scripts/check-model-prices.py

Exit codes: 0 = in sync, 1 = drift found, 2 = couldn't fetch the upstream map.
"""
import json
import sys
import urllib.request

from aitelier.pricing import PRICES_AS_OF, find_price_drift

# LiteLLM's canonical, community-maintained price map.
LITELLM_PRICES_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)


def main() -> int:
    try:
        with urllib.request.urlopen(LITELLM_PRICES_URL, timeout=20) as resp:
            litellm_map = json.load(resp)
    except Exception as exc:  # network / parse — can't compare, don't claim sync
        print(f"✗ could not fetch LiteLLM price map: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    drift = find_price_drift(litellm_map)
    if not drift:
        print(f"✓ price table in sync with LiteLLM (PRICES_AS_OF={PRICES_AS_OF})")
        return 0

    print(f"✗ price drift vs LiteLLM (PRICES_AS_OF={PRICES_AS_OF}) — update "
          f"core/src/aitelier/pricing.py:")
    for d in drift:
        print(f"    {d['model']}.{d['field']}: ours={d['ours']:.3e} "
              f"litellm={d['theirs']:.3e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
