from __future__ import annotations

import anyio
import pytest
from fastapi.testclient import TestClient

from sovereign_gateway.app import create_app
from sovereign_gateway.rate_limit import LocalConcurrencyLimiter


class RecordingLimiter:
    def __init__(self, admit: bool) -> None:
        self.admit = admit
        self.acquires = 0
        self.releases = 0

    async def acquire(self, backend: str, limit: int) -> bool:
        self.acquires += 1
        return self.admit

    async def release(self, backend: str) -> None:
        self.releases += 1


def test_local_concurrency_limiter_enforces_and_recovers() -> None:
    async def scenario() -> tuple[bool, bool, bool]:
        limiter = LocalConcurrencyLimiter()
        first = await limiter.acquire("backend", 1)
        second = await limiter.acquire("backend", 1)
        await limiter.release("backend")
        third = await limiter.acquire("backend", 1)
        return first, second, third

    first, second, third = anyio.run(scenario)
    assert first is True
    assert second is False
    assert third is True


def test_saturated_backend_returns_503_with_retry_after(client: TestClient) -> None:
    limiter = RecordingLimiter(admit=False)
    client.app.state.concurrency = limiter

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Confidential-Compute": "required"},
        json={"model": "dual-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "10"
    assert response.json()["error"]["code"] == "model_overloaded"
    assert limiter.acquires == 1
    assert limiter.releases == 0


def test_admitted_request_releases_capacity(client: TestClient) -> None:
    limiter = RecordingLimiter(admit=True)
    client.app.state.concurrency = limiter

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Confidential-Compute": "required"},
        json={"model": "dual-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert limiter.acquires == 1
    assert limiter.releases == 1


def test_streaming_request_releases_capacity_after_stream(client: TestClient) -> None:
    limiter = RecordingLimiter(admit=True)
    client.app.state.concurrency = limiter

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"X-Confidential-Compute": "required"},
        json={"model": "dual-model", "stream": True, "messages": []},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "[DONE]" in body
    assert limiter.acquires == 1
    assert limiter.releases == 1


class FailingLimiter:
    async def acquire(self, backend: str, limit: int) -> bool:
        raise ConnectionError("redis down")

    async def release(self, backend: str) -> None:
        raise ConnectionError("redis down")

    async def allow(self, principal: str) -> tuple[bool, int]:
        raise ConnectionError("redis down")


def test_redis_loss_serves_via_bounded_fallbacks(client: TestClient) -> None:
    failing = FailingLimiter()
    client.app.state.service_limiter = failing
    client.app.state.concurrency = failing

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Confidential-Compute": "required"},
        json={"model": "dual-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert response.headers["x-ratelimit-limit-requests"] == "40"
    assert response.headers["x-ratelimit-remaining-requests"] == "39"


def test_redis_loss_rate_limit_fallback_caps_requests(
    client: TestClient, frozen_limiter_clock: None
) -> None:
    client.app.state.service_limiter = FailingLimiter()

    # The per-process fallback window allows 2x the configured 20 RPM.
    for _ in range(40):
        response = client.get("/v1/models")
        assert response.status_code == 200

    response = client.get("/v1/models")

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limit_exceeded"


def test_redis_loss_still_throttles_failed_auth(
    client: TestClient, frozen_limiter_clock: None
) -> None:
    # A Redis outage must not drop the failed-auth throttle to fully open: it
    # falls back to the same bounded per-process window (2x the 20 RPM = 40).
    # Rotating keys means only the per-source IP bucket can catch the scan.
    client.app.state.limiter = FailingLimiter()

    for attempt in range(40):
        response = client.get(
            "/v1/models", headers={"Authorization": f"Bearer sk-candidate-{attempt}"}
        )
        assert response.status_code == 401

    response = client.get("/v1/models", headers={"Authorization": "Bearer sk-candidate-final"})

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limit_exceeded"


def test_redis_loss_admission_fallback_caps_concurrency(client: TestClient) -> None:
    client.app.state.concurrency = FailingLimiter()

    async def saturate_fallback() -> None:
        # tee:dual-model configures max_concurrency: 4 in the test catalog.
        for _ in range(4):
            assert await client.app.state.concurrency_fallback.acquire("tee:dual-model", 4)

    anyio.run(saturate_fallback)

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Confidential-Compute": "required"},
        json={"model": "dual-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "10"
    assert response.json()["error"]["code"] == "model_overloaded"


def test_unlimited_backend_skips_admission_control(client: TestClient) -> None:
    limiter = RecordingLimiter(admit=False)
    client.app.state.concurrency = limiter

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Confidential-Compute": "disabled"},
        json={"model": "standard-only", "messages": []},
    )

    assert response.status_code == 200
    assert limiter.acquires == 0


def test_redis_service_counters_do_not_share_managed_user_namespace() -> None:
    from sovereign_gateway.rate_limit import RedisFixedWindowLimiter

    class RedisCounter:
        def __init__(self) -> None:
            self.counts: dict[str, int] = {}

        def pipeline(self, transaction: bool):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def incr(self, key: str) -> None:
            self.key = key
            self.counts[key] = self.counts.get(key, 0) + 1

        def expire(self, key: str, seconds: int) -> None:
            pass

        async def execute(self):
            return self.counts[self.key], True

    async def scenario() -> None:
        redis = RedisCounter()
        regular = RedisFixedWindowLimiter(redis, 1)
        service = RedisFixedWindowLimiter(redis, 2, namespace="service-ratelimit")
        assert await regular.allow("same-tenant") == (True, 0)
        assert await regular.allow("same-tenant") == (False, 0)
        assert await service.allow("same-tenant") == (True, 1)
        assert await service.allow("same-tenant") == (True, 0)
        assert await service.allow("same-tenant") == (False, 0)
        assert len(redis.counts) == 2

    anyio.run(scenario)


def test_service_redis_fallback_is_independent_from_managed_user_fallback(
    client: TestClient, frozen_limiter_clock: None
) -> None:
    from sovereign_gateway.auth import Principal

    class ManagedKeys:
        async def authenticate(self, key: str) -> Principal | None:
            return Principal("test-tenant", "managed", key_id="managed-key")

    client.app.state.managed_keys = ManagedKeys()
    client.app.state.limiter = FailingLimiter()
    client.app.state.service_limiter = FailingLimiter()
    for _ in range(40):
        assert client.get("/v1/models").status_code == 200
    response = client.get("/v1/models")
    assert response.status_code == 429
    assert response.headers["x-ratelimit-limit-requests"] == "40"
    assert response.headers["x-ratelimit-remaining-requests"] == "0"

    headers = {"Authorization": "Bearer sk-managed-test"}
    for _ in range(40):
        assert client.get("/v1/models", headers=headers).status_code == 200
    response = client.get("/v1/models", headers=headers)
    assert response.status_code == 429
    assert response.headers["x-ratelimit-limit-requests"] == "40"
    assert response.headers["x-ratelimit-remaining-requests"] == "0"


def test_service_budget_can_be_configured_without_changing_regular_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, frozen_limiter_clock: None
) -> None:
    monkeypatch.setenv("SERVICE_REQUESTS_PER_MINUTE", "2")
    with TestClient(create_app()) as configured:
        configured.headers.update(client.headers)
        for _ in range(2):
            response = configured.get("/v1/models")
            assert response.status_code == 200
        response = configured.get("/v1/models")
        assert response.status_code == 429
        assert response.headers["x-ratelimit-limit-requests"] == "2"
        assert configured.app.state.limiter.limit == 20


@pytest.mark.parametrize("budget", ["0", "-1", "unlimited"])
def test_service_budget_rejects_invalid_configuration(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    monkeypatch.setenv("SERVICE_REQUESTS_PER_MINUTE", budget)
    with pytest.raises(ValueError), TestClient(create_app()):
        pass
