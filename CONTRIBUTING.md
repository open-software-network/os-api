# Contributing to OpenSoftware API

Thanks for helping build private inference infrastructure.

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and maintainer decision-making is described in [GOVERNANCE.md](GOVERNANCE.md).

## Before you start

- Open an issue before a substantial change so maintainers and contributors can agree on scope.
- Report suspected vulnerabilities privately using [SECURITY.md](SECURITY.md), never in a public
  issue.
- Keep credentials, customer data, prompts, responses, account identifiers, and production
  infrastructure details out of issues, pull requests, fixtures, logs, and commits.

## Development setup

Install Python 3.11 or newer and `uv`, then run:

```bash
uv sync --locked --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python scripts/validate_provider_catalog.py
uv run pip-audit
```

The provider-catalog check makes read-only requests to the public Phala and Venice model catalogs.
It does not need provider credentials. Tests use in-process mock providers and do not send prompts
or authorize charges.

Infrastructure changes should also pass the relevant checks from `.github/workflows/ci.yml`:
Terraform formatting and validation, all Kustomize renders, ShellCheck, and the container build.

## Pull requests

Describe the problem, the behavior change, validation performed, operational impact, and anything
left out of scope. Add regression tests for fixes and update public documentation when behavior or
configuration changes.

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
