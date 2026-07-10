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
