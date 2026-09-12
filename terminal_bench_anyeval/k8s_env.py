"""Harbor 0.22 environment for AnyEval's restricted GKE Autopilot namespace.

Each instance owns one pod and one isolation policy. Harbor's Trial creates a
second instance for a separate verifier, supplies its image/config/build context,
stages artifacts/tests, downloads rewards, and stops it in a finally block.
Requires kubernetes (with binary stream support) in Harbor's Python environment.
"""

from __future__ import annotations

import asyncio
import codecs
import io
import json
import hashlib
import os
import re
import shlex
import tarfile
import time
import tempfile
import shutil
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .iron_proxy import allowlist_hash, proxy_env, proxy_egress, DENY_CIDRS, load_allowlist, source_hash

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import (
    EnvironmentCapabilities, EnvironmentResourceCapabilities,
)
from .bounded_io import TransferLimitError, LimitedWriter, as_file, members, inventory
from harbor.models.task.config import NetworkMode
from harbor.models.trial.config import ResourceMode


COMPOSE_NAMES = ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml")
ACTIVE_ENVIRONMENTS = set()
TMUX_SHA256 = "becf4184397f0095862f01a2658bc3ddcfa7b2dee6347f84510934f7f6650ac0"
PROXY_IMAGE = "ironsh/iron-proxy@sha256:c4628019c24f4cc8d77564a26b7c9cedb00accee6f93d06270e85fb8f9c6a7da"


def pinned_image(image):
    if re.fullmatch(r".+@sha256:[0-9a-f]{64}", image):
        return image
    pins = json.loads((Path(__file__).parent / "data/image-digests.json").read_text())
    digest = pins.get(image)
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise AnyEvalInfrastructureError("Execution image has no approved digest")
    return image + "@" + digest



class AnyEvalInfrastructureError(RuntimeError):
    """A sandbox failure, never a benchmark reward."""


def _now():
    return datetime.now(timezone.utc).isoformat()


class ExecStreamClosed(RuntimeError):
    """Command outcome is unknown. Never replay arbitrary commands."""
    def __init__(self, pod_name, pod_phase, last_events):
        self.pod_name = pod_name
        self.pod_phase = pod_phase
        self.last_events = last_events
        super().__init__(f"Kubernetes exec closed without a valid exit status; "
                         f"pod={pod_name}; phase={pod_phase}; events={json.dumps(last_events)}")


class VerifierPreflightError(RuntimeError):
    pass


