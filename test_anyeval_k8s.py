"""Offline contract tests; no benchmark instructions, tests, or solutions read."""
import asyncio
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from anyeval_k8s import AnyEvalK8sEnvironment
from harbor.environments.factory import EnvironmentFactory
from harbor.models.task.config import EnvironmentConfig, NetworkPolicy, TaskConfig
from harbor.models.trial.config import EnvironmentConfig as RuntimeConfig
from harbor.models.trial.paths import TrialPaths


class ContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.context = self.root / "environment"
        self.context.mkdir()
        self.config = EnvironmentConfig(docker_image="example/test:unchanged", cpus=1,
                                        memory_mb=2048, storage_mb=10240, gpus=0)

    def env(self, **kwargs):
        return AnyEvalK8sEnvironment(environment_dir=self.context, environment_name="test",
                                    session_id="test__abc__env", trial_paths=TrialPaths(self.root / "test__abc"),
                                    task_env_config=self.config,
                                    network_policy=NetworkPolicy(network_mode="no-network"), **kwargs)

    def test_manifest_and_capabilities(self):
        env = self.env()
        pod, policy = env._manifests()
        spec = pod["spec"]
        self.assertEqual(spec["runtimeClassName"], "gvisor")
        self.assertEqual(spec["nodeSelector"], {"cloud.google.com/gke-spot": "true"})
        self.assertIs(spec["automountServiceAccountToken"], False)
        self.assertEqual(spec["restartPolicy"], "Never")
        self.assertEqual(len(spec["containers"]), 1)
        c = spec["containers"][0]
        self.assertEqual(c["image"], self.config.docker_image)
        self.assertEqual(c["command"], ["sleep", "infinity"])
        expected = {"cpu": "1", "memory": "2048Mi", "ephemeral-storage": "10240Mi"}
        self.assertEqual(c["resources"], {"requests": expected, "limits": expected})
        self.assertEqual(pod["metadata"]["labels"]["anyeval.io/trial"], "test__abc")
        self.assertEqual(pod["metadata"]["labels"]["inspect/service"], "default")
        self.assertEqual(policy["spec"]["ingress"], [])
        self.assertEqual(policy["spec"]["egress"], [])
        for k, v in policy["spec"]["podSelector"]["matchLabels"].items():
            self.assertEqual(pod["metadata"]["labels"][k], v)
        self.assertTrue(env.capabilities.disable_internet)
        self.assertFalse(env.capabilities.mounted)
        self.assertFalse(env.capabilities.docker_compose)

    def test_refusals(self):
        for field, value, expected in (("gpus", 1, "GPU"), ("allow_internet", True, "allow_internet"),
                                        ("docker_image", None, "docker_image"), ("cpus", None, "cpus")):
            original = getattr(self.config, field)
            setattr(self.config, field, value)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, expected):
                self.env()
            setattr(self.config, field, original)
        (self.context / "docker-compose.yaml").touch()
        with self.assertRaisesRegex(ValueError, "Compose"):
            self.env()

    def test_legacy_metadata_cannot_be_bypassed(self):
        (self.root / "task.toml").write_text("[environment]\nallow_internet = true\n")
        with self.assertRaisesRegex(ValueError, "task.toml declares allow_internet"):
            self.env()

    def test_resource_override_refused(self):
        with self.assertRaisesRegex(ValueError, "overrides"):
            self.env(override_cpus=2)
        with self.assertRaisesRegex(ValueError, "requests == limits"):
            self.env(cpu_enforcement_policy="ignore")

    def test_phase_network_refused(self):
        with self.assertRaisesRegex(ValueError, "deny-all"):
            self.env(phase_network_policies=[NetworkPolicy(network_mode="public")])

    def test_harbor_factory_separate_verifier(self):
        # Mirror Trial._separate_verifier_env: same runtime import; distinct
        # session and verifier config/context. No private verifier reimplementation.
        runtime = RuntimeConfig(import_path="anyeval_k8s:AnyEvalK8sEnvironment")
        verifier_config = self.config.model_copy(update={"docker_image": "example/verifier:digest-tag"})
        env = EnvironmentFactory.create_environment_from_config(
            config=runtime, environment_dir=self.context, environment_name="test",
            session_id="test__abc__verifier__grade", trial_paths=TrialPaths(self.root / "test__abc"),
            task_env_config=verifier_config, network_policy=NetworkPolicy(network_mode="no-network"))
        self.assertEqual(env._manifests()[0]["spec"]["containers"][0]["image"], "example/verifier:digest-tag")
        self.assertNotEqual(env.pod_name, self.env().pod_name)

    async def local_stream(self, command, *, data=None, timeout_sec=None, callback=None):
        proc = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate(data)
        return out, err, proc.returncode

    async def test_exec_quoting_env_cwd_and_exit(self):
        env = self.env(persistent_env={"VALUE": "persistent"})
        env._stream = self.local_stream
        directory = self.root / "spaces ' and $ dollar"
        directory.mkdir()
        marker = self.root / "should-not-exist"
        value = f"' $(touch {marker}) ; $VALUE"
        with env.scoped_exec_env({"VALUE": value}):
            result = await env.exec('printf "%s\\n" "$PWD" "$VALUE"; printf error >&2; exit 7',
                                    cwd=str(directory), env={"VALUE": "per-exec"})
        self.assertEqual(result.stdout, f"{directory}\n{value}\n")
        self.assertEqual(result.stderr, "error")
        self.assertEqual(result.return_code, 7)
        self.assertFalse(marker.exists())

    async def test_timeout_wrapper_and_user(self):
        env = self.env()
        env._stream = AsyncMock(return_value=(b"", b"", 124))
        result = await env.exec("sleep 100", timeout_sec=1, user=123)
        args, kwargs = env._stream.call_args
        self.assertIn("timeout --signal=TERM --kill-after=5 1", args[0][-1])
        self.assertIn("getent passwd 123", args[0][-1])
        self.assertEqual(kwargs["timeout_sec"], 11)
        self.assertEqual(result.return_code, 124)

    async def test_tar_binary_roundtrip_modes_links_empty_and_renamed_file(self):
        env = self.env()
        env._stream = self.local_stream
        source, remote, target = (self.root / n for n in ("source", "remote ' dir", "download"))
        source.mkdir()
        payload = bytes(range(256)) * 257
        (source / "binary").write_bytes(payload)
        (source / "binary").chmod(0o751)
        (source / "empty").mkdir()
        (source / "link").symlink_to("binary")
        await env.upload_dir(source, str(remote))
        await env.download_dir(str(remote), target)
        self.assertEqual((target / "binary").read_bytes(), payload)
        self.assertEqual((target / "binary").stat().st_mode & 0o777, 0o751)
        self.assertTrue((target / "empty").is_dir())
        self.assertTrue((target / "link").is_symlink())
        await env.upload_file(source / "binary", str(remote / "renamed"))
        await env.download_file(str(remote / "renamed"), self.root / "final")
        self.assertEqual((self.root / "final").read_bytes(), payload)

    async def test_download_failure_not_silent(self):
        env = self.env()
        env._stream = AsyncMock(return_value=(b"", b"missing", 2))
        with self.assertRaisesRegex(RuntimeError, "exit 2"):
            await env.download_dir("/missing", self.root / "out")

    async def test_cleanup_attempts_both_resources_and_retries(self):
        env = self.env()
        env._client = MagicMock()
        env._core = MagicMock()
        env._network = MagicMock()
        env._pod_attempted = env._policy_attempted = True
        env._core.delete_namespaced_pod.side_effect = RuntimeError("unavailable")
        with patch("anyeval_k8s.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                await env.stop(delete=False)
        self.assertEqual(env._core.delete_namespaced_pod.call_count, 3)
        env._network.delete_namespaced_network_policy.assert_called_once()
        self.assertFalse(env._policy_attempted)
        env._core.delete_namespaced_pod.side_effect = None
        await env.stop()
        self.assertIsNone(env._client)
        await env.stop()

    async def test_start_failure_rolls_back_policy_and_pod(self):
        env = self.env()
        env._client = MagicMock()
        env._core = MagicMock()
        env._network = MagicMock()
        env._events = AsyncMock(return_value=[{"reason": "FailedScheduling"}])
        env._wait_running = AsyncMock(side_effect=TimeoutError("cold start"))
        core, net = env._core, env._network
        with self.assertRaisesRegex(RuntimeError, "FailedScheduling"):
            await env.start()
        core.delete_namespaced_pod.assert_called_once()
        net.delete_namespaced_network_policy.assert_called_once()

    async def test_binary_websocket_status_and_incremental_callbacks(self):
        class Response:
            def __init__(self):
                self.frames = [(b"\xc3", b""), (b"\xbc\xff", b"error")]
                self.current = (b"", b"")
                self.sock = MagicMock()
                self.returncode = 9
                self.closed = False
            def is_open(self):
                return bool(self.frames)
            def update(self, timeout):
                self.current = self.frames.pop(0)
            def read_stdout(self, timeout):
                value = self.current[0]
                self.current = (b"", self.current[1])
                return value
            def read_stderr(self, timeout):
                value = self.current[1]
                self.current = (self.current[0], b"")
                return value
            def close(self):
                self.closed = True
        response = Response()
        stream = MagicMock(return_value=response)
        client = MagicMock()
        env = self.env()
        env._client = MagicMock()
        chunks = []
        async def callback(text, channel):
            chunks.append((text, channel))
        with patch.dict(sys.modules, {"kubernetes": NS(client=client),
                                      "kubernetes.stream": NS(stream=stream)}):
            out, err, code = await env._stream(["true"], callback=callback)
        self.assertEqual(out, b"\xc3\xbc\xff")
        self.assertEqual(err, b"error")
        self.assertEqual(code, 9)
        self.assertEqual(chunks, [("ü�", "stdout"), ("error", "stderr")])
        self.assertTrue(stream.call_args.kwargs["binary"])
        self.assertTrue(response.closed)
        client.ApiClient.return_value.close.assert_called_once()

    async def test_websocket_missing_exit_status_fails(self):
        response = MagicMock()
        response.is_open.return_value = False
        response.read_stdout.return_value = b""
        response.read_stderr.return_value = b""
        response.returncode = None
        client = MagicMock()
        env = self.env()
        env._client = MagicMock()
        with patch.dict(sys.modules, {"kubernetes": NS(client=client),
                                      "kubernetes.stream": NS(stream=MagicMock(return_value=response))}):
            with self.assertRaisesRegex(RuntimeError, "without a valid exit status"):
                await env._stream(["true"])
        response.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
