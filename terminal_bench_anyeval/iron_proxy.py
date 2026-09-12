"""Shared enforced egress policy and iron-proxy configuration helpers."""
import hashlib
import ipaddress
import json

NAMESPACE = "anyeval-sandbox"
ROLE = {"anyeval.io/role": "egress-proxy"}
from pathlib import Path
import yaml

ALLOWLIST_PATH = Path(__file__).resolve().parent / "k8s" / "allowlist.yaml"


def load_allowlist(path=ALLOWLIST_PATH):
    source = yaml.safe_load(Path(path).read_text())
    if not isinstance(source.get("version"), str) or not source["version"].strip():
        raise ValueError("Allowlist requires a nonempty string version")
    hosts = source.get("hosts", [])
    domains = [entry["host"] for entry in hosts]
    import re
    if (not domains or len(set(domains)) != len(domains)
            or any(not isinstance(d, str) or not re.fullmatch(
                r"(?:\*\.)?[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", d) for d in domains)
            or any(not isinstance(e.get("reason"), str) or not e["reason"].strip()
                   or "\n" in e["reason"] for e in hosts)):
        raise ValueError("Allowlist requires unique DNS hosts and one-line reasons")
    return source


def source_hash(source):
    """Hash canonical source JSON, including version and reasons (not YAML styling)."""
    return hashlib.sha256(json.dumps(source, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


DOMAINS = [e["host"] for e in load_allowlist()["hosts"]]
DENY_CIDRS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
              "169.254.0.0/16", "fc00::/7", "::1/128", "127.0.0.0/8"]


def allowlist_hash(config):
    """SHA256 of canonical JSON of the entire allowlist transform config."""
    matches = [t["config"] for t in config["transforms"] if t["name"] == "allowlist"]
    if len(matches) != 1:
        raise ValueError("Exactly one allowlist transform is required")
    return hashlib.sha256(json.dumps(matches[0], sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def proxy_env(ip):
    ipaddress.IPv4Address(ip)
    url = f"http://{ip}:8080"
    return {**{k: url for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")},
            "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}


def proxy_egress():
    # A podSelector without namespaceSelector is confined to this namespace.
    return [{"to": [{"podSelector": {"matchLabels": dict(ROLE)}}],
             "ports": [{"port": 8080, "protocol": "TCP"},
                       {"port": 53, "protocol": "UDP"},
                       {"port": 53, "protocol": "TCP"}]}]


def make_config(ip, cluster_cidrs, *, source=None):
    ipaddress.IPv4Address(ip)
    if not cluster_cidrs:
        raise ValueError("Cluster pod and Service CIDRs must be discovered before deployment")
    cidrs = list(dict.fromkeys(DENY_CIDRS + [str(ipaddress.ip_network(c)) for c in cluster_cidrs]))
    return {"dns": {"listen": ":53", "proxy_ip": ip},
            "proxy": {"tunnel_listen": ":8080", "http_listen": "127.0.0.1:8081",
                      "https_listen": "127.0.0.1:8443",
                      "max_request_body_bytes": 64 * 1024 * 1024,
                      "max_response_body_bytes": 512 * 1024 * 1024,
                      "upstream_deny_cidrs": cidrs},
            "tls": {"mode": "sni-only"},
            "transforms": [{"name": "allowlist", "config": {"domains": [e["host"] for e in (source or load_allowlist())["hosts"]], "warn": False}}],
            "metrics": {"listen": "127.0.0.1:9090"}, "log": {"level": "info"}}
