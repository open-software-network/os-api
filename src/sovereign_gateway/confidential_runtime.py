from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from google.cloud import secretmanager

DEFAULT_TOKEN_FILE = "/run/container_launcher/attestation_verifier_claims_token"
DEFAULT_CREDENTIAL_FILE = "/tmp/os-api-confidential-space-credentials.json"


class ConfidentialRuntimeError(RuntimeError):
    pass


def configure_confidential_runtime() -> None:
    """Configure attestation-bound ADC and release runtime secrets."""

    if not _enabled():
        return
    token_file = Path(os.getenv("CONFIDENTIAL_SPACE_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    provider = _required("CONFIDENTIAL_SPACE_WIP_PROVIDER")
    service_account = _required("CONFIDENTIAL_SPACE_IMPERSONATE_SERVICE_ACCOUNT")
    if not token_file.is_file():
        raise ConfidentialRuntimeError("Confidential Space attestation token is unavailable")

    credential_file = Path(
        os.getenv("CONFIDENTIAL_SPACE_CREDENTIAL_FILE", DEFAULT_CREDENTIAL_FILE)
    )
    _write_private(
        credential_file,
        json.dumps(
            {
                "type": "external_account",
                "audience": f"//iam.googleapis.com/{provider}",
                "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
                "token_url": "https://sts.googleapis.com/v1/token",
                "credential_source": {"file": str(token_file)},
                "service_account_impersonation_url": (
                    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
                    f"{service_account}:generateAccessToken"
                ),
            },
            separators=(",", ":"),
        ).encode(),
    )
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(credential_file)
    _release_secret_environment(_mapping("CONFIDENTIAL_SPACE_SECRET_ENV"))
    _release_secret_files(_mapping("CONFIDENTIAL_SPACE_SECRET_FILES"))


def _enabled() -> bool:
    return os.getenv("REQUIRE_CONFIDENTIAL_SPACE", "false").lower() in {"1", "true", "yes"}


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfidentialRuntimeError(f"{name} is required")
    return value


def _mapping(name: str) -> dict[str, str]:
    try:
        value = json.loads(os.getenv(name, "{}"))
    except json.JSONDecodeError as exc:
        raise ConfidentialRuntimeError(f"{name} must be a JSON object") from exc
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(secret, str)
        for key, secret in value.items()
    ):
        raise ConfidentialRuntimeError(f"{name} must map strings to strings")
    return value


def _secret_client() -> secretmanager.SecretManagerServiceClient:
    return secretmanager.SecretManagerServiceClient()


def _secret_name(secret_id: str) -> str:
    project = _required("GOOGLE_CLOUD_PROJECT")
    return f"projects/{project}/secrets/{secret_id}/versions/latest"


def _release_secret_environment(mapping: Mapping[str, str]) -> None:
    if not mapping:
        return
    client = _secret_client()
    for environment_name, secret_id in mapping.items():
        response = client.access_secret_version(request={"name": _secret_name(secret_id)})
        os.environ[environment_name] = response.payload.data.decode()


def _release_secret_files(mapping: Mapping[str, str]) -> None:
    if not mapping:
        return
    client = _secret_client()
    for path, secret_id in mapping.items():
        response = client.access_secret_version(request={"name": _secret_name(secret_id)})
        target = Path(path)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_private(target, response.payload.data)


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
