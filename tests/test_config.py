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


def test_rejects_quality_score_outside_closed_range(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text(
        """
providers:
  provider:
    base_url: https://provider.invalid
models:
  private/model:
    quality_score: 101
endpoints:
  endpoint:
    model: private/model
    provider: provider
    pricing:
      input_per_million_usd: 1
      output_per_million_usd: 1
"""
    )

    with pytest.raises(ConfigurationError, match="between 0 and 100"):
        GatewayConfig.load(path)


def test_production_catalog_contains_full_venice_and_phala_tee_union() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")

    assert len(config.models) == 128
    assert "zai-org-glm-5-2" in config.models
    assert "claude-opus-4-8" in config.models
    assert "openai-gpt-54" in config.models
    assert "phala/uncensored-24b" in config.models
    assert (
        len([endpoint for endpoint in config.endpoints.values() if endpoint.provider == "phala"])
        == 28
    )
    assert (
        len([endpoint for endpoint in config.endpoints.values() if endpoint.provider == "venice"])
        == 112
    )


def test_generated_catalog_preserves_provider_pricing_and_privacy() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    private_model = config.models["zai-org-glm-5-2"]
    anonymized_model = config.models["claude-opus-4-8"]
    phala_model = config.models["phala/uncensored-24b"]

    assert private_model.name == "GLM 5.2"
    assert private_model.provider_privacy == "private"
    assert config.endpoints_for(private_model.id)[0].privacy.level == "no-retention"
    assert private_model.input_price_microusd_per_million == 1_400_000
    assert anonymized_model.provider_privacy == "anonymized"
    assert config.endpoints_for(anonymized_model.id)[0].privacy.retention == "anonymized"
    assert phala_model.provider_privacy == "private"
    assert config.endpoints_for(phala_model.id)[0].privacy.level == "tee"
    assert phala_model.quality_score is None


def test_broad_catalog_does_not_inflate_auto_hold_price() -> None:
    from sovereign_gateway.app import _auto_catalog_model

    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    auto = _auto_catalog_model(config, 0)

    assert auto["model_spec"]["pricing"]["input"]["usd"] == 1.75
    assert auto["model_spec"]["pricing"]["output"]["usd"] == 5.75


def test_catalog_include_rejects_duplicate_models(tmp_path: Path) -> None:
    (tmp_path / "extra.yaml").write_text("models:\n  duplicate: {}\nendpoints:\n  included: {}\n")
    path = tmp_path / "models.yaml"
    path.write_text(
        """
providers:
  provider:
    base_url: https://provider.invalid
catalog_includes: [extra.yaml]
models:
  duplicate: {}
endpoints:
  base:
    model: duplicate
    provider: provider
    pricing:
      input_per_million_usd: 1
      output_per_million_usd: 1
"""
    )

    with pytest.raises(ConfigurationError, match="duplicates model 'duplicate'"):
        GatewayConfig.load(path)


def test_rejects_unknown_top_level_configuration_key(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        """
providers:
  provider:
    base_url: https://provider.invalid
models:
  model: {}
endpoints:
  endpoint:
    model: model
    provider: provider
    pricing:
      input_per_million_usd: 1
      output_per_million_usd: 1
catalog_include: [misspelled.yaml]
"""
    )

    with pytest.raises(ConfigurationError, match="unknown configuration keys.*catalog_include"):
        GatewayConfig.load(path)
