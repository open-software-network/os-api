from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from google.cloud import firestore

from .auth import Principal

KEY_PATTERN = re.compile(r"^sk-os-(key_[A-Za-z0-9_-]{12,32})\.([A-Za-z0-9_-]{32,})$")


class SpendLimitExceeded(Exception):
    pass


class KeyUnavailable(Exception):
    pass


@dataclass(frozen=True)
class ApiKeyRecord:
    id: str
    user_id: str
    name: str
    key_prefix: str
    created_at: datetime
    expires_at: datetime | None
    spend_limit_microusd: int | None
    spend_used_microusd: int
    spend_reserved_microusd: int
    revoked_at: datetime | None

    @property
    def status(self) -> str:
        if self.revoked_at:
            return "revoked"
        if self.expires_at and self.expires_at <= datetime.now(UTC):
            return "expired"
        if (
            self.spend_limit_microusd is not None
            and self.spend_used_microusd >= self.spend_limit_microusd
        ):
            return "limit_reached"
        return "active"

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "prefix": self.key_prefix,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "spend_limit_microusd": self.spend_limit_microusd,
            "spend_used_microusd": self.spend_used_microusd,
            "spend_reserved_microusd": self.spend_reserved_microusd,
            "status": self.status,
        }


@dataclass(frozen=True)
class CreatedApiKey:
    record: ApiKeyRecord
    secret: str


class ManagedKeyRepository(Protocol):
    async def healthcheck(self) -> None: ...

    async def authenticate(self, raw_key: str) -> Principal | None: ...

    async def create_key(
        self,
        user_id: str,
        name: str,
        expires_at: datetime | None,
        spend_limit_microusd: int | None,
    ) -> CreatedApiKey: ...

    async def list_keys(self, user_id: str) -> list[ApiKeyRecord]: ...

    async def revoke_key(self, user_id: str, key_id: str) -> bool: ...

    async def reserve(self, key_id: str, billing_id: str, estimate_microusd: int) -> None: ...

    async def settle(
        self,
        key_id: str,
        billing_id: str,
        actual_microusd: int,
        usage: dict[str, Any],
    ) -> None: ...

    async def release(self, key_id: str, billing_id: str, reason: str) -> None: ...

    async def list_usage(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]: ...


