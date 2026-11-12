"""Configuration loader and data models for campaigns, tenants, and leads."""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class WindowConfig:
    start: str  # "HH:MM"
    end: str    # "HH:MM"
    days: tuple[str, ...] | None = None  # None means every day


@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    max_concurrent_calls: int


@dataclass(frozen=True)
class CampaignConfig:
    campaign_id: str
    tenant_id: str
    leads_file: str
    window: WindowConfig
    max_attempts: int


@dataclass(frozen=True)
class RetryPolicyConfig:
    no_answer_minutes: int
    busy_minutes: int
    failed_minutes: int


@dataclass(frozen=True)
class AppConfig:
    sim_start: datetime
    timezone_name: str
    regulatory_window: WindowConfig
    window_grace_seconds: int
    retry_policy: RetryPolicyConfig
    webhook_secret: str
    tenants: dict[str, TenantConfig]
    campaigns: dict[str, CampaignConfig]


@dataclass(frozen=True)
class LeadRecord:
    campaign_id: str
    lead_id: str
    tenant_id: str
    phone: str
    customer_name: str
    seq: int
    max_attempts: int


def load_config(fixtures_path: str) -> tuple[AppConfig, list[LeadRecord]]:
    """Loads campaigns.json and all associated lead CSV fixtures."""
    fixtures_path = os.path.abspath(fixtures_path)
    fixtures_dir = os.path.dirname(fixtures_path)

    with open(fixtures_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    reg_win = WindowConfig(
        start=raw["regulatory_window"]["start"],
        end=raw["regulatory_window"]["end"],
        days=None,
    )

    retry_pol = RetryPolicyConfig(
        no_answer_minutes=int(raw["retry_policy"]["no_answer_minutes"]),
        busy_minutes=int(raw["retry_policy"]["busy_minutes"]),
        failed_minutes=int(raw["retry_policy"]["failed_minutes"]),
    )

    tenants: dict[str, TenantConfig] = {}
    for t in raw["tenants"]:
        tenants[t["tenant_id"]] = TenantConfig(
            tenant_id=t["tenant_id"],
            max_concurrent_calls=int(t["max_concurrent_calls"]),
        )

    campaigns: dict[str, CampaignConfig] = {}
    for c in raw["campaigns"]:
        w = c["window"]
        win = WindowConfig(
            start=w["start"],
            end=w["end"],
            days=tuple(w["days"]),
        )
        campaigns[c["campaign_id"]] = CampaignConfig(
            campaign_id=c["campaign_id"],
            tenant_id=c["tenant_id"],
            leads_file=c["leads_file"],
            window=win,
            max_attempts=int(c["max_attempts"]),
        )

    app_cfg = AppConfig(
        sim_start=datetime.fromisoformat(raw["sim_start"]),
        timezone_name=raw.get("timezone", "Asia/Kolkata"),
        regulatory_window=reg_win,
        window_grace_seconds=int(raw.get("window_grace_seconds", 120)),
        retry_policy=retry_pol,
        webhook_secret=raw["webhook_secret"],
        tenants=tenants,
        campaigns=campaigns,
    )

    leads: list[LeadRecord] = []
    global_seq = 0
    for camp in campaigns.values():
        csv_path = os.path.join(fixtures_dir, camp.leads_file)
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                global_seq += 1
                leads.append(
                    LeadRecord(
                        campaign_id=camp.campaign_id,
                        lead_id=row["lead_id"],
                        tenant_id=camp.tenant_id,
                        phone=row["phone"],
                        customer_name=row.get("customer_name", ""),
                        seq=global_seq,
                        max_attempts=camp.max_attempts,
                    )
                )

    return app_cfg, leads

