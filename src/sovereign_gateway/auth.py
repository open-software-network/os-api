from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    key_fingerprint: str
    key_id: str | None = None
    user_id: str | None = None


class ApiKeyStore:
    """Validates keys against tenant_id=sha256(key) entries without storing plaintext."""

    def __init__(self, entries: dict[str, str]) -> None:
        self._entries = entries

    @classmethod
    def from_environment(cls, *, allow_empty: bool = False) -> ApiKeyStore:
        raw = os.getenv("API_KEY_HASHES", "")
        entries: dict[str, str] = {}
        for entry in filter(None, (part.strip() for part in raw.split(","))):
            try:
                tenant_id, digest = entry.split("=", 1)
            except ValueError as exc:
                raise ValueError("API_KEY_HASHES entries must be tenant_id=sha256") from exc
            if len(digest) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in digest):
                raise ValueError(f"invalid SHA-256 digest for tenant {tenant_id!r}")
            entries[tenant_id] = digest.lower()
        if not entries and not allow_empty:
            raise ValueError("API_KEY_HASHES must configure at least one API key")
        return cls(entries)

    def authenticate(self, raw_key: str) -> Principal | None:
        candidate = hashlib.sha256(raw_key.encode()).hexdigest()
        for tenant_id, expected in self._entries.items():
            if hmac.compare_digest(candidate, expected):
                return Principal(tenant_id=tenant_id, key_fingerprint=candidate[:12])
        return None


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()
