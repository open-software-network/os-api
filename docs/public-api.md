# OpenSoftware API usage

## Connection details

- Site and dashboard: `https://api.opensoftware.co`
- OpenAI-compatible base URL: `https://api.opensoftware.co/v1`
- Model catalog: `GET https://api.opensoftware.co/v1/models`

Invited users can create a key in the access-controlled dashboard and store it in a server-side secret manager. The raw key is displayed once; the service retains only its prefix and SHA-256 hash. Do not embed it in browser JavaScript, a mobile binary, source control, logs, or support tickets.

Model availability and capabilities can change, so discover models instead of hard-coding an undocumented deployment. The examples below use `openai/gpt-oss-20b`, which is part of the repository's reviewed catalog.

```bash
export OS_API_KEY="replace-with-your-key"
export OS_API_BASE_URL="https://api.opensoftware.co/v1"

curl "$OS_API_BASE_URL/models" \
  -H "Authorization: Bearer $OS_API_KEY"
```

## OpenAI client

```python
import os

from openai import OpenAI

client = OpenAI(
    api_key=os.environ["OS_API_KEY"],
    base_url=os.environ.get("OS_API_BASE_URL", "https://api.opensoftware.co/v1"),
)

response = client.chat.completions.create(
    model="openai/gpt-oss-20b",
    messages=[{"role": "user", "content": "Hello from my app"}],
    max_completion_tokens=2048,
)
print(response.choices[0].message.content)
```

## curl

```bash
curl "$OS_API_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $OS_API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Confidential-Compute: required" \
  -d '{
    "model": "openai/gpt-oss-20b",
    "messages": [{"role": "user", "content": "Hello from my app"}],
    "max_completion_tokens": 2048,
    "stream": false
  }'
```

Use `X-Confidential-Compute: required` when a hardware-backed TEE is mandatory. The gateway returns `409 confidential_compute_unavailable` instead of silently downgrading that request. See the [privacy contract](../README.md#api-privacy-contract) and [threat model](threat-model.md) before relying on a privacy class.

Successful responses identify the route with headers including `X-OS-Privacy-Level`, `X-Compute-Privacy`, `X-Model-Backend`, and `X-Request-ID`. Rate-limit responses include `Retry-After`; rate-limit reset values are Unix timestamps. You may supply a safe `X-Request-ID` containing only letters, digits, `.`, `_`, `:`, `/`, or `-` (up to 128 characters) to correlate an error with support. Never place sensitive data in the request ID.

Self-hosted operators should use [production operations](operations.md); public API users do not need access to the project's cloud account or deployment secrets.
