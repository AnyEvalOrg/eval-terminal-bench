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
  --ak api_base=https://api.trustedrouter.com/v1 --ak 'llm_call_kwargs={"extra_body":{"provider":{"only":["openai"],"order":["openai"],"allow_fallbacks":false}}}' \
  --env terminal_bench_anyeval.k8s_env:AnyEvalK8sEnvironment \
   --include-task-name filter-js-from-html --include-task-name gpt2-codegolf --include-task-name mteb-leaderboard --include-task-name torch-pipeline-parallelism --include-task-name torch-tensor-parallelism --include-task-name caffe-cifar-10 --include-task-name compile-compcert --include-task-name mcmc-sampling-stan --include-task-name merge-diff-arc-agi-task --include-task-name query-optimize --include-task-name schemelike-metacircular-eval --include-task-name train-fasttext --include-task-name portfolio-optimization --include-task-name dna-insert --include-task-name mailman --include-task-name large-scale-text-editing --include-task-name extract-moves-from-video -n 4 -k 1 -r 1 -o "$SPIKE_ROOT/jobs"
