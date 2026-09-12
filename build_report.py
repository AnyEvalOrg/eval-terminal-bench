"""Build the metadata-only feasibility audit and spike report."""
import json
from pathlib import Path
import tomllib
import warnings

from anyeval_k8s import AnyEvalK8sEnvironment, COMPOSE_NAMES
from harbor.models.task.config import TaskConfig
from harbor.models.trial.paths import TrialPaths

ROOT = Path(__file__).parent
SCRATCH = Path('/private/tmp/claude-501/-Users-jperla-josh/7b8624b6-7520-461a-875c-e13899ff3a86/scratchpad')
DATASET = SCRATCH / 'tb/2.1.0-dl/terminal-bench-2-1'
rows = []
for path in sorted(DATASET.glob('*/task.toml')):
    raw = tomllib.loads(path.read_text())
    env = raw['environment']
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        config = TaskConfig.model_validate(raw)
    try:
        AnyEvalK8sEnvironment(environment_dir=path.parent / 'environment',
                              environment_name=path.parent.name,
                              session_id='metadata-audit__env',
                              trial_paths=TrialPaths(ROOT / 'metadata-audit'),
                              task_env_config=config.environment)
        refusal = None
    except (ValueError, RuntimeError) as exc:
        refusal = str(exc)
    rows.append({'task': path.parent.name, **{k: env[k] for k in
                ('cpus', 'memory_mb', 'storage_mb', 'gpus', 'allow_internet', 'docker_image')},
                 'compose_files': [n for n in COMPOSE_NAMES if (path.parent/'environment'/n).exists()],
                 'separate_verifier': raw['verifier'].get('environment_mode') == 'separate',
                 'refusal': refusal})
assert len(rows) == 89
assert all(row['refusal'] and row['allow_internet'] for row in rows)
(ROOT / 'feasibility.json').write_text(json.dumps(rows, indent=2) + '\n')

audit = json.loads((ROOT/'secret-audit.json').read_text())
table = '\n'.join(f"| {r['task']} | {r['cpus']} | {r['memory_mb']} | {r['storage_mb']} | Refused: allow_internet=true |"
                  for r in rows)
