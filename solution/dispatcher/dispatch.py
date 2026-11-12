"""Asynchronous dispatch loop: selection, write-ahead reservation, concurrent dialing."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .clock import Clock
from .config import AppConfig
from .policy import mask_phone
from .provider import DialOutcome, TelephonyClient
from .store import DialReservation, StateStore
from .windows import is_window_open

logger = logging.getLogger("dispatcher.dispatch")


class DispatchLoop:
    def __init__(
        self,
        store: StateStore,
        provider: TelephonyClient,
        clock: Clock,
        config: AppConfig,
        wake_event: asyncio.Event,
    ):
        self.store = store
        self.provider = provider
        self.clock = clock
        self.config = config
        self.wake_event = wake_event

    async def _dial_one(self, res: DialReservation) -> None:
        """Execute a single outbound dial with robust error handling and slot resolution."""
        # Double-check calling window right before firing HTTP request to prevent race with window closing
        camp = self.config.campaigns.get(res.campaign_id)
        if camp:
            margin_s = max(120.0, 3.0 * self.clock.speed)
            if not is_window_open(self.clock.now(), camp.window, self.config.regulatory_window, margin_seconds=margin_s):
                logger.warning("Window closing or closed for %s before HTTP request; releasing slot", res.request_id)
                self.store.release_dialing(res.request_id, backoff_real_seconds=0.0)
                self.wake_event.set()
                return

        logger.info("Dialing %s (%s, %s, attempt %d)",
                    res.request_id, res.tenant_id, mask_phone(res.phone), res.attempt_no)

        result = await self.provider.place_call(res)

        if result.outcome == DialOutcome.ACCEPTED:
            logger.info("Carrier accepted %s (call_sid=%s)", res.request_id, result.call_sid)
            self.store.record_accept(res.request_id, result.call_sid or "", self.clock.now_epoch())
            self.wake_event.set()

        elif result.outcome == DialOutcome.TRANSIENT_503:
            logger.warning("Carrier returned 503 for %s; releasing slot with 1s real backoff", res.request_id)
            self.store.release_dialing(res.request_id, backoff_real_seconds=1.0)
            self.wake_event.set()

        elif result.outcome == DialOutcome.UNKNOWN_504:
            logger.warning("Carrier 504 / network timeout for %s; marking UNKNOWN and resolving...", res.request_id)
            self.store.mark_unknown(res.request_id)

            # Immediately probe carrier by request_id to resolve ambiguity
            probe = await self.provider.get_call_by_request_id(res.request_id)
            if probe is not None:
                sid = probe.get("call_sid", "")
                logger.info("504 resolution found call %s (call_sid=%s)", res.request_id, sid)
                self.store.record_accept(res.request_id, sid, self.clock.now_epoch())
                if probe.get("state") == "ended" and "result" in probe:
                    self.store.apply_result(probe["result"], self.config)
            else:
                logger.info("504 resolution confirmed call %s not placed (404); releasing", res.request_id)
                self.store.release_dialing(res.request_id, backoff_real_seconds=1.0)
            self.wake_event.set()

        elif result.outcome == DialOutcome.VIOLATION_4XX:
            logger.error("4xx violation for %s: %s", res.request_id, result.detail)
            self.store.release_dialing(res.request_id, backoff_real_seconds=5.0)
            self.wake_event.set()

    async def run(self) -> None:
        """Run dispatch loop until all leads reach final states and all calls end."""
        logger.info("Dispatch loop started")

        while True:
            if self.store.is_all_finished():
                logger.info("All leads completed/exhausted/dnd_blocked and no calls active; dispatch complete")
                break

            sim_now = self.clock.now()
            margin_s = max(120.0, 3.0 * self.clock.speed)
            reservations = self.store.reserve_dials(sim_now, self.config, margin_seconds=margin_s)

            if reservations:
                logger.info("Reserved %d dials at sim_time=%s", len(reservations), sim_now.isoformat())
                # Fire dials concurrently
                tasks = [asyncio.create_task(self._dial_one(res)) for res in reservations]
                await asyncio.gather(*tasks, return_exceptions=True)
                continue

            # If no reservations were made, wait on wake_event or brief delay
            try:
                # Max idle sleep 0.5 real seconds (prevents callback lateness drift)
                self.wake_event.clear()
                await asyncio.wait_for(self.wake_event.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break
