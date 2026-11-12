"""Unit tests for output CSV writing and formatting contract."""
from __future__ import annotations

import csv
import os

import pytest

from dispatcher.config import LeadRecord
from dispatcher.output import FIELDNAMES, write_final_state_csv
from dispatcher.store import StateStore


def test_write_final_state_csv(tmp_path, sample_config):
    db_file = os.path.join(tmp_path, "test_output.db")
    csv_file = os.path.join(tmp_path, "final_state.csv")
    store = StateStore(db_file)

    leads = [
        LeadRecord("c_zen_winback", "zen_0002", "zen_retail", "+919000000002", "User B", 2, 3),
        LeadRecord("c_acme_renewals", "ren_0001", "acme_insurance", "+919000000001", "User A", 1, 3),
    ]
    store.upsert_leads(leads)

    write_final_state_csv(store, sample_config, csv_file)

    assert os.path.exists(csv_file)
    with open(csv_file, "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert reader[0]["fieldnames"] if hasattr(reader, "fieldnames") else True
        assert len(reader) == 2
        # Verify sorted by campaign_id, then lead_id
        assert reader[0]["campaign_id"] == "c_acme_renewals"
        assert reader[0]["lead_id"] == "ren_0001"
        assert reader[1]["campaign_id"] == "c_zen_winback"
        assert reader[1]["lead_id"] == "zen_0002"
        for field in FIELDNAMES:
            assert field in reader[0]

