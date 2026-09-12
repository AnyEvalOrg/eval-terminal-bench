# AnyEval / Terminal-Bench child contract v1.1

This is the 2026-09-12 publication extension to v1. The JSON wire discriminator
remains `version: 1` for compatibility with the application's Harbor parser.
The worker launches exactly one child:

```
/opt/anyeval/harbor-venv/bin/python -I -m terminal_bench_anyeval.trial --spec /absolute/spec.json --result /absolute/result.json
```

The worker uses two interpreters: its app environment installs the base
`eval-terminal-bench` wheel alongside the app's Inspect and OpenAI 3.x pins;
`/opt/anyeval/harbor-venv` installs `eval-terminal-bench[trial]` (Harbor 0.22.0,
Kubernetes 36.0.3). Set
`ANYEVAL_TB_TRIAL_PYTHON=/opt/anyeval/harbor-venv/bin/python` so the runner
probes and launches that interpreter. Harbor's LiteLLM/OpenAI dependencies remain in the
child environment. Both interpreters share `ANYEVAL_TB_DATA_DIR`. Fetching and
verification work in the base interpreter using the Harbor CLI when available,
or plain HTTP to the public package registry. This changes interpreter
selection only; spec/result v1 and process isolation are unchanged.


The parent owns the model shim (loopback HTTP, authenticated non-streaming OpenAI
chat completions), provider routing, receipts, budgets and termination. The child
receives only the per-run shim credential, never the delegated provider key. The
parent creates the spec exclusively with mode 0600 and revokes it after cleanup.
The child writes its result atomically on every exit path, including validation
errors and SIGTERM; writing a result permits exit 0, not a claim of success.

## Spec fields

| Field | Type / meaning |
| --- | --- |
| `version` | Integer `1` |
| `dataset` | `terminal-bench-2-1` or `terminal-bench@4.0.0` |
| `task` | Eligible native task/sample ID |
| `sample_id` | Optional string; if supplied must equal `task` |
| `task_dir` | Absolute path to the selected installed and integrity-checked task |
| `run_id` | Nonempty parent run identity |
| `attempt` | Positive integer |
| `trial_id` | Required nonempty parent-authorized trial identity; never replaced with Harbor's ID |
| `api_base` | Worker-local HTTP `/v1` shim endpoint with an explicit port |
| `api_key` | Per-run shim bearer token |
| `model` | `openai/<authorized gateway model>` |
| `agent` | `terminus-2` |
| `agent_kwargs` | Pinned Terminus settings validated by `agent_settings.py`; no credentials |
| `env` | Allowlisted string values: `ANYEVAL_TB_EGRESS_PROXY`, `ANYEVAL_TB_NO_SPOT`, `ANYEVAL_TB_TMUX_STATIC`, `ANYEVAL_TB_OFFLINE_PROTOCOL` |
| `namespace` | `anyeval-sandbox` |
| `kubeconfig` | Worker-local Kubernetes configuration path |
| `timeouts.agent_sec`, `timeouts.verifier_sec` | Positive finite seconds |

## Result fields

| Field | Type / meaning |
| --- | --- |
| `version` | Integer `1` |
| `run_id`, `sample_id`, `attempt`, `trial_id` | Echoed parent binding (`sample_id` comes from `task`) |
| `harbor_trial_id` | Separate internal Harbor ID, or null if creation failed |
| `outcome` | `verified`, `agent_timeout`, `verifier_timeout`, `infrastructure`, or `error` |
| `reward` | Binary number 0/1 for verified results, otherwise null |
| `exception` | Null or `{type, message}`; credential-redacted |
| `agent` | Numeric `episodes`, `input_tokens`, `output_tokens`, `cache_tokens`, `summarizations` |
| `pods` | Agent and, when required by the task, separate verifier records below |
| `verifier_health` | Boolean `setup_completed`, `completed` |
| `verified_artifacts` | Actual transfer hash records below; empty when no transfers occurred |
| `artifacts` | `trajectory_path`, `verifier_stdout_path`, `proxy_log_path`; existing paths or null |
| `timing` | ISO-8601 `started_at`, `finished_at`; numeric `agent_seconds`, `verifier_seconds` |

