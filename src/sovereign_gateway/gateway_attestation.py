from __future__ import annotations

import os
import re
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

    resolved_audience = audience or os.getenv(
        "GATEWAY_ATTESTATION_AUDIENCE", DEFAULT_AUDIENCE
    )
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
            # Confidential Space returns the generated token as the raw HTTP
            # response body, not as a JSON object. Keep this strict so a
            # launcher error document can never be relayed as evidence.
            raw_token = response.content
    except (httpx.HTTPError, OSError) as exc:
        raise GatewayAttestationUnavailable from exc

    if len(raw_token) == 0 or len(raw_token) > 128 * 1024:
        raise GatewayAttestationUnavailable
    try:
        token = raw_token.decode("ascii")
    except UnicodeDecodeError as exc:
        raise GatewayAttestationUnavailable from exc
    if token != token.strip() or not re.fullmatch(
        r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token
    ):
        raise GatewayAttestationUnavailable
    return GatewayAttestation(token=token, audience=resolved_audience, nonce=nonce)
