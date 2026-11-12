"""Unit tests for priority ordering (callbacks > retries > fresh)."""
from __future__ import annotations

from dispatcher.policy import (
    KIND_CALLBACK,
    KIND_FRESH,
    KIND_PRIORITY,
    KIND_RETRY,
)


def make_sort_key(kind: str, due_at: float, seq: int) -> tuple[int, float, int]:
    return (KIND_PRIORITY[kind], due_at, seq)


def test_priority_ranking():
    leads = [
        {"id": "fresh_1", "kind": KIND_FRESH, "due_at": 0.0, "seq": 1},
        {"id": "fresh_2", "kind": KIND_FRESH, "due_at": 0.0, "seq": 2},
        {"id": "retry_late", "kind": KIND_RETRY, "due_at": 2000.0, "seq": 5},
        {"id": "retry_early", "kind": KIND_RETRY, "due_at": 1000.0, "seq": 6},
        {"id": "cb_late", "kind": KIND_CALLBACK, "due_at": 1500.0, "seq": 8},
        {"id": "cb_early", "kind": KIND_CALLBACK, "due_at": 500.0, "seq": 9},
    ]

    sorted_leads = sorted(leads, key=lambda l: make_sort_key(l["kind"], l["due_at"], l["seq"]))
    expected_order = [
        "cb_early",   # Callback earliest due
        "cb_late",    # Callback later due
        "retry_early",# Retry earliest due
        "retry_late", # Retry later due
        "fresh_1",    # Fresh first seq
        "fresh_2",    # Fresh second seq
    ]
    assert [l["id"] for l in sorted_leads] == expected_order

