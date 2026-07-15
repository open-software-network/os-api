# Security policy

OpenSoftware API handles credentials, billing state, and private model traffic. Please do not open a public issue for a suspected vulnerability.

Report security issues privately to `security@opensoftware.co` with the affected endpoint or component, impact, reproduction steps, and a safe request ID if available. Never include a live API key, access token, wallet grant, prompt, completion, or other customer data. Encrypt sensitive supporting material before sending it and ask us for a preferred transfer method.

We will acknowledge a complete report within three business days, provide a severity assessment after reproduction, and coordinate disclosure after a fix is deployed. Rotate any credential included in a report immediately.

## Supported versions

Only the latest release on `main` and the current production revision receive security updates. Older commits, forks, modified model images, and third-party provider services are outside that support window.

## Scope

Reports about this gateway, its official deployment configuration, or `api.opensoftware.co` are in scope. Provider platforms, model publishers, and other Open Software Network services should be reported to their respective security contacts unless the issue is caused by this gateway's integration.

Good-faith research should avoid privacy violations, data destruction, denial of service, social engineering, and access to accounts or data you do not own. Stop testing and report the issue if you encounter customer data.

Privacy and attestation boundaries are documented in [the threat model](docs/threat-model.md).
