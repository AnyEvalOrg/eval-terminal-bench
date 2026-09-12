"""Local adapter safety regressions using synthetic fixtures only."""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from harbor.models.task.config import EnvironmentConfig, NetworkPolicy, NetworkMode
from harbor.models.trial.paths import TrialPaths
from anyeval_k8s import AnyEvalK8sEnvironment
from iron_proxy import DENY_CIDRS, DOMAINS, allowlist_hash, make_config, proxy_egress, load_allowlist, source_hash


class EgressChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "environment").mkdir()

    def env(self, role="env", enabled=True, offline=False, internet=False):
        with patch.dict(os.environ, {"ANYEVAL_TB_EGRESS_PROXY": "1" if enabled else "0",
                                     "ANYEVAL_TB_OFFLINE_PROTOCOL": "1" if offline else "0"}):
            return AnyEvalK8sEnvironment(
                environment_dir=self.root / "environment", environment_name="synthetic",
                session_id=f"synthetic__{role}", trial_paths=TrialPaths(self.root / "synthetic"),
                network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK),
                task_env_config=EnvironmentConfig(docker_image="python:3.12-slim", cpus=1,
                    memory_mb=1024, storage_mb=1024, allow_internet=internet))

    def prepared(self):
        env = self.env()
        config = make_config("10.20.0.3", ["10.21.0.0/16", "10.20.0.0/20"])
        raw = yaml.safe_dump(config)
        service = NS(spec=NS(cluster_ip="10.20.0.3", selector={"anyeval.io/role": "egress-proxy"}))
        from k8s.render_proxy_config import render
        rendered, deployment_patch = render(load_allowlist(), "10.20.0.3", ["10.21.0.0/16", "10.20.0.0/20"])
        raw = rendered["data"]["proxy.yaml"]
        cm = NS(data=rendered["data"], immutable=True,
                metadata=NS(annotations=rendered["metadata"]["annotations"]))
        volumes = [NS(name="config", config_map=NS(name=rendered["metadata"]["name"]))]
        annotations = deployment_patch["spec"]["template"]["metadata"]["annotations"]
        pod = NS(metadata=NS(annotations=dict(annotations), labels={"anyeval.io/role": "egress-proxy"}),
                 status=NS(phase="Running", container_statuses=[NS(ready=True)],
                           conditions=[NS(type="Ready", status="True")]),
                 spec=NS(volumes=volumes))
        deployment = NS(spec=NS(template=NS(spec=NS(volumes=volumes),
                                            metadata=NS(annotations=dict(annotations)))))
        env._core = MagicMock()
        env._call = AsyncMock(side_effect=[service, deployment, cm, NS(items=[pod])])
        return env, service, cm, pod, config

    def test_primary_exact_egress_dns_env_and_resources(self):
        env, _, _, _, config = self.prepared()
        asyncio.run(env._prepare_proxy())
        pod, policy = env._manifests()
        self.assertEqual(policy["spec"]["egress"], proxy_egress())
        self.assertEqual(policy["spec"]["ingress"], [])
        self.assertEqual(pod["spec"]["dnsPolicy"], "None")
        self.assertEqual(pod["spec"]["dnsConfig"], {"nameservers": ["10.20.0.3"]})
        container = pod["spec"]["containers"][0]
        values = {v["name"]: v["value"] for v in container["env"]}
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertEqual(values[key], "http://10.20.0.3:8080")
        self.assertEqual(values["NO_PROXY"], "localhost,127.0.0.1")
        self.assertEqual(container["resources"]["requests"], container["resources"]["limits"])
        self.assertEqual(env._proxy_facts["allowlist_sha256"], source_hash(load_allowlist()))
        self.assertEqual(env._proxy_facts["allowlist_version"], load_allowlist()["version"])
        self.assertEqual(env._proxy_facts["allowlist_mode"], "enforce")
        self.assertEqual(pod["spec"]["runtimeClassName"], "gvisor")
        self.assertFalse(pod["spec"]["automountServiceAccountToken"])

    def test_separate_and_unknown_roles_stay_denied(self):
        for role in ("verifier__grade", "verifier__truncated__01234567", "unrecognized"):
            env = self.env(role=role)
            asyncio.run(env._prepare_proxy())
            pod, policy = env._manifests()
            self.assertFalse(env.egress_proxy_enabled)
            self.assertEqual(policy["spec"]["egress"], [])
            self.assertNotIn("dnsConfig", pod["spec"])
            self.assertFalse(any(v["name"] == "HTTPS_PROXY" for v in pod["spec"]["containers"][0]["env"]))

    def test_default_deny_all(self):
        env = self.env(enabled=False)
        self.assertEqual(env._manifests()[1]["spec"]["egress"], [])

    def test_no_unresolved_proxy(self):
        with self.assertRaises(RuntimeError):
            self.env()._manifests()

    def test_internet_refusal_is_preserved(self):
        with self.assertRaises(ValueError):
            self.env(internet=True)
        self.assertTrue(self.env(internet=True, offline=True).offline_protocol_override)

    def test_ip_mismatch_refused(self):
        env, service, *_ = self.prepared()
        service.spec.cluster_ip = "10.20.0.9"
        with self.assertRaises(RuntimeError):
            asyncio.run(env._prepare_proxy())

    def test_config_revision_mismatch_refused(self):
        env, _, _, pod, _ = self.prepared()
        pod.metadata.annotations = {}
        with self.assertRaises(RuntimeError):
            asyncio.run(env._prepare_proxy())

    def test_missing_cluster_ranges_refused(self):
        env, _, cm, _, _ = self.prepared()
        cm.metadata.annotations = {}
        with self.assertRaises(RuntimeError):
            asyncio.run(env._prepare_proxy())

    def test_unready_proxy_refused(self):
        env, _, _, pod, _ = self.prepared()
        pod.status.conditions = []
        with self.assertRaises(RuntimeError):
            asyncio.run(env._prepare_proxy())

    def test_warn_mode_refused(self):
        env, _, cm, _, config = self.prepared()
        config["transforms"][0]["config"]["warn"] = True
        cm.data["proxy.yaml"] = yaml.safe_dump(config)
        with self.assertRaisesRegex(RuntimeError, "ENFORCE"):
            asyncio.run(env._prepare_proxy())

    def test_source_hash_tampering_refused(self):
        env, _, cm, _, _ = self.prepared()
        cm.metadata.annotations["anyeval.io/allowlist-sha256"] = "incorrect"
        with self.assertRaisesRegex(RuntimeError, "provenance"):
            asyncio.run(env._prepare_proxy())

    def test_mixed_rollout_refused(self):
        env, service, cm, pod, _ = self.prepared()
        calls = list(env._call.side_effect)
        old = copy.deepcopy(pod)
        old.metadata.annotations["anyeval.io/config-sha256"] = "old"
        calls[-1].items.append(old)
        env._call.side_effect = calls
        with self.assertRaisesRegex(RuntimeError, "revision"):
            asyncio.run(env._prepare_proxy())

    def test_mutable_config_refused(self):
        env, _, cm, _, _ = self.prepared()
        cm.immutable = False
        with self.assertRaises(RuntimeError):
            asyncio.run(env._prepare_proxy())

    def test_caps_allowlist_and_denied_ranges(self):
        config = make_config("10.20.0.3", ["10.21.0.0/16", "10.20.0.0/20"])
        self.assertEqual(config["transforms"][0]["config"], {"domains": DOMAINS, "warn": False})
        self.assertTrue(set(DENY_CIDRS).issubset(config["proxy"]["upstream_deny_cidrs"]))
        self.assertEqual(config["proxy"]["max_request_body_bytes"], 67108864)
        self.assertEqual(config["proxy"]["max_response_body_bytes"], 536870912)
        changed = copy.deepcopy(config)
        changed["transforms"][0]["config"]["warn"] = True
        self.assertNotEqual(allowlist_hash(config), allowlist_hash(changed))
        with self.assertRaises(ValueError):
            make_config("10.20.0.3", [])


if __name__ == "__main__":
    unittest.main()
