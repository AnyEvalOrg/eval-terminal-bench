#!/bin/bash
# --timeout-multiplier scales all phase deadlines unless a phase override is set.
# --agent-timeout-multiplier 1.0 explicitly preserves each task's 8 h agent budget;
# increasing --timeout-multiplier therefore affects other phases, not that budget.
set -euo pipefail
set +x
TB_ROOT=/Users/jperla/josh/repos/eval-terminal-bench
TB_SCRATCH=/private/tmp/claude-501/-Users-jperla-josh/7b8624b6-7520-461a-875c-e13899ff3a86/scratchpad
TB_VENV="$TB_SCRATCH/harbor-venv"
cd "$TB_ROOT"
export KUBECONFIG="$TB_SCRATCH/fakehome/.kube/config"
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/keys/claude-automation.json"
export CLOUDSDK_CONFIG="$TB_SCRATCH/fakehome/.config/gcloud"
export OPENAI_BASE_URL=https://api.trustedrouter.com/v1
export HARBOR_SPIKE_CACHE_DIR="$TB_ROOT/.harbor-cache"
export PYTHONPATH="$TB_ROOT/cli_support:$TB_ROOT"
export ANYEVAL_TB_EGRESS_PROXY=1 ANYEVAL_TB_NO_SPOT=1
export ANYEVAL_TB_TMUX_STATIC="${ANYEVAL_TB_TMUX_STATIC:-$TB_SCRATCH/tools/tmux-3.5a-linux-amd64-static}"
# Prevent a caller's old protocol override from silently altering this run.
unset ANYEVAL_TB_OFFLINE_PROTOCOL
[[ -f "$ANYEVAL_TB_TMUX_STATIC" ]] || { echo 'Static tmux binary missing' >&2; exit 1; }
TB_NAMES=$("$TB_VENV/bin/python" "$TB_ROOT/k8s/eligible_tasks.py" --output "$TB_ROOT/eligibility-4.0.json")
TB_FLAGS=()
while IFS= read -r TB_NAME; do
  [[ -n "$TB_NAME" ]] && TB_FLAGS+=(--include-task-name "$TB_NAME")
done <<< "$TB_NAMES"
(( ${#TB_FLAGS[@]} > 0 )) || { echo 'No eligible tasks' >&2; exit 1; }
# --dry-run validates selection/arguments without credentials, Harbor, or cluster access.
TB_COMMAND=("$TB_VENV/bin/harbor" run --path "$TB_SCRATCH/tb/4.0.0/terminal-bench"
  -a terminus-2 -m openai/gpt-5-mini
  --ak api_base=https://api.trustedrouter.com/v1
  --ak 'llm_call_kwargs={"extra_body":{"provider":{"only":["openai"],"order":["openai"],"allow_fallbacks":false}}}'
  --env terminal_bench_anyeval.k8s_env:AnyEvalK8sEnvironment "${TB_FLAGS[@]}"
  --agent-timeout-multiplier 1.0 -n 4 -k 1 -r 1 -o "$TB_ROOT/jobs")
if [[ "${1:-}" == --dry-run && $# == 1 ]]; then
  printf '%q ' "${TB_COMMAND[@]}"; printf '\n'; exit 0
fi
[[ $# == 0 ]] || { echo 'Usage: run_40_eligible.sh [--dry-run]' >&2; exit 2; }
OPENAI_API_KEY=$(CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE="$GOOGLE_APPLICATION_CREDENTIALS" gcloud secrets versions access latest --secret=archimedes-tr-key --project=openevalz-sbx-84737)
export OPENAI_API_KEY
trap '"$TB_VENV/bin/python" "$TB_ROOT/scan_job_secrets.py" "$TB_ROOT/jobs" > "$TB_ROOT/secret-audit.json"; unset OPENAI_API_KEY' EXIT
"${TB_COMMAND[@]}"
