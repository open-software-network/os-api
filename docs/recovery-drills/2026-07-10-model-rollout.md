# Model rollout and rollback drill — 2026-07-10

## Result

Passed. One good rollout and one deliberately broken rollout were exercised live against the serving confidential CPU model deployment using `scripts/model_rollout.sh`. A continuous public sampler sent a confidential inference request every 10 seconds throughout both drills: **82 of 82 requests returned HTTP 200** (window 19:43:10Z–19:57:23Z), which also spanned the 0.6.0 gateway promotion in both regions.

## Good rollout (19:43:17Z → 19:51:32Z, ~8m15s)

- Phase 1: the exact artifact started as an off-service canary deployment (`confidential-vllm-cpu-rollout-canary`); no Service selects its pods, so it can receive no customer traffic. The surge pod triggered cluster-autoscaler scale-up (1→2 nodes), pulled the image, loaded the model, and passed the golden-prompt gate via direct port-forward.
- Phase 2: promotion used `RollingUpdate` with `maxSurge: 1, maxUnavailable: 0`; the old model pod served until the replacement was ready.
- Phase 3: the golden gate passed against the serving deployment. The drill used `--stamp` to force a new ReplicaSet with the unchanged pinned digest.

## Broken rollout with automatic rollback (19:52:07Z → 19:57:10Z)

- A non-model image (`registry.k8s.io/pause:3.9`) was promoted with `--skip-canary` to force the failure into the promote path.
- The broken pod never passed readiness; with `maxUnavailable: 0` the previous model pod was never removed, so serving capacity never dropped.
- `rollout status` timed out after the configured 300s, the script automatically ran `rollout undo`, and the deployment reported the previous pinned vLLM digest serving again 2 seconds later.
- Public sampler observed no non-200 response at any point.

## Version visibility

Gateway 0.6.0 stamps `x-model-revision` (the pinned image revision from `models.yaml`) on every inference response, alongside the existing `x-model-backend` and `x-compute-privacy`. Verified on the private canary and then on the public endpoint after promotion:

```
x-compute-privacy: confidential
x-model-backend: confidential-vllm-cpu
x-model-revision: vllm-openai-cpu@89e1fbe82903
```

## Scope notes

- The canary phase uses synthetic golden prompts, not diverted customer traffic. At single-replica scale this is deliberate: replica-weighted canary traffic would send half of production requests to an unvalidated build. Revisit when model deployments run multiple replicas.
- Golden prompts are smoke-level quality gates (valid completion, deterministic settings). A scored evaluation suite as a promotion gate remains future work.
- The GPU deployment retains `Recreate` because it owns a single dedicated TDX GPU node; GPU rollouts are planned brief unavailability until capacity is diversified.

Rerun this drill after changing the rollout script, deployment strategy, node-pool sizing, or probe configuration.
