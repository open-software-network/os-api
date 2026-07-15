from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sovereign_gateway.managed_keys import (
    FirestoreKeyRepository,
    InMemoryKeyRepository,
    SpendLimitExceeded,
)


class RecordingQuery:
    def __init__(
        self,
        rows: list[tuple[str, dict[str, object]]],
        documents: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.rows = rows
        self.documents = documents or {}
        self.calls: list[tuple[object, ...]] = []

    def where(self, field: str, operator: str, value: str) -> RecordingQuery:
        self.calls.append(("where", field, operator, value))
        return self

    def order_by(self, field: str, *, direction: str) -> RecordingQuery:
        self.calls.append(("order_by", field, direction))
        return self

    def limit(self, value: int) -> RecordingQuery:
        self.calls.append(("limit", value))
        return self

    def start_after(self, snapshot: RecordingSnapshot) -> RecordingQuery:
        self.calls.append(("start_after", snapshot.id))
        return self

    def document(self, row_id: str) -> RecordingDocument:
        self.calls.append(("document", row_id))
        return RecordingDocument(self, row_id)

    def stream(self):
        self.calls.append(("stream",))
        for row_id, row in self.rows:
            yield RecordingSnapshot(row_id, row)


class RecordingSnapshot:
    def __init__(self, row_id: str, row: dict[str, object]) -> None:
        self.id = row_id
        self.row = row
        self.exists = True

    def to_dict(self) -> dict[str, object]:
        return self.row


class RecordingDocument:
    def __init__(self, query: RecordingQuery, row_id: str) -> None:
        self.query = query
        self.row_id = row_id

    def get(self) -> RecordingSnapshot:
        self.query.calls.append(("get", self.row_id))
        return RecordingSnapshot(self.row_id, self.query.documents[self.row_id])


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
        {
            "request_id": "req_test",
            "model": "test-model",
            "privacy": "confidential",
            "backend": "tee",
            "total_tokens": 12,
        },
    )
    key_page = await repository.list_keys("usr_test")
    assert key_page.records[0].spend_used_microusd == 2_500
    assert key_page.records[0].spend_reserved_microusd == 0
    usage = await repository.list_usage("usr_test")
    assert usage[0]["status"] == "settled"
    assert usage[0]["total_tokens"] == 12

    assert await repository.revoke_key("usr_test", created.record.id)
    assert await repository.authenticate(created.secret) is None
    audit = await repository.list_audit_events("usr_test")
    assert [event["event_type"] for event in audit] == [
        "api_key.revoked",
        "inference.settled",
        "api_key.created",
    ]
    settled = audit[1]
    assert settled["request_id"] == "req_test"
    assert settled["privacy"] == "confidential"
    assert settled["backend"] == "tee"
    assert settled["total_tokens"] == 12
    assert created.secret not in repr(audit)


@pytest.mark.asyncio
async def test_spend_limit_reservation_is_atomic() -> None:
    repository = InMemoryKeyRepository()
    created = await repository.create_key("usr_test", "Capped", None, 1_000)

    await repository.reserve(created.record.id, "bill_1", 800)
    with pytest.raises(SpendLimitExceeded):
        await repository.reserve(created.record.id, "bill_2", 300)
    await repository.release(created.record.id, "bill_1", "test")
    audit = await repository.list_audit_events("usr_test")
    assert audit[0]["event_type"] == "inference.released"
    assert audit[0]["reason"] == "test"
    await repository.reserve(created.record.id, "bill_3", 1_000)


@pytest.mark.asyncio
async def test_settlement_cannot_exceed_reserved_hard_limit() -> None:
    repository = InMemoryKeyRepository()
    created = await repository.create_key("usr_test", "Capped", None, 800)

    await repository.reserve(created.record.id, "bill_1", 800)
    await repository.settle(created.record.id, "bill_1", 1_200, {"total_tokens": 10})

    key_page = await repository.list_keys("usr_test")
    usage = await repository.list_usage("usr_test")
    assert key_page.records[0].spend_used_microusd == 800
    assert key_page.records[0].spend_reserved_microusd == 0
    assert usage[0]["actual_microusd"] == 800