report = f'''# Terminal-Bench 2.1 / AnyEval GKE spike — 2026-09-10

**Outcome: adapter implemented and locally tested; no benchmark trial executed on GKE.**
All 89 tasks in the supplied download explicitly declare `environment.allow_internet = true`.
The requested mandatory refusal therefore excludes all 89 tasks. I preserved the dataset and
the refusal rule. A clarification offering one explicitly modified offline copy received no
answer before this report; no such copy was made.

## Deliverables

- Adapter: `{ROOT / 'anyeval_k8s.py'}`.
- Official CLI launch script: `{ROOT / 'run_spike.sh'}` (run with `zsh`; includes in-memory secret retrieval and post-run scan).
- Contract tests: `{ROOT / 'test_anyeval_k8s.py'}`; **14 passed**, recorded in `unit-tests.log`.
- Metadata audit: `{ROOT / 'feasibility.json'}` (all 89 parsed through Harbor's TaskConfig and actually rejected by adapter construction).
- Secret scanner: `{ROOT / 'scan_job_secrets.py'}`; scan result: `{ROOT / 'secret-audit.json'}`.
- Opt-in cache relocation: `{ROOT / 'cli_support/sitecustomize.py'}`.

## Environment contract

Read the installed Harbor 0.22.0 `environments/base.py`, `environments/gke.py`,
`environments/docker/docker.py`, capability/resource models, factory, tar helpers, and
`trial/trial.py` separate-verifier flow. No benchmark instruction, test, or solution content
was printed. Selection/audit used `task.toml` and filenames only.

Implemented public/contract methods:

| Method | Behavior |
|---|---|
| `__init__` | Fixed namespace, unique DNS-safe pod identity, exact trial label, timeout options; refuses resource overrides. |
| `type` | Returns `anyeval-k8s`, supported by Harbor's custom import factory. |
| `capabilities` | `disable_internet=True`; mounted, GPUs, TPUs, Compose, Windows, allowlists and dynamic networking false. |
| `resource_capabilities` | CPU/memory requests and limits supported. |
| `_validate_definition` | Requires prebuilt image and positive CPU/memory/storage; refuses GPUs, TPUs, Compose filenames, internet and external MCP requirements. Checks original task.toml because Harbor clears legacy allow_internet after translating it. |
| `_validate_resource_mode_support` | Only auto/guarantee, preserving requests == limits. |
| `validate_network_policy_support` | Only no-network; rejects public and allowlist baseline/phase policies before pod creation. |
| `start(force_build)` | Refuses builds; loads supplied kubeconfig, creates deny-all policy first, then pod; waits up to 600 seconds for Running/container running, initializes Harbor directory targets, invokes inherited prebuilt-context staging helper. Failures include events and trigger cleanup. |
| `stop(delete)` | Attempts deletion of both resources even with delete=False, retries API failures, tolerates 404, closes clients. Reports persistent cleanup failures; cannot guarantee deletion during control-plane outages. |
| `exec(command,cwd,env,timeout_sec,user)` | Kubernetes WebSocket exec; distinct stdout/stderr and required remote exit status; shlex quoting, configured workdir, persistent/per-call/scoped environment precedence, username/numeric UID support, incremental output callbacks. GNU timeout terminates timed-out foreground process groups; hard transport timeout returns 124. |
| `upload_file`, `upload_dir` | Binary tar over exec stdin, preserving directory modes, links and empty directories; checks tar exit status. |
| `download_file`, `download_dir` | Binary tar over exec stdout, required exit status; renamed single-file download; shared Harbor directory extractor with data filter. |

Private lifecycle/transport helpers: `_call`, `_ensure_client`, `_manifests`, `_events`,
`_save_facts`, `_wait_running`, `_stream`, `_upload`, `_download`.
Inherited service operations target the main container and reject other services; inherited
directory reset, healthcheck, filtered artifact downloads and log handling use these methods.
`mounted=False` tells Harbor to download logs/artifacts instead of assuming host mounts.

Pod specification: original image reference unchanged; `runtimeClassName: gvisor`;
`cloud.google.com/gke-spot: "true"`; `sleep infinity`; one `main` container;
`restartPolicy: Never`; no service-account token, hostPath, privileged mode, or host mounts.
Labels include `inspect/service: default`, exact `anyeval.io/trial`, and a unique environment
label so agent/verifier deny-all policies do not collide. CPU uses cores, memory and ephemeral
storage use Mi quantities from task MB values (matching Harbor GKE's memory convention).
Requests and limits are equal. Admission-time resource mutations cause refusal, preserving
benchmark resource fidelity. Pod facts record actual imageID/digest, node, runtime and resources
under the trial's `anyeval/` directory if startup reaches Running.

Separate verifier: Harbor `Trial._separate_verifier_env` creates another instance of the same
custom import, passing the resolved verifier EnvironmentConfig, verifier image, tests build
context, role session ID, log target and network policy. This adapter consequently creates a
second pod/policy from that image. Harbor handles artifact staging, uploading tests, grading,
reward retrieval and finally stopping the verifier. Shared mode uses the original pod.
The factory path was tested with a distinct verifier image; no live separate-verifier run was
possible. None of these 89 downloaded tasks declares a separate verifier.

Transfer implementation reuses Harbor's `pack_dir_to_bytes` / `extract_dir_from_bytes` and
uses binary WebSocket output explicitly to avoid corrupting arbitrary tar bytes. The official
[Kubernetes Python stream source](https://raw.githubusercontent.com/kubernetes-client/python/master/kubernetes/stream/ws_client.py)
documents the binary stream and channel-status behavior used here. A private ApiClient per
exec avoids the SDK's stream transport mutation racing ordinary REST calls.

## Selected task and CLI evidence

Candidate: **regex-log**, image `alexgshaw/regex-log:20251031`, 1 CPU, 2048 MB memory,
10240 MB ephemeral storage, zero GPUs. Its metadata describes regex/log analysis and its
environment listing contains one Dockerfile; no Compose files, declared extra ports, external
MCP services, or separate verifier. Dockerfile contents were not examined, so absence of
runtime services cannot be certified from the listing alone. It still declares internet=true.

The supplied dataset directory is actually `{DATASET}`, not `.../terminal-bench`.
`harbor run --help` confirms `--path`/`-p`, `--include-task-name`/`-i`, `--n-tasks`/`-l`,
`--env`/`-e`, `--jobs-dir`/`-o`. There is no `--dataset-path` or `--task-name` in this version.
`-n 1` is concurrency; `-k 1` is attempts. The exact single-task filter plus `--n-tasks 1`
ensures at most one task, with retries disabled.

**No command completed a GKE trial.** The following exact official-CLI invocation reached the
custom environment and correctly failed its mandatory internet validation (exit 1):

```sh
KUBECONFIG={SCRATCH}/fakehome/.kube/config \\
GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/keys/claude-automation.json" \\
HARBOR_SPIKE_CACHE_DIR={ROOT}/.harbor-cache \\
PYTHONPATH={ROOT}/cli_support:{ROOT} \\
OPENAI_BASE_URL=https://api.trustedrouter.com/v1 \\
{SCRATCH}/harbor-venv/bin/harbor run \\
  --path {DATASET} \\
  --include-task-name regex-log --n-tasks 1 \\
  -a terminus-2 -m openai/gpt-5-mini \\
  --env anyeval_k8s:AnyEvalK8sEnvironment \\
  -n 1 -k 1 -r 0 -o {ROOT}/jobs \\
  --job-name tb21-regex-log-spike \\
  --ak api_base=https://api.trustedrouter.com/v1 \\
  >jobs/cli-cache-relocated.log 2>&1
```

This validation invocation inherited the existing process OPENAI_API_KEY; the requested
TrustedRouter key could not be fetched. It never reached any model call. `openai/gpt-5-mini`
was accepted by CLI configuration, but API routing/model compatibility was **not tested**.
`litellm_proxy/openai/gpt-5-mini` was not tried because execution stopped before model access.
The explicit non-secret `api_base` agent kwarg ensures the intended endpoint in the prepared
launch script; no key is supplied through CLI args or configuration kwargs.

## Trial result and pod facts

| Requested fact | Observed value |
|---|---|
| Job | `jobs/tb21-regex-log-spike` |
| Allocated trial directory | `regex-log__4Atm2c3` |
| Outcome | Constructor refused internet requirement, before pod start or agent execution. |
| Reward | N/A — verifier never ran; not a reward of zero. |
| Trial wall time | N/A — no trial executed. Job result has started_at `2026-09-10T08:43:46.739340`, finished_at null after constructor failure. |
| Tokens / trajectory cost | N/A — no trajectory/model call; job token and cost fields are null. |
| Actual image digest | N/A — no image pulled/pod created. Requested tag is listed above. |
| Actual runtimeClass / node | N/A — no admitted pod. Requested runtime is gvisor/Spot. |
| Cleanup | No cluster objects were created by these attempts. |

Harbor left a partial job result with one pending trial, zero completed trials and no trial
result after the constructor exception. Do not interpret that pending count as a live pod.

## Failures and limits

1. All 89 input tasks conflict with the explicit internet refusal requirement. This is the
   immediate blocker even on an otherwise fully working cluster.
2. `python -m pip install kubernetes` in the specified venv failed: package index DNS/network
   unavailable. `kubernetes` was not installed. No cached package was found in the inspected
   temporary/cache locations. Adapter imports the SDK lazily so metadata validation and local
   tests work without it; live startup requires installation.
3. Secret access with the supplied service-account credential failed refreshing ADC at
   `oauth2.googleapis.com` due to DNS resolution failure. No requested secret value was
   returned, printed, or written. Sandbox approval policy prevents requesting network elevation.
4. Initial official CLI invocation failed writing Harbor's first-run notification under
   `/Users/jperla/.cache/harbor` (PermissionError). The opt-in sitecustomize shim redirects
   Harbor cache constants into this workspace, leaving the CLI and runner intact. No HOME
   changes, Harbor package edits, or alternative execution harness were used.
5. Live cluster scheduling, gVisor runtime behavior, policy enforcement, image pull, exec
   transport, agent completion, reward, and model pricing remain unvalidated. Offline tests
   cover manifests/refusals, stream bytes/status/callbacks, real local tar transfers, shell
   quoting, cleanup failures, and verifier factory selection; they do not substitute for a
   GKE integration test. Pods require bash, tar, sleep and GNU timeout; non-root exec additionally
   requires su/getent. Transfers currently buffer archives in host memory.
6. Kubernetes NetworkPolicies are additive; the requested deny-all policy is created, but
   effective isolation also depends on the cluster's existing policies. No live policy audit
   or connectivity test was possible. No namespace-wide policies were modified.

## Secret-leak audit

The requested TrustedRouter secret was unavailable, so its exact leakage status is **not
verified**. An inherited `OPENAI_API_KEY` was present in this tool process; scanning all
{audit.get('files_scanned', 0)} current job files found **zero matches** for that value in literal,
Base64, URL-encoded or JSON-escaped form, with no unreadable files. This is not proof that the
inherited key equals `archimedes-tr-key`. No trajectory files were generated. The scanner
emits only relative paths and encoding names, never secret bytes. A synthetic binary-file
sentinel check passed. The prepared run script fetches the requested key only into process
memory, calls the official CLI, then scans job files before unsetting it.

Installed Harbor LiteLLM logging hashes top-level `api_key` and `x-api-key` fields. That source
inspection alone cannot establish leak freedom for nested request fields or future trajectories;
an actual completed keyed run is still required for the requested empirical conclusion.

## All 89 tasks: strict feasibility

This is a **metadata feasibility audit**, not an empirical task success estimate. Each row was
constructed through the real adapter's validation. All 89 declare internet=true; all have
zero GPUs, none has a recognized Compose filename, and none declares separate verifier mode.
Eligibility if task metadata were changed is unknown; no such changes were authorized or made.

| Task | CPUs | Memory MB | Storage MB | Strict result / reason |
|---|---:|---:|---:|---|
{table}
'''
destination = SCRATCH / 'CODEX-REPORT-tb-spike.md'
destination.write_text(report)
print(f'Report: {destination}')
print(f'Feasibility rows: {len(rows)}; refused: {sum(bool(r["refusal"]) for r in rows)}')
