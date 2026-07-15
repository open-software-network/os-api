from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

DEFAULT_SOCKET_PATH = "/run/container_launcher/teeserver.sock"
DEFAULT_AUDIENCE = "https://june-api.opensoftware.co/os-api-gateway"


class GatewayAttestationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class GatewayAttestation:
    token: str
    audience: str
    nonce: str

    def public_dict(self) -> dict[str, str]:
        return {
            "object": "gateway.attestation",
            "format": "confidential-space.oidc",
            "token": self.token,
            "audience": self.audience,
            "nonce": self.nonce,
        }


async def request_gateway_attestation(
    nonce: str,
    *,
    audience: str | None = None,
    socket_path: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GatewayAttestation:
    """Request a nonce-bound token from the Confidential Space launcher.

    The launcher owns evidence collection and sends it to Google Cloud
    Attestation. The returned JWT is verified by the relying party, not by
    this gateway. Local and non-confidential deployments deliberately fail
    instead of manufacturing substitute evidence.
    """

    resolved_audience = audience or os.getenv("GATEWAY_ATTESTATION_AUDIENCE", DEFAULT_AUDIENCE)
    resolved_socket = socket_path or os.getenv(
        "CONFIDENTIAL_SPACE_ATTESTATION_SOCKET", DEFAULT_SOCKET_PATH
    )
    resolved_transport = transport or httpx.AsyncHTTPTransport(uds=resolved_socket)
    try:
        async with httpx.AsyncClient(
            transport=resolved_transport,
            timeout=httpx.Timeout(30),
        ) as client:
            response = await client.post(
                "http://localhost/v1/token",
                json={
                    "audience": resolved_audience,
                    "token_type": "OIDC",
                    "nonces": [nonce],
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError, OSError) as exc:
        raise GatewayAttestationUnavailable from exc

    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise GatewayAttestationUnavailable
    return GatewayAttestation(token=token, audience=resolved_audience, nonce=nonce)