Artifact paths must resolve inside the child result directory. The app bounds
reads and independently hashes their literal file bytes. These files remain
subject to the package publication/redaction rules; verifier material is private.
Errors can contain incomplete/null evidence and never substitute desired state.

## Every pod record

| Field | Meaning |
| --- | --- |
| `role` | `agent` or `verifier` |
| `name`, `namespace`, `uid` | Live Kubernetes pod identity |
| `run_id`, `sample_id`, `attempt`, `trial_id` | Live metadata binding checked against the parent at Running |
| `labels` | All live pod labels at Running |
| `finished_labels` | All live pod labels at stop |
| `created_uid`, `finished_uid` | UID observed at Running and immediately before deletion |
| `resource_version`, `finished_resource_version` | Pod resourceVersions at those observations; status changes may advance them |
| `container_name`, `container_id` | Main container name (`main`) and runtime container ID; final ID must equal initial ID |
| `restart_count` | Sum of regular-container restart counts, refreshed at stop; publication requires integer zero |
| `node` | Live assigned node name |
| `runtime_class_name` | Admitted RuntimeClass name (`gvisor`) |
| `runtime_class_exists` | Boolean result of live NodeV1 RuntimeClass lookup; false on lookup failure |
| `runtime_class_handler`, `runtime_class_uid` | RuntimeClass handler and API UID, or null if unavailable |
| `image` | Requested image reference |
| `image_digest` | Resolved main-container digest, `sha256:` plus 64 lowercase hex digits |
| `resources_requested`, `resources_admitted` | Kubernetes resource maps (`requests`, `limits`) before and after admission |
| `kernel_release`, `dmesg_gvisor_boot` | Guest `uname -r` and gVisor boot evidence captured before execution |
| `kubelet_version`, `node_labels` | Node status kubelet version and relevant `sandbox.gke.io/runtime` label |
| `selecting_policy_uids`, `finished_selecting_policy_uids` | Sorted UIDs of ALL same-namespace policies selecting this pod at initial/final capture |
| `network_policy` | Live selecting package policy record below |
| `proxy` | Agent's live proxy record below; null for separate verifier |
| `started_at`, `ended_at` | ISO-8601 start and cleanup timestamps |
| `environment_context` | Optional diagnostic string for omitted prebuilt-image build context, otherwise null |

Identity labels are `anyeval.io/run-id`, `anyeval.io/sample-id`,
`anyeval.io/attempt`, and `anyeval.io/trial-id`. Kubernetes-compatible values are
literal; other values use the first 63 lowercase hex digits of SHA-256(UTF-8).
Annotations under the same keys retain exact values. The adapter checks both
labels and annotations before saving observed bindings. The existing
`anyeval.io/trial` identifies Harbor's local directory and is not the parent ID.

## NetworkPolicy record

`name`, `uid`, and `resource_version` identify the API object. `spec` is its live
Kubernetes camelCase spec, with absent/null ingress and egress lists normalized
to empty arrays. `observed_from` is `kubernetes_api`, `source` is `package_policy`,
and `pod_uid` identifies the selected live pod. `finished_resource_version` is
read again before deletion and must equal the original revision.

`mode` is `enforce`; `deny_all` reports whether egress is empty;
`egress_to_proxy_only` records the checked agent proxy profile. Both policyTypes
must be present (`Ingress`, `Egress`), ingress is empty, the agent permits only
the same-namespace proxy pod selector on TCP 8080 and TCP/UDP 53, and a separate
verifier has empty egress. All selecting policies are enumerated. Additional selecting policies are permitted
only when both `spec.ingress` and `spec.egress` are absent, null, or empty arrays:
they add no allows in either direction. A nonempty rule list (including `[{}]`)
is refused, regardless of policyTypes. This permits the namespace-wide
`deny-all-egress` policy (`podSelector: {}`). The adapter records each extra policy
in `additional_network_policies` with `name`, `uid`, `resource_version`, live
`spec`, `observed_from: kubernetes_api`, `pod_uid`, and `effect: "adds no allows"`.
`finished_additional_network_policies` must equal the initial list (sorted by UID).
The app validator must independently check these rule lists and metadata,
require the selecting UID lists to equal the package policy UID plus all recorded
extra UIDs, and require initial/final evidence equality. Policy drift, pod
replacement/restarts, missing final capture, or changed labels fail the attempt
while cleanup deletes the owned pod first. Its policy remains until a live read
confirms 404 or a terminal phase has persisted for at least the pod's termination
grace period (minimum one second). Failed deletion or unconfirmed termination
retains isolation and reports infrastructure failure.

