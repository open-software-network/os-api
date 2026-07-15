from __future__ import annotations

import os
import stat
import sys

from sovereign_gateway.gateway_attestation import DEFAULT_SOCKET_PATH

GATEWAY_UID = 10001
GATEWAY_GID = 10001


def prepare_launcher_socket(socket_path: str, *, required: bool) -> None:
    try:
        socket_stat = os.lstat(socket_path)
    except FileNotFoundError:
        if required:
            raise RuntimeError("Confidential Space launcher socket is missing") from None
        return

    if not stat.S_ISSOCK(socket_stat.st_mode):
        raise RuntimeError("Confidential Space launcher path is not a socket")

    # The launcher creates this root-owned socket as 0755, but Unix socket
    # clients require write permission to connect. Restrict the path to the
    # gateway principal before permanently dropping privileges.
    os.chown(socket_path, GATEWAY_UID, GATEWAY_GID)
    os.chmod(socket_path, 0o600)


def verify_gateway_identity() -> None:
    if os.getresuid() != (GATEWAY_UID, GATEWAY_UID, GATEWAY_UID):
        raise RuntimeError("Failed to drop to gateway uid")
    if os.getresgid() != (GATEWAY_GID, GATEWAY_GID, GATEWAY_GID):
        raise RuntimeError("Failed to drop to gateway gid")
    if os.getgroups():
        raise RuntimeError("Failed to clear supplementary groups")


def drop_gateway_privileges() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Container entrypoint must start as root")
    os.setgroups([])
    os.setgid(GATEWAY_GID)
    os.setuid(GATEWAY_UID)
    verify_gateway_identity()


def main() -> None:
    if len(sys.argv) < 2:
        raise RuntimeError("Container command is required")

    required = os.getenv("REQUIRE_CONFIDENTIAL_SPACE", "").lower() == "true"
    socket_path = os.getenv(
        "CONFIDENTIAL_SPACE_ATTESTATION_SOCKET", DEFAULT_SOCKET_PATH
    )
    if os.geteuid() == 0:
        prepare_launcher_socket(socket_path, required=required)
        drop_gateway_privileges()
    else:
        if required:
            raise RuntimeError(
                "Confidential Space entrypoint requires root for socket preparation"
            )
        verify_gateway_identity()
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
