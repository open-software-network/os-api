from __future__ import annotations

import anyio
from fastapi.testclient import TestClient

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
    client.app.state.limiter = failing
    client.app.state.concurrency = failing

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Confidential-Compute": "required"},
        json={"model": "dual-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert "x-ratelimit-remaining-requests" not in response.headers


def test_redis_loss_rate_limit_fallback_caps_requests(client: TestClient) -> None:
    client.app.state.limiter = FailingLimiter()

    # The per-process fallback window allows 2x the configured 20 RPM.
    for _ in range(40):
        response = client.get("/v1/models")
        assert response.status_code == 200

    response = client.get("/v1/models")

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
