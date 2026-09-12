from terminal_bench_anyeval.k8s_env import AnyEvalInfrastructureError
from b5_fixtures import upgrade_synthetic_environment
from terminal_bench_anyeval.bounded_io import as_file
import os
"""One cross-repo boundary test: real child bytes -> real app parser/validator.

Only synthetic task and Kubernetes objects are used. The companion checkout is
read-only. No substitute consumer validator or producer-derived expected hashes.
"""
import asyncio
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from kubernetes import client
from harbor.models.task.config import EnvironmentConfig, NetworkPolicy
from harbor.models.trial.paths import TrialPaths
from harbor.verifier.verifier import Verifier
from terminal_bench_anyeval import trial
from terminal_bench_anyeval.k8s_env import AnyEvalK8sEnvironment, ACTIVE_ENVIRONMENTS
from terminal_bench_anyeval.iron_proxy import make_config, load_allowlist, source_hash

APP = Path(os.environ["ANYEVAL_APP_CHECKOUT"]) if os.environ.get("ANYEVAL_APP_CHECKOUT") else None
PAYLOAD = b'contract artifact\x00\xff\n'
# Literal independent known bytes enter the transfer in both directions.
TRAJECTORY = b'{"messages": ["synthetic transcript"]}\n'
STDOUT = b'synthetic verifier completed\n'


def archive_bytes(payload=PAYLOAD):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        entry = tarfile.TarInfo('artifact.bin')
        entry.size = len(payload)
        archive.addfile(entry, io.BytesIO(payload))
    return output.getvalue()