@pytest.mark.asyncio
async def test_in_memory_key_pagination_reaches_every_key() -> None:
    repository = InMemoryKeyRepository()
    created = [
        await repository.create_key("usr_test", f"Key {index}", None, None) for index in range(5)
    ]
    await repository.create_key("other_user", "Other tenant", None, None)

    first = await repository.list_keys("usr_test", limit=2)
    second = await repository.list_keys("usr_test", limit=2, cursor=first.next_cursor)
    third = await repository.list_keys("usr_test", limit=2, cursor=second.next_cursor)

    assert [record.id for record in (*first.records, *second.records, *third.records)] == [
        item.record.id for item in reversed(created)
    ]
    assert first.next_cursor == first.records[-1].id
    assert second.next_cursor == second.records[-1].id
    assert third.next_cursor is None


@pytest.mark.asyncio
async def test_firestore_lists_order_and_limit_in_the_query() -> None:
    now = datetime.now(UTC)
    keys = RecordingQuery(
        [
            (
                "key_newest",
                {
                    "user_id": "usr_test",
                    "name": "Newest",
                    "key_prefix": "sk-os-key_newest…",
                    "created_at": now,
                    "expires_at": None,
                    "spend_limit_microusd": None,
                    "spend_used_microusd": 0,
                    "spend_reserved_microusd": 0,
                    "revoked_at": None,
                },
            )
        ]
    )
    usage = RecordingQuery(
        [("bill_newest", {"user_id": "usr_test", "created_at": now, "status": "settled"})]
    )
    audit = RecordingQuery(
        [("audit_newest", {"user_id": "usr_test", "occurred_at": now, "event_type": "test"})]
    )
    repository = object.__new__(FirestoreKeyRepository)
    repository.keys = keys
    repository.usage = usage
    repository.audit = audit

    key_page = await repository.list_keys("usr_test")
    usage_rows = await repository.list_usage("usr_test", 7)
    audit_rows = await repository.list_audit_events("usr_test", 11)

    assert [record.id for record in key_page.records] == ["key_newest"]
    assert key_page.next_cursor is None
    assert [row["id"] for row in usage_rows] == ["bill_newest"]
    assert [row["id"] for row in audit_rows] == ["audit_newest"]
    assert keys.calls == [
        ("where", "user_id", "==", "usr_test"),
        ("order_by", "created_at", "DESCENDING"),
        ("limit", 101),
        ("stream",),
    ]
    assert usage.calls == [
        ("where", "user_id", "==", "usr_test"),
        ("order_by", "created_at", "DESCENDING"),
        ("limit", 7),
        ("stream",),
    ]
    assert audit.calls == [
        ("where", "user_id", "==", "usr_test"),
        ("order_by", "occurred_at", "DESCENDING"),
        ("limit", 11),
        ("stream",),
    ]


@pytest.mark.asyncio
async def test_firestore_key_page_starts_after_tenant_validated_cursor() -> None:
    now = datetime.now(UTC)

    def key_row(name: str) -> dict[str, object]:
        return {
            "user_id": "usr_test",
            "name": name,
            "key_prefix": f"sk-os-{name}…",
            "created_at": now,
            "expires_at": None,
            "spend_limit_microusd": None,
            "spend_used_microusd": 0,
            "spend_reserved_microusd": 0,
            "revoked_at": None,
        }

    keys = RecordingQuery(
        [("key_older0000000000", key_row("Older"))],
        documents={"key_cursor000000000": key_row("Cursor")},
    )
    repository = object.__new__(FirestoreKeyRepository)
    repository.keys = keys

    page = await repository.list_keys(
        "usr_test",
        limit=1,
        cursor="key_cursor000000000",
    )

    assert [record.id for record in page.records] == ["key_older0000000000"]
    assert page.next_cursor is None
    assert keys.calls == [
        ("where", "user_id", "==", "usr_test"),
        ("order_by", "created_at", "DESCENDING"),
        ("document", "key_cursor000000000"),
        ("get", "key_cursor000000000"),
        ("start_after", "key_cursor000000000"),
        ("limit", 2),
        ("stream",),
    ]
