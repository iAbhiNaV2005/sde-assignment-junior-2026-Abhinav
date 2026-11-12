"""HTTP client for the mock telephony provider."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

import httpx

from .store import DialReservation

logger = logging.getLogger("dispatcher.provider")


class DialOutcome(Enum):
    ACCEPTED = "ACCEPTED"       # 201 or 200, call placed
    TRANSIENT_503 = "TRANSIENT_503" # 503, definitely not placed
    UNKNOWN_504 = "UNKNOWN_504" # 504 or network timeout, outcome unknown
    VIOLATION_4XX = "VIOLATION_4XX" # 4xx error (indicates a pre-check bug)


@dataclass(frozen=True)
class PlaceCallResult:
    outcome: DialOutcome
    request_id: str
    call_sid: Optional[str] = None
    status_code: Optional[int] = None
    detail: Optional[str] = None
    data: Optional[dict[str, Any]] = None


class TelephonyClient:
    def __init__(self, base_url: str, callback_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.callback_url = callback_url
        self.timeout = timeout
        # Using a persistent AsyncClient with appropriate connection and read timeouts
        self._client: Optional[httpx.AsyncClient] = None

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=3.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def get_clock(self) -> dict[str, Any]:
        client = await self.get_client()
        resp = await client.get("/v1/clock")
        resp.raise_for_status()
        return resp.json()

    async def place_call(self, res: DialReservation) -> PlaceCallResult:
        """Place call through provider API adhering to the strict response taxonomy."""
        client = await self.get_client()
        payload = {
            "request_id": res.request_id,
            "campaign_id": res.campaign_id,
            "lead_id": res.lead_id,
            "phone": res.phone,
            "callback_url": self.callback_url,
        }

        try:
            resp = await client.post("/v1/calls", json=payload)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            logger.warning("Network timeout or connection error for %s: %s", res.request_id, exc)
            return PlaceCallResult(
                outcome=DialOutcome.UNKNOWN_504,
                request_id=res.request_id,
                detail=str(exc),
            )

        code = resp.status_code
        if code in (201, 200):
            data = resp.json()
            return PlaceCallResult(
                outcome=DialOutcome.ACCEPTED,
                request_id=res.request_id,
                call_sid=data.get("call_sid"),
                status_code=code,
                data=data,
            )
        elif code == 503:
            return PlaceCallResult(
                outcome=DialOutcome.TRANSIENT_503,
                request_id=res.request_id,
                status_code=503,
                detail="503 Service Unavailable (not placed)",
            )
        elif code == 504:
            return PlaceCallResult(
                outcome=DialOutcome.UNKNOWN_504,
                request_id=res.request_id,
                status_code=504,
                detail="504 Upstream Timeout (outcome unknown)",
            )
        else:
            # 400, 409, 422, 429: recorded violation by mock
            text = resp.text
            logger.error("Provider returned unexpected error %d for %s: %s", code, res.request_id, text)
            return PlaceCallResult(
                outcome=DialOutcome.VIOLATION_4XX,
                request_id=res.request_id,
                status_code=code,
                detail=text,
            )

    async def get_call_by_request_id(self, request_id: str) -> Optional[dict[str, Any]]:
        """Query provider by request_id. Returns call dict if found, None if 404."""
        client = await self.get_client()
        try:
            resp = await client.get("/v1/calls", params={"request_id": request_id})
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 404:
                return None
            else:
                logger.warning("Query by request_id %s got status %d", request_id, resp.status_code)
                return None
        except Exception as exc:
            logger.warning("Failed to query call %s: %s", request_id, exc)
            return None

