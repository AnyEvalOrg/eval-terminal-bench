#!/usr/bin/env python3
"""Deploy/probe using the Kubernetes Python client; never reads task content."""
import argparse
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

def _proxy_pod_name(core):
    """The egress proxy is a Deployment; its pod name carries a hash."""
    pods = core.list_namespaced_pod("anyeval-sandbox", label_selector="anyeval.io/role=egress-proxy").items
    ready = [p for p in pods if p.status.phase == "Running"]
    if not ready:
        raise RuntimeError("no running egress-proxy pod")
    return ready[0].metadata.name

import yaml
from kubernetes import client, config
from kubernetes.stream import stream

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from iron_proxy import NAMESPACE, ROLE, allowlist_hash, make_config, proxy_egress, proxy_env

EVIDENCE = ROOT / "evidence"
IMAGE = "ironsh/iron-proxy:0.49.0"


def save(name, value):
    EVIDENCE.mkdir(exist_ok=True)
    (EVIDENCE / name).write_text(value if isinstance(value, str) else json.dumps(value, indent=2) + "\n")


def registry_image():
    try:
        url = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:ironsh/iron-proxy:pull"
        with urllib.request.urlopen(url, timeout=20) as r:
            token = json.load(r)["token"]
        req = urllib.request.Request("https://registry-1.docker.io/v2/ironsh/iron-proxy/manifests/v0.49.0",
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            content = r.read()
            digest = r.headers["Docker-Content-Digest"]
        if digest != "sha256:" + hashlib.sha256(content).hexdigest():
            raise ValueError("Registry digest does not match manifest bytes")
        save("registry.json", {"image": IMAGE + "@" + digest, "digest_verified": True})
        return IMAGE + "@" + digest
    except Exception as exc:
        save("registry.json", {"image": IMAGE, "digest_verified": False,
                               "error_type": type(exc).__name__, "error": str(exc)})
        return IMAGE


def apis():
    cfg = client.Configuration()
    config.load_kube_config(config_file=os.environ["KUBECONFIG"],
                          client_configuration=cfg, persist_config=False)
    cfg.retries = 0
    api = client.ApiClient(cfg)
    return api, client.CoreV1Api(api), client.NetworkingV1Api(api)


def cluster_cidrs(api, explicit):
    if explicit:
        cidrs = [str(ipaddress.ip_network(c.strip())) for c in explicit.split(",")]
        if len(cidrs) < 2:
            raise ValueError("Supply all pod and Service CIDRs (at least two entries)")
        save("cluster-ranges.json", {"source": "explicit operator input", "cidrs": cidrs})
        return cidrs
    command = ["gcloud", "container", "clusters", "list", "--project=openevalz-sbx-84737", "--format=json"]
    result = subprocess.run(command, text=True, capture_output=True, timeout=60)
    if result.returncode:
        save("cluster-ranges.json", {"command": command, "returncode": result.returncode,
                                     "stderr": result.stderr})
        raise RuntimeError("Cannot discover cluster ranges; see evidence/cluster-ranges.json")
    clusters = [c for c in json.loads(result.stdout)
                if c.get("endpoint") and c["endpoint"] in api.configuration.host]
    if len(clusters) != 1:
        raise RuntimeError("Cannot identify the current cluster by API endpoint")
    c = clusters[0]
    ranges = c.get("ipAllocationPolicy", {})
    pod = ranges.get("clusterIpv4CidrBlock") or c.get("clusterIpv4Cidr")
    service = ranges.get("servicesIpv4CidrBlock") or c.get("servicesIpv4Cidr")
    if not pod or not service:
        raise RuntimeError("GKE did not return both pod and Service CIDRs")
    cidrs = [pod, service]
    cidrs.extend(ranges.get("additionalPodRangesConfig", {}).get("podRangeInfo", []))
    # Do not guess CIDRs from range names or node IPs.
    if any(not isinstance(cidr, str) for cidr in cidrs):
        raise RuntimeError("Additional pod ranges require explicit --cluster-cidrs")
    if ranges.get("additionalPodRangesConfig", {}).get("podRangeNames"):
        raise RuntimeError("Additional pod ranges require explicit --cluster-cidrs")
    for key in ("servicesIpv6CidrBlock", "subnetIpv6CidrBlock"):
        if ranges.get(key):
            cidrs.append(ranges[key])
    cidrs = list(dict.fromkeys(str(ipaddress.ip_network(cidr)) for cidr in cidrs))
    save("cluster-ranges.json", {"command": command, "cluster": c["name"], "cidrs": cidrs})
    return cidrs


def service_manifest():
    return {"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": "iron-proxy", "namespace": NAMESPACE, "labels": ROLE},
            "spec": {"type": "ClusterIP", "selector": ROLE,
                     "ports": [{"name": "tunnel", "port": 8080, "targetPort": 8080, "protocol": "TCP"},
                               {"name": "dns-udp", "port": 53, "targetPort": 53, "protocol": "UDP"},
                               {"name": "dns-tcp", "port": 53, "targetPort": 53, "protocol": "TCP"}]}}


