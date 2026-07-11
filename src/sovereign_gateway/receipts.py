from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Any, Protocol

from google.cloud import firestore


class PrivacyReceiptRepository(Protocol):
    async def save(self, tenant_id: str, generation_id: str, receipt: dict[str, Any]) -> None: ...

    async def get(self, tenant_id: str, generation_id: str) -> dict[str, Any] | None: ...


class InMemoryPrivacyReceiptRepository:
    def __init__(self, limit: int = 10_000) -> None:
        self.limit = limit
        self.records: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def save(self, tenant_id: str, generation_id: str, receipt: dict[str, Any]) -> None:
        async with self._lock:
            if len(self.records) >= self.limit:
                self.records.pop(next(iter(self.records)))
            self.records[_key(tenant_id, generation_id)] = dict(receipt)

    async def get(self, tenant_id: str, generation_id: str) -> dict[str, Any] | None:
        async with self._lock:
            value = self.records.get(_key(tenant_id, generation_id))
            return dict(value) if value else None


class FirestorePrivacyReceiptRepository:
    def __init__(self, project: str | None, prefix: str, database: str | None) -> None:
        self.client = firestore.Client(project=project or None, database=database or None)
        self.collection = self.client.collection(f"{prefix}_privacy_receipts")

    async def save(self, tenant_id: str, generation_id: str, receipt: dict[str, Any]) -> None:
        payload = {**receipt, "tenant_id": tenant_id}
        await asyncio.to_thread(
            self.collection.document(_key(tenant_id, generation_id)).set, payload
        )

    async def get(self, tenant_id: str, generation_id: str) -> dict[str, Any] | None:
        snapshot = await asyncio.to_thread(
            self.collection.document(_key(tenant_id, generation_id)).get
        )
        if not snapshot.exists:
            return None
        value = snapshot.to_dict() or {}
        if value.get("tenant_id") != tenant_id:
            return None
        value.pop("tenant_id", None)
        return value


def build_receipt_repository() -> PrivacyReceiptRepository:
    if os.getenv("ENABLE_MANAGED_KEYS", "false").lower() in {"1", "true", "yes"}:
        return FirestorePrivacyReceiptRepository(
            project=os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT"),
            prefix=os.getenv("FIRESTORE_COLLECTION_PREFIX", "sovereign"),
            database=os.getenv("FIRESTORE_DATABASE_ID") or None,
        )
    return InMemoryPrivacyReceiptRepository(
        limit=int(os.getenv("PRIVACY_RECEIPT_CACHE_SIZE", "10000"))
    )


def _key(tenant_id: str, generation_id: str) -> str:
    # Firestore document IDs cannot contain slashes. Both values are retained
    # in the document and access remains tenant-scoped.
    return hashlib.sha256(f"{tenant_id}\0{generation_id}".encode()).hexdigest()
