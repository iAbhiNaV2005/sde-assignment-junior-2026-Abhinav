"""Simulated clock abstraction for local calculation and testing."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx

IST = timezone(timedelta(hours=5, minutes=30))


class Clock(Protocol):
    def now(self) -> datetime:
        """Current simulated time (IST timezone)."""
        ...

    def now_epoch(self) -> float:
        """Current simulated time as Unix epoch seconds (UTC float)."""
        ...

    @property
    def speed(self) -> float:
        """Clock acceleration factor."""
        ...

    @property
    def real_epoch(self) -> float:
        """Mock provider's real start time (run fingerprint)."""
        ...

    def sim_to_real_seconds(self, sim_seconds: float) -> float:
        """Convert simulated duration into real wall-clock seconds."""
        ...


class ProviderClock:
    """Calculates simulated time locally based on provider's /v1/clock parameters."""

    def __init__(self, sim_epoch: datetime, real_epoch: float, speed: float):
        self._sim_epoch = sim_epoch.astimezone(IST)
        self._real_epoch = real_epoch
        self._speed = max(1.0, speed)

    @classmethod
    def from_provider_sync(cls, provider_url: str, timeout: float = 5.0) -> ProviderClock:
        """Fetch clock parameters synchronously from provider."""
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{provider_url.rstrip('/')}/v1/clock")
            resp.raise_for_status()
            data = resp.json()
            sim_epoch = datetime.fromisoformat(data["sim_epoch"])
            real_epoch = float(data["real_epoch"])
            speed = float(data["speed"])
            return cls(sim_epoch=sim_epoch, real_epoch=real_epoch, speed=speed)

    @classmethod
    async def from_provider_async(cls, provider_url: str, timeout: float = 5.0) -> ProviderClock:
        """Fetch clock parameters asynchronously from provider."""
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{provider_url.rstrip('/')}/v1/clock")
            resp.raise_for_status()
            data = resp.json()
            sim_epoch = datetime.fromisoformat(data["sim_epoch"])
            real_epoch = float(data["real_epoch"])
            speed = float(data["speed"])
            return cls(sim_epoch=sim_epoch, real_epoch=real_epoch, speed=speed)

    def now(self) -> datetime:
        elapsed_real = time.time() - self._real_epoch
        return self._sim_epoch + timedelta(seconds=elapsed_real * self._speed)

    def now_epoch(self) -> float:
        return self.now().timestamp()

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def real_epoch(self) -> float:
        return self._real_epoch

    @property
    def sim_epoch(self) -> datetime:
        return self._sim_epoch

    def sim_to_real_seconds(self, sim_seconds: float) -> float:
        return max(0.0, sim_seconds / self._speed)


class FakeClock:
    """Controllable clock for unit tests."""

    def __init__(self, current_sim: datetime, speed: float = 600.0, real_epoch: float = 0.0):
        self._sim_now = current_sim.astimezone(IST)
        self._speed = speed
        self._real_epoch = real_epoch

    def now(self) -> datetime:
        return self._sim_now

    def now_epoch(self) -> float:
        return self._sim_now.timestamp()

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def real_epoch(self) -> float:
        return self._real_epoch

    def set_now(self, new_sim: datetime) -> None:
        self._sim_now = new_sim.astimezone(IST)

    def advance_sim(self, seconds: float = 0, minutes: float = 0, hours: float = 0, days: float = 0) -> datetime:
        self._sim_now += timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)
        return self._sim_now

    def sim_to_real_seconds(self, sim_seconds: float) -> float:
        return max(0.0, sim_seconds / self._speed)

