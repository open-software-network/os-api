# OpenSoftware API usage

## Connection details

- Site and dashboard: `https://api.opensoftware.co`
- OpenAI-compatible base URL: `https://api.opensoftware.co/v1`
- Model catalog: `GET https://api.opensoftware.co/v1/models`

Create a key in the dashboard and store it in a server-side secret manager. The raw key is shown
once; the service retains only its prefix and SHA-256 hash. Never embed it in browser JavaScript,
a mobile binary, source control, logs, or support tickets.

Model availability changes, so discover models instead of hard-coding an undocumented deployment:

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

models = client.models.list()
model = models.data[0].id
response = client.chat.completions.create(
    model=model,
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
    "model": "MODEL_ID_FROM_V1_MODELS",
    "messages": [{"role": "user", "content": "Hello from my app"}],
    "max_completion_tokens": 2048,
    "stream": false
  }'
```

Use `X-Confidential-Compute: required` when a hardware-backed TEE is mandatory. The gateway returns
`409 confidential_compute_unavailable` instead of silently downgrading. See the
[privacy contract](../README.md#api-privacy-contract) and [threat model](threat-model.md).

Successful responses identify the route with `X-OS-Privacy-Level`, `X-Compute-Privacy`,
`X-Model-Backend`, and `X-Request-ID`. Never put sensitive data in a request ID.

Self-hosted operators should use [production operations](operations.md); public API users do not
need access to cloud deployment settings or secrets.
