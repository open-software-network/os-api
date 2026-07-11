# os-june Venice replacement assessment

The OpenSoftware API implements the complete Venice upstream surface currently consumed by June
API. It can be inserted between June API and Venice without changing June's client-facing routes.
The compatibility surface is dormant by default and has not been enabled in os-june.

## Compatible today

- `POST /v1/chat/completions`, streamed and buffered
- Function tools, tool choice, structured output, reasoning controls, and usage frames
- Canonical model listing and context-window metadata
- Provider failover with a fixed privacy floor
- Automatic model selection through `model: open-software/auto`
- `GET /v1/models?type=text|asr` using Venice's catalog wire shape
- `POST /v1/audio/transcriptions` with multipart audio and the Parakeet ASR model
- `POST /v1/augment/search` and `/v1/augment/scrape`
- Conservative auto-model pricing for June's preauthorization hold
- Backward-compatible resolution of June's stored GLM, Kimi, and Nemotron model IDs

June's local Hermes proxy and June API's existing Venice provider adapter can keep their request and
response contracts. Set June's Venice upstream base URL to the os-api origin plus `/v1`, and use a
dedicated static os-api service key. Compatibility responses include `X-OS-Billing-Owner: caller`:
June continues to authorize and settle OS Accounts usage exactly once. Managed end-user os-api keys
are rejected from these routes, preventing an accidental second billing path.

## Deliberately not switched

- os-june configuration still points directly to Venice.
- `VENICE_COMPAT_ENABLED` remains false in os-api production.
- No production service key has been issued to June API for this path.
- The ASR and augment implementations currently route to Venice behind os-api. Their stable public
  contracts are provider-independent, so another private provider can be added without another
  os-june migration.
- Venice client-side E2EE requires encryption in the desktop client. os-api can validate and relay
  Venice ciphertext but cannot turn a plaintext June request into end-to-end encryption because
  the router would necessarily see the plaintext.

## Safe migration sequence

1. Create a dedicated static service key scoped to the June API deployment.
2. Enable `VENICE_COMPAT_ENABLED=true` on an os-api canary only.
3. Run the June API provider contract suite against that canary base URL.
4. Point a June API staging canary at `{os_api_origin}/v1`; keep June as billing owner.
5. Compare catalog, ASR, chat, streaming usage, search, scrape, error, and latency telemetry.
6. Set `OS_JUNE_AUTO_MODE_DEFAULT=true` only after the canary catalog exposes
   `open-software/auto` and end-to-end agent tests pass.
7. Roll back by restoring the Venice base URL and removing the default flag. No desktop release is
   required.

## Runtime controls

| Variable | Purpose |
|---|---|
| `VENICE_COMPAT_ENABLED` | Enables the compatibility endpoints; defaults to false. |
| `ASR_MODEL_ALLOWLIST` | Comma-separated ASR models; defaults to Parakeet. |
| `ASR_PRICE_PER_SECOND_USD` | Venice-shaped catalog price used by June billing. |
| `MAX_AUDIO_BYTES` | Hard multipart audio cap; defaults to 25 MiB. |

The proxy strips the user's audio filename, never logs request or response bodies, validates ASR
models against an allowlist, clamps web result counts, and permits scrape targets only when HTTPS
DNS resolution contains exclusively public addresses.
