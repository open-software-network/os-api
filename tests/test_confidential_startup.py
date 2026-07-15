from __future__ import annotations

import logging

import pytest

from sovereign_gateway import app as app_module


def test_confidential_runtime_failure_is_logged_before_failing_closed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail() -> None:
        raise RuntimeError("secret access denied")

    monkeypatch.setattr(app_module, "configure_confidential_runtime", fail)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="secret access denied"):
        app_module._initialize_confidential_runtime()

    assert "confidential_runtime_init_failed" in caplog.text
