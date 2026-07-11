from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .config import Endpoint, Provider


@dataclass(frozen=True)
class PrivacyEvidence:
    status: str
    evidence_type: str
    receipt_id: str | None = None
    workload_identity: str | None = None
    keyset_digest: str | None = None
    raw: dict[str, Any] | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "type": self.evidence_type,
            "receipt_id": self.receipt_id,
            "workload_identity": self.workload_identity,
            "keyset_digest": self.keyset_digest,
        }


class ProviderAdapter(Protocol):
    def build_request(
        self,
        http: httpx.AsyncClient,
        provider: Provider,
        endpoint: Endpoint,
        proxy_path: str,
        payload: dict[str, Any],
        request_id: str,
        client_headers: Mapping[str, str],
    ) -> httpx.Request: ...

    def evidence(self, response: httpx.Response, endpoint: Endpoint) -> PrivacyEvidence: ...


class OpenAIAdapter:
    def build_request(
        self,
        http: httpx.AsyncClient,
        provider: Provider,
        endpoint: Endpoint,
        proxy_path: str,
        payload: dict[str, Any],
        request_id: str,
        client_headers: Mapping[str, str],
    ) -> httpx.Request:
        outbound = dict(payload)
        outbound.pop("privacy", None)
        outbound.pop("provider", None)
        outbound.pop("models", None)
        outbound["model"] = endpoint.upstream_model
        headers = {"content-type": "application/json", "x-request-id": request_id}
        if provider.api_key:
            headers["authorization"] = f"Bearer {provider.api_key}"
        for name in (
            "x-venice-tee-client-pub-key",
            "x-venice-tee-model-pub-key",
            "x-venice-tee-signing-algo",
        ):
            if value := client_headers.get(name):
                headers[name] = value
        return http.build_request(
            "POST",
            f"{provider.base_url}/v1/{proxy_path}",
            headers=headers,
            content=json.dumps(outbound, separators=(",", ":")),
        )

    def evidence(self, response: httpx.Response, endpoint: Endpoint) -> PrivacyEvidence:
        return PrivacyEvidence(
            status="unverified" if endpoint.privacy.attestation != "none" else "not-applicable",
            evidence_type=endpoint.privacy.attestation,
        )


class PhalaAdapter(OpenAIAdapter):
    def evidence(self, response: httpx.Response, endpoint: Endpoint) -> PrivacyEvidence:
        receipt_id = response.headers.get("x-receipt-id")
        workload = response.headers.get("x-aci-identity")
        keyset = response.headers.get("x-aci-keyset-digest")
        return PrivacyEvidence(
            status="present" if receipt_id and workload and keyset else "missing",
            evidence_type="phala-aci-receipt",
            receipt_id=receipt_id,
            workload_identity=workload,
            keyset_digest=keyset,
        )


class VeniceAdapter(OpenAIAdapter):
    def evidence(self, response: httpx.Response, endpoint: Endpoint) -> PrivacyEvidence:
        signature = response.headers.get("x-venice-tee-signature")
        request_id = response.headers.get("x-request-id") or response.headers.get(
            "x-venice-request-id"
        )
        return PrivacyEvidence(
            status="present" if signature else "missing",
            evidence_type="venice-tee-signature",
            receipt_id=request_id,
            raw={"signature": signature} if signature else None,
        )


ADAPTERS: dict[str, ProviderAdapter] = {
    "openai": OpenAIAdapter(),
    "phala": PhalaAdapter(),
    "venice": VeniceAdapter(),
}


def adapter_for(provider: Provider) -> ProviderAdapter:
    try:
        return ADAPTERS[provider.adapter]
    except KeyError as exc:
        raise ValueError(f"unsupported provider adapter {provider.adapter!r}") from exc