def synthetic_environment(root, role, binding, monkeypatch):
    env = AnyEvalK8sEnvironment(
        environment_dir=root / 'absent-prebuilt-context', environment_name='synthetic',
        session_id=root.name + ('__env' if role == 'agent' else '__verifier'),
        trial_paths=TrialPaths(root), binding=binding,
        task_env_config=EnvironmentConfig(docker_image='example/synthetic:1@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', cpus=1,
            memory_mb=2048, storage_mb=10240, gpus=0),
        network_policy=NetworkPolicy(network_mode='no-network'))
    env._client = client.ApiClient()
    env._core, env._network, env._node_api = MagicMock(), MagicMock(), MagicMock()
    env._node_api.read_runtime_class.return_value = NS(handler='runsc', metadata=NS(uid='runtime-uid'))
    env._core.read_node.return_value = NS(status=NS(node_info=NS(kubelet_version='v1.34.1-gke.1')),
        metadata=NS(labels={'sandbox.gke.io/runtime': 'gvisor'}))
    env._events = AsyncMock(return_value=[])
    env._provision_tmux = AsyncMock()
    # Keep HTTP/config validation real; only the API transport is synthetic.
    source = load_allowlist()
    config = make_config('10.0.0.2', ['10.0.0.0/8'])
    raw = yaml.safe_dump(config)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    source_digest = source_hash(source)
    revision = hashlib.sha256((digest + source_digest).encode()).hexdigest()
    cm_name = 'iron-proxy-' + revision[:8]
    annotations = {'anyeval.io/config-sha256': digest, 'anyeval.io/allowlist-version': source['version'],
                   'anyeval.io/allowlist-sha256': source_digest, 'anyeval.io/cluster-cidrs': '["10.0.0.0/8"]'}
    volume = NS(name='config', config_map=NS(name=cm_name))
    proxy = NS(metadata=NS(name='iron', uid='proxy-uid', annotations=annotations,
                         labels={'anyeval.io/role': 'egress-proxy'}),
               spec=NS(volumes=[volume]), status=NS(phase='Running',
                   container_statuses=[NS(ready=True, image_id='example/proxy@sha256:'+'b'*64)],
                   conditions=[NS(type='Ready', status='True')]))
    env._core.read_namespaced_service.return_value = NS(metadata=NS(uid='service-uid'),
        spec=NS(cluster_ip='10.0.0.2', selector={'anyeval.io/role': 'egress-proxy'}))
    env._core.read_namespaced_config_map.return_value = NS(metadata=NS(uid='config-uid', annotations=annotations),
        data={'proxy.yaml': raw, 'allowlist.yaml': yaml.safe_dump(source)}, immutable=True)
    env._core.list_namespaced_pod.return_value = NS(items=[proxy])
    env._core.read_namespaced_endpoints.return_value = NS(subsets=[NS(addresses=[NS(target_ref=NS(kind='Pod', uid='proxy-uid'))])])
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = NS(spec=NS(template=NS(spec=NS(volumes=[volume]), metadata=NS(annotations=annotations))))
    monkeypatch.setattr(client, 'AppsV1Api', lambda _: apps)
    policies = [client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(name='proxy-isolation', uid='proxy-policy-uid', resource_version='11'),
        spec=client.V1NetworkPolicySpec(pod_selector=client.V1LabelSelector(match_labels={'anyeval.io/role': 'egress-proxy'}),
                                        policy_types=['Ingress', 'Egress'], ingress=[], egress=[]))]
    env._network.list_namespaced_network_policy.side_effect = lambda *a, **kw: NS(items=policies)
    def create_policy(namespace, body, **kwargs):
        wire = deepcopy(body)
        wire['metadata'].update(uid=role+'-policy-uid', resourceVersion='7')
        policy = env._client._ApiClient__deserialize(wire, 'V1NetworkPolicy')
        policies.append(policy)
        env._network.read_namespaced_network_policy.return_value = policy
    env._network.create_namespaced_network_policy.side_effect = create_policy
    def create_pod(namespace, body, **kwargs):
        wire = deepcopy(body)
        wire['metadata'].update(uid=role+'-uid', resourceVersion='3')
        wire['spec']['nodeName'] = 'node-1'
        wire['status'] = {'phase': 'Running', 'podIP': '10.2.0.1', 'containerStatuses': [
            {'name': 'main', 'image': 'example/synthetic:1@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'imageID': 'example/synthetic@sha256:'+'a'*64,
             'containerID': 'containerd://'+role, 'ready': True, 'restartCount': 0, 'state': {'running': {}}}]}
        env._core.read_namespaced_pod.return_value = env._client._ApiClient__deserialize(wire, 'V1Pod')
    env._core.create_namespaced_pod.side_effect = create_pod
    async def stream(command, **kwargs):
        text = ' '.join(command)
        if 'uname -r' in text:
            return b'4.4.0\n', b'', 0
        if 'dmesg' in text:
            return b'[    0.000000] Starting gVisor...\n', b'', 0
        if kwargs.get('data') is not None:
            with tarfile.open(fileobj=as_file(kwargs['data'])) as archive:
                assert archive.extractfile(next(m for m in archive.getmembers() if Path(m.name).name == 'artifact.bin')).read() == b'contract artifact\x00\xff\n'
        return b'', b'', 0
    env._stream = stream
    upgrade_synthetic_environment(env, monkeypatch)
    return env


