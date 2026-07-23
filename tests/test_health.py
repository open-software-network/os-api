from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from sovereign_gateway.accounts import AccountUser


class AccountsSessionStub:
    def __init__(self, user_id: str) -> None:
        self.user = AccountUser(user_id, "operator", "ops@example.com", "Operator", None)

    async def me(self, access_token: str) -> tuple[AccountUser | None, int | None]:
        return (self.user, None) if access_token == "session-token" else (None, 3001)

    async def balance(self, access_token: str) -> dict[str, Any] | None:
        raise AssertionError("The health dashboard must not fetch wallet balance")


def _sign_in(client: TestClient, user_id: str) -> None:
    client.app.state.accounts = AccountsSessionStub(user_id)
    client.cookies.set("osapi_access", "session-token")


def test_health_dashboard_requires_os_accounts_login(client: TestClient) -> None:
    response = client.get("/health", follow_redirects=False)
    api = client.get("/api/health")

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/start?next=/health"
    assert api.status_code == 401


def test_health_dashboard_bypasses_site_basic_auth(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("SITE_BASIC_AUTH_USERNAME", "private-site")
    monkeypatch.setenv("SITE_BASIC_AUTH_PASSWORD", "private-password")
    monkeypatch.setattr(client.app.state.accounts, "client_id", "client-id")

    page = client.get("/health", follow_redirects=False)
    api = client.get("/api/health")
    login = client.get("/auth/start?next=/health", follow_redirects=False)
    callback = client.get("/auth/callback", follow_redirects=False)
    stylesheet = client.get("/static/app.css")
    script = client.get("/static/health.js")
    home = client.get("/")

    assert page.status_code == 303
    assert page.headers["location"] == "/auth/start?next=/health"
    assert api.status_code == 401
    assert api.json() == {"error": "authentication_required"}
    assert login.status_code == 303
    assert login.headers["location"].startswith("https://accounts.opensoftware.co/login?")
    assert callback.status_code == 303
    assert stylesheet.status_code == 200
    assert script.status_code == 200
    assert home.status_code == 401
    assert home.headers["www-authenticate"].startswith("Basic ")


def test_health_dashboard_fails_closed_for_non_allowlisted_user(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("HEALTH_DASHBOARD_AUTHORIZED_USER_IDS", "usr_allowed")
    _sign_in(client, "usr_other")

    page = client.get("/health")
    api = client.get("/api/health")

    assert page.status_code == 403
    assert "not authorized" in page.text
    assert api.status_code == 403
    assert api.json() == {"error": "forbidden"}


def test_authorized_operator_can_view_health_snapshot(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(
        "HEALTH_DASHBOARD_AUTHORIZED_USER_IDS",
        "usr_other, usr_operator ,usr_third",
    )
    _sign_in(client, "usr_operator")

    page = client.get("/health")
    client.get("/v1/models")
    api = client.get("/api/health")

    assert page.status_code == 200
    assert "Service health" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert api.status_code == 200
    snapshot = api.json()
    assert snapshot["status"] == "operational"
    assert snapshot["service"]["name"] == "os-api"
    assert snapshot["requests"]["last_5_minutes"]["requests"] == 1
    assert snapshot["requests"]["last_5_minutes"]["operations"] == {"models": 1}
    assert snapshot["requests"]["series"][-1]["requests"] == 1
    assert {component["id"] for component in snapshot["readiness"]["components"]} == {
        "gateway",
        "provider_credentials",
    }


def test_health_snapshot_reports_optional_redis_degradation(
    client: TestClient, monkeypatch
) -> None:
    class DownRedis:
        async def ping(self) -> None:
            raise ConnectionError("redis down")

    monkeypatch.setenv("HEALTH_DASHBOARD_AUTHORIZED_USER_IDS", "usr_operator")
    _sign_in(client, "usr_operator")
    client.app.state.redis = DownRedis()

    response = client.get("/api/health")

    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["status"] == "degraded"
    assert snapshot["readiness"]["status"] == "ready"
    redis = next(
        component
        for component in snapshot["readiness"]["components"]
        if component["id"] == "redis"
    )
    assert redis["status"] == "degraded"


def test_login_return_path_is_restricted_to_known_internal_pages(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(client.app.state.accounts, "client_id", "client-id")

    allowed = client.get("/auth/start?next=/health", follow_redirects=False)
    rejected = client.get("/auth/start?next=https://evil.example", follow_redirects=False)

    assert allowed.status_code == 303
    assert 'osapi_return_to="/health"' in allowed.headers["set-cookie"]
    assert parse_qs(urlparse(allowed.headers["location"]).query)["scope"] == ["profile:read"]
    assert "osapi_return_to=https" not in rejected.headers["set-cookie"]
