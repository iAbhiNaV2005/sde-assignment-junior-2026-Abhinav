"""Startup and periodic reconciler: resolves in-flight ambiguities, 504s, and lost webhooks."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .clock import Clock
from .config import AppConfig
from .provider import TelephonyClient
from .store import StateStore

logger = logging.getLogger("dispatcher.reconcile")


class Reconciler:
    def __init__(
        self,
        store: StateStore,
        provider: TelephonyClient,
        clock: Clock,
        config: AppConfig,
        wake_event: Optional[asyncio.Event] = None,
    ):
        self.store = store
        self.provider = provider
        self.clock = clock
        self.config = config
        self.wake_event = wake_event

    async def reconcile_call(self, row) -> None:
        """Resolve a single DIALING, UNKNOWN, or overdue ACTIVE call against provider records."""
        req_id = row["request_id"]
        state = row["state"]

        logger.info("Reconciling %s call: %s", state, req_id)
        carrier_call = await self.provider.get_call_by_request_id(req_id)

        if carrier_call is None:
            # 404: Call was never placed on carrier side
            if state in ("DIALING", "UNKNOWN"):
                logger.info("Call %s not found on carrier (404); releasing reservation", req_id)
                self.store.release_dialing(req_id, backoff_real_seconds=0.0)
                if self.wake_event:
                    self.wake_event.set()
        else:
            # Carrier placed the call
            sid = carrier_call.get("call_sid")
            c_state = carrier_call.get("state")
            self.store.record_accept(req_id, sid or "", self.clock.now_epoch())

            if c_state == "ended" and "result" in carrier_call:
                logger.info("Adopting ended outcome from provider poll for %s", req_id)
                self.store.apply_result(carrier_call["result"], self.config)
                if self.wake_event:
                    self.wake_event.set()

    async def run_startup_recovery(self) -> None:
        """Perform full recovery pass before dispatch loop begins."""
        now_epoch = self.clock.now_epoch()
        unresolved = self.store.get_unresolved_calls(now_epoch, overdue_threshold_sim_seconds=0.0)
        if unresolved:
            logger.info("Found %d unresolved calls on startup; reconciling...", len(unresolved))
            for row in unresolved:
                await self.reconcile_call(row)
            logger.info("Startup reconciliation complete")

    async def run_periodic_loop(self, poll_interval_real_seconds: float = 1.5) -> None:
        """Background task polling for lost webhooks and resolving 504 calls."""
        while True:
            try:
                await asyncio.sleep(poll_interval_real_seconds)
                now_epoch = self.clock.now_epoch()
                # Overdue threshold: 450 sim seconds (max call talk is ~390 sim seconds)
                overdue = self.store.get_unresolved_calls(now_epoch, overdue_threshold_sim_seconds=450.0)
                for row in overdue:
                    await self.reconcile_call(row)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error during periodic reconciliation: %s", exc, exc_info=True)

