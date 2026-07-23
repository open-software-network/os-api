# Contributing

Thank you for helping improve OpenSoftware API.

## Before opening a change

- Search existing issues and pull requests.
- Open an issue before a large behavioral or architectural change.
- Never include credentials, customer data, prompts, production identifiers, or raw provider
  evidence in an issue, commit, test fixture, or screenshot.

## Development

Install Python 3.11+ and `uv`, then run:

```bash
uv sync --locked --extra dev
uv run pytest -q
uv run ruff check .
uv run python scripts/validate_provider_catalog.py
uv run pip-audit
```

Changes to Terraform must pass `terraform fmt -check -recursive` and `terraform validate` from
`deploy/terraform`. Kubernetes changes must render successfully with `kubectl kustomize`.

## Pull requests

Keep changes focused, explain user-visible and security impact, add tests, and update documentation.
All required checks, review conversations, and CODEOWNERS approval must be complete before merge.

By contributing, you agree that your contribution is licensed under the repository's MIT License.
