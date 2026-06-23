#!/usr/bin/env bash
# Emit the FULL pairwise (server, runner) backwards-compat matrices on
# $GITHUB_OUTPUT as `e2e_matrix` and `integration_matrix`.
#
# The version universe is `main` (the checked-out code = client + tests, always)
# plus every non-rc release tag. We cross every server version with every runner
# version — each cell pins the server and/or runner subprocess to that build
# (an empty/"main" value leaves that component on the checked-out code). The
# (main, main) cell is omitted: it pins nothing and is exactly the normal e2e
# gate. Integration is the single openai-agents leg (claude-sdk/codex reject the
# mock LLM's "mock-model" — see integration-matrix.sh), crossed with the pairs.
#
# Env in:
#   VERSIONS    optional comma-separated override of the version set used for
#               BOTH axes (e.g. "main,v0.2.0"). Empty = main + all non-rc tags.
#   NUM_SHARDS  e2e shard count per cell (default 4).
# Out (GITHUB_OUTPUT):
#   e2e_matrix={"include":[{"server":..,"runner":..,"shard_id":..,"num_shards":..}, ...]}
#   integration_matrix={"include":[{"server":..,"runner":..,"harness":..,"model":..,"workers":..}, ...]}

set -euo pipefail

if [ -n "${VERSIONS:-}" ]; then
  IFS=',' read -ra V <<<"$VERSIONS"
else
  # Portable (no mapfile/bash-4): main + every non-rc release tag, newest first.
  V=("main")
  while IFS= read -r tag; do
    [ -n "$tag" ] && V+=("$tag")
  done < <(git tag --sort=-v:refname | grep -vi rc)
fi
num_shards="${NUM_SHARDS:-4}"

# The integration suite runs a single openai-agents leg in mock mode (matches
# integration-matrix.sh); the model name is unused under the mock LLM.
integ_harness="openai-agents"
integ_model="databricks-gpt-5-4-mini"
integ_workers="4"

e2e_items=()
integ_items=()
for s in "${V[@]}"; do
  for r in "${V[@]}"; do
    # Skip the all-main cell: it pins nothing (== the normal e2e gate).
    if [ "$s" = "main" ] && [ "$r" = "main" ]; then
      continue
    fi
    integ_items+=("{\"server\":\"$s\",\"runner\":\"$r\",\"harness\":\"$integ_harness\",\"model\":\"$integ_model\",\"workers\":$integ_workers}")
    for ((i = 0; i < num_shards; i++)); do
      e2e_items+=("{\"server\":\"$s\",\"runner\":\"$r\",\"shard_id\":$i,\"num_shards\":$num_shards}")
    done
  done
done

e2e_json=$(
  IFS=,
  echo "${e2e_items[*]}"
)
integ_json=$(
  IFS=,
  echo "${integ_items[*]}"
)

{
  echo "e2e_matrix={\"include\":[$e2e_json]}"
  echo "integration_matrix={\"include\":[$integ_json]}"
} >>"${GITHUB_OUTPUT:-/dev/stdout}"

echo "versions: ${V[*]}" >&2
echo "pairs: $((${#integ_items[@]})) (excludes main/main); e2e jobs: ${#e2e_items[@]}; integration jobs: ${#integ_items[@]}" >&2