def manifests(service, cidrs, image):
    raw = yaml.safe_dump(make_config(service["spec"]["clusterIP"], cidrs), sort_keys=False)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": "iron-proxy", "namespace": NAMESPACE,
                       "annotations": {"anyeval.io/cluster-cidrs": json.dumps(cidrs)}},
          "immutable": True, "data": {"proxy.yaml": raw}}
    pod = {"apiVersion": "v1", "kind": "Pod",
           "metadata": {"name": "iron-proxy", "namespace": NAMESPACE, "labels": ROLE,
                        "annotations": {"anyeval.io/config-sha256": digest}},
           "spec": {"restartPolicy": "Always", "automountServiceAccountToken": False,
                    "dnsPolicy": "ClusterFirst",
                    "containers": [{"name": "iron-proxy", "image": image,
                                    "args": ["-config", "/etc/iron-proxy/proxy.yaml"],
                                    "resources": {"requests": {"cpu": "500m", "memory": "1Gi"},
                                                  "limits": {"cpu": "500m", "memory": "1Gi"}},
                                    "securityContext": {"runAsUser": 0, "allowPrivilegeEscalation": False},
                                    "readinessProbe": {"tcpSocket": {"port": 8080}, "periodSeconds": 3},
                                    "volumeMounts": [{"name": "config", "mountPath": "/etc/iron-proxy", "readOnly": True}]}],
                    "volumes": [{"name": "config", "configMap": {"name": "iron-proxy"}}]}}
    return [service, cm, pod]


def wait_ready(core, name):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        p = core.read_namespaced_pod(name, NAMESPACE, _request_timeout=20)
        if any(c.type == "Ready" and c.status == "True" for c in p.status.conditions or []):
            return p
        if p.status.phase in ("Failed", "Succeeded"):
            raise RuntimeError(f"{name} terminated: {p.status.phase}")
        time.sleep(3)
    raise TimeoutError(f"{name} did not become Ready")


def deploy(args):
    image = registry_image()
    api, core, _ = apis()
    # Never allocate a guessed Service IP. Let Kubernetes allocate it first.
    service = service_manifest()
    try:
        live = core.create_namespaced_service(NAMESPACE, service, _request_timeout=20)
    except client.ApiException as exc:
        if exc.status != 409:
            raise
        live = core.read_namespaced_service("iron-proxy", NAMESPACE, _request_timeout=20)
        if live.spec.selector != ROLE:
            raise RuntimeError("Existing iron-proxy Service has a different selector")
    service["spec"]["clusterIP"] = live.spec.cluster_ip
    cidrs = cluster_cidrs(api, args.cluster_cidrs)
    if not any(ipaddress.ip_address(live.spec.cluster_ip) in ipaddress.ip_network(c)
               for c in cidrs):
        raise RuntimeError("Assigned Service IP is outside the discovered cluster ranges")
    docs = manifests(service, cidrs, image)
    (ROOT / "k8s/iron-proxy.yaml").write_text(yaml.safe_dump_all(docs, sort_keys=False))
    for doc, create, read in ((docs[1], core.create_namespaced_config_map, core.read_namespaced_config_map),
                              (docs[2], core.create_namespaced_pod, core.read_namespaced_pod)):
        try:
            create(NAMESPACE, doc, _request_timeout=30)
        except client.ApiException as exc:
            if exc.status != 409:
                raise
            old = read("iron-proxy", NAMESPACE, _request_timeout=20)
            if doc["kind"] == "ConfigMap":
                same = old.data == doc["data"] and old.immutable is True
            else:
                same = ((old.metadata.annotations or {}).get("anyeval.io/config-sha256") ==
                        doc["metadata"]["annotations"]["anyeval.io/config-sha256"]
                        and old.spec.containers[0].image == image)
            if not same:
                raise RuntimeError("Existing proxy differs; refusing to replace a running shared proxy")
    p = wait_ready(core, "iron-proxy")
    save("proxy-pod.json", api.sanitize_for_serialization(p))
    save("proxy-startup.jsonl", core.read_namespaced_pod_log(_proxy_pod_name(core), NAMESPACE, _request_timeout=20))
    print(f"iron-proxy Ready at {live.spec.cluster_ip}:8080; left running")


