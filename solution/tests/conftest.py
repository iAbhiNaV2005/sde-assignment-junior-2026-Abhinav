"""Shared pytest fixtures."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from dispatcher.clock import FakeClock
from dispatcher.config import (
    AppConfig,
    CampaignConfig,
    RetryPolicyConfig,
    TenantConfig,
    WindowConfig,
    load_config,
)

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture
def fake_clock() -> FakeClock:
    # Thursday 12 Nov 2026, 17:30 IST (mock start time)
    start_time = datetime(2026, 11, 12, 17, 30, 0, tzinfo=IST)
    return FakeClock(start_time, speed=600.0)


@pytest.fixture
def sample_config() -> AppConfig:
    reg_win = WindowConfig(start="09:00", end="21:00", days=None)
    retry_pol = RetryPolicyConfig(no_answer_minutes=60, busy_minutes=60, failed_minutes=15)
    tenants = {
        "acme_insurance": TenantConfig("acme_insurance", max_concurrent_calls=4),
        "zen_retail": TenantConfig("zen_retail", max_concurrent_calls=3),
    }
    campaigns = {
        "c_acme_renewals": CampaignConfig(
            campaign_id="c_acme_renewals",
            tenant_id="acme_insurance",
            leads_file="leads_c_acme_renewals.csv",
            window=WindowConfig("10:00", "19:00", ("mon", "tue", "wed", "thu", "fri", "sat")),
            max_attempts=3,
        ),
        "c_acme_feedback": CampaignConfig(
            campaign_id="c_acme_feedback",
            tenant_id="acme_insurance",
            leads_file="leads_c_acme_feedback.csv",
            window=WindowConfig("11:00", "20:30", ("mon", "tue", "wed", "thu", "fri", "sat", "sun")),
            max_attempts=3,
        ),
        "c_zen_winback": CampaignConfig(
            campaign_id="c_zen_winback",
            tenant_id="zen_retail",
            leads_file="leads_c_zen_winback.csv",
            window=WindowConfig("09:30", "18:00", ("mon", "tue", "wed", "thu", "fri")),
            max_attempts=3,
        ),
    }
    return AppConfig(
        sim_start=datetime(2026, 11, 12, 17, 30, 0, tzinfo=IST),
        timezone_name="Asia/Kolkata",
        regulatory_window=reg_win,
        window_grace_seconds=120,
        retry_policy=retry_pol,
        webhook_secret="whsec_au_assignment_local",
        tenants=tenants,
        campaigns=campaigns,
    )


@pytest.fixture
def real_fixtures() -> tuple[AppConfig, list]:
    fixtures_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "assignment-b-workflow", "fixtures", "campaigns.json"
    )
    return load_config(fixtures_path)

