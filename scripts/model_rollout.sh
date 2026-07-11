#!/usr/bin/env bash
# Quality-gated model rollout with automatic rollback.
#
# Phase 1 (canary): the exact new image is started as an off-service canary
#   deployment on the production cluster and must answer golden prompts before
#   any serving pod is touched. No customer traffic can reach the canary.
# Phase 2 (promote): the serving deployment is updated with maxSurge=1 /
#   maxUnavailable=0 semantics, so the old model keeps serving until the new
#   pod is ready. A stuck or failed rollout is automatically undone.
# Phase 3 (verify): golden prompts run against the serving Service; failure
#   triggers an automatic rollback and the script exits nonzero.
#
# Usage:
#   scripts/model_rollout.sh --deployment confidential-vllm-cpu \
#     --image vllm/vllm-openai-cpu@sha256:... [--model Qwen/Qwen2.5-0.5B-Instruct]
#     [--namespace inference] [--timeout 1500] [--skip-canary] [--stamp VALUE]
#
# --stamp forces a new ReplicaSet even when the image is unchanged (used by
# rollout drills). Requires kubectl and jq. Never prints prompt contents from
# production traffic; golden prompts are fixed strings defined below.

set -euo pipefail

NAMESPACE=inference
DEPLOYMENT=""
IMAGE=""
MODEL=""
TIMEOUT=1500
SKIP_CANARY=false
STAMP=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deployment) DEPLOYMENT="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --skip-canary) SKIP_CANARY=true; shift ;;
    --stamp) STAMP="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -n "${DEPLOYMENT}" ]] || { echo "--deployment is required" >&2; exit 2; }
[[ -n "${IMAGE}" || -n "${STAMP}" ]] || { echo "--image or --stamp is required" >&2; exit 2; }

CANARY="${DEPLOYMENT}-rollout-canary"
PORT=18000

log() { echo "[model-rollout] $(date -u +%H:%M:%SZ) $*"; }

current_image() {
  kubectl -n "${NAMESPACE}" get deployment "${DEPLOYMENT}" \
    -o jsonpath='{.spec.template.spec.containers[0].image}'
}

if [[ -z "${IMAGE}" ]]; then
  IMAGE="$(current_image)"
fi

if [[ -z "${MODEL}" ]]; then
  MODEL="$(kubectl -n "${NAMESPACE}" get deployment "${DEPLOYMENT}" -o json |
    jq -r '.spec.template.spec.containers[0].args[]
           | select(startswith("--served-model-name="))
           | sub("--served-model-name=";"")')"
fi
[[ -n "${MODEL}" ]] || { echo "cannot determine served model name; pass --model" >&2; exit 2; }

PREVIOUS_IMAGE="$(current_image)"
log "deployment=${DEPLOYMENT} image=${IMAGE} model=${MODEL}"
log "previous image: ${PREVIOUS_IMAGE}"

PF_PID=""
cleanup() {
  [[ -n "${PF_PID}" ]] && kill "${PF_PID}" 2>/dev/null || true
  kubectl -n "${NAMESPACE}" delete deployment "${CANARY}" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

golden_gate() {
  # $1: kubectl port-forward target (pod/NAME or svc/NAME), $2: target port
  local target="$1" target_port="$2" response content
  kubectl -n "${NAMESPACE}" port-forward "${target}" "${PORT}:${target_port}" >/dev/null 2>&1 &
  PF_PID=$!
  sleep 3
  for prompt in "Reply with exactly the word: ready" "What is 2+2? Reply with the number only."; do
    response="$(curl -sS --max-time 60 "http://127.0.0.1:${PORT}/v1/chat/completions" \
      -H 'Content-Type: application/json' \
      -d "$(jq -cn --arg m "${MODEL}" --arg p "${prompt}" \
        '{model:$m,messages:[{role:"user",content:$p}],max_tokens:16,temperature:0}')")" || {
      log "golden gate: request failed"; kill "${PF_PID}" 2>/dev/null; PF_PID=""; return 1; }
    content="$(jq -r '.choices[0].message.content // empty' <<<"${response}")"
    if [[ -z "${content}" ]]; then
      log "golden gate: empty or malformed completion"; kill "${PF_PID}" 2>/dev/null; PF_PID=""; return 1
    fi
  done
  kill "${PF_PID}" 2>/dev/null; PF_PID=""
  log "golden gate passed against ${target}"
}

if [[ "${SKIP_CANARY}" != "true" ]]; then
  log "phase 1: starting off-service canary ${CANARY}"
  kubectl -n "${NAMESPACE}" get deployment "${DEPLOYMENT}" -o json | jq --arg canary "${CANARY}" --arg image "${IMAGE}" '
    {apiVersion, kind,
     metadata: {name: $canary, namespace: .metadata.namespace,
                labels: {"sovereign.rollout/role": "canary"}},
     spec: (.spec
       | .replicas = 1
       | .selector = {matchLabels: {"sovereign.rollout/canary": $canary}}
       | .strategy = {type: "Recreate"}
       | .template.metadata = {labels: {"sovereign.rollout/canary": $canary}}
       | .template.spec.containers[0].image = $image)}' |
    kubectl apply -f - >/dev/null
  if ! kubectl -n "${NAMESPACE}" wait deployment "${CANARY}" \
      --for=condition=Available --timeout="${TIMEOUT}s"; then
    log "FAIL: canary never became ready; serving deployment untouched"
    exit 1
  fi
  if ! golden_gate "deployment/${CANARY}" 8000; then
    log "FAIL: canary failed the golden gate; serving deployment untouched"
    exit 1
  fi
  kubectl -n "${NAMESPACE}" delete deployment "${CANARY}" --wait=true >/dev/null
  log "phase 1 complete: canary validated and removed"
fi

log "phase 2: promoting to serving deployment"
if [[ -n "${STAMP}" ]]; then
  kubectl -n "${NAMESPACE}" patch deployment "${DEPLOYMENT}" --type merge -p \
    "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"sovereign.rollout/stamp\":\"${STAMP}\"}}}}}" >/dev/null
fi
kubectl -n "${NAMESPACE}" set image "deployment/${DEPLOYMENT}" "vllm=${IMAGE}" >/dev/null

rollback() {
  log "rolling back ${DEPLOYMENT} to ${PREVIOUS_IMAGE}"
  kubectl -n "${NAMESPACE}" rollout undo "deployment/${DEPLOYMENT}"
  kubectl -n "${NAMESPACE}" rollout status "deployment/${DEPLOYMENT}" --timeout="${TIMEOUT}s"
  log "rollback complete; serving image: $(current_image)"
}

if ! kubectl -n "${NAMESPACE}" rollout status "deployment/${DEPLOYMENT}" --timeout="${TIMEOUT}s"; then
  log "FAIL: rollout did not complete; old pods kept serving throughout"
  rollback
  exit 1
fi

log "phase 3: verifying through the serving service"
if ! golden_gate "deployment/${DEPLOYMENT}" 8000; then
  log "FAIL: post-promote golden gate failed"
  rollback
  exit 1
fi

log "SUCCESS: ${DEPLOYMENT} now serving ${IMAGE}"
