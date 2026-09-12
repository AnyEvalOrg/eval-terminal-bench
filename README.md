# Terminal-Bench for AnyEval

This package provides the eligible Terminal-Bench 2.1.0 (89 tasks) and 4.0.0
(44 tasks) catalogues. Task bytes are fetched from Harbor and verified against
committed manifests at worker build time.
Task files are Apache-2.0. This is the AnyEval protocol using Terminus 2 from
Harbor 0.22.0; results should identify that scaffold and this eligibility policy.

Install with Python 3.12 or later:

```sh
python -m pip install '.[inspect]'
python -m terminal_bench_anyeval.fetch_data
python -m terminal_bench_anyeval.fetch_data --verify-only
```

The package depends on `harbor==0.22.0`; the extra pins
`inspect_ai==0.3.260` and `kubernetes==36.0.3`. Inspect discovers
`terminal_bench_anyeval/terminal_bench_2_1` and
`terminal_bench_anyeval/terminal_bench_4_0`. They contain 89 and 44 samples
respectively, in the order of each eligibility file's `included` list.
Sample IDs are task names; input is the original instruction. Metadata includes
only category, timeouts, resource requests, verifier mode and dataset provenance.
No Inspect sandbox is declared. The solver deliberately raises under plain
Inspect: the catalogue's `execution: harbor` route must execute the child.

The runner starts an authenticated worker-local OpenAI-compatible model shim,
then invokes exactly one child per attempt:

```sh
python -m terminal_bench_anyeval.trial --spec /path/spec.json --result /path/result.json
```

The v1 spec carries `dataset` (`terminal-bench-2-1` or `terminal-bench@4.0.0`),
`task`, absolute fetched `task_dir`, `api_base` (loopback HTTP `/v1`), `api_key`
(per-run shim token), `model` (`openai/<gateway model>`), `agent: terminus-2`,
`agent_kwargs`, `env`, `namespace: anyeval-sandbox`, `kubeconfig`,
`timeouts: {agent_sec, verifier_sec}`, positive `attempt`, and `run_id`.
Build the settings with `agent_settings.agent_kwargs(provider=...)`; this is the
single source of the pinned JSON parser, terminal recording, summarisation,
8000-token proactive threshold, LiteLLM backend and chat-completions protocol.
The optional gateway pin is passed unchanged in
`llm_call_kwargs.extra_body.provider`. Credentials are set in the child's
process environment from the spec, never in `llm_kwargs`, AgentConfig, or pods.
The delegated gateway key belongs only to the runner's shim.

The child calls `Trial.create` and `run` once. It atomically writes contract v1
results for success, exceptions, malformed specs and SIGTERM, returning 0 when a
result was written. SIGTERM writes a fallback immediately and cancels Harbor;
cleanup retries owned environments and rewrites the result with final evidence.
The runner remains responsible for watchdogs, orphan cleanup and provenance
validation. SIGKILL, host loss, failure to start Python, and an unwritable result
location cannot be recovered by a child. Raw Harbor files and diagnostics reside
beside the result under `harbor/` and `result.json.private.log`.

For 2.1 Harbor stages tests after the agent phase into the shared pod. For 4.0
Harbor creates a separate verifier pod using the task's prebuilt verifier image,
collects the declared agent artifacts, and uses the image's `/tests` entrypoint.
The compatibility hook retries verifier output download, never verification.

The Kubernetes adapter uses gVisor, disables service-account token mounting,
requests the task's CPU/memory/storage, refuses downward admission changes and
records upward changes. It retries transient exec failures for transfers. Pods
and NetworkPolicies are deleted even when Harbor requests `delete=False`.
The adapter records pod and policy UIDs, image digest, requested/admitted resources,
node/runtime evidence, proxy identity and lifecycle timestamps for the runner.
Missing evidence is null and must not be treated as successful attestation.

The catalogue records `ANYEVAL_TB_EGRESS_PROXY=1`,
`ANYEVAL_TB_OFFLINE_PROTOCOL=1`, and `ANYEVAL_TB_NO_SPOT=1` as protocol
environment settings for the runner to carry in the spec.

