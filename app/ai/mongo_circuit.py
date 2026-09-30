import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.ai.circuit import CircuitOpen
from app.db.connection import server_time, transaction


@dataclass(frozen=True)
class SharedPermit:
    token: str
    epoch: int
    probe: bool


class MongoCircuitBreaker:
    """One account/provider document atomically owns cooldown, leases and budgets.

    Small time buckets conservatively cover rolling windows without an unbounded
    ledger: minute reservations expire after 60–61s; daily ones after 24–25h.
    Cancellation or ambiguous errors do not refund charged reservations.
    """

    def __init__(self, db, provider, settings, clock=None):
        self.db = db
        self.collection = db.sid_provider_state
        self.key = f"{settings.ai_account_scope}:{provider}"
        self.settings = settings
        self.clock = clock
        self.rpm = getattr(settings, f"{provider}_requests_per_minute")
        self.tpm = getattr(settings, f"{provider}_tokens_per_minute")
        self.rpd = getattr(settings, f"{provider}_requests_per_day")
        self.quota_configured = all(limit > 0 for limit in (self.rpm, self.tpm, self.rpd))

    async def now(self):
        return self.clock() if self.clock else await server_time(self.db)

    async def initialize(self):
        if not self.quota_configured:
            return
        now = await self.now()
        await self.collection.update_one(
            {"_id": self.key},
            {
                "$setOnInsert": {
                    "state": "closed",
                    "epoch": 0,
                    "failures": 0,
                    "retry_at": now,
                    "probe_token": None,
                    "probe_until": now,
                    "leases": {},
                    "minute_buckets": [],
                    "day_buckets": [],
                    # Inconsistent deployment configuration must fail closed.
                    "limits": self.limits,
                }
            },
            upsert=True,
        )

    @property
    def limits(self):
        return {
            "rpm": self.rpm,
            "tpm": self.tpm,
            "rpd": self.rpd,
            "concurrency": self.settings.ai_shared_max_concurrent_requests,
            "threshold": self.settings.ai_circuit_failure_threshold,
            "recovery": self.settings.ai_circuit_recovery_seconds,
        }

    async def acquire(self, *, reserved_tokens=0):
        if reserved_tokens < 1:
            raise ValueError("A positive token reservation is required")
        if not self.quota_configured:
            raise CircuitOpen(60)
        token = str(uuid4())

        async def reserve(session):
            now = await self.now()
            state = await self.collection.find_one({"_id": self.key}, session=session)
            if state is None or state["limits"] != self.limits:
                raise CircuitOpen(60)
            if state["state"] == "open":
                if state["retry_at"] > now:
                    raise CircuitOpen((state["retry_at"] - now).total_seconds())
                state["state"] = "half_open"
            probe = state["state"] == "half_open"
            if probe and state["probe_token"]:
                if state["probe_until"] > now:
                    raise CircuitOpen((state["probe_until"] - now).total_seconds())
                # A crashed probe cannot later close a replacement probe's circuit.
                state["epoch"] += 1
            leases = {k: v for k, v in state["leases"].items() if v["until"] > now}
            if len(leases) >= self.settings.ai_shared_max_concurrent_requests:
                raise CircuitOpen(
                    max(1, min((v["until"] - now).total_seconds() for v in leases.values()))
                )
            minutes = [b for b in state["minute_buckets"] if b["until"] > now]
            days = [b for b in state["day_buckets"] if b["until"] > now]
            if sum(b["requests"] for b in days) >= self.rpd:
                raise CircuitOpen(min((b["until"] - now).total_seconds() for b in days))
            if (
                sum(b["requests"] for b in minutes) >= self.rpm
                or sum(b["tokens"] for b in minutes) + reserved_tokens > self.tpm
            ):
                raise CircuitOpen(
                    min((b["until"] - now).total_seconds() for b in minutes) if minutes else 60
                )
            add_bucket(minutes, now, 1, 60, reserved_tokens)
            add_bucket(days, now, 3600, 86400, 0)
            until = now + timedelta(seconds=self.settings.ai_request_timeout_seconds + 10)
            leases[token] = {"until": until, "epoch": state["epoch"]}
            state.update(
                leases=leases,
                minute_buckets=minutes,
                day_buckets=days,
            )
            if probe:
                state.update(probe_token=token, probe_until=until)
            await self.collection.replace_one({"_id": self.key}, state, session=session)
            return SharedPermit(token, state["epoch"], probe)

        return await transaction(self.db, reserve)

    async def _finish(self, permit, action, force_open=False, retry_after=None):
        async def complete(session):
            now = await self.now()
            state = await self.collection.find_one({"_id": self.key}, session=session)
            if state is None:
                return
            lease = state["leases"].pop(permit.token, None)
            if lease is None:
                return
            current = permit.epoch == state["epoch"] and lease["until"] > now
            if not current:
                if (
                    action == "failure"
                    and state["state"] == "open"
                    and permit.epoch + 1 == state["epoch"]
                    and retry_after is not None
                ):
                    state["retry_at"] = max(state["retry_at"], now + timedelta(seconds=retry_after))
            elif action == "success":
                state["failures"] = 0
                if permit.probe:
                    state.update(state="closed", probe_token=None, epoch=state["epoch"] + 1)
            elif action == "failure":
                state["failures"] += 1
                if (
                    permit.probe
                    or force_open
                    or state["failures"] >= self.settings.ai_circuit_failure_threshold
                ):
                    cooldown = max(self.settings.ai_circuit_recovery_seconds, retry_after or 0)
                    state.update(
                        state="open",
                        retry_at=now + timedelta(seconds=cooldown),
                        probe_token=None,
                        epoch=state["epoch"] + 1,
                    )
            elif action == "abandon" and permit.probe:
                state["probe_token"] = None
            await self.collection.replace_one({"_id": self.key}, state, session=session)

        await transaction(self.db, complete)

    async def success(self, permit):
        await self._finish(permit, "success")

    async def failure(self, permit, *, force_open=False, retry_after=None):
        await self._finish(permit, "failure", force_open, retry_after)

    async def abandon(self, permit):
        await self._finish(permit, "abandon")

    async def apply_policy(self):
        """Explicit operator action: preserve budgets and cooldowns when tuning limits."""
        if not self.quota_configured:
            raise ValueError("Configure positive RPM, TPM and RPD limits first")
        await self.initialize()

        async def apply(session):
            now = await self.now()
            state = await self.collection.find_one({"_id": self.key}, session=session)
            if any(lease["until"] > now for lease in state["leases"].values()):
                raise RuntimeError("Stop account traffic and wait for active leases to expire")
            state.update(limits=self.limits, leases={}, probe_token=None, epoch=state["epoch"] + 1)
            if state["state"] == "half_open":
                state["state"] = "open"
            await self.collection.replace_one({"_id": self.key}, state, session=session)

        await transaction(self.db, apply)

    async def snapshot(self):
        now = await self.now()
        state = await self.collection.find_one({"_id": self.key})
        if state is None:
            return {
                "state": "disabled",
                "consecutive_failures": 0,
                "retry_after_seconds": 0,
                "quota_configured": False,
                "requests_reserved": 0,
                "tokens_reserved": 0,
                "daily_requests_reserved": 0,
                "active_leases": 0,
            }
        return {
            "state": state["state"],
            "consecutive_failures": state["failures"],
            "retry_after_seconds": max(0, (state["retry_at"] - now).total_seconds())
            if state["state"] == "open"
            else 0,
            "quota_configured": self.quota_configured and state["limits"] == self.limits,
            "requests_reserved": sum(
                b["requests"] for b in state["minute_buckets"] if b["until"] > now
            ),
            "tokens_reserved": sum(
                b["tokens"] for b in state["minute_buckets"] if b["until"] > now
            ),
            "daily_requests_reserved": sum(
                b["requests"] for b in state["day_buckets"] if b["until"] > now
            ),
            "active_leases": sum(v["until"] > now for v in state["leases"].values()),
        }


def add_bucket(buckets, now, precision, window_seconds, tokens):
    # Round expiry forward, preserving the full rolling window at boundaries.
    until = datetime.fromtimestamp(
        math.ceil(now.timestamp() / precision) * precision + window_seconds, UTC
    )
    bucket = next((item for item in buckets if item["until"] == until), None)
    if bucket is None:
        bucket = {"until": until, "requests": 0, "tokens": 0}
        buckets.append(bucket)
    bucket["requests"] += 1
    bucket["tokens"] += tokens


async def shared_breakers(db, settings):
    breakers = {
        provider: MongoCircuitBreaker(db, provider, settings) for provider in ("groq", "gemini")
    }
    for breaker in breakers.values():
        await breaker.initialize()
    return breakers
