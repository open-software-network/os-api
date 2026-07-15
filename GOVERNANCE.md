# Governance

OpenSoftware API is maintained by the Open Software Network engineering team identified in `.github/CODEOWNERS`.

- Changes are proposed through pull requests and require passing CI plus review from a code owner.
- Security, privacy, billing, attestation, and production-deployment changes require an explicit risk assessment in the pull request.
- Maintainers may reject changes that weaken the documented privacy contract, reproducibility, tenant isolation, or supply-chain controls.
- Releases follow Semantic Versioning and are cut from protected `main` after the release checks pass. Breaking API or privacy-policy changes require release notes and a migration path.
- Vulnerabilities and conduct reports use their private reporting processes; public technical decisions should remain in issues, pull requests, and versioned documentation.

The maintainer team may update this lightweight model as the contributor community grows. Changes to governance use the same reviewed pull-request process as code.
