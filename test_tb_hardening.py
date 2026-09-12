"""Offline regressions, exclusively synthetic harness fixtures."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from anyeval_k8s import ExecStreamClosed, VerifierPreflightError
from harbor.verifier.verifier import Verifier, DownloadVerifierDirError
from iron_proxy import load_allowlist, source_hash
from k8s.eligible_tasks import eligibility
from k8s.render_proxy_config import render
from summarize_job import classify, summarize
import test_anyeval_k8s as adapter_tests


class TransferTests(unittest.IsolatedAsyncioTestCase):
    setUp = adapter_tests.ContractTests.setUp
    env = adapter_tests.ContractTests.env

    def prepared(self):
        env = self.env()
        env._pod_phase = AsyncMock(return_value='Running')
        env._events = AsyncMock(return_value=[{'reason': 'SampleEvent'}])
        env._stream = AsyncMock()
        return env

    async def test_upload_retries_three_times(self):
        env = self.prepared()
        closed = ExecStreamClosed(env.pod_name, 'Running', [])
        env._stream.side_effect = [closed, closed, closed, (b'', b'', 0)]
        with patch('anyeval_k8s.asyncio.sleep', new=AsyncMock()) as sleep:
            await env._upload(b'synthetic archive', '/tmp')
        self.assertEqual(env._stream.await_count, 4)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [1, 2, 4])
        self.assertEqual(env._pod_phase.await_count, 3)
        self.assertTrue(all(c.kwargs['data'] == b'synthetic archive' for c in env._stream.await_args_list))

    async def test_download_retries_and_discards_failed_attempt(self):
        env = self.prepared()
        env._stream.side_effect = [ExecStreamClosed(env.pod_name, 'Running', []), (b'complete archive', b'', 0)]
        with patch('anyeval_k8s.asyncio.sleep', new=AsyncMock()):
            self.assertEqual(await env._download(['tar']), b'complete archive')
        self.assertEqual(env._stream.await_count, 2)

    async def test_retry_exhaustion(self):
        env = self.prepared()
        env._stream.side_effect = ExecStreamClosed(env.pod_name, 'Running', [])
        with patch('anyeval_k8s.asyncio.sleep', new=AsyncMock()):
            with self.assertRaises(ExecStreamClosed):
                await env._download(['tar'])
        self.assertEqual(env._stream.await_count, 4)

    async def test_no_retry_if_pod_failed_or_unknown(self):
        for phase in ['Failed', 'Unknown (ApiException)']:
            env = self.prepared()
            env._pod_phase.return_value = phase
            env._stream.side_effect = ExecStreamClosed(env.pod_name, 'Running', [])
            with patch('anyeval_k8s.asyncio.sleep', new=AsyncMock()):
                with self.assertRaises(ExecStreamClosed) as caught:
                    await env._upload(b'archive', '/tmp')
            self.assertEqual(caught.exception.pod_phase, phase)
            self.assertEqual(caught.exception.last_events, [{'reason': 'SampleEvent'}])
            self.assertEqual(env._stream.await_count, 1)

    async def test_arbitrary_exec_never_replayed(self):
        env = self.prepared()
        failure = ExecStreamClosed(env.pod_name, 'Running', [{'reason': 'SampleEvent'}])
        env._stream.side_effect = failure
        with self.assertRaises(ExecStreamClosed) as caught:
            await env.exec('synthetic-non-idempotent-command')
        self.assertIs(caught.exception, failure)
        env._stream.assert_awaited_once()
        env._pod_phase.assert_not_awaited()

    async def test_nonzero_transfer_does_not_retry(self):
        env = self.prepared()
        env._stream.return_value = (b'', b'', 2)
        with self.assertRaisesRegex(RuntimeError, 'exit 2'):
            await env._download(['tar'])
        env._stream.assert_awaited_once()

    async def test_filtered_transfer_is_retryable(self):
        env = self.prepared()
        with patch('harbor.environments.base.BaseEnvironment.download_dir_filtered', new=AsyncMock(
                side_effect=[ExecStreamClosed(env.pod_name, 'Running', []), None])) as download:
            with patch('anyeval_k8s.asyncio.sleep', new=AsyncMock()):
                await env.download_dir_filtered(source_dir='/logs', target_dir=self.root/'out')
        self.assertEqual(download.await_count, 2)

    async def test_status_parse_error_has_phase_events_and_private_client(self):
        import sys
        class Response:
            sock = None
            def is_open(self): return False
            def read_stdout(self, timeout): return b""
            def read_stderr(self, timeout): return b""
            @property
            def returncode(self): raise TypeError("missing status channel")
            def close(self): pass
        env = self.env()
        env._client = MagicMock()
        env._pod_phase = AsyncMock(return_value="Running")
        env._events = AsyncMock(return_value=[{"reason": "Synthetic"}])
        client = MagicMock()
        with patch.dict(sys.modules, {"kubernetes": NS(client=client),
                "kubernetes.stream": NS(stream=MagicMock(return_value=Response()))}):
            with self.assertRaises(ExecStreamClosed) as caught:
                await env.exec("synthetic-command")
        self.assertEqual(caught.exception.pod_phase, "Running")
        self.assertEqual(caught.exception.last_events, [{"reason": "Synthetic"}])
        client.ApiClient.assert_called_once_with(env._client.configuration)
        client.ApiClient.return_value.close.assert_called_once()

    async def test_ping_optional_and_sent_when_supported(self):
        import sys
        import itertools
        for supports_ping in (True, False):
            class Response:
                def __init__(self):
                    self.remaining = 3
                    self.sock = NS(ping=MagicMock()) if supports_ping else NS()
                    self.returncode = 0
                def is_open(self): return self.remaining > 0
                def update(self, timeout): self.remaining -= 1
                def read_stdout(self, timeout): return b""
                def read_stderr(self, timeout): return b""
                def close(self): pass
            response = Response()
            env = self.env()
            env._client = MagicMock()
            # Patch only this module's time binding, not asyncio's clock.
            with patch.dict(sys.modules, {"kubernetes": NS(client=MagicMock()),
                    "kubernetes.stream": NS(stream=MagicMock(return_value=response))}):
                with patch('anyeval_k8s.time', NS(monotonic=lambda: next(ticks))):
                    ticks = itertools.count(0, 21)
                    await env._stream(["true"])
            if supports_ping:
                self.assertGreater(response.sock.ping.call_count, 0)


class VerifierTests(unittest.IsolatedAsyncioTestCase):
    setUp = adapter_tests.ContractTests.setUp
    env = adapter_tests.ContractTests.env

    def prepared(self, separate=False):
        env = self.env()
        env._pod_phase = AsyncMock(return_value='Running')
        env._events = AsyncMock(return_value=[])
        env._upload = AsyncMock()
        env._stream = AsyncMock(return_value=(b'', b'', 0))
        source = self.root / 'synthetic-tests'
        source.mkdir()
        (source / 'test.sh').write_text('synthetic fixture')
        verifier = Verifier.__new__(Verifier)
        verifier.environment = env
        verifier.trial_paths = env.trial_paths
        verifier.trial_paths.verifier_dir.mkdir(parents=True)
        verifier._skip_tests_upload = separate
        verifier._resolve_tests = lambda: ([] if separate else [source], source, source/'test.sh')
        verifier.task = NS(config=NS(verifier=NS(env={})))
        verifier.verifier_env = None
        verifier.override_env = {}
        verifier.include_logs = []
        verifier.exclude_logs = []
        verifier.logger = MagicMock()
        async def download(*args, **kwargs):
            verifier.trial_paths.reward_text_path.write_text('1')
        env.download_dir = AsyncMock(side_effect=download)
        return env, verifier, download

    async def test_preflight_after_upload_before_verifier(self):
        env, verifier, _ = self.prepared()
        result = await verifier.verify()
        self.assertEqual(result.rewards, {'reward': 1.0})
        env._upload.assert_awaited_once()
        self.assertEqual(env._stream.await_count, 3)  # presence probe, chmod, verification
        self.assertIn('test -d /tests', env._stream.await_args_list[0].args[0][-1])
        facts = json.loads(next((env.trial_paths.trial_dir/'anyeval').glob('*.json')).read_text())
        self.assertEqual(facts['verifier_preflight']['tests_source'], 'uploaded')
        self.assertIsNone(env._verifier_guard)

    async def test_separate_verifier_checks_image_owned_tests(self):
        env, verifier, _ = self.prepared(separate=True)
        await verifier.verify()
        env._upload.assert_not_awaited()
        self.assertEqual(env._stream.await_count, 3)

    async def test_incomplete_upload_stops_verification(self):
        env, verifier, _ = self.prepared()
        env.upload_dir = AsyncMock()  # does not record successful transfer
        with self.assertRaisesRegex(VerifierPreflightError, 'upload did not complete'):
            await verifier.verify()
        env._stream.assert_not_awaited()

    async def test_missing_tests_stops_verification(self):
        env, verifier, _ = self.prepared(separate=True)
        env._stream.return_value = (b'', b'', 1)
        with self.assertRaisesRegex(VerifierPreflightError, 'missing'):
            await verifier.verify()
        env._stream.assert_awaited_once()

    async def test_failed_pod_stops_before_upload(self):
        env, verifier, _ = self.prepared()
        env._pod_phase.return_value = 'Failed'
        with self.assertRaises(VerifierPreflightError):
            await verifier.verify()
        env._upload.assert_not_awaited()
        env._stream.assert_not_awaited()

    async def test_download_failure_retries_once_without_reverification(self):
        env, verifier, download = self.prepared()
        async def flaky(*args, **kwargs):
            if env.download_dir.await_count == 1:
                raise RuntimeError('synthetic transfer failure')
            await download()
        env.download_dir.side_effect = flaky
        with patch('anyeval_verifier.asyncio.sleep', new=AsyncMock()):
            result = await verifier.verify()
        self.assertEqual(result.rewards, {'reward': 1.0})
        self.assertEqual(env.download_dir.await_count, 2)
        self.assertEqual(env._stream.await_count, 3)
        env._upload.assert_awaited_once()

    async def test_filtered_recovery_preserves_rewards_and_runs_once(self):
        env, verifier, download = self.prepared(separate=True)
        verifier.include_logs = ["*.txt"]
        async def flaky(**kwargs):
            if env.download_dir_filtered.await_count == 1:
                raise RuntimeError("synthetic transfer failure")
            await download()
        env.download_dir_filtered = AsyncMock(side_effect=flaky)
        with patch('anyeval_verifier.asyncio.sleep', new=AsyncMock()):
            result = await verifier.verify()
        self.assertEqual(result.rewards, {"reward": 1.0})
        self.assertEqual(env.download_dir_filtered.await_count, 2)
        self.assertIn(verifier.trial_paths.reward_text_path.name,
                      env.download_dir_filtered.await_args.kwargs["protect"])
        self.assertEqual(env._stream.await_count, 3)

    async def test_download_failure_exhaustion(self):
        env, verifier, _ = self.prepared()
        env.download_dir.side_effect = RuntimeError('synthetic transfer failure')
        with patch('anyeval_verifier.asyncio.sleep', new=AsyncMock()):
            with self.assertRaises(DownloadVerifierDirError):
                await verifier.verify()
        self.assertEqual(env.download_dir.await_count, 2)
        self.assertEqual(env._stream.await_count, 3)


class MetadataTests(unittest.TestCase):
    def test_config_determinism_and_version_rollout(self):
        source = load_allowlist()
        a, patch_a = render(source, '34.118.229.224', ['10.26.0.0/17', '34.118.224.0/20'])
        b, _ = render(source, '34.118.229.224', ['10.26.0.0/17', '34.118.224.0/20'])
        self.assertEqual(a, b)
        self.assertTrue(a['immutable'])
        self.assertEqual(patch_a['spec']['template']['spec']['volumes'][0]['configMap']['name'], a['metadata']['name'])
        source = copy.deepcopy(source)
        source['version'] = 'next'
        c, _ = render(source, '34.118.229.224', ['10.26.0.0/17', '34.118.224.0/20'])
        self.assertNotEqual(a['metadata']['name'], c['metadata']['name'])
        self.assertEqual(c['metadata']['annotations']['anyeval.io/allowlist-sha256'], source_hash(source))

    def test_eligibility_both_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = '[environment]\ndocker_image="a"\nstorage_mb=10240\ngpus=0\n[verifier.environment]\ndocker_image="b"\nstorage_mb=10240\n'
            variants = {'good': good, 'gpu': good+'gpu_types=["T4"]\n',
                        'storage': good.replace('storage_mb=10240', 'storage_mb=10241'),
                        'image': good.replace('docker_image="b"', ''), 'compose': good}
            for name, metadata in variants.items():
                task = root/name
                task.mkdir()
                (task/'task.toml').write_text(metadata)
                if name == 'compose':
                    (task/'docker-compose.yaml').touch()
            report = eligibility(root)
            self.assertEqual(report['eligible'], ['good'])
            self.assertEqual(len(report['excluded']), 4)

    def test_result_precedence(self):
        self.assertEqual(classify({'verifier_result': {'rewards': {'reward': 0}},
                                   'exception_info': {'exception_type': 'AgentTimeoutError'}})[0], 'fail')
        self.assertEqual(classify({'exception_info': {'exception_type': 'AgentTimeoutError'}})[0], 'timeout')
        self.assertEqual(classify({'exception_info': {'exception_type': 'ExecStreamClosed'}})[0], 'infra')
        self.assertEqual(classify({})[0], 'infra')

    def test_latest_task_and_all_job_costs(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = []
            for i in range(2):
                job = Path(tmp)/str(i)
                trial = job/'sample__trial'
                trial.mkdir(parents=True)
                (trial/'config.json').write_text('{}')
                (trial/'result.json').write_text(json.dumps({'task_name': 'sample', 'finished_at': str(i),
                    'verifier_result': {'rewards': {'reward': i}}, 'agent_result': {'cost_usd': 1}}))
                (job/'result.json').write_text(json.dumps({'stats': {'cost_usd': 2}}))
                jobs.append(job)
            report = summarize(jobs)
            self.assertEqual(report['counts'], {'pass': 1, 'fail': 0, 'timeout': 0, 'infra': 0})
            self.assertEqual(report['cost_usd'], 4)
            self.assertEqual(report['selected_task_cost_usd'], 1)


if __name__ == '__main__':
    unittest.main()
