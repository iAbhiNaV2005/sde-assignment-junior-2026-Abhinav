"""SQLite-backed transactional store with atomic reservations and lead state persistence."""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .config import AppConfig, LeadRecord
from .policy import (
    KIND_CALLBACK,
    KIND_FRESH,
    KIND_RETRY,
    STATE_ACTIVE,
    STATE_COMPLETED,
    STATE_DIALING,
    STATE_DND_BLOCKED,
    STATE_EXHAUSTED,
    STATE_PENDING,
    TERMINAL_STATES,
    derive_final_state,
    determine_next_lead_state,
    get_retry_delay_seconds,
)
from .windows import is_window_open, next_dialable_time

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class DialReservation:
    request_id: str
    campaign_id: str
    lead_id: str
    tenant_id: str
    phone: str
    attempt_no: int


class StateStore:
    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = FULL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                campaign_id TEXT NOT NULL,
                lead_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                phone TEXT NOT NULL,
                customer_name TEXT,
                seq INTEGER NOT NULL,
                max_attempts INTEGER NOT NULL,
                state TEXT NOT NULL,
                kind TEXT,
                due_at REAL DEFAULT 0.0,
                not_before_real REAL DEFAULT 0.0,
                PRIMARY KEY (campaign_id, lead_id)
            );
            """)
            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_leads_schedule 
            ON leads(state, due_at, not_before_real);
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS calls (
                request_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                lead_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                phone TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                state TEXT NOT NULL,
                accepted INTEGER DEFAULT 0,
                call_sid TEXT,
                reserved_real REAL NOT NULL,
                accepted_sim REAL,
                status TEXT,
                disposition TEXT,
                callback_at REAL,
                started_at REAL,
                ended_at REAL,
                duration_sec INTEGER,
                UNIQUE (campaign_id, lead_id, attempt_no)
            );
            """)
            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_calls_state ON calls(state);
            """)
            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_calls_lead ON calls(campaign_id, lead_id);
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS dnd_phones (
                phone TEXT PRIMARY KEY,
                source_request_id TEXT NOT NULL,
                at REAL NOT NULL
            );
            """)

    def check_fingerprint_or_reset(self, provider_real_epoch: float, force_reset: bool = False) -> bool:
        """Wipes DB if provider_real_epoch differs from stored value (mock restarted). Returns True if wiped."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'provider_real_epoch'").fetchone()
            stored_epoch = float(row["value"]) if row else None

            if force_reset or (stored_epoch is not None and abs(stored_epoch - provider_real_epoch) > 0.001):
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM calls")
                conn.execute("DELETE FROM leads")
                conn.execute("DELETE FROM dnd_phones")
                conn.execute("DELETE FROM meta")
                conn.execute("INSERT INTO meta (key, value) VALUES ('provider_real_epoch', ?)", (str(provider_real_epoch),))
                conn.execute("COMMIT")
                return True

            if stored_epoch is None:
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('provider_real_epoch', ?)", (str(provider_real_epoch),))
            return False

    def upsert_leads(self, leads: list[LeadRecord]) -> None:
        """Insert leads if not already present. Preserves existing state on restart."""
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for lead in leads:
                conn.execute("""
                INSERT INTO leads (
                    campaign_id, lead_id, tenant_id, phone, customer_name,
                    seq, max_attempts, state, kind, due_at, not_before_real
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, 0.0)
                ON CONFLICT(campaign_id, lead_id) DO NOTHING;
                """, (
                    lead.campaign_id,
                    lead.lead_id,
                    lead.tenant_id,
                    lead.phone,
                    lead.customer_name,
                    lead.seq,
                    lead.max_attempts,
                    STATE_PENDING,
                    KIND_FRESH,
                ))
            conn.execute("COMMIT")

    def reserve_dials(self, sim_now: datetime, config: AppConfig, margin_seconds: float = 60.0) -> list[DialReservation]:
        """Atomically inspects candidates, verifies all business constraints (R1-R4, R8),
        and commits write-ahead DIALING reservations to SQLite.
        """
        reservations: list[DialReservation] = []
        real_now = time.time()
        sim_epoch = sim_now.timestamp()

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")

            # 1. In-flight tenant concurrency
            active_tenants: dict[str, int] = {t: 0 for t in config.tenants}
            for row in conn.execute("SELECT tenant_id, COUNT(*) as cnt FROM calls WHERE state IN ('DIALING', 'UNKNOWN', 'ACTIVE') GROUP BY tenant_id"):
                active_tenants[row["tenant_id"]] = row["cnt"]

            # 2. In-flight active phones
            busy_phones: set[str] = {
                row["phone"]
                for row in conn.execute("SELECT DISTINCT phone FROM calls WHERE state IN ('DIALING', 'UNKNOWN', 'ACTIVE')")
            }

            # 3. Known DND phones
            dnd_phones: set[str] = {
                row["phone"]
                for row in conn.execute("SELECT phone FROM dnd_phones")
            }

            # 4. Fetch eligible PENDING candidates ordered by priority (Callbacks -> Retries -> Fresh)
            candidates = conn.execute("""
                SELECT campaign_id, lead_id, tenant_id, phone, seq, max_attempts, kind, due_at
                FROM leads
                WHERE state = 'PENDING'
                  AND due_at <= ?
                  AND not_before_real <= ?
                ORDER BY
                  CASE kind
                    WHEN 'CALLBACK' THEN 0
                    WHEN 'RETRY' THEN 1
                    ELSE 2
                  END ASC,
                  due_at ASC,
                  seq ASC
            """, (sim_epoch, real_now)).fetchall()

            for cand in candidates:
                cid = cand["campaign_id"]
                lid = cand["lead_id"]
                tid = cand["tenant_id"]
                phone = cand["phone"]
                camp = config.campaigns.get(cid)
                tenant = config.tenants.get(tid)

                if not camp or not tenant:
                    continue

                # R1: Calling window check with safety margin
                if not is_window_open(sim_now, camp.window, config.regulatory_window, margin_seconds=margin_seconds):
                    continue

                # R8: DND check
                if phone in dnd_phones:
                    conn.execute("UPDATE leads SET state = ? WHERE campaign_id = ? AND lead_id = ?", (STATE_DND_BLOCKED, cid, lid))
                    continue

                # R3: One call per phone
                if phone in busy_phones:
                    continue

                # R2: Tenant concurrency limit
                if active_tenants[tid] >= tenant.max_concurrent_calls:
                    continue

                # R4: Count accepted attempts
                row_attempts = conn.execute("""
                    SELECT COUNT(*) as cnt FROM calls
                    WHERE campaign_id = ? AND lead_id = ? AND accepted = 1
                """, (cid, lid)).fetchone()
                accepted_attempts = row_attempts["cnt"] if row_attempts else 0

                if accepted_attempts >= cand["max_attempts"]:
                    conn.execute("UPDATE leads SET state = ? WHERE campaign_id = ? AND lead_id = ?", (STATE_EXHAUSTED, cid, lid))
                    continue

                attempt_no = accepted_attempts + 1
                request_id = f"{cid}:{lid}:a{attempt_no}"

                # Commit reservation write-ahead
                conn.execute("""
                    INSERT INTO calls (
                        request_id, campaign_id, lead_id, tenant_id, phone,
                        attempt_no, state, accepted, reserved_real
                    ) VALUES (?, ?, ?, ?, ?, ?, 'DIALING', 0, ?)
                """, (request_id, cid, lid, tid, phone, attempt_no, real_now))

                conn.execute("UPDATE leads SET state = 'DIALING' WHERE campaign_id = ? AND lead_id = ?", (cid, lid))

                active_tenants[tid] += 1
                busy_phones.add(phone)
                reservations.append(
                    DialReservation(
                        request_id=request_id,
                        campaign_id=cid,
                        lead_id=lid,
                        tenant_id=tid,
                        phone=phone,
                        attempt_no=attempt_no,
                    )
                )

            conn.execute("COMMIT")

        return reservations

    def record_accept(self, request_id: str, call_sid: str, sim_now_epoch: float) -> None:
        """Mark call accepted by carrier (201/200/found via GET). State becomes ACTIVE unless already ENDED."""
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            call = conn.execute("SELECT campaign_id, lead_id, state FROM calls WHERE request_id = ?", (request_id,)).fetchone()
            if call:
                cid = call["campaign_id"]
                lid = call["lead_id"]
                current_call_state = call["state"]

                new_call_state = "ENDED" if current_call_state == "ENDED" else "ACTIVE"
                conn.execute("""
                    UPDATE calls
                    SET accepted = 1, call_sid = ?, state = ?, accepted_sim = ?
                    WHERE request_id = ?
                """, (call_sid, new_call_state, sim_now_epoch, request_id))

                if new_call_state != "ENDED":
                    conn.execute("UPDATE leads SET state = 'ACTIVE' WHERE campaign_id = ? AND lead_id = ? AND state IN ('DIALING', 'UNKNOWN')", (cid, lid))
            conn.execute("COMMIT")

    def release_dialing(self, request_id: str, backoff_real_seconds: float = 1.0) -> None:
        """Release reservation after 503 or confirmed-not-placed 404, with short real-time backoff."""
        real_now = time.time()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            call = conn.execute("SELECT campaign_id, lead_id FROM calls WHERE request_id = ? AND state IN ('DIALING', 'UNKNOWN')", (request_id,)).fetchone()
            if call:
                cid = call["campaign_id"]
                lid = call["lead_id"]
                conn.execute("DELETE FROM calls WHERE request_id = ? AND state IN ('DIALING', 'UNKNOWN')", (request_id,))
                conn.execute("""
                    UPDATE leads
                    SET state = 'PENDING', not_before_real = ?
                    WHERE campaign_id = ? AND lead_id = ? AND state IN ('DIALING', 'UNKNOWN')
                """, (real_now + backoff_real_seconds, cid, lid))
            conn.execute("COMMIT")

    def mark_unknown(self, request_id: str) -> None:
        """Mark call outcome UNKNOWN after 504/timeout, holding tenant slot and phone lock for safety."""
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE calls SET state = 'UNKNOWN' WHERE request_id = ? AND state = 'DIALING'", (request_id,))
            conn.execute("COMMIT")

    def apply_result(self, payload: dict[str, Any], config: AppConfig) -> bool:
        """Idempotently process call outcome (webhook or polling result). Returns True if state changed."""
        req_id = payload.get("request_id")
        if not req_id:
            return False

        cid = payload["campaign_id"]
        lid = payload["lead_id"]
        tid = payload.get("tenant_id")
        phone = payload["phone"]
        call_sid = payload.get("call_sid")
        status = payload.get("status")
        disposition = payload.get("disposition")
        duration_sec = int(payload.get("duration_sec", 0))

        # Parse timestamps into epoch floats
        cb_raw = payload.get("callback_at")
        cb_dt = datetime.fromisoformat(cb_raw).astimezone(IST) if cb_raw else None
        cb_epoch = cb_dt.timestamp() if cb_dt else None

        st_raw = payload.get("started_at")
        st_dt = datetime.fromisoformat(st_raw).astimezone(IST) if st_raw else None
        st_epoch = st_dt.timestamp() if st_dt else None

        end_raw = payload.get("ended_at")
        end_dt = datetime.fromisoformat(end_raw).astimezone(IST) if end_raw else None
        end_epoch = end_dt.timestamp() if end_dt else None

        camp = config.campaigns.get(cid)
        if not camp:
            return False

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")

            existing = conn.execute("SELECT state FROM calls WHERE request_id = ?", (req_id,)).fetchone()
            if existing and existing["state"] == "ENDED":
                conn.execute("COMMIT")
                return False  # Duplicate delivery / already ended

            # Parse attempt number from request_id (e.g. c_acme_renewals:ren_0001:a1)
            attempt_no = int(req_id.split(":a")[-1]) if ":a" in req_id else 1
            if not tid:
                tid = camp.tenant_id

            # Upsert into calls with state ENDED and accepted=1
            conn.execute("""
                INSERT INTO calls (
                    request_id, campaign_id, lead_id, tenant_id, phone,
                    attempt_no, state, accepted, call_sid, reserved_real,
                    status, disposition, callback_at, started_at, ended_at, duration_sec
                ) VALUES (?, ?, ?, ?, ?, ?, 'ENDED', 1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    state = 'ENDED',
                    accepted = 1,
                    call_sid = excluded.call_sid,
                    status = excluded.status,
                    disposition = excluded.disposition,
                    callback_at = excluded.callback_at,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    duration_sec = excluded.duration_sec;
            """, (
                req_id, cid, lid, tid, phone, attempt_no, call_sid,
                time.time(), status, disposition, cb_epoch, st_epoch, end_epoch, duration_sec
            ))

            # R8: If disposition is DND, persist to dnd_phones and cascade to pending sibling leads
            if disposition == "dnd":
                conn.execute("""
                    INSERT INTO dnd_phones (phone, source_request_id, at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(phone) DO NOTHING;
                """, (phone, req_id, end_epoch or time.time()))

                conn.execute("""
                    UPDATE leads
                    SET state = 'DND_BLOCKED', kind = NULL, due_at = 0.0
                    WHERE phone = ? AND state = 'PENDING';
                """, (phone,))

            # Check if this phone is in DND
            dnd_row = conn.execute("SELECT phone FROM dnd_phones WHERE phone = ?", (phone,)).fetchone()
            phone_is_dnd = dnd_row is not None

            # Calculate total accepted attempts for this lead
            attempts_row = conn.execute("""
                SELECT COUNT(*) as cnt FROM calls
                WHERE campaign_id = ? AND lead_id = ? AND accepted = 1
            """, (cid, lid)).fetchone()
            total_attempts = attempts_row["cnt"] if attempts_row else attempt_no

            lead_row = conn.execute("SELECT max_attempts FROM leads WHERE campaign_id = ? AND lead_id = ?", (cid, lid)).fetchone()
            max_att = lead_row["max_attempts"] if lead_row else camp.max_attempts

            next_state, next_kind = determine_next_lead_state(
                accepted_attempts=total_attempts,
                max_attempts=max_att,
                status=status,
                disposition=disposition,
                phone_in_dnd=phone_is_dnd,
            )

            due_at = 0.0
            if next_kind == KIND_CALLBACK and cb_dt is not None:
                # Next dialable instant on or after callback_at
                due_dt = next_dialable_time(cb_dt, camp.window, config.regulatory_window)
                due_at = due_dt.timestamp()
            elif next_kind == KIND_RETRY and end_dt is not None:
                # Next dialable instant on or after ended_at + retry_delay
                delay_sec = get_retry_delay_seconds(status or "", config.retry_policy)
                target_dt = end_dt + timedelta(seconds=delay_sec)
                due_dt = next_dialable_time(target_dt, camp.window, config.regulatory_window)
                due_at = due_dt.timestamp()

            conn.execute("""
                UPDATE leads
                SET state = ?, kind = ?, due_at = ?, not_before_real = 0.0
                WHERE campaign_id = ? AND lead_id = ?;
            """, (next_state, next_kind, due_at, cid, lid))

            conn.execute("COMMIT")

        return True

    def get_unresolved_calls(self, sim_now_epoch: float, overdue_threshold_sim_seconds: float = 450.0) -> list[sqlite3.Row]:
        """Fetch calls that are DIALING, UNKNOWN, or ACTIVE but overdue for completion (potential lost webhooks)."""
        with self._get_connection() as conn:
            return conn.execute("""
                SELECT request_id, call_sid, campaign_id, lead_id, tenant_id, phone, state, attempt_no, accepted_sim, reserved_real
                FROM calls
                WHERE state IN ('DIALING', 'UNKNOWN')
                   OR (state = 'ACTIVE' AND accepted_sim IS NOT NULL AND (? - accepted_sim) > ?)
            """, (sim_now_epoch, overdue_threshold_sim_seconds)).fetchall()

    def is_all_finished(self) -> bool:
        """True if every lead is in a terminal state and no calls are active/unresolved."""
        with self._get_connection() as conn:
            unresolved = conn.execute("SELECT COUNT(*) as cnt FROM calls WHERE state IN ('DIALING', 'UNKNOWN', 'ACTIVE')").fetchone()["cnt"]
            if unresolved > 0:
                return False

            non_terminal_leads = conn.execute("SELECT COUNT(*) as cnt FROM leads WHERE state NOT IN ('COMPLETED', 'EXHAUSTED', 'DND_BLOCKED')").fetchone()["cnt"]
            return non_terminal_leads == 0

    def get_final_states(self, config: AppConfig) -> list[dict[str, Any]]:
        """Compute the final states of all leads for final_state.csv."""
        results = []
        with self._get_connection() as conn:
            leads = conn.execute("SELECT campaign_id, lead_id, phone, max_attempts FROM leads ORDER BY campaign_id, lead_id").fetchall()
            for l in leads:
                cid, lid, phone, max_att = l["campaign_id"], l["lead_id"], l["phone"], l["max_attempts"]

                calls = conn.execute("""
                    SELECT status, disposition, ended_at, accepted
                    FROM calls
                    WHERE campaign_id = ? AND lead_id = ?
                    ORDER BY ended_at DESC, attempt_no DESC
                """, (cid, lid)).fetchall()

                accepted_attempts = sum(1 for c in calls if c["accepted"] == 1)
                last_call = calls[0] if calls else None
                last_status = last_call["status"] if last_call and last_call["status"] else ""
                last_disp = last_call["disposition"] if last_call and last_call["disposition"] else ""

                has_terminal = any(c["disposition"] in ("interested", "not_interested", "wrong_number", "dnd") for c in calls)
                dnd_exists = conn.execute("SELECT 1 FROM dnd_phones WHERE phone = ?", (phone,)).fetchone() is not None

                final_st = derive_final_state(
                    has_terminal=has_terminal,
                    attempts=accepted_attempts,
                    max_attempts=max_att,
                    is_busy=False,
                    phone_in_dnd=dnd_exists,
                )

                results.append({
                    "campaign_id": cid,
                    "lead_id": lid,
                    "final_state": final_st,
                    "attempts": accepted_attempts,
                    "last_status": last_status,
                    "last_disposition": last_disp,
                })
        return results

    def get_dashboard_summary(self) -> dict[str, Any]:
        """Summary statistics for monitoring and /status endpoint."""
        with self._get_connection() as conn:
            total_leads = conn.execute("SELECT COUNT(*) as c FROM leads").fetchone()["c"]
            state_counts = dict(conn.execute("SELECT state, COUNT(*) as c FROM leads GROUP BY state").fetchall())
            call_counts = dict(conn.execute("SELECT state, COUNT(*) as c FROM calls GROUP BY state").fetchall())
            active_tenants = dict(conn.execute("SELECT tenant_id, COUNT(*) as c FROM calls WHERE state IN ('DIALING', 'UNKNOWN', 'ACTIVE') GROUP BY tenant_id").fetchall())
            dnd_count = conn.execute("SELECT COUNT(*) as c FROM dnd_phones").fetchone()["c"]

            return {
                "total_leads": total_leads,
                "leads_by_state": state_counts,
                "calls_by_state": call_counts,
                "active_by_tenant": active_tenants,
                "dnd_phones_count": dnd_count,
            }
