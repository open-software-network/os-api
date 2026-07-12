# Automatic routing quality policy

## Snapshot v1 - July 2026

`quality_score` is an ordinal routing input on a 0 to 100 scale. It is intentionally separate from
provider price and latency. The first production snapshot orders the catalog as follows:

| Canonical model | Score | Policy rationale |
| --- | ---: | --- |
| `qwen/qwen-2.5-7b-instruct` | 45 | Small, inexpensive instruction model. |
| `openai/gpt-oss-20b` | 58 | Small reasoning model with tool-capable endpoints. |
| `google/gemma-3-27b-it` | 65 | Mid-size instruction model with schema-capable endpoints. |
| `deepseek/deepseek-v4-flash` | 76 | Higher reasoning tier with long context. |
| `openai/gpt-oss-120b` | 78 | Large reasoning and tool model. |
| `qwen/qwen3.6-27b` | 84 | Newer reasoning/tool tier. |
| `moonshotai/kimi-k2.6` | 92 | Frontier reasoning/tool tier. |
| `z-ai/glm-5.2` | 94 | Highest general-quality tier in this snapshot. |

These are product-policy scores, not percentages, benchmark results, or provider claims. A small
score difference only establishes ordering for the router. It does not promise that the higher
model wins every prompt.

## Release gate

A score or model change must include all of the following:

1. A dated update to this snapshot explaining the ordering change.
2. Live catalog validation for model identity, price, privacy, context, and capabilities.
3. Router regression tests showing cost and configured quality do not decrease as the slider moves
   from 0 to 1 for the representative request mix.
4. End-to-end June tests for buffered text, SSE, usage, tool calls, structured output, and note
   generation at both slider extremes.

Production telemetry should record only the selected canonical model, provider endpoint, latency,
usage, and error class. It must not record prompts or completions. Repeated quality regressions
should be handled by lowering/removing the score in a catalog release; explicit model selection is
the reproducibility escape hatch.
