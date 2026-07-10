#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import secrets


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a gateway API key and its stored hash")
    parser.add_argument("tenant_id")
    args = parser.parse_args()

    key = f"sk-sov-{secrets.token_urlsafe(32)}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    print(f"API key (show once): {key}")
    print(f"API_KEY_HASHES entry: {args.tenant_id}={digest}")


if __name__ == "__main__":
    main()
