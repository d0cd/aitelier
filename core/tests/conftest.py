"""Test fixtures shared across the whole core test suite.

Every test gets a fresh InMemoryStore so trace/run state doesn't leak
between cases. Tests that want to assert against store state can import
`from aitelier.storage import get_store` and await it as usual.
"""

from __future__ import annotations

import pytest
from aitelier.storage import InMemoryStore
from aitelier.storage._store import _set_store_for_tests


@pytest.fixture(autouse=True)
def _fresh_store():
    """Replace the module-level store with a clean InMemoryStore per test."""
    fresh = InMemoryStore()
    _set_store_for_tests(fresh)
    yield fresh
    # Reset back to None so the next test re-initializes.
    _set_store_for_tests(None)  # type: ignore[arg-type]
