"""Unit tests for startup recovery and ambiguous call reconciliation."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from dispatcher.clock import FakeClock
from dispatcher.config import LeadRecord
from dispatcher.reconcile import Reconciler
from dispatcher.store import StateStore

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture
def recovery_env(tmp_path, sample_config):
    db_file = os.path.join(tmp_path, "test_recovery.db")
    store = StateStore(db_file)
    clock = FakeClock(datetime(2026, 11, 12, 17, 30, 0, tzinfo=IST))
    provider = AsyncMock()
    reconciler = Reconciler(store, provider, clock, sample_config)
    return store, reconciler, provider, clock, sample_config


def test_recovery_call_never_placed_404(recovery_env):
    store, reconciler, provider, clock, config = recovery_env

    lead = LeadRecord("c_acme_renewals", "ren_crash1", "acme_insurance", "+919111111111", "Crash User", 1, 3)
    store.upsert_leads([lead])

    # Reserve call
    now_dt = clock.now()
    reservations = store.reserve_dials(now_dt, config)
    assert len(reservations) == 1
    req_id = reservations[0].request_id

    # Carrier returns None (404)
    provider.get_call_by_request_id.return_value = None

    # Run startup recovery
    asyncio.run(reconciler.run_startup_recovery())

    # Lead should be back to PENDING, reservation released
    unresolved = store.get_unresolved_calls(clock.now_epoch(), overdue_threshold_sim_seconds=0)
    assert len(unresolved) == 0

    # Next reserve should reuse the same request_id and attempt number 1
    second_res = store.reserve_dials(now_dt, config)
    assert len(second_res) == 1
    assert second_res[0].request_id == req_id
    assert second_res[0].attempt_no == 1


def test_recovery_call_placed_and_ended(recovery_env):
    store, reconciler, provider, clock, config = recovery_env

    lead = LeadRecord("c_acme_renewals", "ren_crash2", "acme_insurance", "+919222222222", "Placed User", 1, 3)
    store.upsert_leads([lead])

    # Reserve call
    now_dt = clock.now()
    reservations = store.reserve_dials(now_dt, config)
    req_id = reservations[0].request_id

    # Carrier returns call that completed while dispatcher was down
    provider.get_call_by_request_id.return_value = {
        "call_sid": "CA_RECOVERED",
        "request_id": req_id,
        "state": "ended",
        "result": {
            "event": "call.completed",
            "call_sid": "CA_RECOVERED",
            "request_id": req_id,
            "tenant_id": "acme_insurance",
            "campaign_id": "c_acme_renewals",
            "lead_id": "ren_crash2",
            "phone": "+919222222222",
            "status": "completed",
            "disposition": "interested",
            "started_at": now_dt.isoformat(),
            "ended_at": (now_dt + timedelta(seconds=60)).isoformat(),
            "duration_sec": 60,
        },
    }

    asyncio.run(reconciler.run_startup_recovery())

    # Call should be adopted and marked ended, lead COMPLETED
    states = {r["lead_id"]: r["final_state"] for r in store.get_final_states(config)}
    assert states["ren_crash2"] == "completed"