def test_app_contract_literal_bytes(tmp_path, spec, monkeypatch):
    if APP is None:
        pytest.skip("ANYEVAL_APP_CHECKOUT is unset; companion app contract gate skipped")
    assert (APP / 'app/harbor_trial.py').is_file(), 'Companion checkout is required for the contract gate'
    monkeypatch.syspath_prepend(str(APP))
    from app.harbor_trial import run_trial
    from app.sandbox_provenance import validate_harbor_pods, SandboxProvenanceError
    from harbor.trial.trial import Trial
    from harbor.trial.artifact_handler import ArtifactHandler

    from terminal_bench_anyeval.eligibility import task_directory
    spec = dict(spec, dataset='terminal-bench@4.0.0',
                task_dir=str(task_directory('terminal-bench@4.0.0', spec['task'])))
    binding = {'run_id': spec['run_id'], 'sample_id': spec['task'], 'attempt': 1, 'trial_id': 'parent-authorized'}
    observed = {}
    class SyntheticTrial:
        id = 'internal-harbor-id'
        @classmethod
        async def create(cls, config):
            obj = cls()
            obj.config = config
            return obj
        async def run(self):
            root = self.config.trials_dir / self.config.trial_name
            root.mkdir(parents=True)
            (root / 'agent').mkdir()
            (root / 'verifier').mkdir()
            (root / 'agent/trajectory.json').write_bytes(b'{"messages": ["synthetic transcript"]}\n')
            (root / 'verifier/test-stdout.txt').write_bytes(b'synthetic verifier completed\n')
            (root / 'verifier/reward.txt').write_bytes(b'1\n')
            environments = []
            for role in ('agent', 'verifier'):
                env = synthetic_environment(root, role, self.config.environment.kwargs['binding'], monkeypatch)
                await env.start()
                environments.append(env)
            agent, verifier_env = environments
            # Exercise the installed artifact hook, plus real upload/read-back hashing.
            source = root / 'transferred'
            source.mkdir()
            (source / 'artifact.bin').write_bytes(b'contract artifact\x00\xff\n')
            verifier_env._download = AsyncMock(return_value=archive_bytes(b'contract artifact\x00\xff\n'))
            handler = object.__new__(ArtifactHandler)
            handler._normalized_artifacts = lambda *a: ['synthetic-entry']
            handler._host_path = lambda *a: source
            handler._upload_target_source = lambda *a, **kw: '/artifacts'
            await handler.upload_artifacts(verifier_env, source,
                source_artifacts_dir='/artifacts', target_artifacts_dir='/artifacts')
            verifier_env.download_dir = AsyncMock()
            task = NS(paths=NS(tests_dir=root/'tests', test_path_for=lambda os: root/'tests/test.sh'),
                      config=NS(verifier=NS(env={})))
            verified = await Verifier(task, TrialPaths(root), verifier_env, skip_tests_upload=True).verify()
            for env in environments:
                # Final resourceVersion is read from a later API object, before delete.
                env._core.read_namespaced_pod.return_value.metadata.resource_version = '4'
                await env.stop(delete=True)
            observed['environments'] = environments
            return NS(verifier_result=verified, exception_info=None)
    monkeypatch.setattr(Trial, 'create', SyntheticTrial.create)

    class Child:
        pid = 777777
        returncode = 0
        def __init__(self, argv, **kwargs):
            spec_path, result_path = Path(argv[-3]), Path(argv[-1])
            # The real parent writes wire bytes; the real CLI parses those exact bytes.
            wire = spec_path.read_bytes()
            assert b'"trial_id": "parent-authorized"' in wire
            assert b'"api_key": "synthetic-shim-token"' in wire
            trial.main(['--spec', str(spec_path), '--result', str(result_path)])
            observed['result_bytes'] = result_path.read_bytes()
            assert b'"trial_id": "parent-authorized"' in observed['result_bytes']
        def poll(self): return 0
        def wait(self, timeout=None): return 0
    import app.harbor_trial as consumer
    monkeypatch.setattr(consumer.subprocess, 'Popen', Child)
    monkeypatch.setattr(consumer.os, 'killpg', lambda *a: None)
    shim = NS(api_base='http://127.0.0.1:12345/v1', token='synthetic-shim-token', model='gpt-5-mini',
              identity={'trial_id': 'parent-authorized'})
    result = run_trial(spec=spec, root=tmp_path/'child', shim=shim)
    assert result['status'] == 'success'
    assert result['scores'] == {'harbor': {'value': 'C'}}
    assert result['trial_id'] == 'parent-authorized'
    assert result['harbor_trial_id'] == 'internal-harbor-id'
    assert result['verifier_health'] == {'setup_completed': True, 'completed': True}
    assert result['transcript'] == {'trajectory_path': TRAJECTORY.decode(), 'verifier_stdout_path': STDOUT.decode()}
    assert result['artifact_hashes']['trajectory_path']['sha256'] == hashlib.sha256(b'{"messages": ["synthetic transcript"]}\n').hexdigest()
    validate_harbor_pods(result['pods'], separate_verifier=True, binding=binding)
    for pod in result['pods']:
        assert pod['resource_version'] == '3' and pod['finished_resource_version'] == '4'
        assert pod['labels']['anyeval.io/trial-id'] == 'parent-authorized'
        assert pod['runtime_class_exists'] is True
        assert pod['network_policy']['spec']['ingress'] == []
        assert pod['network_policy']['source'] == 'package_policy'
    proxy = result['pods'][0]['proxy']
    assert proxy['network_policies'][0]['source'] == 'proxy_policy'
    assert proxy['network_policies'][0]['finished_resource_version'] == '11'
    assert proxy['active_config_name'] == proxy['finished_config_name']
    assert proxy['config_sha256'] == proxy['finished_proxy_config_sha256']
    transfers = result['verified_artifacts']
    assert_app_transfer_guard(result, binding, monkeypatch)
    assert any(r['source_sha256'] == hashlib.sha256(b'contract artifact\x00\xff\n').hexdigest() for r in transfers)
    # These are real consumer rejections, not a reimplementation of its checks.
    with pytest.raises(SandboxProvenanceError, match='separate verifier'):
        validate_harbor_pods(result['pods'][:1], separate_verifier=True, binding=binding)
    wrong_policy = deepcopy(result['pods'])
    wrong_policy[0]['network_policy']['spec']['egress'][0]['to'] = [{'ipBlock': {'cidr': '0.0.0.0/0'}}]
    with pytest.raises(SandboxProvenanceError, match='same-namespace'):
        validate_harbor_pods(wrong_policy, separate_verifier=True, binding=binding)
    with pytest.raises(SandboxProvenanceError, match='trial_id binding mismatch'):
        validate_harbor_pods(result['pods'], separate_verifier=True, binding=dict(binding, trial_id='other-parent'))
    assert not any(e in ACTIVE_ENVIRONMENTS for e in observed['environments'])