class InMemoryKeyRepository:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.usage: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def healthcheck(self) -> None:
        return None

    async def authenticate(self, raw_key: str) -> Principal | None:
        parsed = _parse_key(raw_key)
        if not parsed:
            return None
        key_id, _ = parsed
        async with self._lock:
            item = self.records.get(key_id)
            if not item or not _record_is_usable(item):
                return None
            digest = hashlib.sha256(raw_key.encode()).hexdigest()
            if not hmac.compare_digest(digest, item["key_hash"]):
                return None
            return Principal(
                tenant_id=f"key:{key_id}",
                key_fingerprint=digest[:12],
                key_id=key_id,
                user_id=item["user_id"],
            )

    async def create_key(
        self,
        user_id: str,
        name: str,
        expires_at: datetime | None,
        spend_limit_microusd: int | None,
    ) -> CreatedApiKey:
        key_id, raw_key = _new_key()
        now = datetime.now(UTC)
        item = {
            "user_id": user_id,
            "name": name,
            "key_prefix": _display_prefix(raw_key),
            "key_hash": hashlib.sha256(raw_key.encode()).hexdigest(),
            "created_at": now,
            "expires_at": expires_at,
            "spend_limit_microusd": spend_limit_microusd,
            "spend_used_microusd": 0,
            "spend_reserved_microusd": 0,
            "revoked_at": None,
        }
        async with self._lock:
            self.records[key_id] = item
        return CreatedApiKey(_to_record(key_id, item), raw_key)

    async def list_keys(self, user_id: str) -> list[ApiKeyRecord]:
        async with self._lock:
            records = [
                _to_record(key_id, item)
                for key_id, item in self.records.items()
                if item["user_id"] == user_id
            ]
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    async def revoke_key(self, user_id: str, key_id: str) -> bool:
        async with self._lock:
            item = self.records.get(key_id)
            if not item or item["user_id"] != user_id:
                return False
            item["revoked_at"] = datetime.now(UTC)
            return True

    async def reserve(self, key_id: str, billing_id: str, estimate_microusd: int) -> None:
        async with self._lock:
            item = self.records.get(key_id)
            if not item or not _record_is_usable(item):
                raise KeyUnavailable
            limit = item["spend_limit_microusd"]
            committed = item["spend_used_microusd"] + item["spend_reserved_microusd"]
            if limit is not None and committed + estimate_microusd > limit:
                raise SpendLimitExceeded
            item["spend_reserved_microusd"] += estimate_microusd
            self.usage[billing_id] = {
                "key_id": key_id,
                "user_id": item["user_id"],
                "estimate_microusd": estimate_microusd,
                "actual_microusd": None,
                "status": "reserved",
                "created_at": datetime.now(UTC),
            }

    async def settle(
        self,
        key_id: str,
        billing_id: str,
        actual_microusd: int,
        usage: dict[str, Any],
    ) -> None:
        async with self._lock:
            event = self.usage.get(billing_id)
            item = self.records.get(key_id)
            if not event or not item or event["status"] != "reserved":
                return
            estimate = int(event["estimate_microusd"])
            item["spend_reserved_microusd"] = max(0, item["spend_reserved_microusd"] - estimate)
            item["spend_used_microusd"] += max(0, actual_microusd)
            event.update(
                {
                    "actual_microusd": max(0, actual_microusd),
                    "status": "settled",
                    "settled_at": datetime.now(UTC),
                    **usage,
                }
            )

    async def release(self, key_id: str, billing_id: str, reason: str) -> None:
        async with self._lock:
            event = self.usage.get(billing_id)
            item = self.records.get(key_id)
            if not event or not item or event["status"] != "reserved":
                return
            item["spend_reserved_microusd"] = max(
                0, item["spend_reserved_microusd"] - int(event["estimate_microusd"])
            )
            event.update({"status": "released", "reason": reason, "settled_at": datetime.now(UTC)})

    async def list_usage(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        async with self._lock:
            rows = [
                dict(row, id=row_id)
                for row_id, row in self.usage.items()
                if row["user_id"] == user_id
            ]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return [_public_usage(row) for row in rows[:limit]]


class FirestoreKeyRepository:
    def __init__(self, project: str | None = None, prefix: str = "sovereign") -> None:
        self.client = firestore.Client(project=project or None)
        self.keys = self.client.collection(f"{prefix}_api_keys")
        self.usage = self.client.collection(f"{prefix}_usage")

    async def healthcheck(self) -> None:
        def probe() -> None:
            next(iter(self.keys.limit(1).stream()), None)

        await asyncio.to_thread(probe)

    async def authenticate(self, raw_key: str) -> Principal | None:
        parsed = _parse_key(raw_key)
        if not parsed:
            return None
        key_id, _ = parsed
        snapshot = await asyncio.to_thread(self.keys.document(key_id).get)
        if not snapshot.exists:
            return None
        item = snapshot.to_dict() or {}
        if not _record_is_usable(item):
            return None
        digest = hashlib.sha256(raw_key.encode()).hexdigest()
        if not hmac.compare_digest(digest, str(item.get("key_hash", ""))):
            return None
        return Principal(
            tenant_id=f"key:{key_id}",
            key_fingerprint=digest[:12],
            key_id=key_id,
            user_id=str(item["user_id"]),
        )

    async def create_key(
        self,
        user_id: str,
        name: str,
        expires_at: datetime | None,
        spend_limit_microusd: int | None,
    ) -> CreatedApiKey:
        for _ in range(3):
            key_id, raw_key = _new_key()
            now = datetime.now(UTC)
            item = {
                "user_id": user_id,
                "name": name,
                "key_prefix": _display_prefix(raw_key),
                "key_hash": hashlib.sha256(raw_key.encode()).hexdigest(),
                "created_at": now,
                "expires_at": expires_at,
                "spend_limit_microusd": spend_limit_microusd,
                "spend_used_microusd": 0,
                "spend_reserved_microusd": 0,
                "reservations": {},
                "revoked_at": None,
            }
            try:
                await asyncio.to_thread(self.keys.document(key_id).create, item)
            except Exception as exc:
                if exc.__class__.__name__ == "AlreadyExists":
                    continue
                raise
            return CreatedApiKey(_to_record(key_id, item), raw_key)
        raise RuntimeError("could not allocate a unique API key id")

    async def list_keys(self, user_id: str) -> list[ApiKeyRecord]:
        def fetch() -> list[ApiKeyRecord]:
            return [
                _to_record(snapshot.id, snapshot.to_dict() or {})
                for snapshot in self.keys.where("user_id", "==", user_id).stream()
            ]

        records = await asyncio.to_thread(fetch)
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    async def revoke_key(self, user_id: str, key_id: str) -> bool:
        def revoke() -> bool:
            ref = self.keys.document(key_id)
            transaction = self.client.transaction()

            @firestore.transactional
            def update(transaction: Any) -> bool:
                snapshot = ref.get(transaction=transaction)
                item = snapshot.to_dict() if snapshot.exists else None
                if not item or item.get("user_id") != user_id:
                    return False
                transaction.update(ref, {"revoked_at": datetime.now(UTC)})
                return True

            return update(transaction)

        return await asyncio.to_thread(revoke)

    async def reserve(self, key_id: str, billing_id: str, estimate_microusd: int) -> None:
        def reserve_sync() -> None:
            key_ref = self.keys.document(key_id)
            usage_ref = self.usage.document(billing_id)
            transaction = self.client.transaction()

            @firestore.transactional
            def update(transaction: Any) -> None:
                key_snapshot = key_ref.get(transaction=transaction)
                item = key_snapshot.to_dict() if key_snapshot.exists else None
                if not item or not _record_is_usable(item):
                    raise KeyUnavailable
                used = int(item.get("spend_used_microusd", 0))
                now = datetime.now(UTC)
                reservations = _active_reservations(item.get("reservations"), now)
                reserved = sum(entry["amount_microusd"] for entry in reservations.values())
                limit = item.get("spend_limit_microusd")
                if limit is not None and used + reserved + estimate_microusd > int(limit):
                    raise SpendLimitExceeded
                reservations[billing_id] = {
                    "amount_microusd": estimate_microusd,
                    "expires_at": now + timedelta(minutes=15),
                }
                transaction.update(
                    key_ref,
                    {
                        "reservations": reservations,
                        "spend_reserved_microusd": reserved + estimate_microusd,
                    },
                )
                transaction.create(
                    usage_ref,
                    {
                        "key_id": key_id,
                        "user_id": item["user_id"],
                        "estimate_microusd": estimate_microusd,
                        "actual_microusd": None,
                        "status": "reserved",
                        "created_at": now,
                        "reservation_expires_at": now + timedelta(minutes=15),
                    },
                )

            update(transaction)

        await asyncio.to_thread(reserve_sync)

    async def settle(
        self,
        key_id: str,
        billing_id: str,
        actual_microusd: int,
        usage: dict[str, Any],
    ) -> None:
        await asyncio.to_thread(self._finish_sync, key_id, billing_id, actual_microusd, usage, None)

    async def release(self, key_id: str, billing_id: str, reason: str) -> None:
        await asyncio.to_thread(self._finish_sync, key_id, billing_id, None, {}, reason)

    def _finish_sync(
        self,
        key_id: str,
        billing_id: str,
        actual_microusd: int | None,
        usage: dict[str, Any],
        release_reason: str | None,
    ) -> None:
        key_ref = self.keys.document(key_id)
        usage_ref = self.usage.document(billing_id)
        transaction = self.client.transaction()

        @firestore.transactional
        def update(transaction: Any) -> None:
            key_snapshot = key_ref.get(transaction=transaction)
            usage_snapshot = usage_ref.get(transaction=transaction)
            if not key_snapshot.exists or not usage_snapshot.exists:
                return
            item = key_snapshot.to_dict() or {}
            event = usage_snapshot.to_dict() or {}
            if event.get("status") != "reserved":
                return
            now = datetime.now(UTC)
            reservations = _active_reservations(item.get("reservations"), now)
            reservations.pop(billing_id, None)
            reserved = sum(entry["amount_microusd"] for entry in reservations.values())
            key_update: dict[str, Any] = {
                "reservations": reservations,
                "spend_reserved_microusd": reserved,
            }
            event_update: dict[str, Any] = {"settled_at": now}
            if actual_microusd is None:
                event_update.update({"status": "released", "reason": release_reason})
            else:
                actual = max(0, actual_microusd)
                key_update["spend_used_microusd"] = int(item.get("spend_used_microusd", 0)) + actual
                event_update.update({"status": "settled", "actual_microusd": actual, **usage})
            transaction.update(key_ref, key_update)
            transaction.update(usage_ref, event_update)

        update(transaction)

    async def list_usage(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        def fetch() -> list[dict[str, Any]]:
            return [
                dict(snapshot.to_dict() or {}, id=snapshot.id)
                for snapshot in self.usage.where("user_id", "==", user_id).stream()
            ]

        rows = await asyncio.to_thread(fetch)
        rows.sort(
            key=lambda row: row.get("created_at", datetime.min.replace(tzinfo=UTC)),
            reverse=True,
        )
        return [_public_usage(row) for row in rows[:limit]]


def build_managed_repository() -> ManagedKeyRepository | None:
    if os.getenv("ENABLE_MANAGED_KEYS", "false").lower() not in {"1", "true", "yes"}:
        return None
    return FirestoreKeyRepository(
        project=os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT"),
        prefix=os.getenv("FIRESTORE_COLLECTION_PREFIX", "sovereign"),
    )


def _new_key() -> tuple[str, str]:
    key_id = f"key_{secrets.token_urlsafe(12)}"
    return key_id, f"sk-os-{key_id}.{secrets.token_urlsafe(32)}"


def _parse_key(raw_key: str) -> tuple[str, str] | None:
    match = KEY_PATTERN.fullmatch(raw_key)
    return (match.group(1), match.group(2)) if match else None


def _display_prefix(raw_key: str) -> str:
    return f"{raw_key[:22]}…"


def _record_is_usable(item: dict[str, Any]) -> bool:
    if item.get("revoked_at"):
        return False
    expires_at = item.get("expires_at")
    if expires_at and _as_utc(expires_at) <= datetime.now(UTC):
        return False
    limit = item.get("spend_limit_microusd")
    if limit is not None and int(item.get("spend_used_microusd", 0)) >= int(limit):
        return False
    return True


def _to_record(key_id: str, item: dict[str, Any]) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=key_id,
        user_id=str(item["user_id"]),
        name=str(item["name"]),
        key_prefix=str(item["key_prefix"]),
        created_at=_as_utc(item["created_at"]),
        expires_at=_as_utc(item["expires_at"]) if item.get("expires_at") else None,
        spend_limit_microusd=(
            int(item["spend_limit_microusd"])
            if item.get("spend_limit_microusd") is not None
            else None
        ),
        spend_used_microusd=int(item.get("spend_used_microusd", 0)),
        spend_reserved_microusd=int(item.get("spend_reserved_microusd", 0)),
        revoked_at=_as_utc(item["revoked_at"]) if item.get("revoked_at") else None,
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _public_usage(row: dict[str, Any]) -> dict[str, Any]:
    public = dict(row)
    expires_at = public.get("reservation_expires_at")
    if (
        public.get("status") == "reserved"
        and isinstance(expires_at, datetime)
        and _as_utc(expires_at) <= datetime.now(UTC)
    ):
        public["status"] = "expired"
    for key in ("created_at", "settled_at", "reservation_expires_at"):
        if isinstance(public.get(key), datetime):
            public[key] = _as_utc(public[key]).isoformat()
    return public


def _active_reservations(value: Any, now: datetime) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    active: dict[str, dict[str, Any]] = {}
    for billing_id, raw in value.items():
        if not isinstance(raw, dict) or not isinstance(raw.get("expires_at"), datetime):
            continue
        if _as_utc(raw["expires_at"]) <= now:
            continue
        active[str(billing_id)] = {
            "amount_microusd": max(0, int(raw.get("amount_microusd", 0))),
            "expires_at": _as_utc(raw["expires_at"]),
        }
    return active