The worker must provide the `anyeval-sandbox` kubeconfig, cluster permissions,
predeployed iron-proxy with the packaged `k8s/allowlist.yaml`, and a compatible
static tmux binary if images lack tmux. `env` accepts only
`ANYEVAL_TB_EGRESS_PROXY`, `ANYEVAL_TB_NO_SPOT`, `ANYEVAL_TB_OFFLINE_PROTOCOL`,
and `ANYEVAL_TB_TMUX_STATIC`. Proxy mode is explicit (`1`) and applies to the
agent pod; separate verifier pods have deny-all egress. Both supplied datasets resolve to Harbor public-network baselines (2.1 explicitly
declares internet access; 4.0 inherits Harbor's default). Therefore the runner
must explicitly pass `ANYEVAL_TB_EGRESS_PROXY=1` or
`ANYEVAL_TB_OFFLINE_PROTOCOL=1` to accept this restricted protocol for both
datasets; public/legacy `allow_internet` declarations are refused when neither
is set. The adapter records that decision. No proxy
or static binary is deployed by installing this package. The spike `run*.sh`
scripts retain their existing behavior and now use the package adapter path.

## What is packaged and why

The wheel, source distribution, and repository contain the adapter code,
`eligibility-*.json`, and `manifest.json`; they contain **no benchmark task
bytes**. Runtime retrieval keeps the upstream verifier bytes unchanged while
avoiding 167 MB of task data and upstream placeholder tokens in git. The two
runtime dataset directories are ignored and excluded from distributions even
when fetched into the checkout.

`python -m terminal_bench_anyeval.fetch_data` (also `python scripts/fetch_data.py`)
uses `harbor dataset download --export` for
`terminal-bench/terminal-bench-2-1` and
`terminal-bench/terminal-bench@4.0.0`. Downloads and pruned results are staged
in temporary directories. Only eligible tasks' `task.toml`, `instruction.md`,
and `tests/**` are installed; **solutions and environment trees are never
installed**. The 4.0 exclusions are taken from the committed eligibility list.
Harbor needs the unchanged tests for verifier staging and execution.

Every installed file must match the committed SHA256 and exact retained file
set. Missing files, extra retained files, unexpected tasks, and hash drift fail
with a filename and nonzero exit status before staged data is installed. The
original manifest also pins task READMEs: retrieval verifies those bytes before
pruning them, preserving the original manifest and registry inventory digest.
The manifest and eligibility files always come from the installed package,
never from the downloaded data root.

Set `ANYEVAL_TB_DATA_DIR` to choose the runtime data root. The default is the
package's `data/` directory when writable, otherwise
`~/.cache/anyeval/terminal-bench`. Use the same explicit setting during worker
build and execution, especially when the runtime user differs from the build
user. Missing data produces a `run python -m terminal_bench_anyeval.fetch_data`
error; catalogue construction only reads metadata and instructions, never tests.
Repeated fetches verify existing installations without downloading again; corrupt
existing data is a hard error. `--verify-only` checks both datasets without
network access or writes. See [worker and local installation lines](docker/worker-snippet.md).

**Task `environment/` directories are not packaged**. Both roles require prebuilt
images; AnyEval never builds their contexts. In Harbor 0.22.0, prebuilt context
staging applies only to nonempty contexts without a Dockerfile or Compose build
spec. None of the included tasks' downloaded agent contexts uses that path.
The adapter skips an absent context and records
`environment_context: "not packaged (prebuilt image)"` in pod facts. Separate
verifier `tests/` contexts retain Harbor's staging behavior. The packaging script
refuses to discard any future included environment that needs runtime staging.

Eligibility is a reproducible metadata/filename scan: no GPUs/accelerators,
no Compose manifests, storage at most 10 GiB in each required environment,
and prebuilt images for agent and separate verifier. Network requirements and
successful image pulls are not eligibility criteria. The 22 excluded 4.0 task
directories are omitted; `eligibility-4.0.json` retains their IDs and reasons,
and `manifest.json` retains each excluded `task.toml` hash. Scanning fetched data
reproduces the eligibility files' `included` lists. Reproducing exclusion reasons
requires the original download.

The manifest hashes every retained file, separately identifies `task.toml` and
`instruction.md`, and hashes the canonical tests filename-to-SHA256 mapping.
Its `dropped_environment_trees` map preserves all 155 original `environment/`
tree hashes, including excluded tasks: SHA-256 of canonical JSON mapping
environment-relative POSIX filenames to their file SHA-256 hex digests.

**Provenance limitation:** the provided downloads contain no upstream registry
response, and the registry was unreachable during packaging. `registry_digest`
is explicitly a `downloaded-task-inventory-sha256`, binding dataset name/version
and retained task bytes, excluded metadata hashes and dropped environment tree
hashes. `upstream_registry_digest` is null and
`upstream_registry_verified` is false. This is not an upstream registry attestation.

Public export must apply the packaged `redaction.yaml` through AnyEval's consumer.
It redacts exact structured keys for tests, verifier streams, private artifacts,
raw task/config/result material and exception content. YAML keys are not file
path patterns. File attachment publishers must use
`publication.public_artifact_paths`: only agent trajectory JSON, continuation
and summarisation trajectories are public. Every other Harbor file is private,
including verifier output, recordings, proxy logs, downloaded artifacts and raw
configuration. Never publish the task data or trial directory recursively.
The agent transcript remains public; installing this package alone does not
sanitize arbitrary exports or remove test material deliberately copied into a
transcript by a compromised environment.

Run local checks without a cluster or model calls:

```sh
python -m pip install -e '.[inspect,test]'
python -m pytest -q
python -m pip wheel --no-deps --no-build-isolation -w dist .
```

Tests cover fake Harbor round trips and real SIGTERM delivery, exception results,
settings and provider forwarding, both catalogue tasks with synthetic data,
registry pruning and drift failures, eligibility rescans, manifest consistency,
the AnyEval redaction parser and installed wheel contents.
`ANYEVAL_APP_ROOT` may point to an AnyEval checkout for the consumer integration
check. `scripts/package_data.py --source <download-root>` is the legacy maintainer tool
for regenerating provenance from an original download; it rewrites pins and is
not part of installation. Normal retrieval never changes the committed manifests.
