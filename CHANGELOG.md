# Changelog

This project follows [Semantic Versioning](https://semver.org/). Security fixes may initially be described without exploit details while coordinated disclosure is in progress.

## Unreleased

- Added public contribution, support, security, licensing, and third-party notice documentation.
- Hardened CI actions, build contexts, secret-file writes, packaging, and reusable infrastructure examples.
- Updated hosted-provider and self-hosted usage documentation.
- Preserved provider defaults for text generation without an explicit output limit while reserving
  managed spend against the model maximum; non-text generation retains a 2,048-token default.
- Added bounded, best-effort cursor pagination to the dashboard key repository contract.

## 0.10.0 - 2026-07-11

- Added policy-driven Phala and Venice routing, automatic model selection, privacy evidence, and same-model fallback behavior.
- Added the June compatibility surface and production canary validation.