## Proxy record

| Field | Meaning |
| --- | --- |
| `pod`, `pod_uid`, `finished_pod_uid` | Initial name/UID and final UID of the captured ready proxy |
| `image_digest` | Live resolved proxy image digest |
| `serving_container` | Checked live container name, pinned image, args, ConfigMap name, mount path, and read-only flag; rechecked at final capture |
| `labels` | All live proxy labels |
| `service_ip`, `service_uid` | Live iron-proxy Service ClusterIP and UID |
| `endpoint_uids` | All ready endpoint Pod targetRef UIDs read from the Kubernetes Endpoints API |
| `mode`, `allowlist_mode` | Observed allowlist mode (`warn` or `enforce`); warn is refused |
| `allowlist_version`, `allowlist_sha256` | Mounted allowlist version and canonical source JSON SHA-256 |
| `active_config_name`, `finished_config_name` | Initial and final mounted immutable ConfigMap names |
| `active_config_sha256`, `finished_config_sha256` | Initial/final mounted allowlist source hashes; app requires equality with `allowlist_sha256` |
| `configmap`, `configmap_uid`, `finished_configmap_uid` | ConfigMap name alias and initial/final API UIDs |
| `config_sha256`, `finished_proxy_config_sha256` | SHA-256 of the actual UTF-8 `proxy.yaml` ConfigMap value initially/finally |
| `allowlist_transform_sha256` | Canonical JSON hash of the active allowlist transform |
| `proxy_ip`, `tls_mode`, `cluster_cidrs` | Service-IP alias, checked `sni-only` mode, recorded blocked cluster CIDRs |
| `network_policies` | All live policies selecting the proxy: `name`, `uid`, `resource_version`, `spec`, `observed_from: kubernetes_api`, `source: proxy_policy`, `pod_uid`, `finished_resource_version` |

Canonical JSON uses sorted keys and comma/colon separators, UTF-8, then SHA-256
(lowercase hex without a prefix). Active/final configuration hashes above follow
the application's allowlist-hash convention; the separate proxy configuration
hashes identify literal config bytes. Both are recorded, never conflated.

The adapter verifies the sole serving container in both Deployment and ready Pod:
`iron-proxy`, the package-pinned image digest, no command override, exact arguments
`[-config, /etc/iron-proxy/proxy.yaml]`, and a read-only `config` volume mounted at
`/etc/iron-proxy` without subPath or alternate item mappings. That volume must
reference the recorded immutable ConfigMap. The live serving imageID must match
the approved digest. It also verifies matching Deployment/Pod
annotations, approved allowlist contents, enforce mode, Service selector and all
ready endpoints. This release requires exactly one ready proxy Pod/endpoint to
bind the singular proxy record; a rollout or additional endpoint fails closed.
Final API capture rechecks configuration, endpoints, policy revisions, image and
Service identity before cleanup. It cannot prove that nothing transient changed
between observations; this is evidence collection, not independent attestation.

## Verifier and artifact evidence

`setup_completed` becomes true only after Running, successful test staging (or
prebuilt-image ownership), and an executable-entrypoint existence preflight.
`completed` requires observing the exact Harbor verifier command complete and
successful verifier output download/reward parsing. Exit 124–127, negative signal exits, and exits 128 or higher clear both health
flags and are infrastructure failures. Preflight uses `test -x`. Known setup-failure signatures in verifier stdout
clear both flags, even if a reward file exists. An exception does not set health.
A binary reward alone never establishes either flag.

Only Harbor's actual artifact upload phase enables transfer evidence; environment
context and test staging do not count. Each upload is independently downloaded
from the verifier after delivery. Every `verified_artifacts` item contains
`source_sha256`, `delivered_sha256`, `verifier_pod_uid`, and `kind`:

- `entry_bytes_v1`: SHA-256 of the exact regular-file bytes (hardlinks resolve to
  file bytes), or SHA-256 of `directory` / `symlink\0` plus the link target for
  directory/symlink entries. No file contents or filenames are exported.
