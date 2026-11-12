"""Unit tests for the webhook receiver (HMAC verification, deduplication, races)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from dispatcher.config import LeadRecord
from dispatcher.store import StateStore
from dispatcher.webhook import create_webhook_app

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture
def webhook_client(tmp_path, sample_config):
    db_file = os.path.join(tmp_path, "test_webhook.db")
    store = StateStore(db_file)
    leads = [
        LeadRecord("c_acme_renewals", "ren_0001", "acme_insurance", "+919876543210", "Test User", 1, 3),
    ]
    store.upsert_leads(leads)
    app = create_webhook_app(store, sample_config)
    client = TestClient(app)
    return client, store, sample_config


def test_webhook_hmac_verification(webhook_client):
    client, store, config = webhook_client
    now = datetime(2026, 11, 12, 17, 30, 0, tzinfo=IST)

    payload = {
        "event": "call.completed",
        "call_sid": "CA1111",
        "request_id": "c_acme_renewals:ren_0001:a1",
        "tenant_id": "acme_insurance",
        "campaign_id": "c_acme_renewals",
        "lead_id": "ren_0001",
        "phone": "+919876543210",
        "status": "completed",
        "disposition": "interested",
        "started_at": now.isoformat(),
        "ended_at": (now + timedelta(seconds=60)).isoformat(),
        "duration_sec": 60,
    }
    raw_body = json.dumps(payload).encode("utf-8")

    # 1. Missing signature
    resp = client.post("/webhooks/telephony", content=raw_body)
    assert resp.status_code == 401

    # 2. Tampered signature
    resp = client.post("/webhooks/telephony", content=raw_body, headers={"X-AU-Signature": "bad_hex_sig"})
    assert resp.status_code == 401

    # 3. Valid HMAC-SHA256 signature
    valid_sig = hmac.new(config.webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    resp = client.post("/webhooks/telephony", content=raw_body, headers={"X-AU-Signature": valid_sig})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_webhook_duplicate_delivery_idempotency(webhook_client):
    client, store, config = webhook_client
    now = datetime(2026, 11, 12, 17, 30, 0, tzinfo=IST)

    payload = {
        "event": "call.completed",
        "call_sid": "CA2222",
        "request_id": "c_acme_renewals:ren_0001:a1",
        "tenant_id": "acme_insurance",
        "campaign_id": "c_acme_renewals",
        "lead_id": "ren_0001",
        "phone": "+919876543210",
        "status": "completed",
        "disposition": "interested",
        "started_at": now.isoformat(),
        "ended_at": (now + timedelta(seconds=90)).isoformat(),
        "duration_sec": 90,
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(config.webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {"X-AU-Signature": sig}

    # First delivery
    resp1 = client.post("/webhooks/telephony", content=raw_body, headers=headers)
    assert resp1.status_code == 200
    assert resp1.json()["recorded"] is True

    # Duplicate delivery (carrier 10% duplicate behavior)
    resp2 = client.post("/webhooks/telephony", content=raw_body, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["recorded"] is False  # Safe no-op

    # Final state check
    states = store.get_final_states(config)
    assert len(states) == 1
    assert states[0]["final_state"] == "completed"
    assert states[0]["attempts"] == 1

