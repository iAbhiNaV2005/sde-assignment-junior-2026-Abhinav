"""Unit tests for business policies, state transitions, and precedence."""
from __future__ import annotations

from dispatcher.config import RetryPolicyConfig
from dispatcher.policy import (
    KIND_CALLBACK,
    KIND_RETRY,
    STATE_COMPLETED,
    STATE_DND_BLOCKED,
    STATE_EXHAUSTED,
    STATE_PENDING,
    derive_final_state,
    determine_next_lead_state,
    get_retry_delay_seconds,
)


def test_retry_delay_policy():
    cfg = RetryPolicyConfig(no_answer_minutes=60, busy_minutes=60, failed_minutes=15)
    assert get_retry_delay_seconds("no_answer", cfg) == 3600.0
    assert get_retry_delay_seconds("busy", cfg) == 3600.0
    assert get_retry_delay_seconds("failed", cfg) == 900.0
    assert get_retry_delay_seconds("completed", cfg) == 0.0


def test_terminal_dispositions():
    for disp in ["interested", "not_interested", "wrong_number", "dnd"]:
        state, kind = determine_next_lead_state(
            accepted_attempts=1,
            max_attempts=3,
            status="completed",
            disposition=disp,
            phone_in_dnd=False,
        )
        assert state == STATE_COMPLETED
        assert kind is None


def test_max_attempts_exhausted():
    # Attempt 3 reached max (3) with retryable status
    state, kind = determine_next_lead_state(
        accepted_attempts=3,
        max_attempts=3,
        status="no_answer",
        disposition=None,
    )
    assert state == STATE_EXHAUSTED
    assert kind is None

    # Crucial edge case: Attempt 3 reached max (3) but customer asked for callback
    # Rule R9 & Mock fact 9: must end as EXHAUSTED (cannot dial 4th attempt)
    state, kind = determine_next_lead_state(
        accepted_attempts=3,
        max_attempts=3,
        status="completed",
        disposition="callback_requested",
    )
    assert state == STATE_EXHAUSTED
    assert kind is None


def test_callback_and_retry_transitions():
    # Attempt 1: callback requested
    state, kind = determine_next_lead_state(
        accepted_attempts=1,
        max_attempts=3,
        status="completed",
        disposition="callback_requested",
    )
    assert state == STATE_PENDING
    assert kind == KIND_CALLBACK

    # Attempt 1: busy
    state, kind = determine_next_lead_state(
        accepted_attempts=1,
        max_attempts=3,
        status="busy",
        disposition=None,
    )
    assert state == STATE_PENDING
    assert kind == KIND_RETRY


def test_dnd_blocked_transition():
    # Lead failed, but phone was added to DND by another campaign
    state, kind = determine_next_lead_state(
        accepted_attempts=1,
        max_attempts=3,
        status="no_answer",
        disposition=None,
        phone_in_dnd=True,
    )
    assert state == STATE_DND_BLOCKED
    assert kind is None


def test_r9_precedence():
    # 1. Terminal wins over everything
    assert derive_final_state(has_terminal=True, attempts=3, max_attempts=3, is_busy=False, phone_in_dnd=True) == "completed"

    # 2. Exhausted wins over DND
    assert derive_final_state(has_terminal=False, attempts=3, max_attempts=3, is_busy=False, phone_in_dnd=True) == "exhausted"

    # 3. DND blocked wins if not terminal and not exhausted
    assert derive_final_state(has_terminal=False, attempts=1, max_attempts=3, is_busy=False, phone_in_dnd=True) == "dnd_blocked"

    # 4. If still busy, incomplete
    assert derive_final_state(has_terminal=False, attempts=3, max_attempts=3, is_busy=True, phone_in_dnd=False) == "incomplete"