class AnyEvalK8sEnvironment(BaseEnvironment):
    def __init__(self, *args, namespace="anyeval-sandbox", startup_timeout_sec=600,
                 transfer_timeout_sec=300, binding=None, max_transfer_bytes=256 * 1024 * 1024,
                 max_archive_members=20000, max_output_bytes=16 * 1024 * 1024,
                 cleanup_timeout_sec=60, **kwargs):
        if namespace != "anyeval-sandbox":
            raise ValueError("AnyEval is restricted to namespace anyeval-sandbox")
        for name in ("cpus", "memory_mb", "storage_mb", "gpus", "tpu"):
            if kwargs.get(f"override_{name}") is not None:
                raise ValueError("AnyEval requires task.toml resources; overrides are refused")
        for name, value in (("max_transfer_bytes", max_transfer_bytes),
                            ("max_archive_members", max_archive_members),
                            ("max_output_bytes", max_output_bytes)):
            if type(value) is not int or value <= 0:
                raise ValueError(name + " must be a positive integer")
            setattr(self, name, value)
        self.cleanup_timeout_sec = float(cleanup_timeout_sec)
        if not 0 < self.cleanup_timeout_sec < float("inf"):
            raise ValueError("cleanup_timeout_sec must be positive and finite")
        self.binding = dict(binding or {})
        self._artifact_transfer = False
        self._final_captured = False
        self.namespace = namespace
        self.startup_timeout_sec = float(startup_timeout_sec)
        self.transfer_timeout_sec = float(transfer_timeout_sec)
        if min(self.startup_timeout_sec, self.transfer_timeout_sec) <= 0:
            raise ValueError("Timeouts must be positive")
        self._client = None
        self._core = None
        self._network = None
        self._pod_attempted = False
        self._policy_attempted = False
        self._stop_lock = asyncio.Lock()
        super().__init__(*args, **kwargs)
        stem = re.sub(r"[^a-z0-9-]+", "-", self.session_id.lower()).strip("-")
        self.pod_name = f"tb-{stem[:42]}-{uuid.uuid4().hex[:10]}"
        trial = self.trial_paths.trial_dir.name
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?", trial):
            raise ValueError("Trial name must be a Kubernetes label value (1–63 characters)")
        self._labels = {"inspect/service": "default", "anyeval.io/trial": trial,
                        "anyeval.io/environment": self.pod_name}
        self._annotations = {}
        for key, value in self.binding.items():
            text = str(value)
            label = text if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?", text) else hashlib.sha256(text.encode()).hexdigest()[:63]
            self._labels["anyeval.io/" + key.replace("_", "-")] = label
            self._annotations["anyeval.io/" + key.replace("_", "-")] = text
        self.egress_proxy_requested = os.environ.get("ANYEVAL_TB_EGRESS_PROXY") == "1"
        # Harbor's primary environment has this exact semantic session ID.
        # Separate verifiers (including truncated IDs) never receive proxy egress.
        self.egress_proxy_enabled = (self.egress_proxy_requested and
                                     self.session_id == f"{trial}__env")
        self._proxy_facts = None
        self._verifier_guard = None
        ACTIVE_ENVIRONMENTS.add(self)

    @staticmethod
    def type():
        return "anyeval-k8s"

    @classmethod
    def resource_capabilities(cls):
        return EnvironmentResourceCapabilities(cpu_limit=True, cpu_request=True,
                                               memory_limit=True, memory_request=True)

    @property
    def capabilities(self):
        return EnvironmentCapabilities(disable_internet=True)

    def _validate_definition(self):
        e = self.task_env_config
        reasons = []
        if not e.docker_image:
            reasons.append("environment.docker_image is required; image builds are unsupported")
        if e.gpus:
            reasons.append("GPUs are unsupported by this gVisor adapter")
        if e.tpu:
            reasons.append("TPUs are unsupported")
        if any((self.environment_dir / name).exists() for name in COMPOSE_NAMES):
            reasons.append("docker-compose.yaml/Compose requires multiple services or Docker")
        # ANYEVAL_TB_OFFLINE_PROTOCOL=1 is the explicit, recorded decision to run a task that
        # declares internet access under our restricted network protocol anyway.
        # The deviation is written to the pod facts; nothing is silently relaxed.
        # The proxy protocol (ANYEVAL_TB_EGRESS_PROXY=1) serves a task's declared internet through
        # the enforced egress allowlist, so a public/allow_internet declaration is acceptable
        # under it as well as under the explicit offline override; both are recorded in facts.
        offline_override = (os.environ.get("ANYEVAL_TB_OFFLINE_PROTOCOL") == "1"
                            or os.environ.get("ANYEVAL_TB_EGRESS_PROXY") == "1")
        self.offline_protocol_override = bool(e.allow_internet) and offline_override
        if e.allow_internet and not offline_override:
            reasons.append("allow_internet=true is incompatible with deny-all networking")
        # TaskConfig translates and clears legacy allow_internet. Inspect metadata
        # too so a runtime override cannot silently bypass the explicit refusal.
        metadata = self.environment_dir.parent / "task.toml"
        if metadata.is_file():
            raw = tomllib.loads(metadata.read_text())
            for section in (raw.get("environment", {}),
                            raw.get("verifier", {}).get("environment", {}) or {}):
                if section.get("allow_internet"):
                    self.offline_protocol_override = offline_override
                    if not offline_override:
                        reasons.append("task.toml declares allow_internet=true")
        for name in ("cpus", "memory_mb", "storage_mb"):
            if getattr(e, name) is None or getattr(e, name) <= 0:
                reasons.append(f"positive environment.{name} is required")
        if e.storage_mb and e.storage_mb > 10240:
            reasons.append("storage>10GiB is unsupported")
        if e.mcp_servers:
            reasons.append("external MCP services are unsupported")
        if reasons:
            raise ValueError("AnyEval refuses task: " + "; ".join(reasons))

    def _validate_resource_mode_support(self):
        super()._validate_resource_mode_support()
        if any(mode not in (ResourceMode.AUTO, ResourceMode.GUARANTEE)
               for mode in (self._cpu_resource_mode, self._memory_resource_mode)):
            raise ValueError("AnyEval requires requests == limits; use auto or guarantee")

    def validate_network_policy_support(self, network_policy=None):
        policy = network_policy or self.network_policy
        if policy.network_mode != NetworkMode.NO_NETWORK:
            if (os.environ.get("ANYEVAL_TB_OFFLINE_PROTOCOL") == "1"
                    or os.environ.get("ANYEVAL_TB_EGRESS_PROXY") == "1"):
                self.offline_protocol_override = True
                return  # explicit protocol override; effective isolation is recorded in pod facts
            raise ValueError("AnyEval refuses public/allowlist networking (allow_internet): deny-all only")
        super().validate_network_policy_support(policy)

    async def _call(self, fn, *args, **kwargs):
        """Finish in-flight SDK calls before cancellation cleanup can race them."""
        task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise

    async def _ensure_client(self):
        if self._client is not None:
            return
        try:
            from kubernetes import client, config
        except ImportError as exc:
            raise RuntimeError("Install kubernetes in Harbor's venv: python -m pip install kubernetes") from exc
        kubeconfig = os.environ.get("KUBECONFIG")
        if not kubeconfig:
            raise ValueError("KUBECONFIG must point to the supplied AnyEval kubeconfig")
        cfg = client.Configuration()
        await self._call(config.load_kube_config, config_file=kubeconfig,
                         client_configuration=cfg, persist_config=False)
        self._client = client.ApiClient(cfg)
        self._core = client.CoreV1Api(self._client)
        self._network = client.NetworkingV1Api(self._client)
        self._node_api = client.NodeV1Api(self._client)

    def _manifests(self):
        e = self.task_env_config
        resources = {"cpu": str(e.cpus), "memory": f"{e.memory_mb}Mi",
                     "ephemeral-storage": f"{e.storage_mb}Mi"}
        metadata = {"name": self.pod_name, "namespace": self.namespace,
                    "labels": dict(self._labels), "annotations": dict(self._annotations)}
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": metadata,
               "spec": {"runtimeClassName": "gvisor",
                        "nodeSelector": ({} if os.environ.get("ANYEVAL_TB_NO_SPOT") == "1" else {"cloud.google.com/gke-spot": "true"}),
                        "restartPolicy": "Never", "automountServiceAccountToken": False,
                        "terminationGracePeriodSeconds": 0,
                        "containers": [{"name": "main", "image": pinned_image(e.docker_image),
                                        "command": ["sleep", "infinity"],
                                        "resources": {"requests": dict(resources), "limits": dict(resources)},
                                        "env": [{"name": k, "value": v} for k, v in self._startup_env().items()],
                                        "securityContext": {"runAsUser": 0, "privileged": False, "allowPrivilegeEscalation": False,
                                                            "capabilities": {"drop": ["ALL"], "add": []}}}]}}
        if e.workdir:
            pod["spec"]["containers"][0]["workingDir"] = e.workdir
        policy = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                  "metadata": dict(metadata),
                  "spec": {"podSelector": {"matchLabels": {"anyeval.io/environment": self.pod_name}},
                           "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []}}
        if self.egress_proxy_enabled:
            if self._proxy_facts is None:
                raise RuntimeError("Proxy Service/config must be resolved before creating task manifests")
            ip = self._proxy_facts["proxy_ip"]
            pod["spec"].update(dnsPolicy="None", dnsConfig={"nameservers": [ip]})
            container = pod["spec"]["containers"][0]
            env = {v["name"]: v["value"] for v in container["env"]}
            env.update(proxy_env(ip))
            container["env"] = [{"name": k, "value": v} for k, v in env.items()]
            policy["spec"]["egress"] = proxy_egress()
        return pod, policy

    @staticmethod
    def _validate_proxy_container(spec, cm_name):
        containers = getattr(spec, "containers", None) or []
        if len(containers) != 1:
            raise AnyEvalInfrastructureError("Proxy must have one serving container")
        container = containers[0]
        mounts = getattr(container, "volume_mounts", None) or []
        volumes = getattr(spec, "volumes", None) or []
        if (container.name != "iron-proxy" or container.image != PROXY_IMAGE
                or getattr(container, "command", None)
                or container.args != ["-config", "/etc/iron-proxy/proxy.yaml"]
                or len(mounts) != 1 or mounts[0].name != "config"
                or mounts[0].mount_path != "/etc/iron-proxy"
                or mounts[0].read_only is not True
                or getattr(mounts[0], "sub_path", None)
                or getattr(mounts[0], "sub_path_expr", None)):
            raise AnyEvalInfrastructureError("Proxy serving container is not bound to approved configuration")
        configs = [v for v in volumes if v.name == "config"]
        if (len(configs) != 1 or not configs[0].config_map
                or configs[0].config_map.name != cm_name
                or getattr(configs[0].config_map, "items", None)
                or getattr(configs[0].config_map, "optional", False)):
            raise AnyEvalInfrastructureError("Proxy config mount does not expose the recorded ConfigMap")

    async def _prepare_proxy(self):
        if not self.egress_proxy_enabled:
            return
        import ipaddress
        import yaml
        service = await self._call(self._core.read_namespaced_service, "iron-proxy", self.namespace,
                                   _request_timeout=20)
        from kubernetes import client
        deployment = await self._call(client.AppsV1Api(self._client).read_namespaced_deployment,
                                      "iron-proxy", self.namespace, _request_timeout=20)
        mounts = [v.config_map.name for v in deployment.spec.template.spec.volumes or []
                  if v.name == "config" and v.config_map]
        if len(mounts) != 1:
            raise RuntimeError("Deployment must mount exactly one config ConfigMap")
        cm_name = mounts[0]
        self._validate_proxy_container(deployment.spec.template.spec, cm_name)
        cm = await self._call(self._core.read_namespaced_config_map, cm_name, self.namespace,
                             _request_timeout=20)
        proxies = await self._call(self._core.list_namespaced_pod, self.namespace,
                                   label_selector="anyeval.io/role=egress-proxy", _request_timeout=20)
        ready = [p for p in proxies.items if p.status.phase == "Running"
                 and p.status.container_statuses
                 and all(c.ready for c in p.status.container_statuses)]
        # Refuse a mixed rollout: every ready Service endpoint must enforce this revision.
        if not ready:
            raise RuntimeError("no ready egress-proxy pod in the namespace")
        proxy = ready[0]
        ip = str(ipaddress.IPv4Address(service.spec.cluster_ip))
        raw = cm.data["proxy.yaml"]
        config = yaml.safe_load(raw)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        if (config["dns"]["proxy_ip"] != ip or config["tls"]["mode"] != "sni-only"
                or config["proxy"]["tunnel_listen"] != ":8080"
                or config["dns"]["listen"] != ":53"):
            raise RuntimeError("Proxy Service and config do not match the enforced proxy protocol")
        allow = [t["config"] for t in config["transforms"] if t["name"] == "allowlist"]
        if len(allow) != 1 or allow[0].get("warn") is not False:
            raise RuntimeError("Requires one ENFORCE-mode allowlist")
        source = yaml.safe_load(cm.data["allowlist.yaml"])
        annotations = cm.metadata.annotations or {}
        source_digest = source_hash(source)
        if (annotations.get("anyeval.io/allowlist-version") != source.get("version")
                or annotations.get("anyeval.io/allowlist-sha256") != source_digest
                or source_digest != source_hash(load_allowlist())):
            raise RuntimeError("Mounted allowlist provenance differs from approved source")
        revision = hashlib.sha256((digest + source_digest).encode()).hexdigest()
        if cm_name != f"iron-proxy-{revision[:8]}":
            raise RuntimeError("ConfigMap name does not match immutable config revision")
        if (allow[0].get("domains") != [e["host"] for e in source["hosts"]]
                or allow[0].get("cidrs") or allow[0].get("rules")
                or config["proxy"].get("max_request_body_bytes") != 64 * 1024 * 1024
                or config["proxy"].get("max_response_body_bytes") != 512 * 1024 * 1024):
            raise RuntimeError("Proxy allowlist/body caps differ from approved protocol")
        if not set(DENY_CIDRS).issubset(config["proxy"]["upstream_deny_cidrs"]):
            raise RuntimeError("Proxy is missing required upstream deny CIDRs")
        cluster_cidrs = json.loads((cm.metadata.annotations or {}).get("anyeval.io/cluster-cidrs", "[]"))
        if not cluster_cidrs or not set(cluster_cidrs).issubset(config["proxy"]["upstream_deny_cidrs"]):
            raise RuntimeError("Proxy lacks recorded cluster CIDRs")
        if (annotations.get("anyeval.io/config-sha256") != digest or
                (deployment.spec.template.metadata.annotations or {}).get("anyeval.io/config-sha256") != digest):
            raise RuntimeError("Deployment does not reference this config revision")
        for endpoint in ready:
            self._validate_proxy_container(endpoint.spec, cm_name)
            if (endpoint.metadata.annotations or {}).get("anyeval.io/config-sha256") != digest:
                raise RuntimeError("Proxy Pod does not reference this config revision")
            if cm.immutable is not True or not any(
                    v.config_map and v.config_map.name == cm_name for v in endpoint.spec.volumes or []):
                raise RuntimeError("Proxy config must be immutable and mounted by every ready Pod")
            if not any(c.type == "Ready" and c.status == "True" for c in endpoint.status.conditions or []):
                raise RuntimeError("iron-proxy Pod is not Ready")
        if (service.spec.selector != {"anyeval.io/role": "egress-proxy"}
                or (proxy.metadata.labels or {}).get("anyeval.io/role") != "egress-proxy"):
            raise RuntimeError("Proxy Pod/Service must use the egress-proxy role selector")
        endpoints = await self._call(self._core.read_namespaced_endpoints,
                                      "iron-proxy", self.namespace, _request_timeout=20)
        endpoint_uids = sorted({a.target_ref.uid for subset in endpoints.subsets or []
                                for a in subset.addresses or [] if a.target_ref and a.target_ref.kind == "Pod"})
        ready_uids = sorted(p.metadata.uid for p in ready)
        if endpoint_uids != ready_uids or len(ready) != 1:
            raise AnyEvalInfrastructureError("Proxy endpoints must identify exactly one captured ready Pod")
        image_id = next((c.image_id for c in proxy.status.container_statuses if c.name == "iron-proxy"), None)
        match = re.search(r"sha256:[0-9a-f]{64}$", image_id or "")
        if match is None or match.group(0) != PROXY_IMAGE.rsplit("@", 1)[1]:
            raise AnyEvalInfrastructureError("Proxy image digest was not resolved")
        policies = await self._call(self._network.list_namespaced_network_policy,
                                    self.namespace, _request_timeout=20)
        proxy_policies = []
        for policy in policies.items:
            actual = self._client.sanitize_for_serialization(policy)
            if self._selects(actual["spec"]["podSelector"], proxy.metadata.labels or {}):
                proxy_policies.append({"name": policy.metadata.name, "uid": policy.metadata.uid,
                                       "resource_version": policy.metadata.resource_version,
                                       "spec": actual["spec"], "observed_from": "kubernetes_api",
                                       "source": "proxy_policy", "pod_uid": proxy.metadata.uid})
        serving = proxy.spec.containers[0]
        self._proxy_facts = {"serving_container": {
                             "name": serving.name, "image": serving.image,
                             "args": list(serving.args), "configmap": cm_name,
                             "mount_path": serving.volume_mounts[0].mount_path,
                             "read_only": serving.volume_mounts[0].read_only},
                             "image_digest": match.group(0), "service_uid": service.metadata.uid,
                             "endpoint_uids": endpoint_uids, "labels": dict(proxy.metadata.labels or {}),
                             "mode": "warn" if allow[0].get("warn") else "enforce",
                             "active_config_name": cm_name, "active_config_sha256": source_digest,
                             "configmap_uid": cm.metadata.uid,
                             "network_policies": sorted(proxy_policies, key=lambda p: p["uid"]),
                             "pod": getattr(proxy.metadata, "name", None),
                             "pod_uid": getattr(proxy.metadata, "uid", None),
                             "service_ip": ip,
                             "proxy_ip": ip, "allowlist_sha256": source_digest,
                             "allowlist_version": source["version"], "configmap": cm_name,
                             "allowlist_transform_sha256": allowlist_hash(config),
                             "allowlist_mode": "enforce", "tls_mode": "sni-only",
                             "config_sha256": digest, "cluster_cidrs": cluster_cidrs}

    async def _events(self):
        try:
            events = await self._call(self._core.list_namespaced_event, self.namespace,
                                     field_selector=f"involvedObject.name={self.pod_name}",
                                     _request_timeout=20)
            ordered = sorted(events.items, key=lambda e: str(
                getattr(e, "last_timestamp", None) or getattr(e, "event_time", None)
                or getattr(e, "first_timestamp", None) or ""))
            return [{"reason": e.reason, "message": e.message, "count": e.count}
                    for e in ordered]
        except Exception as exc:
            return [{"event_query_error": type(exc).__name__}]

    def _save_facts(self, data):
        directory = self.trial_paths.trial_dir / "anyeval"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.pod_name}.json"
        previous = json.loads(path.read_text()) if path.exists() else {}
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps({**previous, **data}, indent=2) + "\n")
        os.replace(temporary, path)

    @staticmethod
    def _selects(selector, labels):
        if any(labels.get(k) != v for k, v in (selector.get("matchLabels") or {}).items()):
            return False
        for rule in selector.get("matchExpressions") or []:
            key, op, values = rule["key"], rule["operator"], rule.get("values", [])
            if op == "In" and (key not in labels or labels[key] not in values):
                return False
            if op == "NotIn" and key in labels and labels[key] in values:
                return False
            if op == "Exists" and key not in labels:
                return False
            if op == "DoesNotExist" and key in labels:
                return False
            if op not in {"In", "NotIn", "Exists", "DoesNotExist"}:
                raise AnyEvalInfrastructureError("Unknown NetworkPolicy selector operator")
        return True

    async def _policy_facts(self, pod, *, final=False):
        policy = await self._call(self._network.read_namespaced_network_policy,
                                  self.pod_name, self.namespace, _request_timeout=20)
        spec = self._client.sanitize_for_serialization(policy)["spec"]
        # Kubernetes serializes omitted empty lists as null; preserve all other API fields.
        spec = dict(spec, ingress=spec.get("ingress") or [], egress=spec.get("egress") or [])
        if spec != self._manifests()[1]["spec"]:
            raise AnyEvalInfrastructureError("Admitted network policy differs from requested isolation")
        policies = await self._call(self._network.list_namespaced_network_policy,
                                    self.namespace, _request_timeout=20)
        selected = sorted(p.metadata.uid for p in policies.items if self._selects(
            self._client.sanitize_for_serialization(p)["spec"]["podSelector"], pod.metadata.labels or {}))
        self._save_facts({"finished_selecting_policy_uids" if final else "selecting_policy_uids": selected})
        additional = []
        if policy.metadata.uid not in selected:
            raise AnyEvalInfrastructureError("Missing selecting package NetworkPolicy")
        for other in policies.items:
            if other.metadata.uid not in selected or other.metadata.uid == policy.metadata.uid:
                continue
            other_spec = self._client.sanitize_for_serialization(other)["spec"]
            if any(other_spec.get(direction) not in (None, []) for direction in ("ingress", "egress")):
                raise AnyEvalInfrastructureError("Additional selecting NetworkPolicy adds allows")
            if not all(isinstance(value, str) and value for value in
                       (other.metadata.name, other.metadata.uid, other.metadata.resource_version)):
                raise AnyEvalInfrastructureError("Additional selecting policy identity is incomplete")
            additional.append({"name": other.metadata.name, "uid": other.metadata.uid,
                               "resource_version": other.metadata.resource_version,
                               "effect": "adds no allows", "spec": other_spec,
                               "observed_from": "kubernetes_api", "pod_uid": pod.metadata.uid})
        additional.sort(key=lambda p: p["uid"])
        self._save_facts({"finished_additional_network_policies" if final else
                          "additional_network_policies": additional})
        if not self._selects(spec["podSelector"], pod.metadata.labels or {}):
            raise AnyEvalInfrastructureError("Policy does not select the live pod")
        return {"additional_network_policies": additional, "selecting_policy_uids": selected, "network_policy": {
            "name": policy.metadata.name, "uid": policy.metadata.uid,
            "resource_version": policy.metadata.resource_version,
            "observed_from": "kubernetes_api", "source": "package_policy",
            "pod_uid": pod.metadata.uid, "spec": spec,
            "egress_to_proxy_only": self.egress_proxy_enabled,
            "deny_all": not bool(spec["egress"]), "mode": "enforce"}}

    async def _capture_final_facts(self):
        path = self.trial_paths.trial_dir / "anyeval" / f"{self.pod_name}.json"
        initial = json.loads(path.read_text())
        pod = await self._call(self._core.read_namespaced_pod, self.pod_name,
                               self.namespace, _request_timeout=20)
        statuses = pod.status.container_statuses or []
        final = {"finished_uid": pod.metadata.uid,
                 "finished_resource_version": pod.metadata.resource_version,
                 "finished_labels": pod.metadata.labels,
                 "restart_count": sum(s.restart_count for s in statuses),
                 "container_id": next(s.container_id for s in statuses if s.name == "main")}
        self._save_facts(final)
        observed = await self._policy_facts(pod, final=True)
        policy = dict(initial["network_policy"],
                      finished_resource_version=observed["network_policy"]["resource_version"])
        self._save_facts({"network_policy": policy,
                          "finished_selecting_policy_uids": observed["selecting_policy_uids"]})
        if (pod.metadata.uid != initial["created_uid"] or final["restart_count"] != 0
                or final["container_id"] != initial["container_id"]
                or final["finished_labels"] != initial["labels"]
                or any((pod.metadata.annotations or {}).get("anyeval.io/" + k.replace("_", "-")) != str(v)
                       for k, v in self.binding.items())
                or observed["additional_network_policies"] != initial.get("additional_network_policies", [])
                or observed["selecting_policy_uids"] != initial["selecting_policy_uids"]
                or policy["finished_resource_version"] != policy["resource_version"]):
            raise AnyEvalInfrastructureError("Pod or NetworkPolicy changed during the attempt")
        if self.egress_proxy_enabled:
            active = dict(self._proxy_facts)
            await self._prepare_proxy()
            final_proxy = self._proxy_facts
            active.update(finished_pod_uid=final_proxy["pod_uid"],
                          finished_config_name=final_proxy["active_config_name"],
                          finished_config_sha256=final_proxy["active_config_sha256"],
                          finished_configmap_uid=final_proxy["configmap_uid"],
                          finished_proxy_config_sha256=final_proxy["config_sha256"])
            self._proxy_facts = active
            self._save_facts({"egress_proxy": active})
            for key in ("pod_uid", "image_digest", "service_uid", "endpoint_uids", "labels",
                        "active_config_name", "active_config_sha256", "config_sha256", "mode",
                        "configmap_uid", "network_policies", "serving_container"):
                if active[key] != final_proxy[key]:
                    raise AnyEvalInfrastructureError("Proxy identity/configuration changed during the attempt")
            active["network_policies"] = [dict(p, finished_resource_version=p["resource_version"])
                                          for p in final_proxy["network_policies"]]
            self._save_facts({"egress_proxy": active})

    async def _capture_runtime_facts(self, pod):
        """Evidence from live API objects and exec; absence is never an attestation."""
        facts = {}
        try:
            runtime = await self._call(self._node_api.read_runtime_class,
                                       pod.spec.runtime_class_name, _request_timeout=20)
            facts.update(runtime_class_exists=runtime is not None,
                         runtime_class_handler=runtime.handler,
                         runtime_class_uid=runtime.metadata.uid)
        except Exception as exc:
            facts.update(runtime_class_exists=False, runtime_class_evidence_error=type(exc).__name__)
        try:
            node = await self._call(self._core.read_node, pod.spec.node_name, _request_timeout=20)
            facts.update(kubelet_version=node.status.node_info.kubelet_version,
                         node_labels={k: v for k, v in (node.metadata.labels or {}).items()
                                      if k == "sandbox.gke.io/runtime"})
        except Exception as exc:
            facts["node_evidence_error"] = type(exc).__name__
        try:
            facts.update(await self._policy_facts(pod))
        except Exception as exc:
            facts["network_policy_evidence_error"] = type(exc).__name__
            self._save_facts(facts)
            raise AnyEvalInfrastructureError("Could not attest admitted network policy") from exc
        for key, command in (("kernel_release", "uname -r"),
                             ("dmesg_gvisor_boot", "dmesg 2>/dev/null | head -n 40")):
            try:
                result = await self.exec(command, timeout_sec=20)
                facts[key] = (result.stdout or "").strip() if result.return_code == 0 else None
            except Exception as exc:
                facts[key + "_error"] = type(exc).__name__
        self._save_facts(facts)

    def _validate_admitted_pod(self, pod):
        spec = pod.spec
        wanted = self._manifests()[0]["spec"]
        if (spec.runtime_class_name != "gvisor"
                or any(getattr(spec, k, False) not in (False, None)
                       for k in ("host_network", "host_pid", "host_ipc"))
                or spec.automount_service_account_token is not False
                or pod.metadata.namespace != self.namespace
                or pod.metadata.name != self.pod_name
                or any((pod.metadata.labels or {}).get(k) != v for k, v in self._labels.items())
                or any(getattr(v, "host_path", None) is not None for v in spec.volumes or [])
                or getattr(spec, "init_containers", None)
                or getattr(spec, "ephemeral_containers", None)
                or len(spec.containers) != 1):
            raise AnyEvalInfrastructureError("Pod admission violated security invariants")
        container = spec.containers[0]
        security = container.security_context
        caps = getattr(security, "capabilities", None)
        requested = wanted["containers"][0]
        if (container.name != "main" or container.image != requested["image"]
                or security is None or security.privileged not in (False, None)
                or security.allow_privilege_escalation is not False
                or caps is None
                or sorted(caps.add or []) != requested["securityContext"]["capabilities"]["add"]
                or sorted(caps.drop or []) != requested["securityContext"]["capabilities"]["drop"]):
            raise AnyEvalInfrastructureError("Container admission violated security invariants")
        statuses = pod.status.container_statuses or []
        main = [status for status in statuses if status.name == "main"]
        expected_digest = requested["image"].rsplit("@", 1)[1]
        if len(main) != 1 or not (main[0].image_id or "").endswith("@" + expected_digest):
            raise AnyEvalInfrastructureError("Observed execution image digest differs from approved pin")

    async def _wait_running(self):
        deadline = time.monotonic() + self.startup_timeout_sec
        last_phase = None
        while time.monotonic() < deadline:
            pod = await self._call(self._core.read_namespaced_pod, self.pod_name,
                                   self.namespace, _request_timeout=20)
            last_phase = pod.status.phase
            statuses = pod.status.container_statuses or []
            if last_phase == "Running" and statuses and all(s.state.running for s in statuses):
                self._validate_admitted_pod(pod)
                facts = {"pod": self.pod_name, "namespace": self.namespace,
                         "uid": getattr(pod.metadata, "uid", None),
                         "created_uid": pod.metadata.uid,
                         "resource_version": pod.metadata.resource_version,
                         "container_name": "main",
                         "container_id": next(s.container_id for s in statuses if s.name == "main"),
                         "restart_count": sum(s.restart_count for s in statuses),
                         "pod_ip": getattr(pod.status, "pod_ip", None),
                         "session_id": self.session_id, "image": self.task_env_config.docker_image,
                         "runtimeClassName": pod.spec.runtime_class_name,
                         "node": pod.spec.node_name, "nodeSelector": pod.spec.node_selector,
                         "automountServiceAccountToken": pod.spec.automount_service_account_token,
                         "labels": pod.metadata.labels,
                         "containers": [{"name": s.name, "image": s.image, "imageID": s.image_id}
                                        for s in statuses],
                         "resources": self._client.sanitize_for_serialization(pod.spec.containers[0].resources),
                         "events": await self._events(),
                         "offline_protocol_override": getattr(self, "offline_protocol_override", False)}
                facts.update(egress_proxy_requested=self.egress_proxy_requested,
                             network_mode="egress-proxy" if self.egress_proxy_enabled else "deny-all",
                             egress_proxy=self._proxy_facts,
                             dnsPolicy=pod.spec.dns_policy,
                             dnsConfig=self._client.sanitize_for_serialization(pod.spec.dns_config),
                             networkPolicy=self._manifests()[1])
                annotations = pod.metadata.annotations or {}
                for key, expected in self.binding.items():
                    observed = annotations.get("anyeval.io/" + key.replace("_", "-"))
                    if observed != str(expected) or any((pod.metadata.labels or {}).get(k) != v for k, v in self._labels.items()):
                        raise AnyEvalInfrastructureError("Pod identity binding changed at admission")
                    facts[key] = int(observed) if key == "attempt" else observed
                self._save_facts(facts)
                # Record upward admission changes; refuse reduced resources.
                wanted = self._manifests()[0]["spec"]["containers"][0]["resources"]
                from kubernetes.utils.quantity import parse_quantity
                actual = facts["resources"]
                # Autopilot may RAISE a request to satisfy its CPU:memory ratio (e.g. 1 vCPU with
                # 8 GiB becomes 1231m); more headroom than the task asked for does not change what
                # the task can do, so it is recorded, not refused. A reduction would, and is refused.
                admitted = {}
                for kind, values in wanted.items():
                    for k, v in values.items():
                        got = parse_quantity(actual.get(kind, {}).get(k, "0"))
                        want = parse_quantity(v)
                        if got < want:
                            raise RuntimeError(f"Autopilot reduced {kind}.{k} from {v} to {actual.get(kind, {}).get(k)}; see pod facts")
                        if got != want:
                            admitted[f"{kind}.{k}"] = {"requested": v, "admitted": actual.get(kind, {}).get(k)}
                if admitted:
                    self._save_facts({"pod": self.pod_name, "resources_raised_by_admission": admitted})
                if pod.spec.runtime_class_name != "gvisor":
                    raise RuntimeError("Pod admission did not preserve gvisor runtime")
                return
            if last_phase in ("Failed", "Succeeded"):
                raise RuntimeError(f"Pod terminated during startup: {last_phase}")
            await asyncio.sleep(3)
        raise TimeoutError(f"Pod not Running after {self.startup_timeout_sec}s (phase={last_phase})")

    async def _upload_environment_dir_after_start(self):
        # Eligible packaged tasks use prebuilt images. Their original build
        # contexts were never runtime uploads. Keep Harbor's behavior for
        # present contexts, including separate verifier tests/ directories.
        if not self.environment_dir.exists():
            self._save_facts({"environment_context": "not packaged (prebuilt image)"})
            return
        await super()._upload_environment_dir_after_start()

    async def start(self, force_build=False):
        if force_build:
            raise ValueError("AnyEval only pulls the exact prebuilt image; force_build is unsupported")
        await self._ensure_client()
        try:
            await self._prepare_proxy()
            pod, policy = self._manifests()
            self._save_facts({"pod": self.pod_name,
                              "role": "agent" if self.session_id == f"{self.trial_paths.trial_dir.name}__env" else "verifier",
                              "started_at": _now(), "image": self.task_env_config.docker_image,
                              "resources_requested": pod["spec"]["containers"][0]["resources"],
                              "egress_proxy_requested": self.egress_proxy_requested,
                              "network_mode": "egress-proxy" if self.egress_proxy_enabled else "deny-all",
                              "egress_proxy": self._proxy_facts})
            # Policy first, so the pod is selected from creation onward.
            self._policy_attempted = True
            await self._call(self._network.create_namespaced_network_policy, self.namespace,
                             policy, _request_timeout=30)
            self._pod_attempted = True
            await self._call(self._core.create_namespaced_pod, self.namespace, pod, _request_timeout=30)
            await self._wait_running()
            running = await self._call(self._core.read_namespaced_pod, self.pod_name,
                                       self.namespace, _request_timeout=20)
            await self._capture_runtime_facts(running)
            result = await self.ensure_dirs(self._mount_targets(writable_only=True))
            if result is not None and result.return_code:
                raise RuntimeError("Failed to create Harbor log/artifact directories")
            await self._upload_environment_dir_after_start()
            await self._provision_tmux()
        except BaseException as exc:
            events = await self._events()
            self._save_facts({"pod": self.pod_name, "startup_error_type": type(exc).__name__, "events": events})
            try:
                await asyncio.shield(self.stop(delete=True))
            except Exception as cleanup:
                self.logger.error("AnyEval startup cleanup failed: %s", cleanup)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AnyEvalInfrastructureError(f"AnyEval pod startup failed: {exc}; events={json.dumps(events)}") from exc

    async def _provision_tmux(self):
        """Terminus 2 needs tmux and installs it with apt/pip when absent; under deny-all
        egress that install cannot succeed, so AnyEval places a pinned static tmux
        (ANYEVAL_TB_TMUX_STATIC, built by Cloud Build from tmux 3.5a) into the pod before
        agent setup. The official harness also adds tmux to the image at setup time; the
        difference — a static binary instead of the distro package — is recorded in facts."""
        source = os.environ.get("ANYEVAL_TB_TMUX_STATIC", "").strip()
        if not source:
            return
        binary = Path(source)
        if not binary.is_file():
            raise RuntimeError(f"ANYEVAL_TB_TMUX_STATIC does not name a file: {source}")
        with binary.open("rb") as stream:
            data = stream.read(self.max_transfer_bytes + 1)
        if len(data) > self.max_transfer_bytes:
            raise TransferLimitError("Static tmux exceeds transfer byte limit")
        digest = hashlib.sha256(data).hexdigest()
        if digest != TMUX_SHA256:
            raise AnyEvalInfrastructureError("Static tmux SHA256 differs from approved pin")
        probe = await self.exec("command -v tmux >/dev/null 2>&1 && tmux -V", user="root")
        if probe.return_code == 0:
            self._save_facts({"pod": self.pod_name, "tmux": "present in image", "tmux_version": (probe.stdout or "").strip()})
            return
        with tempfile.TemporaryDirectory() as directory:
            checked = Path(directory) / "tmux"
            checked.write_bytes(data)
            await self.upload_file(checked, PurePosixPath("/usr/local/bin/tmux"))
        result = await self.exec("chmod 0755 /usr/local/bin/tmux && tmux -V", user="root")
        if result.return_code != 0:
            raise RuntimeError(f"static tmux does not run in this image: {result.stderr!r}")
        self._save_facts({"pod": self.pod_name, "tmux": "static binary uploaded by AnyEval",
                          "tmux_sha256": digest, "tmux_version": (result.stdout or "").strip()})

    async def _wait_terminated(self):
        deadline = time.monotonic() + self.cleanup_timeout_sec
        terminal_since = None
        while time.monotonic() < deadline:
            try:
                pod = await self._call(self._core.read_namespaced_pod, self.pod_name,
                                       self.namespace, _request_timeout=20)
            except Exception as exc:
                if getattr(exc, "status", None) == 404:
                    return
                raise
            if pod.status.phase in ("Succeeded", "Failed"):
                terminal_since = terminal_since or time.monotonic()
                grace = max(1, pod.spec.termination_grace_period_seconds or 0)
                if time.monotonic() - terminal_since >= grace:
                    return
            else:
                terminal_since = None
            await asyncio.sleep(1)
        raise AnyEvalInfrastructureError("Pod termination was not confirmed; isolation retained")

    async def stop(self, delete=True):
        """Always delete owned resources, including when Harbor passes delete=False."""
        async with self._stop_lock:
            if self._client is None:
                ACTIVE_ENVIRONMENTS.discard(self)
                return
            if self.egress_proxy_enabled and self._proxy_facts:
                await self._collect_proxy_log()
            failures = []
            if self._pod_attempted and not self._final_captured:
                try:
                    await self._capture_final_facts()
                    self._final_captured = True
                except Exception as exc:
                    self._save_facts({"final_evidence_error": type(exc).__name__})
                    failures.append("final evidence: " + type(exc).__name__)
            for attempted, api, name in (
                ("_pod_attempted", self._core.delete_namespaced_pod, "pod"),
                ("_policy_attempted", self._network.delete_namespaced_network_policy, "policy"),
            ):
                if not getattr(self, attempted):
                    continue
                if name == "policy" and self._pod_attempted:
                    failures.append("policy retained: pod termination unconfirmed")
                    continue
                for attempt in range(3):
                    try:
                        kw = {"grace_period_seconds": 0} if name == "pod" else {}
                        await self._call(api, self.pod_name, self.namespace, _request_timeout=20, **kw)
                        if name == "pod":
                            await self._wait_terminated()
                        setattr(self, attempted, False)
                        break
                    except Exception as exc:
                        if getattr(exc, "status", None) == 404:
                            setattr(self, attempted, False)
                            break
                        if attempt == 2:
                            failures.append(f"{name}: {type(exc).__name__}")
                        else:
                            await asyncio.sleep(1)
            if self._pod_attempted or self._policy_attempted:
                raise AnyEvalInfrastructureError("AnyEval cleanup failed: " + "; ".join(failures))
            await self._call(self._client.close)
            self._client = self._core = self._network = None
            self._save_facts({"ended_at": _now()})
            ACTIVE_ENVIRONMENTS.discard(self)
            if failures:
                raise AnyEvalInfrastructureError("AnyEval final evidence failed: " + "; ".join(failures))

    async def _collect_proxy_log(self):
        """Private, bounded proxy diagnostics attributable to this pod IP only."""
        directory = self.trial_paths.trial_dir / "anyeval"
        facts_path = directory / f"{self.pod_name}.json"
        try:
            facts = json.loads(facts_path.read_text())
            pod_ip = facts.get("pod_ip")
            proxy_pod = self._proxy_facts.get("pod")
            if not pod_ip or not proxy_pod:
                return
            started = datetime.fromisoformat(facts["started_at"])
            since_seconds = max(1, int((datetime.now(timezone.utc) - started).total_seconds()) + 1)
            raw = await self._call(self._core.read_namespaced_pod_log, proxy_pod,
                                   self.namespace, since_seconds=since_seconds,
                                   limit_bytes=4 * 1024 * 1024, _request_timeout=20)
            # Never attach another trial's traffic: match structured client/source
            # address fields, not arbitrary URL or payload substrings.
            selected = []
            for line in raw.splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                addresses = [record.get(k) for k in ("client_ip", "source_ip", "remote_addr", "client_addr")]
                if any(isinstance(v, str) and (v == pod_ip or v.startswith(pod_ip + ":"))
                       for v in addresses):
                    selected.append(line)
            (directory / "proxy.log").write_text("\n".join(selected) + ("\n" if selected else ""))
            self._save_facts({"proxy_log_collected": True, "proxy_log_lines": len(selected)})
        except Exception as exc:
            self._save_facts({"proxy_log_error": type(exc).__name__})

    async def _stream(self, command, *, data=None, timeout_sec=None, callback=None, output=None):
        from kubernetes import client
        from kubernetes.stream import stream
        from websocket import WebSocketConnectionClosedException
        await self._ensure_client()
        # stream() temporarily replaces ApiClient.request. Use a private client
        # for each exec so simultaneous transfers/exec cannot corrupt REST calls.
        api_client = client.ApiClient(self._client.configuration)
        api = client.CoreV1Api(api_client)
        response = None
        out, err = bytearray(), bytearray()
        decoders = [codecs.getincrementaldecoder("utf-8")("replace") for _ in range(2)]
        deadline = time.monotonic() + timeout_sec if timeout_sec else float("inf")
        try:
            opening = asyncio.create_task(asyncio.to_thread(
                stream, api.connect_get_namespaced_pod_exec,
                self.pod_name, self.namespace, container="main",
                command=command, stderr=True, stdout=True, stdin=data is not None,
                tty=False, _preload_content=False, binary=True, _request_timeout=30))
            try:
                response = await asyncio.shield(opening)
            except asyncio.CancelledError:
                # Retain the returned socket so finally can close it even if
                # cancellation arrives during the blocking HTTP upgrade.
                response = await opening
                raise
            if data is not None:
                data = as_file(data)
                data.seek(0)
            sending = data is not None
            received = 0
            def append_output(index, buffer, chunk):
                nonlocal received
                received += len(chunk)
                limit = self.max_transfer_bytes if output is not None else self.max_output_bytes
                if received > limit or (index == 1 and len(buffer) + len(chunk) > self.max_output_bytes):
                    raise TransferLimitError("Remote output byte limit exceeded")
                if index == 0 and output is not None:
                    output.write(chunk)
                else:
                    buffer.extend(chunk)
            last_ping = time.monotonic()
            while response.is_open():
                if time.monotonic() >= deadline:
                    return bytes(out), bytes(err) + b"\nKubernetes exec timed out", 124
                if sending:
                    chunk = data.read(65536)
                    if chunk:
                        await self._call(response.write_stdin, chunk)
                    else:
                        sending = False
                await self._call(response.update, timeout=0.1 if sending else 1)
                for index, (reader, buffer) in enumerate(((response.read_stdout, out), (response.read_stderr, err))):
                    chunk = reader(timeout=0)
                    if chunk:
                        if not isinstance(chunk, bytes):
                            raise RuntimeError("Kubernetes stream did not preserve binary output")
                        append_output(index, buffer, chunk)
                        if callback:
                            text = decoders[index].decode(chunk)
                            if text:
                                await callback(text, "stdout" if index == 0 else "stderr")
                if time.monotonic() - last_ping > 20 and response.is_open():
                    ping = getattr(getattr(response, "sock", None), "ping", None)
                    if callable(ping):
                        await self._call(ping)
                    last_ping = time.monotonic()
            # A read of one channel can process the socket's final frames and
            # close it while leaving data buffered on the other channel.
            for index, (reader, buffer) in enumerate(((response.read_stdout, out), (response.read_stderr, err))):
                chunk = reader(timeout=0)
                if chunk:
                    if not isinstance(chunk, bytes):
                        raise RuntimeError("Kubernetes stream did not preserve binary output")
                    append_output(index, buffer, chunk)
                    if callback:
                        text = decoders[index].decode(chunk)
                        if text:
                            await callback(text, "stdout" if index == 0 else "stderr")
            if callback:
                for i, decoder in enumerate(decoders):
                    text = decoder.decode(b"", final=True)
                    if text:
                        await callback(text, "stdout" if i == 0 else "stderr")
            try:
                code = response.returncode
                if code is None:
                    raise ValueError("missing status")
                code = int(code)
            except Exception as exc:
                raise await self._stream_closed_error() from exc
            return bytes(out), bytes(err), code
        except (ConnectionError, EOFError, WebSocketConnectionClosedException) as exc:
            raise await self._stream_closed_error() from exc
        finally:
            if response is not None:
                await self._call(response.close)
            await self._call(api_client.close)

    async def _pod_phase(self):
        try:
            pod = await self._call(self._core.read_namespaced_pod, self.pod_name,
                                   self.namespace, _request_timeout=20)
            return pod.status.phase
        except Exception as exc:
            return f"Unknown ({type(exc).__name__})"

    async def _stream_closed_error(self):
        return ExecStreamClosed(self.pod_name, await self._pod_phase(), (await self._events())[-10:])

    async def _require_running(self):
        phase = await self._pod_phase()
        if phase != "Running":
            raise VerifierPreflightError(
                f"Pod {self.pod_name} is not Running: {phase}; events={json.dumps((await self._events())[-10:])}")

    async def _transfer_stream(self, command, **kwargs):
        # Initial attempt plus at most three retries; never used by arbitrary exec.
        for attempt in range(4):
            try:
                if kwargs.get("output") is not None:
                    kwargs["output"].seek(0)
                    kwargs["output"].truncate()
                return await self._stream(command, **kwargs)
            except ExecStreamClosed:
                if attempt == 3:
                    raise
                await asyncio.sleep(2 ** attempt)
                if await self._pod_phase() != "Running":
                    raise await self._stream_closed_error()
                self._save_facts({"last_transfer_retry": attempt + 1})

    async def _check_verifier_guard(self):
        guard = self._verifier_guard
        if guard is None or guard["checked"]:
            return
        await self._require_running()
        if guard["pending_uploads"]:
            raise VerifierPreflightError("tests/ upload did not complete")
        _, _, code = await self._transfer_stream(
            ["sh", "-c", "test -d /tests && test -f " + shlex.quote(guard["script"])
             + " && test -x " + shlex.quote(guard["script"])],
            timeout_sec=self.transfer_timeout_sec)
        if code:
            raise VerifierPreflightError("tests/ or verifier entrypoint is missing")
        guard["checked"] = True
        self._save_facts({"verifier_health": {"setup_completed": True, "completed": False}})
        self._save_facts({"verifier_preflight": {"pod_phase": "Running",
                         "tests_ready": True, "tests_source": guard["source"]}})

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        effective_env = self._merge_env(env) or {}
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) for k in effective_env):
            raise ValueError("Invalid exec environment variable name")
        argv = ["env", *[f"{k}={v}" for k, v in effective_env.items()], "bash", "-c", command]
        # GNU timeout kills the remote foreground process group too; merely
        # closing a WebSocket is not a reliable remote cancellation mechanism.
        if timeout_sec is not None:
            if timeout_sec <= 0:
                raise ValueError("timeout_sec must be positive")
            argv = ["timeout", "--signal=TERM", "--kill-after=5", str(timeout_sec), *argv]
        script = shlex.join(argv)
        directory = cwd or self.task_env_config.workdir
        if directory:
            script = f"cd {shlex.quote(directory)} && {script}"
        user = self._resolve_user(user)
        if user not in (None, "root", 0, "0"):
            if isinstance(user, int) or str(user).isdigit():
                uid = int(user)
                if uid < 0:
                    raise ValueError("UID must be nonnegative")
                script = (f"u=$(getent passwd {uid} | cut -d: -f1); test -n \"$u\" && "
                          f"su \"$u\" -s /bin/bash -c {shlex.quote(script)}")
            else:
                script = shlex.join(["su", str(user), "-s", "/bin/bash", "-c", script])
        if self._verifier_guard and command == self._verifier_guard.get("command"):
            await self._check_verifier_guard()
        stdout, stderr, code = await self._stream(
            ["sh", "-c", script], timeout_sec=timeout_sec + 10 if timeout_sec else None,
            callback=self._output_callback())
        if self._verifier_guard and command == self._verifier_guard.get("command"):
            healthy = 0 <= code < 124
            self._verifier_guard["execution_completed"] = healthy
            if not healthy:
                self._verifier_guard["checked"] = False
                self._save_facts({"verifier_health": {"setup_completed": False, "completed": False}})
                raise AnyEvalInfrastructureError("Verifier launch or termination failed")
        return ExecResult(stdout=stdout.decode("utf-8", "replace"),
                          stderr=stderr.decode("utf-8", "replace"), return_code=code)

    async def _upload(self, data, target_dir):
        # Tar's end-of-archive blocks terminate extraction without stdin EOF
        # (works with the v4 exec protocol as in Harbor GKE).
        inventory(data, self.max_transfer_bytes, self.max_archive_members)
        command = f"mkdir -p {shlex.quote(target_dir)} && tar --no-same-owner -xf - -C {shlex.quote(target_dir)}"
        _, _, code = await self._transfer_stream(["sh", "-c", command], data=data,
                                        timeout_sec=self.transfer_timeout_sec)
        if code:
            raise RuntimeError(f"Tar upload failed (exit {code})")

    async def _record_artifact_delivery(self, data, target_dir, *, file_name=None):
        """Hash the upload payload and an independent read-back of the delivered tree.

        Tar headers vary across hosts. Each regular file is hashed over its exact
        bytes; directory/link records use a canonical inventory of their semantics.
        Neither bytes nor path contents are logged.
        """
        source = inventory(data, self.max_transfer_bytes, self.max_archive_members)
        command = (["tar", "cf", "-", "-C", target_dir, "--", file_name] if file_name
                   else ["tar", "cf", "-", "-C", target_dir, "."])
        with as_file(await self._download(command)) as downloaded:
            delivered = inventory(downloaded, self.max_transfer_bytes, self.max_archive_members)
        pod = await self._call(self._core.read_namespaced_pod, self.pod_name,
                               self.namespace, _request_timeout=20)
        path = self.trial_paths.trial_dir / "anyeval" / f"{self.pod_name}.json"
        facts = json.loads(path.read_text())
        records = facts.get("verified_artifacts", [])
        # One digest binds the complete inventory including empty directories and links.
        def tree_hash(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        records.append({"source_sha256": tree_hash(source), "delivered_sha256": tree_hash(delivered),
                        "verifier_pod_uid": pod.metadata.uid, "kind": "tree_inventory_v1"})
        for name, digest in sorted(source.items()):
            records.append({"source_sha256": digest, "delivered_sha256": delivered.get(name),
                            "verifier_pod_uid": pod.metadata.uid, "kind": "entry_bytes_v1"})
        self._save_facts({"verified_artifacts": records})
        if source != delivered or pod.metadata.uid != facts["created_uid"]:
            raise AnyEvalInfrastructureError("Verifier artifact delivery mismatch")

    def _pack(self, source, archive_name, buffer):
        count = 0
        expanded = 0
        def check(member):
            nonlocal count, expanded
            count += 1
            expanded += member.size
            if count > self.max_archive_members or expanded > self.max_transfer_bytes:
                raise TransferLimitError("Archive expanded byte/member limit exceeded")
            member.uid = member.gid = 0
            member.uname = member.gname = "root"
            return member
        with tarfile.open(fileobj=LimitedWriter(buffer, self.max_transfer_bytes), mode="w|") as archive:
            archive.add(Path(source), arcname=archive_name, filter=check)
        buffer.seek(0)

    async def upload_file(self, source_path, target_path):
        target = PurePosixPath(target_path)
        with tempfile.TemporaryFile() as data:
            self._pack(source_path, target.name, data)
            await self._upload(data, str(target.parent))
            if self._artifact_transfer:
                await self._record_artifact_delivery(data, str(target.parent), file_name=target.name)

    async def upload_dir(self, source_dir, target_dir):
        with tempfile.TemporaryFile() as data:
            self._pack(source_dir, ".", data)
            await self._upload(data, str(target_dir))
            if self._artifact_transfer:
                await self._record_artifact_delivery(data, str(target_dir))
        if self._verifier_guard is not None and str(target_dir).rstrip("/") == "/tests":
            self._verifier_guard["pending_uploads"].discard(Path(source_dir))

    async def _download(self, command):
        data = tempfile.TemporaryFile()
        try:
            _, _, code = await self._transfer_stream(command, output=data, timeout_sec=self.transfer_timeout_sec)
            if code:
                raise AnyEvalInfrastructureError(f"Tar download failed (exit {code})")
            if not data.tell():
                raise AnyEvalInfrastructureError("Tar download returned no archive")
            inventory(data, self.max_transfer_bytes, self.max_archive_members)
            data.seek(0)
            return data
        except BaseException:
            data.close()
            raise

    async def download_file(self, source_path, target_path):
        source, target = PurePosixPath(source_path), Path(target_path)
        with as_file(await self._download(["tar", "chf", "-", "-C", str(source.parent), "--", source.name])) as data:
            inventory(data, self.max_transfer_bytes, self.max_archive_members)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=data, mode="r:") as archive:
                member = archive.getmember(source.name)
                if not member.isfile():
                    raise AnyEvalInfrastructureError("download_file requires a regular file")
                if target.is_symlink():
                    raise AnyEvalInfrastructureError("Refusing to overwrite a local symlink")
                with archive.extractfile(member) as content, target.open("wb") as destination:
                    shutil.copyfileobj(content, destination, 65536)
                target.chmod(member.mode & 0o777)

    async def download_dir_filtered(self, *, source_dir, target_dir, include=None,
                                    exclude=None, protect=None):
        # Bound the entire source archive before selecting files. Harbor's default
        # implementation extracts an inner gzip tar without expanded/member caps.
        from harbor.utils.path_filter import filter_paths_by_patterns
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        with as_file(await self._download(["tar", "cf", "-", "-C", str(source_dir), "."])) as data:
            inventory(data, self.max_transfer_bytes, self.max_archive_members)
            with tarfile.open(fileobj=data, mode="r:") as archive:
                regular = {str(PurePosixPath(m.name)): m
                           for m in members(archive, self.max_transfer_bytes, self.max_archive_members)
                           if m.isfile() or m.islnk()}
                selected = set(filter_paths_by_patterns(list(regular), include=include, exclude=exclude))
                selected.update(set(protect or []) & regular.keys())
                for name in sorted(selected):
                    destination = target / name
                    if destination.is_symlink() or not destination.resolve().is_relative_to(target.resolve()):
                        raise AnyEvalInfrastructureError("Unsafe filtered download destination")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(regular[name]) as content, destination.open("wb") as stream:
                        shutil.copyfileobj(content, stream, 65536)
                    destination.chmod(regular[name].mode & 0o777)

    async def download_dir_with_exclusions(self, source_dir, target_dir, exclude):
        await self.download_dir_filtered(source_dir=source_dir, target_dir=target_dir, exclude=exclude)

    async def download_dir(self, source_dir, target_dir):
        with as_file(await self._download(["tar", "cf", "-", "-C", str(source_dir), "."])) as data:
            inventory(data, self.max_transfer_bytes, self.max_archive_members)
            with tarfile.open(fileobj=data, mode="r:") as archive:
                for member in members(archive, self.max_transfer_bytes, self.max_archive_members):
                    archive.extract(member, target_dir, filter="data")


# Harbor has no before-verifier/download-recovery hook in 0.22. Scope the wrapper
# to this adapter; leave all other environments and verification commands intact.
from .verifier import install_verifier_hook, install_artifact_hook
install_verifier_hook(AnyEvalK8sEnvironment)

install_artifact_hook(AnyEvalK8sEnvironment)
