The current proxy uses the immutable ConfigMap and Service in `iron-proxy.yaml`,
the Deployment/PriorityClass in `iron-proxy-deployment.yaml`, and
`iron-proxy-egress-policy.yaml`. The policy retains NodeLocal DNS at
169.254.20.10/32:53 and the public-internet exclusions for private and Service ranges.

`allowlist.yaml` is the single source of approved hosts and their reasons.
`render_proxy_config.py` writes the Service/ConfigMap manifest and the strategic
Deployment patch `iron-proxy-config-patch.yaml`. It performs no cluster calls.
The allowlist is in enforce mode (`warn: false`), with SNI-only TLS, the existing
DNS settings, deny CIDRs, and 64 MiB request / 512 MiB response caps. SNI-only
passthrough does not inspect encrypted request paths or decrypted body sizes.

From the repository, with the supplied Harbor venv:

```sh
TB_SCRATCH=/private/tmp/claude-501/-Users-jperla-josh/7b8624b6-7520-461a-875c-e13899ff3a86/scratchpad
"$TB_SCRATCH/harbor-venv/bin/python" k8s/render_proxy_config.py
kubectl apply -f k8s/iron-proxy.yaml
kubectl patch deployment iron-proxy -n anyeval-sandbox --type strategic --patch-file k8s/iron-proxy-config-patch.yaml
kubectl rollout status deployment/iron-proxy -n anyeval-sandbox --timeout=600s
```

For an initial setup, apply the Deployment/PriorityClass and egress policy after
creating the ConfigMap, before patching. The checked-in Deployment matches the
current generated revision. For subsequent allowlist edits, use the ConfigMap
and patch commands above; the patch changes the mounted name and rolls the pods.
Do not delete the old ConfigMap until no pod references it.

The full SHA256 of canonical allowlist JSON (version, hosts, reasons) and the
SHA256 of the actual proxy configuration bytes are separate annotations. The
ConfigMap suffix is the first eight characters of SHA256(config SHA256 + source
SHA256), so provenance-only changes also create new immutable revisions. The
adapter reads the Deployment's mounted ConfigMap, validates its provenance and
every ready endpoint, and records the source version/hash and config hash in pod
facts. It needs read access to apps/Deployments as well as pods/ConfigMaps/Services.

`spike2.py deploy` and its old WARN-mode probe are historical tools; do not run
them against this configuration. The former produces a legacy bare Pod and the
latter expects off-list requests to succeed. Use the commands above.

`eligible_tasks.py` reads only task metadata and Compose filenames, prints the
eligible names, and writes `eligibility-4.0.json`. `run_40_eligible.sh --dry-run`
checks the selection and command without credentials or cluster calls. The runner
uses 44 eligible tasks in the supplied 66-task snapshot, the provider pin, four
concurrent trials, one attempt and one whole-trial retry, on-demand pods and the
static tmux binary. It preserves the declared eight-hour agent deadline with
`--agent-timeout-multiplier 1.0`. Harbor's `--timeout-multiplier` scales phase
budgets unless a phase-specific multiplier overrides it; to change the agent
budget, change its explicit multiplier as well.