def probe(args):
    api, core, network = apis()
    service = core.read_namespaced_service("iron-proxy", NAMESPACE, _request_timeout=20)
    ip = service.spec.cluster_ip
    name = "iron-proxy-probe-" + str(int(time.time()))
    labels = {"anyeval.io/environment": name}
    policy = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
              "metadata": {"name": name, "namespace": NAMESPACE},
              "spec": {"podSelector": {"matchLabels": labels}, "policyTypes": ["Ingress", "Egress"],
                       "ingress": [], "egress": proxy_egress()}}
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
           "spec": {"runtimeClassName": "gvisor", "nodeSelector": {"cloud.google.com/gke-spot": "true"},
                    "restartPolicy": "Never", "automountServiceAccountToken": False,
                    "dnsPolicy": "None", "dnsConfig": {"nameservers": [ip]},
                    "containers": [{"name": "main", "image": "python:3.12-slim", "command": ["sleep", "infinity"],
                                    "env": [{"name": k, "value": v} for k, v in proxy_env(ip).items()],
                                    "resources": {"requests": {"cpu": "500m", "memory": "1Gi", "ephemeral-storage": "1Gi"},
                                                  "limits": {"cpu": "500m", "memory": "1Gi", "ephemeral-storage": "1Gi"}},
                                    "securityContext": {"runAsUser": 0, "allowPrivilegeEscalation": False}}]}}
    save("probe-manifests.yaml", yaml.safe_dump_all([policy, pod], sort_keys=False))
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    attempted = False
    try:
        network.create_namespaced_network_policy(NAMESPACE, policy, _request_timeout=20)
        attempted = True
        core.create_namespaced_pod(NAMESPACE, pod, _request_timeout=30)
        p = wait_ready(core, name)
        save("probe-pod.json", api.sanitize_for_serialization(p))
        command = ["python", "-c", (ROOT / "k8s/probe_egress.py").read_text(), ip]
        save("probe-command.json", {"api": "pods/exec", "pod": name, "command": command})
        with client.ApiClient(api.configuration) as exec_api:
            output = stream(client.CoreV1Api(exec_api).connect_get_namespaced_pod_exec,
                            name, NAMESPACE, command=command, container="main", stdout=True,
                            stderr=True, stdin=False, tty=False, _request_timeout=300)
        save("probe-output.jsonl", output)
        rows = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        elapsed = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(started)
        logs = core.read_namespaced_pod_log(_proxy_pod_name(core), NAMESPACE,
                                           since_seconds=int(elapsed.total_seconds()) + 2, _request_timeout=20)
        save("probe-proxy.jsonl", logs)
        warnings = [line for line in logs.splitlines() if "example.com" in line and "warn" in line.lower()]
        save("off-list-warn.jsonl", "\n".join(warnings) + "\n")
        passed = bool(rows) and rows[-1].get("all_passed") is True and bool(warnings)
        save("probe-result.json", {"passed": passed, "started": started, "finished": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                   "proxy_ip": ip, "warn_lines": len(warnings)})
        if not passed:
            raise RuntimeError("Isolation probes or off-list warning evidence failed")
        print("Isolation probes passed; off-list host allowed with warning as expected in WARN mode")
    finally:
        if attempted:
            for delete in (core.delete_namespaced_pod, network.delete_namespaced_network_policy):
                try:
                    delete(name, NAMESPACE, _request_timeout=20)
                except client.ApiException as exc:
                    if exc.status != 404:
                        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["deploy", "probe"])
    parser.add_argument("--cluster-cidrs", help="Comma-separated actual pod and Service CIDRs; otherwise query GKE")
    args = parser.parse_args()
    try:
        {"deploy": deploy, "probe": probe}[args.action](args)
    except Exception as exc:
        save(args.action + "-failure.json", {"command": sys.argv, "error_type": type(exc).__name__,
                                             "error": str(exc), "time": datetime.datetime.now(datetime.timezone.utc).isoformat()})
        print(f"{args.action} failed: {type(exc).__name__}; see evidence/{args.action}-failure.json", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
