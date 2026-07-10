from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sovereign_gateway.managed_keys import InMemoryKeyRepository, SpendLimitExceeded


@pytest.mark.asyncio
async def test_managed_key_lifecycle_and_usage() -> None:
    repository = InMemoryKeyRepository()
    created = await repository.create_key(
        "usr_test",
        "Test app",
        datetime.now(UTC) + timedelta(days=30),
        10_000,
    )

    principal = await repository.authenticate(created.secret)
    assert principal is not None
    assert principal.user_id == "usr_test"
    assert created.record.public_dict()["prefix"] != created.secret

    await repository.reserve(created.record.id, "bill_1", 4_000)
    await repository.settle(
        created.record.id,
        "bill_1",
        2_500,
        {"model": "test-model", "total_tokens": 12},
    )
    records = await repository.list_keys("usr_test")
    assert records[0].spend_used_microusd == 2_500
    assert records[0].spend_reserved_microusd == 0
    usage = await repository.list_usage("usr_test")
    assert usage[0]["status"] == "settled"
    assert usage[0]["total_tokens"] == 12

    assert await repository.revoke_key("usr_test", created.record.id)
    assert await repository.authenticate(created.secret) is None


@pytest.mark.asyncio
async def test_spend_limit_reservation_is_atomic() -> None:
    repository = InMemoryKeyRepository()
    created = await repository.create_key("usr_test", "Capped", None, 1_000)

    await repository.reserve(created.record.id, "bill_1", 800)
    with pytest.raises(SpendLimitExceeded):
        await repository.reserve(created.record.id, "bill_2", 300)
    await repository.release(created.record.id, "bill_1", "test")
    await repository.reserve(created.record.id, "bill_3", 1_000)
