# os-june Venice replacement assessment

The OpenSoftware API implements the Venice upstream surface consumed by June API, except image
editing. Image-edit requests can exceed Cloud Run's ingress limit, so that endpoint needs a
separate transport design before it can move behind this compatibility layer. The router sits
between June API and private inference providers without changing June's client-facing routes. The
compatibility surface is enabled in os-api production and June remains the billing owner.

## Compatible today

- `POST /v1/chat/completions`, streamed and buffered
- Function tools, tool choice, structured output, reasoning controls, and usage frames
- Canonical model listing and context-window metadata
- Provider failover with a fixed privacy floor
- Automatic model selection through `model: open-software/auto`
- `GET /v1/models?type=text|asr` using Venice's catalog wire shape
- `POST /v1/audio/transcriptions` with multipart audio and the Parakeet ASR model
- `POST /v1/augment/search` and `/v1/augment/scrape`
- `POST /v1/image/generate`
- `POST /v1/video/quote`, `/v1/video/queue`, and `/v1/video/retrieve`
- Conservative auto-model pricing for June's preauthorization hold
- Backward-compatible resolution of June's stored GLM, Kimi, and Nemotron model IDs

June's local Hermes proxy and June API's existing Venice provider adapter can keep their request and
response contracts. Set June's Venice upstream base URL to the os-api origin plus `/v1`, and use a
dedicated static os-api service key. Compatibility responses include `X-OS-Billing-Owner: caller`:
June continues to authorize and settle OS Accounts usage exactly once. Managed end-user os-api keys
are rejected from these routes, preventing an accidental second billing path.

## Production posture

- `VENICE_COMPAT_ENABLED=true` is declared in Terraform and in the digest-pinned deployment
  workflow for the canary, primary, and secondary Cloud Run services.
- June API authenticates with a dedicated static os-api service key; managed end-user os-api keys
  remain rejected from compatibility routes to prevent double billing.
- Canary validation preceded production promotion, including model catalog, chat, and real-audio
  transcription checks through the June API boundary.
- The ASR and augment implementations currently route to Venice behind os-api. Their stable public
  contracts are provider-independent, so another private provider can be added without another
  os-june migration.
- Venice client-side E2EE requires encryption in the desktop client. os-api can validate and relay
  Venice ciphertext but cannot turn a plaintext June request into end-to-end encryption because
  the router would necessarily see the plaintext.

## Completed migration sequence

1. A dedicated static service key was scoped to the June API deployment.
2. `VENICE_COMPAT_ENABLED=true` was enabled and validated on the os-api canary first.
3. The June API provider contract suite ran against the canary base URL.
4. June API staging was pointed at `{os_api_origin}/v1` with June retained as billing owner.
5. Catalog, ASR, chat, streaming usage, search, scrape, error, and latency behavior was validated.
6. Auto routing was enabled only after the canary catalog exposed `open-software/auto` and
   end-to-end agent tests passed.
7. Production was promoted after a real-audio June API transcription passed in staging. Rollback
   remains restoring the Venice base URL; no desktop release is required.

## Runtime controls

| Variable | Purpose |
|---|---|
| `VENICE_COMPAT_ENABLED` | Enables the compatibility endpoints; defaults to false. |
| `ASR_MODEL_ALLOWLIST` | Comma-separated ASR models; defaults to Parakeet. |
| `ASR_PRICE_PER_SECOND_USD` | Venice-shaped catalog price used by June billing. |
| `MAX_AUDIO_BYTES` | Hard multipart audio cap; defaults to 25 MiB. |
| `MAX_IMAGE_GENERATE_REQUEST_BYTES` | Hard image-generation JSON cap; defaults to 1 MiB. |
| `IMAGE_GENERATE_TIMEOUT_SECONDS` | Venice image-generation timeout; defaults to 360 seconds so June can cancel before its provider-leg budget expires. |
| `MAX_VIDEO_REQUEST_BYTES` | Hard quote and retrieve JSON cap; defaults to 1 MiB. |
| `MAX_VIDEO_QUEUE_REQUEST_BYTES` | Hard queue JSON cap; defaults to 31 MiB, below Cloud Run's HTTP/1 ingress ceiling. |
| `VIDEO_QUEUE_MAX_CONCURRENCY` | Per-instance queue-body concurrency cap; defaults to 4 to bound base64 memory residency. |
| `MAX_VIDEO_JSON_RESPONSE_BYTES` | Hard provider JSON/error response cap; defaults to 1 MiB. |
| `MAX_VIDEO_RESPONSE_BYTES` | Defense-in-depth streamed video cap; defaults to 101 MiB so June's 100 MiB terminal limit fires first. |
| `VIDEO_REQUEST_TIMEOUT_SECONDS` | Per-call Venice video timeout; defaults to 360 seconds. |

The proxy strips the user's audio filename, never logs request or response bodies, validates ASR
models against an allowlist, clamps web result counts, and permits scrape targets only when HTTPS
DNS resolution contains exclusively public addresses.

Video quote and queue responses remain JSON. Retrieve responses preserve JSON progress and error
statuses, while completed video bytes are streamed through os-api without buffering the MP4 in
gateway memory. Both the declared content length and the streamed byte count are bounded.
Because Cloud Run HTTP/1 ingress cannot carry June's maximum 50 MiB source image after base64
expansion, animation queue payloads above 31 MiB require a future upload-by-reference transport.
