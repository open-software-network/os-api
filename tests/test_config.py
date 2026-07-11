from __future__ import annotations

from pathlib import Path

import pytest

from sovereign_gateway.config import ConfigurationError, GatewayConfig


def test_rejects_backend_with_wrong_privacy_class(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text(
        """
backends:
  mislabeled:
    base_url: http://example
    privacy: standard
models:
  model:
    confidential_backend: mislabeled
"""
    )

    with pytest.raises(ConfigurationError, match="confidential backend is not confidential"):
        GatewayConfig.load(path)


def test_rejects_non_string_backend_revision(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text(
        """
backends:
  standard:
    base_url: http://example
    privacy: standard
    revision: 123
models:
  model:
    standard_backend: standard
"""
    )

    with pytest.raises(ConfigurationError, match="revision must be a string"):
        GatewayConfig.load(path)


def test_rejects_non_positive_max_concurrency(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text(
        """
backends:
  standard:
    base_url: http://example
    privacy: standard
    max_concurrency: 0
models:
  model:
    standard_backend: standard
"""
    )

    with pytest.raises(ConfigurationError, match="max_concurrency must be a positive integer"):
        GatewayConfig.load(path)


def test_rejects_e2ee_endpoint_without_client_encryption(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text(
        """
providers:
  venice:
    base_url: https://venice.invalid
    adapter: venice
models:
  private/model: {}
endpoints:
  unsafe:
    model: private/model
    provider: venice
    privacy:
      level: e2ee-tee
      transport: client-e2ee
    pricing:
      input_per_million_usd: 1
      output_per_million_usd: 1
"""
    )

    with pytest.raises(ConfigurationError, match="does not require client encryption"):
        GatewayConfig.load(path)


def test_rejects_unknown_provider_adapter(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text(
        """
providers:
  unsafe:
    base_url: https://provider.invalid
    adapter: arbitrary-code
models:
  private/model: {}
endpoints:
  endpoint:
    model: private/model
    provider: unsafe
    pricing:
      input_per_million_usd: 1
      output_per_million_usd: 1
"""
    )

    with pytest.raises(ConfigurationError, match="unsupported adapter"):
        GatewayConfig.load(path)


def test_rejects_unverified_verified_tee_claim(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text(
        """
providers:
  provider:
    base_url: https://provider.invalid
models:
  private/model: {}
endpoints:
  endpoint:
    model: private/model
    provider: provider
    privacy:
      level: verified-tee
      attestation: quote
    pricing:
      input_per_million_usd: 1
      output_per_million_usd: 1
"""
    )

    with pytest.raises(ConfigurationError, match="no independent measurement-policy verifier"):
        GatewayConfig.load(path)
