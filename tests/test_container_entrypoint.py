from __future__ import annotations

import socket
import stat
import tempfile
from pathlib import Path

import pytest

from sovereign_gateway import container_entrypoint
from sovereign_gateway.container_entrypoint import (
    GATEWAY_GID,
    GATEWAY_UID,
    drop_gateway_privileges,
    prepare_launcher_socket,
)


def test_validates_root_owned_launcher_socket_without_mutating_it() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
        socket_path = Path(temp_dir) / "teeserver.sock"
        launcher = socket.socket(socket.AF_UNIX)
        try:
            launcher.bind(str(socket_path))
            socket_path.chmod(0o755)

            prepare_launcher_socket(str(socket_path), required=True)

            assert stat.S_IMODE(socket_path.stat().st_mode) == 0o755
        finally:
            launcher.close()


def test_optional_launcher_socket_may_be_absent(tmp_path: Path) -> None:
    prepare_launcher_socket(str(tmp_path / "missing.sock"), required=False)


def test_required_launcher_socket_must_exist(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="launcher socket is missing"):
        prepare_launcher_socket(str(tmp_path / "missing.sock"), required=True)


def test_launcher_path_must_be_a_socket(tmp_path: Path) -> None:
    ordinary_file = tmp_path / "not-a-socket"
    ordinary_file.touch()

    with pytest.raises(RuntimeError, match="launcher path is not a socket"):
        prepare_launcher_socket(str(ordinary_file), required=True)


def test_privilege_drop_is_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(container_entrypoint.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        container_entrypoint.os,
        "setgroups",
        lambda groups: calls.append(("setgroups", groups)),
    )
    monkeypatch.setattr(
        container_entrypoint.os,
        "setgid",
        lambda gid: calls.append(("setgid", gid)),
    )
    monkeypatch.setattr(
        container_entrypoint.os,
        "setuid",
        lambda uid: calls.append(("setuid", uid)),
    )
    monkeypatch.setattr(
        container_entrypoint.os,
        "getresuid",
        lambda: (GATEWAY_UID, GATEWAY_UID, GATEWAY_UID),
        raising=False,
    )
    monkeypatch.setattr(
        container_entrypoint.os,
        "getresgid",
        lambda: (GATEWAY_GID, GATEWAY_GID, GATEWAY_GID),
        raising=False,
    )
    monkeypatch.setattr(container_entrypoint.os, "getgroups", lambda: [])

    drop_gateway_privileges()

    assert calls == [
        ("setgroups", []),
        ("setgid", GATEWAY_GID),
        ("setuid", GATEWAY_UID),
    ]


def test_privilege_drop_fails_if_uid_remains_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(container_entrypoint.os, "geteuid", lambda: 0)
    monkeypatch.setattr(container_entrypoint.os, "setgroups", lambda groups: None)
    monkeypatch.setattr(container_entrypoint.os, "setgid", lambda gid: None)
    monkeypatch.setattr(container_entrypoint.os, "setuid", lambda uid: None)
    monkeypatch.setattr(
        container_entrypoint.os, "getresuid", lambda: (0, 0, 0), raising=False
    )

    with pytest.raises(RuntimeError, match="drop to gateway uid"):
        drop_gateway_privileges()


def test_confidential_entrypoint_retains_root_for_launcher_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setenv("REQUIRE_CONFIDENTIAL_SPACE", "true")
    monkeypatch.setenv("CONFIDENTIAL_SPACE_ATTESTATION_SOCKET", "/launcher.sock")
    monkeypatch.setattr(container_entrypoint.sys, "argv", ["entrypoint", "server"])
    monkeypatch.setattr(container_entrypoint.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        container_entrypoint,
        "prepare_launcher_socket",
        lambda path, required: calls.append(("prepare", (path, required))),
    )
    monkeypatch.setattr(
        container_entrypoint,
        "drop_gateway_privileges",
        lambda: calls.append(("drop", None)),
    )
    monkeypatch.setattr(
        container_entrypoint.os,
        "execvp",
        lambda command, argv: calls.append(("exec", (command, argv))),
    )

    container_entrypoint.main()

    assert calls == [
        ("prepare", ("/launcher.sock", True)),
        ("exec", ("server", ["server"])),
    ]
