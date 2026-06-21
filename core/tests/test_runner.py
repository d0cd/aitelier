"""Tests for the run-id helper."""

from __future__ import annotations

import re

from aitelier.runner import make_run_id

_W3C_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


def test_make_run_id_is_w3c_trace_id():
    """run_id IS the trace_id — a 128-bit lowercase-hex value, valid as a
    W3C/OpenTelemetry trace id, so a run is directly addressable in any OTLP
    backend. No timestamp/task payload baked into the id (that's in columns)."""
    rid = make_run_id()
    assert _W3C_TRACE_ID.match(rid), rid


def test_make_run_id_unique():
    ids = {make_run_id() for _ in range(1000)}
    assert len(ids) == 1000