@pytest.mark.parametrize('drift', ['uid', 'restart', 'container', 'labels', 'policy_revision', 'additive_policy', 'proxy_config', 'proxy_endpoint'])
def test_final_capture_rejects_drift_and_still_deletes(tmp_path, monkeypatch, drift):
    from terminal_bench_anyeval.k8s_env import AnyEvalInfrastructureError
    monkeypatch.setenv('ANYEVAL_TB_EGRESS_PROXY', '1')
    binding = {'run_id': 'run', 'sample_id': 'sample', 'attempt': 1, 'trial_id': 'authorized'}
    env = synthetic_environment(tmp_path/'trial', 'agent', binding, monkeypatch)
    async def run():
        await env.start()
        pod = env._core.read_namespaced_pod.return_value
        if drift == 'uid': pod.metadata.uid = 'replacement'
        if drift == 'restart': pod.status.container_statuses[0].restart_count = 1
        if drift == 'container': pod.status.container_statuses[0].container_id = 'containerd://replacement'
        if drift == 'labels': pod.metadata.labels['unexpected'] = 'label'
        if drift == 'policy_revision': env._network.read_namespaced_network_policy.return_value.metadata.resource_version = '99'
        if drift == 'additive_policy':
            policies = env._network.list_namespaced_network_policy().items
            policies.append(client.V1NetworkPolicy(metadata=client.V1ObjectMeta(name='extra', uid='extra'),
                spec=client.V1NetworkPolicySpec(pod_selector=client.V1LabelSelector(), policy_types=['Egress'])))
        if drift == 'proxy_config': env._core.read_namespaced_config_map.return_value.data['proxy.yaml'] += '\n'
        if drift == 'proxy_endpoint': env._core.read_namespaced_endpoints.return_value.subsets[0].addresses[0].target_ref.uid = 'replacement'
        core, network = env._core, env._network
        with pytest.raises(AnyEvalInfrastructureError):
            await env.stop(delete=True)
        core.delete_namespaced_pod.assert_called_once()
        network.delete_namespaced_network_policy.assert_called_once()
        assert env not in ACTIVE_ENVIRONMENTS
        facts = json.loads((env.trial_paths.trial_dir/'anyeval'/f'{env.pod_name}.json').read_text())
        assert facts['final_evidence_error']
        if drift == 'additive_policy': assert 'extra' in facts['finished_selecting_policy_uids']
    asyncio.run(run())


