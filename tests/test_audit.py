from __future__ import annotations

import csv
import io
import json

import anyio
from fastapi.testclient import TestClient

from sovereign_gateway.accounts import AccountUser
from sovereign_gateway.managed_keys import InMemoryKeyRepository
from sovereign_gateway.web import _audit_csv


def test_audit_csv_has_stable_columns_and_escapes_formulas() -> None:
    content = _audit_csv(
        [
            {
                "id": "audit_1",
                "event_type": "api_key.created",
                "key_name": "=IMPORTXML('https://example.invalid')",
                "actual_microusd": 25,
            }
        ]
    )

    rows = list(csv.DictReader(io.StringIO(content)))
    assert rows[0]["id"] == "audit_1"
    assert rows[0]["event_type"] == "api_key.created"
    assert rows[0]["key_name"].startswith("'=")
    assert rows[0]["actual_microusd"] == "25"


def test_audit_routes_are_tenant_isolated(client: TestClient, monkeypatch) -> None:
    repository = InMemoryKeyRepository()
    secrets: list[str] = []

    async def seed() -> None:
        first = await repository.create_key("usr_a", "Tenant A", None, 1_000_000)
        second = await repository.create_key("usr_b", "Tenant B", None, 1_000_000)
        secrets.extend([first.secret, second.secret])

    async def me(access_token: str):
        assert access_token == "session-a"
        return AccountUser("usr_a", "a", "a@example.com", "A", None), None

    async def balance(access_token: str):
        assert access_token == "session-a"
        return {"available_credits": 10}

    anyio.run(seed)
    client.app.state.managed_keys = repository
    monkeypatch.setattr(client.app.state.accounts, "me", me)
    monkeypatch.setattr(client.app.state.accounts, "balance", balance)
    client.cookies.set("osapi_access", "session-a")

    response = client.get("/api/audit?limit=100", headers={"Authorization": ""})
    exported = client.get("/api/audit/export?format=jsonl", headers={"Authorization": ""})

    assert response.status_code == 200
    assert [event["key_name"] for event in response.json()["data"]] == ["Tenant A"]
    exported_events = [json.loads(line) for line in exported.text.splitlines()]
    assert [event["key_name"] for event in exported_events] == ["Tenant A"]
    assert all(secret not in response.text + exported.text for secret in secrets)


def test_key_api_paginates_without_hiding_older_keys(client: TestClient, monkeypatch) -> None:
    repository = InMemoryKeyRepository()
    created = []

    async def seed() -> None:
        for index in range(3):
            created.append(await repository.create_key("usr_a", f"Tenant A {index}", None, None))
        created.append(await repository.create_key("usr_b", "Tenant B", None, None))

    async def me(access_token: str):
        assert access_token == "session-a"
        return AccountUser("usr_a", "a", "a@example.com", "A", None), None

    async def balance(access_token: str):
        assert access_token == "session-a"
        return {"available_credits": 10}

    anyio.run(seed)
    client.app.state.managed_keys = repository
    monkeypatch.setattr(client.app.state.accounts, "me", me)
    monkeypatch.setattr(client.app.state.accounts, "balance", balance)
    client.cookies.set("osapi_access", "session-a")

    first = client.get("/api/keys?limit=2", headers={"Authorization": ""})
    first_body = first.json()
    second = client.get(
        "/api/keys",
        params={"limit": 2, "cursor": first_body["next_cursor"]},
        headers={"Authorization": ""},
    )
    cross_tenant = client.get(
        "/api/keys",
        params={"cursor": created[-1].record.id},
        headers={"Authorization": ""},
    )

    assert first.status_code == 200
    assert [record["name"] for record in first_body["data"]] == ["Tenant A 2", "Tenant A 1"]
    assert first_body["next_cursor"] == first_body["data"][-1]["id"]
    assert second.status_code == 200
    assert [record["name"] for record in second.json()["data"]] == ["Tenant A 0"]
    assert second.json()["next_cursor"] is None
    assert cross_tenant.status_code == 400
    assert cross_tenant.json() == {"error": "invalid_key_cursor"}
