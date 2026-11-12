"""Pure business policies: state transitions, retry intervals, and priority rules."""
from __future__ import annotations

from typing import Optional

from .config import RetryPolicyConfig

# Priority classes: lower number means higher scheduling priority
PRIORITY_CALLBACK = 0
PRIORITY_RETRY = 1
PRIORITY_FRESH = 2

KIND_CALLBACK = "CALLBACK"
KIND_RETRY = "RETRY"
KIND_FRESH = "FRESH"

KIND_PRIORITY = {
    KIND_CALLBACK: PRIORITY_CALLBACK,
    KIND_RETRY: PRIORITY_RETRY,
    KIND_FRESH: PRIORITY_FRESH,
}

STATE_PENDING = "PENDING"
STATE_DIALING = "DIALING"
STATE_ACTIVE = "ACTIVE"
STATE_COMPLETED = "COMPLETED"
STATE_EXHAUSTED = "EXHAUSTED"
STATE_DND_BLOCKED = "DND_BLOCKED"

TERMINAL_STATES = {STATE_COMPLETED, STATE_EXHAUSTED, STATE_DND_BLOCKED}
TERMINAL_DISPOSITIONS = frozenset({"interested", "not_interested", "wrong_number", "dnd"})


def get_retry_delay_seconds(status: str, retry_policy: RetryPolicyConfig) -> float:
    """Return delay in simulated seconds for retryable call statuses (R5)."""
    if status == "no_answer":
        return float(retry_policy.no_answer_minutes * 60)
    elif status == "busy":
        return float(retry_policy.busy_minutes * 60)
    elif status == "failed":
        return float(retry_policy.failed_minutes * 60)
    return 0.0


def determine_next_lead_state(
    accepted_attempts: int,
    max_attempts: int,
    status: Optional[str],
    disposition: Optional[str],
    phone_in_dnd: bool = False,
) -> tuple[str, Optional[str]]:
    """Determine (state, kind) for a lead after an ended call (R4, R5, R6, R7, R8, R9).

    Precedence order:
      1. Terminal disposition (interested, not_interested, wrong_number, dnd) -> COMPLETED
      2. Reached or exceeded max_attempts -> EXHAUSTED (even if callback was requested on attempt 3)
      3. Phone is in DND list -> DND_BLOCKED
      4. Callback requested -> PENDING (KIND_CALLBACK)
      5. Retryable failure (no_answer, busy, failed) -> PENDING (KIND_RETRY)
    """
    if disposition in TERMINAL_DISPOSITIONS:
        return (STATE_COMPLETED, None)

    if accepted_attempts >= max_attempts:
        return (STATE_EXHAUSTED, None)

    if phone_in_dnd:
        return (STATE_DND_BLOCKED, None)

    if disposition == "callback_requested":
        return (STATE_PENDING, KIND_CALLBACK)

    if status in ("no_answer", "busy", "failed"):
        return (STATE_PENDING, KIND_RETRY)

    # Defensive fallback
    return (STATE_EXHAUSTED, None)


def derive_final_state(
    has_terminal: bool,
    attempts: int,
    max_attempts: int,
    is_busy: bool,
    phone_in_dnd: bool,
) -> str:
    """Evaluate final state matching the mock carrier's /admin/leads logic (R9 precedence)."""
    if has_terminal:
        return "completed"
    if attempts >= max_attempts and not is_busy:
        return "exhausted"
    if phone_in_dnd and not is_busy:
        return "dnd_blocked"
    return "incomplete"


def mask_phone(phone: str) -> str:
    """Mask PII in phone numbers for safe logging."""
    if len(phone) > 6:
        return f"{phone[:4]}****{phone[-4:]}"
    return "****"

