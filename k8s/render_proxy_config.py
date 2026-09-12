#!/usr/bin/env python3
"""Render immutable config + strategic Deployment patch; performs no cluster calls."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yaml
from iron_proxy import ALLOWLIST_PATH, load_allowlist, make_config, source_hash


def render(source, ip, cidrs):
    raw = yaml.safe_dump(make_config(ip, cidrs, source=source), sort_keys=False)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    # Include provenance in the name so even a reason/version-only edit rolls out.
    revision = hashlib.sha256((digest + source_hash(source)).encode()).hexdigest()
    name = f"iron-proxy-{revision[:8]}"
    annotations = {"anyeval.io/config-sha256": digest,
                   "anyeval.io/allowlist-version": source["version"],
                   "anyeval.io/allowlist-sha256": source_hash(source)}
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": name, "namespace": "anyeval-sandbox",
                       "annotations": {**annotations, "anyeval.io/cluster-cidrs": json.dumps(cidrs)}},
          "immutable": True,
          "data": {"proxy.yaml": raw, "allowlist.yaml": yaml.safe_dump(source, sort_keys=False)}}
    patch = {"spec": {"template": {"metadata": {"annotations": annotations},
             "spec": {"volumes": [{"name": "config", "configMap": {"name": name}}]}}}}
    return cm, patch


class ManifestDumper(yaml.SafeDumper):
    pass


def _string(dumper, value):
    return dumper.represent_scalar('tag:yaml.org,2002:str', value,
                                   style='|' if '\n' in value else None)


ManifestDumper.add_representer(str, _string)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allowlist', type=Path, default=ALLOWLIST_PATH)
    parser.add_argument('--manifest', type=Path, default=Path(__file__).with_name('iron-proxy.yaml'))
    parser.add_argument('--patch', type=Path, default=Path(__file__).with_name('iron-proxy-config-patch.yaml'))
    parser.add_argument('--proxy-ip', default='34.118.229.224')
    parser.add_argument('--cluster-cidr', action='append', default=None)
    args = parser.parse_args()
    cidrs = args.cluster_cidr or ['10.26.0.0/17', '34.118.224.0/20']
    cm, patch = render(load_allowlist(args.allowlist), args.proxy_ip, cidrs)
    role = {'anyeval.io/role': 'egress-proxy'}
    service = {'apiVersion': 'v1', 'kind': 'Service',
               'metadata': {'name': 'iron-proxy', 'namespace': 'anyeval-sandbox', 'labels': role},
               'spec': {'type': 'ClusterIP', 'clusterIP': args.proxy_ip, 'selector': role,
                        'ports': [{'name': n, 'port': p, 'targetPort': p, 'protocol': proto}
                                  for n, p, proto in [('tunnel', 8080, 'TCP'), ('dns-udp', 53, 'UDP'), ('dns-tcp', 53, 'TCP')]]}}
    args.manifest.write_text(yaml.dump_all([service, cm], Dumper=ManifestDumper, sort_keys=False))
    args.patch.write_text(yaml.safe_dump(patch, sort_keys=False))
    print(json.dumps({'configmap': cm['metadata']['name'], **cm['metadata']['annotations']}))


if __name__ == '__main__':
    main()
