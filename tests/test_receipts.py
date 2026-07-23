from __future__ import annotations

from sovereign_gateway.receipts import InMemoryPrivacyReceiptRepository


async def test_privacy_receipts_are_tenant_scoped() -> None:
    repository = InMemoryPrivacyReceiptRepository()
    receipt = {"generation_id": "gen-1", "provider": "phala"}

    await repository.save("tenant-a", "gen-1", receipt)

    assert await repository.get("tenant-a", "gen-1") == receipt
    assert await repository.get("tenant-b", "gen-1") is None


async def test_privacy_receipt_cache_is_bounded() -> None:
    repository = InMemoryPrivacyReceiptRepository(limit=1)
    await repository.save("tenant", "first", {"generation_id": "first"})
    await repository.save("tenant", "second", {"generation_id": "second"})

    assert await repository.get("tenant", "first") is None
    assert await repository.get("tenant", "second") is not None
