from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from sovereign_gateway import confidential_runtime
from sovereign_gateway.confidential_runtime import (
    ConfidentialRuntimeError,
    configure_confidential_runtime,
)


def test_disabled_runtime_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REQUIRE_CONFIDENTIAL_SPACE", raising=False)
    monkeypatch.delenv("CONFIDENTIAL_SPACE_WIP_PROVIDER", raising=False)

    configure_confidential_runtime()


def test_required_runtime_fails_without_launcher_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("REQUIRE_CONFIDENTIAL_SPACE", "true")
    monkeypatch.setenv("CONFIDENTIAL_SPACE_WIP_PROVIDER", "provider")
    monkeypatch.setenv("CONFIDENTIAL_SPACE_IMPERSONATE_SERVICE_ACCOUNT", "runtime@example.com")
    monkeypatch.setenv("CONFIDENTIAL_SPACE_TOKEN_FILE", str(tmp_path / "missing"))

    with pytest.raises(ConfidentialRuntimeError, match="attestation token"):
        configure_confidential_runtime()


def test_required_runtime_rejects_invalid_secret_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = tmp_path / "attestation-token"
    token.write_text("signed-token")
    monkeypatch.setenv("REQUIRE_CONFIDENTIAL_SPACE", "true")
    monkeypatch.setenv("CONFIDENTIAL_SPACE_WIP_PROVIDER", "provider")
    monkeypatch.setenv("CONFIDENTIAL_SPACE_IMPERSONATE_SERVICE_ACCOUNT", "runtime@example.com")
    monkeypatch.setenv("CONFIDENTIAL_SPACE_TOKEN_FILE", str(token))
    monkeypatch.setenv("CONFIDENTIAL_SPACE_CREDENTIAL_FILE", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("CONFIDENTIAL_SPACE_SECRET_ENV", "[]")

    with pytest.raises(ConfidentialRuntimeError, match="map strings to strings"):
        configure_confidential_runtime()


def test_attested_identity_releases_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = tmp_path / "attestation-token"
    token.write_text("signed-token")
    credentials = tmp_path / "credentials.json"
    secret_file = tmp_path / "tls" / "key.pem"
    monkeypatch.setenv("REQUIRE_CONFIDENTIAL_SPACE", "true")
    monkeypatch.setenv("CONFIDENTIAL_SPACE_WIP_PROVIDER", "projects/1/providers/attested")
    monkeypatch.setenv("CONFIDENTIAL_SPACE_IMPERSONATE_SERVICE_ACCOUNT", "runtime@example.com")
    monkeypatch.setenv("CONFIDENTIAL_SPACE_TOKEN_FILE", str(token))
    monkeypatch.setenv("CONFIDENTIAL_SPACE_CREDENTIAL_FILE", str(credentials))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "privacy-project")
    monkeypatch.setenv("CONFIDENTIAL_SPACE_SECRET_ENV", '{"VENICE_API_KEY":"venice"}')
    monkeypatch.setenv(
        "CONFIDENTIAL_SPACE_SECRET_FILES", json.dumps({str(secret_file): "tls-key"})
    )

    class Secrets:
        def access_secret_version(self, request: dict[str, str]) -> SimpleNamespace:
            value = b"venice-secret" if "/venice/" in request["name"] else b"private-key"
            return SimpleNamespace(payload=SimpleNamespace(data=value))

    monkeypatch.setattr(confidential_runtime, "_secret_client", Secrets)

    configure_confidential_runtime()

    config = json.loads(credentials.read_text())
    assert config["credential_source"]["file"] == str(token)
    assert config["audience"] == "//iam.googleapis.com/projects/1/providers/attested"
    assert "runtime@example.com:generateAccessToken" in config["service_account_impersonation_url"]
    assert os.environ["VENICE_API_KEY"] == "venice-secret"
    assert secret_file.read_bytes() == b"private-key"
    assert credentials.stat().st_mode & 0o777 == 0o600
    assert secret_file.stat().st_mode & 0o777 == 0o600
