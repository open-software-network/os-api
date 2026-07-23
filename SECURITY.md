# Security policy

OpenSoftware API handles credentials, billing state, and private model traffic. Please do not open a public issue for a suspected vulnerability.

Report security issues privately to `security@opensoftware.co` with the affected endpoint or component, impact, reproduction steps, and a safe request ID if available. Never include a live API key, access token, wallet grant, prompt, or completion.

We will acknowledge a complete report within three business days, provide a severity assessment after reproduction, and coordinate disclosure after a fix is deployed. Rotate any credential included in a report immediately.

Only the latest production revision is supported. Privacy and attestation boundaries are documented in `docs/threat-model.md`.
