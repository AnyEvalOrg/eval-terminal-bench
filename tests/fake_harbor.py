"""Synthetic child fixture. Never reads benchmark instruction or test contents."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS


class FakeEnvironment:
    def __init__(self, directory):
        self.directory = directory

    async def stop(self, delete):
        from terminal_bench_anyeval.k8s_env import ACTIVE_ENVIRONMENTS
        assert delete
        (self.directory / "cleanup-complete").write_text("yes")
        ACTIVE_ENVIRONMENTS.discard(self)


class FakeTrial:
    created = 0
    ran = 0
    config = None
    mode = "verified"

    @classmethod
    async def create(cls, config):
        cls.created += 1
        cls.config = config
        if cls.mode == "create_error":
            raise RuntimeError("synthetic create failure")
        self = cls()
        self.id = "synthetic-harbor-trial"
        self.directory = config.trials_dir / config.trial_name
        self.directory.mkdir(parents=True)
        (self.directory / "anyeval").mkdir()
        (self.directory / "agent").mkdir()
        (self.directory / "verifier").mkdir()
        (self.directory / "agent/trajectory.json").write_text(json.dumps({"messages": ["PUBLIC_TRANSCRIPT"]}))
        (self.directory / "verifier/test-stdout.txt").write_text("SYNTHETIC_PRIVATE_VERIFIER")
        for role in ("agent", "verifier"):
            facts = {"role": role, "pod": "tb-" + role, "uid": role + "-uid", "node": "node-1",
                     "runtimeClassName": "gvisor", "image": "synthetic:1",
                     "containers": [{"name": "main", "imageID": "registry/synthetic@sha256:" + "a" * 64}],
                     "resources_requested": {"requests": {"cpu": "1"}},
                     "resources": {"requests": {"cpu": "2"}},
                     "network_policy": {"name": "tb-" + role, "uid": "policy-uid", "resource_version": "1",
                                        "egress_to_proxy_only": role == "agent"},
                     "egress_proxy": {"pod": "proxy", "pod_uid": "proxy-uid", "service_ip": "10.0.0.1",
                                      "allowlist_version": "v1", "allowlist_sha256": "b" * 64} if role == "agent" else None,
                     "dmesg_gvisor_boot": "synthetic gVisor boot", "kernel_release": "synthetic-kernel",
                     "kubelet_version": "v1", "node_labels": {"sandbox.gke.io/runtime": "gvisor"},
                     "started_at": "2026-09-12T00:00:00+00:00", "ended_at": "2026-09-12T00:00:02+00:00"}
            (self.directory / f"anyeval/tb-{role}.json").write_text(json.dumps(facts))
        start = datetime.now(timezone.utc)
        self._result = NS(agent_result=NS(n_input_tokens=123, n_output_tokens=45, n_cache_tokens=67,
                                          metadata={"n_episodes": 3, "summarization_count": 1}),
                          agent_execution=NS(started_at=start, finished_at=start + timedelta(seconds=2)),
                          verifier=NS(started_at=start, finished_at=start + timedelta(seconds=1)),
                          verifier_result=NS(rewards={"reward": 1.0}), exception_info=None)
        return self

    async def run(self):
        type(self).ran += 1
        assert os.environ["OPENAI_API_KEY"] == "synthetic-shim-token"
        assert "api_key" not in self.config.agent.kwargs
        if self.mode == "raise":
            raise RuntimeError("failure synthetic-shim-token")
        if self.mode == "wait":
            from terminal_bench_anyeval.k8s_env import ACTIVE_ENVIRONMENTS
            ACTIVE_ENVIRONMENTS.add(FakeEnvironment(self.directory))
            Path(os.environ["FAKE_READY"]).write_text(str(self.directory))
            await asyncio.Event().wait()
        if self.mode in {"AgentTimeoutError", "VerifierTimeoutError", "EnvironmentStartTimeoutError"}:
            self._result.exception_info = NS(exception_type=self.mode, exception_message="synthetic timeout")
        if self.mode == "missing_reward":
            self._result.verifier_result = None
        return self._result
