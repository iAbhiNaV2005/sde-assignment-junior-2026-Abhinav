"""Unit tests for SQLite transactional invariants (tenant concurrency, phone locks, DND propagation)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from dispatcher.clock import FakeClock
from dispatcher.config import LeadRecord
from dispatcher.store import StateStore

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture
def temp_store(tmp_path) -> StateStore:
    db_file = os.path.join(tmp_path, "test_dispatcher.db")
    return StateStore(db_file)


def test_tenant_concurrency_shared_across_campaigns(temp_store: StateStore, sample_config):
    # Acme insurance has 2 campaigns: c_acme_renewals and c_acme_feedback. Limit is 4 total.
    leads = [
        LeadRecord("c_acme_renewals", f"ren_{i:04d}", "acme_insurance", f"+91980000000{i}", f"Name {i}", i, 3)
        for i in range(1, 6)
    ] + [
        LeadRecord("c_acme_feedback", f"fb_{i:04d}", "acme_insurance", f"+91990000000{i}", f"Name FB {i}", 10 + i, 3)
        for i in range(1, 6)
    ]
    temp_store.upsert_leads(leads)

    # Thursday 12 Nov 2026, 17:30 IST (both Acme windows are open)
    now_dt = datetime(2026, 11, 12, 17, 30, 0, tzinfo=IST)
    reservations = temp_store.reserve_dials(now_dt, sample_config)

    # Acme max_concurrent_calls is 4, so at most 4 reservations should be granted!
    acme_reservations = [r for r in reservations if r.tenant_id == "acme_insurance"]
    assert len(acme_reservations) == 4

    # Second pass immediately after: 0 additional reservations allowed because 4 are in DIALING state
    second_pass = temp_store.reserve_dials(now_dt, sample_config)
    assert len(second_pass) == 0


def test_one_call_per_phone_across_campaigns(temp_store: StateStore, sample_config):
    # Same phone in two different campaigns
    shared_phone = "+919999999999"
    leads = [
        LeadRecord("c_acme_renewals", "ren_shared", "acme_insurance", shared_phone, "User A", 1, 3),
        LeadRecord("c_acme_feedback", "fb_shared", "acme_insurance", shared_phone, "User A", 2, 3),
    ]
    temp_store.upsert_leads(leads)

    now_dt = datetime(2026, 11, 12, 17, 30, 0, tzinfo=IST)
    reservations = temp_store.reserve_dials(now_dt, sample_config)

    # Exactly 1 reservation for this phone
    assert len(reservations) == 1
    assert reservations[0].phone == shared_phone

    # The other lead remains PENDING and cannot be reserved while the first is DIALING/ACTIVE
    second_pass = temp_store.reserve_dials(now_dt, sample_config)
    assert len(second_pass) == 0


def test_dnd_propagation_across_campaigns(temp_store: StateStore, sample_config):
    # Two leads sharing the same phone
    shared_phone = "+918888888888"
    leads = [
        LeadRecord("c_acme_renewals", "ren_01", "acme_insurance", shared_phone, "DND User", 1, 3),
        LeadRecord("c_acme_feedback", "fb_01", "acme_insurance", shared_phone, "DND User", 2, 3),
    ]
    temp_store.upsert_leads(leads)

    # Dial ren_01
    now_dt = datetime(2026, 11, 12, 17, 30, 0, tzinfo=IST)
    reservations = temp_store.reserve_dials(now_dt, sample_config)
    assert len(reservations) == 1
    res = reservations[0]

    temp_store.record_accept(res.request_id, "CA12345", now_dt.timestamp())

    # ren_01 ends with disposition "dnd"
    payload = {
        "event": "call.completed",
        "call_sid": "CA12345",
        "request_id": res.request_id,
        "tenant_id": "acme_insurance",
        "campaign_id": "c_acme_renewals",
        "lead_id": "ren_01",
        "phone": shared_phone,
        "status": "completed",
        "disposition": "dnd",
        "started_at": now_dt.isoformat(),
        "ended_at": (now_dt + timedelta(seconds=120)).isoformat(),
        "duration_sec": 120,
    }
    applied = temp_store.apply_result(payload, sample_config)
    assert applied is True

    # Check that fb_01 was automatically marked DND_BLOCKED and cannot be reserved
    final_states = {row["lead_id"]: row["final_state"] for row in temp_store.get_final_states(sample_config)}
    assert final_states["ren_01"] == "completed"
    assert final_states["fb_01"] == "dnd_blocked"

    # Further reservations for this phone must be 0
    next_reservations = temp_store.reserve_dials(now_dt + timedelta(minutes=5), sample_config)
    assert len(next_reservations) == 0


def test_release_dialing_after_503(temp_store: StateStore, sample_config):
    lead = LeadRecord("c_acme_renewals", "ren_503", "acme_insurance", "+919777777777", "Transient User", 1, 3)
    temp_store.upsert_leads([lead])

    now_dt = datetime(2026, 11, 12, 17, 30, 0, tzinfo=IST)
    reservations = temp_store.reserve_dials(now_dt, sample_config)
    assert len(reservations) == 1
    assert reservations[0].request_id == "c_acme_renewals:ren_503:a1"

    # Simulate 503 response -> release reservation
    temp_store.release_dialing(reservations[0].request_id, backoff_real_seconds=0.0)

    # Next attempt must still be attempt 1, reusing the same request_id!
    reservations_again = temp_store.reserve_dials(now_dt, sample_config)
    assert len(reservations_again) == 1
    assert reservations_again[0].request_id == "c_acme_renewals:ren_503:a1"
    assert reservations_again[0].attempt_no == 1