- `tree_inventory_v1`: SHA-256 of canonical JSON mapping normalized relative paths
  to the entry hashes, excluding the optional root `.` header. This binds empty
  directories, paths and links while excluding nonportable tar header metadata.

Source hashes come from the actual upload archive bytes; delivered hashes come
from independent verifier read-back. Missing/extra entries, changed bytes or a
changed verifier UID fail closed and retain mismatch evidence. The app requires
nonempty transfer evidence for a separate verifier and equal source/delivery
hashes bound to a recorded verifier pod. A task with no actual transfer remains
ineligible for that publication gate; no fictitious empty transfer is invented.

The cross-repo contract gate imports the companion app's `app.harbor_trial` and
`app.sandbox_provenance` through `ANYEVAL_APP_CHECKOUT`. When unset, the cross-repo gate skips with an
explicit message; CI must supply its reviewed companion checkout. Synthetic API
objects exercise real adapter capture, the real child CLI writes result bytes,
and the actual parent parser reads those bytes before provenance validation.

## Egress proxy placement

The iron-proxy Deployment (`k8s/iron-proxy-deployment.yaml`) is shared by every
trial, and each trial pins the proxy pod UID at start and refuses publication if
it changes. It therefore runs on standard (non-Spot) nodes via
`nodeSelector: cloud.google.com/gke-provisioning: standard`, with the
`cluster-autoscaler.kubernetes.io/safe-to-evict: "false"` annotation, and the
`anyeval-egress-proxy` PriorityClass. Autopilot honours that annotation for at
most seven days per pod (extended-duration pods), so scale-down can still move a
proxy older than that, and node failures or a Deployment rollout move it at any
time; every trial in flight at that moment loses its pinned proxy UID and is
refused publication (the run stays unpublished and a resumed sweep re-runs it).
On 2026-09-12 two proxy reschedules invalidated every in-flight trial's evidence.

## Execution and transfer bounds

The adapter requests and checks no hostNetwork/hostPID/hostIPC, no hostPath,
no additional init/ephemeral containers, no privileged execution, explicit
`allowPrivilegeEscalation: false`, `automountServiceAccountToken: false`, the
requested capability sets, gvisor runtime, and expected identity labels.
The requested capability set is exactly the set Docker grants an unprivileged
container by default (`DOCKER_DEFAULT_CAPABILITIES` in `k8s_env.py`: AUDIT_WRITE,
CHOWN, DAC_OVERRIDE, FOWNER, FSETID, KILL, MKNOD, NET_BIND_SERVICE, NET_RAW,
SETFCAP, SETGID, SETPCAP, SETUID, SYS_CHROOT), because the official protocol runs
tasks under Harbor's Docker environment with that set and the 2.1 verifier template
depends on it (apt-get drops to `_apt`, which needs SETGID/SETUID). Contract v1.1
had dropped every capability; every 2.1 verifier then failed its health proof with
"setgroups 65534 failed" and "curl: command not found". Admission still rejects any
capability outside this list. Isolation rests on gVisor and the network policy,
not on the capability set.
2.1 images use `data/image-digests.json`, derived from the two recorded 2.1 sweeps;
4.0 task metadata already uses digests. Created image references and observed
main-container imageIDs must match the approved digest. The static tmux upload
must match the package's `TMUX_SHA256`; the uploaded bytes are those hashed.

Environment kwargs `max_transfer_bytes` (default 268435456),
`max_archive_members` (20000), and `max_output_bytes` (16777216) must be positive
integers. Tar archives use bounded temporary files, bounded member inventories,
and incremental file hashing. Raw and expanded bytes are limited; remote exec
output is bounded before accumulation. Filtered downloads bound the entire source
archive before applying include/exclude/protect rules, so excluded files also
count toward the transfer limit. `TransferLimitError` is infrastructure.

JSON NaN/Infinity constants are rejected at parsing. Echoed identity fields are
validated independently before use; invalid bindings become null. Atomic result
serialization sanitizes non-finite or unsupported values before `allow_nan=False`.
The package's `verifier_health._SETUP_FAILURE` is the sole local definition; the
cross-repo gate compares the app's pattern bytes and flags with it and exercises
the app's actual publication guard for artifact hash and verifier UID mismatches.
