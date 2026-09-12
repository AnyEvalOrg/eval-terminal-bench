#!/bin/zsh
# Strict original-dataset run. As downloaded, regex-log is refused for internet.
set -eu
set +x
SPIKE_ROOT=/Users/jperla/josh/repos/eval-terminal-bench
SPIKE_SCRATCH=/private/tmp/claude-501/-Users-jperla-josh/7b8624b6-7520-461a-875c-e13899ff3a86/scratchpad
SPIKE_VENV="$SPIKE_SCRATCH/harbor-venv"
cd "$SPIKE_ROOT"
export KUBECONFIG="$SPIKE_SCRATCH/fakehome/.kube/config"
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/keys/claude-automation.json"
export CLOUDSDK_CONFIG="$SPIKE_SCRATCH/fakehome/.config/gcloud"
export OPENAI_BASE_URL=https://api.trustedrouter.com/v1
export HARBOR_SPIKE_CACHE_DIR="$SPIKE_ROOT/.harbor-cache"
export PYTHONPATH="$SPIKE_ROOT/cli_support:$SPIKE_ROOT"
# Assignment must succeed before export; export alone masks substitution failure.
OPENAI_API_KEY=$(CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE="$GOOGLE_APPLICATION_CREDENTIALS" gcloud secrets versions access latest --secret=archimedes-tr-key --project=openevalz-sbx-84737)
export OPENAI_API_KEY
trap '"$SPIKE_VENV/bin/python" "$SPIKE_ROOT/scan_job_secrets.py" "$SPIKE_ROOT/jobs" > "$SPIKE_ROOT/secret-audit.json"; unset OPENAI_API_KEY' EXIT
"$SPIKE_VENV/bin/harbor" run \
  --path "$SPIKE_SCRATCH/tb/2.1.0-dl/terminal-bench-2-1" \
  -a terminus-2 -m openai/gpt-5-mini \
  --ak api_base=https://api.trustedrouter.com/v1 \
  --env terminal_bench_anyeval.k8s_env:AnyEvalK8sEnvironment \
  -n 4 -k 1 -r 1 -o "$SPIKE_ROOT/jobs"