def test_artifact_readback_detects_corruption(tmp_path, monkeypatch):
    from terminal_bench_anyeval.k8s_env import AnyEvalInfrastructureError
    env = synthetic_environment(tmp_path/'trial', 'verifier', {}, monkeypatch)
    async def run():
        await env.start()
        try:
            env._download = AsyncMock(return_value=archive_bytes(b'changed delivery'))
            with pytest.raises(AnyEvalInfrastructureError, match='delivery mismatch'):
                await env._record_artifact_delivery(archive_bytes(), '/artifacts')
            facts = json.loads((env.trial_paths.trial_dir/'anyeval'/f'{env.pod_name}.json').read_text())
            assert facts['verified_artifacts'][0]['source_sha256'] != facts['verified_artifacts'][0]['delivered_sha256']
        finally:
            await env.stop(delete=True)
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['setup_output', 'timeout', 'preflight'])
def test_verifier_health_is_observed_not_inferred_from_reward(tmp_path, monkeypatch, failure):
    from terminal_bench_anyeval.k8s_env import VerifierPreflightError
    root = tmp_path/'trial'
    env = synthetic_environment(root, 'verifier', {}, monkeypatch)
    async def run():
        await env.start()
        try:
            (root/'verifier').mkdir(exist_ok=True)
            (root/'verifier/test-stdout.txt').write_bytes(b'No module named pytest' if failure == 'setup_output' else b'completed')
            (root/'verifier/reward.txt').write_bytes(b'1\n')
            env.download_dir = AsyncMock()
            original = env._stream
            async def stream(command, **kwargs):
                text = ' '.join(command)
                if failure == 'preflight' and 'test -d /tests' in text: return b'', b'', 1
                if failure == 'timeout' and '/tests/test.sh' in text and 'chmod' not in text and 'test -d' not in text:
                    return b'', b'', 124
                return await original(command, **kwargs)
            env._stream = stream
            task = NS(paths=NS(tests_dir=root/'tests', test_path_for=lambda os: root/'tests/test.sh'),
                      config=NS(verifier=NS(env={})))
            verifier = Verifier(task, TrialPaths(root), env, skip_tests_upload=True)
            if failure == 'preflight':
                with pytest.raises(VerifierPreflightError): await verifier.verify()
            else:
                with pytest.raises(AnyEvalInfrastructureError):
                    reward = await verifier.verify()
            facts = json.loads((root/'anyeval'/f'{env.pod_name}.json').read_text())
            assert facts['verifier_health']['completed'] is False
            assert facts['verifier_health']['setup_completed'] is False
        finally:
            await env.stop(delete=True)
    asyncio.run(run())


def assert_app_transfer_guard(result, binding, monkeypatch):
    # Execute the production publication guard directly from its AST. Importing
    # runner also imports unrelated web-server dependencies (httpx2, etc.).
    import ast
    import re
    from app.sandbox_provenance import validate_harbor_pods, SandboxProvenanceError
    from app.harbor_trial import _SETUP_FAILURE as app_signature
    from terminal_bench_anyeval.verifier_health import _SETUP_FAILURE
    assert app_signature.pattern.encode() == _SETUP_FAILURE.pattern.encode()
    assert app_signature.flags == _SETUP_FAILURE.flags
    source = APP / 'app/runner.py'
    tree = ast.parse(source.read_text())
    names = {'_REQUIRED_REPRO', '_REQUIRED_INSPECT_SANDBOX', '_SANDBOX_FIELDS_BY_EXECUTION'}
    nodes = [node for node in tree.body if
             isinstance(node, ast.FunctionDef) and node.name == '_missing_reproducibility_fields'
             or isinstance(node, (ast.Assign, ast.AnnAssign)) and
             any(isinstance(target, ast.Name) and target.id in names
                 for target in (node.targets if isinstance(node, ast.Assign) else [node.target]))]
    scope = {'Any': object, 're': re, 'validate_harbor_pods': validate_harbor_pods,
             'SandboxProvenanceError': SandboxProvenanceError}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), scope)
    guard = scope['_missing_reproducibility_fields']
    sandbox = {'execution': 'harbor', 'pods': result['pods'], 'binding': binding,
               'separate_verifier': True, 'verified_artifacts': result['verified_artifacts']}
    def errors(value):
        return [item for item in guard({'sandbox': value}) if item.startswith('sandbox.pod_validation:')]
    assert not errors(sandbox)
    for field, value in [('delivered_sha256', '0' * 64), ('verifier_pod_uid', 'unrelated-uid')]:
        bad = deepcopy(sandbox)
        bad['verified_artifacts'][0][field] = value
        assert errors(bad)
