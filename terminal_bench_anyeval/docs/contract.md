# AnyEval / Terminal-Bench child contract v1.1

This is the 2026-09-12 publication extension to v1. The JSON wire discriminator
remains `version: 1` for compatibility with the application's Harbor parser.
The worker launches exactly one child:

```
python -m terminal_bench_anyeval.trial --spec /absolute/spec.json --result /absolute/result.json
```

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
verifier has empty egress. All selecting policies are enumerated: extra selectors
fail closed because Kubernetes policy allows are additive. Policy drift, pod
replacement/restarts, missing final capture, or changed labels fail the attempt
while cleanup still deletes the owned pod and policy.

## Proxy record

| Field | Meaning |
| --- | --- |
| `pod`, `pod_uid`, `finished_pod_uid` | Initial name/UID and final UID of the captured ready proxy |
| `image_digest` | Live resolved proxy image digest |
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

The adapter verifies immutable ConfigMap mounting, matching Deployment/Pod
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
successful verifier output download/reward parsing. Official termination exit
codes do not set completion. Known setup-failure signatures in verifier stdout
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
`app.sandbox_provenance` through its read-only worktree path. Synthetic API
objects exercise real adapter capture, the real child CLI writes result bytes,
and the actual parent parser reads those bytes before provenance validation.
