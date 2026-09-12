"""Regression coverage for review B5; fixtures contain synthetic data only."""
import asyncio
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import runpy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes import client
from terminal_bench_anyeval import k8s_env as k, trial
from terminal_bench_anyeval.bounded_io import TransferLimitError, inventory
from test_app_contract import synthetic_environment, archive_bytes


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv('ANYEVAL_TB_EGRESS_PROXY', raising=False)
    value = synthetic_environment(tmp_path/'trial', 'agent', {}, monkeypatch)
    yield value
    k.ACTIVE_ENVIRONMENTS.discard(value)


def test_b5_01_baseline_deny_policy_is_recorded(env):
    async def run():
        await env.start()
        pod = env._core.read_namespaced_pod(env.pod_name, env.namespace)
        policy = client.V1NetworkPolicy(metadata=client.V1ObjectMeta(name='deny-all-egress', uid='baseline', resource_version='9'),
            spec=client.V1NetworkPolicySpec(pod_selector=client.V1LabelSelector(), policy_types=['Egress'], egress=[]))
        env._network.list_namespaced_network_policy(env.namespace).items.append(policy)
        observed = await env._policy_facts(pod)
        assert observed['additional_network_policies'][0]['effect'] == 'adds no allows'
        assert observed['additional_network_policies'][0]['resource_version'] == '9'
        for direction in ('ingress', 'egress'):
            setattr(policy.spec, direction, [{}])
            with pytest.raises(k.AnyEvalInfrastructureError, match='adds allows'):
                await env._policy_facts(pod)
            setattr(policy.spec, direction, [])
    asyncio.run(run())


def test_b5_02_failed_delete_retains_policy(env, monkeypatch):
    async def run():
        env._pod_attempted = env._policy_attempted = True
        env._final_captured = True
        env._core.delete_namespaced_pod.side_effect = PermissionError('refused')
        monkeypatch.setattr(k.asyncio, 'sleep', AsyncMock())
        with pytest.raises(k.AnyEvalInfrastructureError):
            await env.stop()
        env._network.delete_namespaced_network_policy.assert_not_called()
        assert env._policy_attempted
    asyncio.run(run())


def test_b5_02_delete_waits_for_404(env):
    async def run():
        env._pod_attempted = env._policy_attempted = True
        env._final_captured = True
        events = []
        env._core.delete_namespaced_pod.side_effect = lambda *a, **kw: events.append('delete-pod')
        def read(*a, **kw):
            events.append('confirmed-404')
            raise client.ApiException(status=404)
        env._core.read_namespaced_pod.side_effect = read
        env._network.delete_namespaced_network_policy.side_effect = lambda *a, **kw: events.append('delete-policy')
        await env.stop()
        assert events == ['delete-pod', 'confirmed-404', 'delete-policy']
    asyncio.run(run())


@pytest.mark.parametrize('code', [124, 125, 126, 127, 128, 129, 130, 137, 143, -9])
def test_b5_03_launch_failure_is_infrastructure(env, code):
    async def run():
        env._verifier_guard = {'checked': True, 'command': 'synthetic-verifier', 'execution_completed': False}
        env._stream = AsyncMock(return_value=(b'', b'', code))
        with pytest.raises(k.AnyEvalInfrastructureError):
            await env.exec('synthetic-verifier')
        assert env._verifier_guard['checked'] is False
        assert env._verifier_guard['execution_completed'] is False
    asyncio.run(run())


def test_b5_03_preflight_checks_executable(env):
    async def run():
        env._require_running = AsyncMock()
        env._verifier_guard = {'checked': False, 'pending_uploads': set(), 'script': '/tests/entry.sh', 'source': 'prebuilt-image'}
        async def stream(command, **kwargs):
            return b'', b'', 1 if 'test -x' in command[-1] else 0
        env._transfer_stream = stream
        with pytest.raises(k.VerifierPreflightError):
            await env._check_verifier_guard()
        assert env._verifier_guard['checked'] is False
    asyncio.run(run())


@pytest.mark.parametrize('mutation', ['host_network','host_pid','host_ipc','privileged','escalation','token','hostpath','caps','runtime','labels'])
def test_b5_04_admission_security_is_checked(env, mutation):
    async def run():
        await env.start()
        pod = env._core.read_namespaced_pod(env.pod_name, env.namespace)
        env._core.read_namespaced_pod.side_effect = None
        env._core.read_namespaced_pod.return_value = pod
        if mutation.startswith('host_'): setattr(pod.spec, mutation, True)
        elif mutation == 'privileged': pod.spec.containers[0].security_context.privileged = True
        elif mutation == 'escalation': pod.spec.containers[0].security_context.allow_privilege_escalation = True
        elif mutation == 'token': pod.spec.automount_service_account_token = True
        elif mutation == 'hostpath': pod.spec.volumes = [client.V1Volume(name='host',host_path=client.V1HostPathVolumeSource(path='/'))]
        elif mutation == 'caps': pod.spec.containers[0].security_context.capabilities.add = ['SYS_ADMIN']
        elif mutation == 'runtime': pod.spec.runtime_class_name = 'runc'
        elif mutation == 'labels': pod.metadata.labels = {}
        with pytest.raises((k.AnyEvalInfrastructureError, RuntimeError)):
            await env._wait_running()
    asyncio.run(run())


@pytest.mark.parametrize('mutation', ['missing','args','mount','image','config'])
def test_b5_05_proxy_serving_container_binding(tmp_path, monkeypatch, mutation):
    monkeypatch.setenv('ANYEVAL_TB_EGRESS_PROXY','1')
    env = synthetic_environment(tmp_path/'trial','agent',{},monkeypatch)
    pod = env._core.list_namespaced_pod.return_value.items[0]
    if mutation == 'missing': pod.spec = NS(volumes=pod.spec.volumes)
    elif mutation == 'args': pod.spec.containers[0].args = ['-config','/tmp/unapproved.yaml']
    elif mutation == 'mount': pod.spec.containers[0].volume_mounts[0].mount_path = '/tmp/unused'
    elif mutation == 'image': pod.spec.containers[0].image = 'unapproved:latest'
    elif mutation == 'config': pod.spec.containers[0].volume_mounts[0].sub_path = 'other.yaml'
    try:
        with pytest.raises(k.AnyEvalInfrastructureError):
            asyncio.run(env._prepare_proxy())
    finally:
        k.ACTIVE_ENVIRONMENTS.discard(env)


def test_b5_06_remote_output_is_bounded(env, monkeypatch):
    import kubernetes.stream
    response = MagicMock()
    response.is_open.side_effect = [True, False]
    response.read_stdout.side_effect = [b'x'*17, b'']
    response.read_stderr.return_value = b''
    response.returncode = 0
    monkeypatch.setattr(kubernetes.stream, 'stream', lambda *a, **kw: response)
    env.max_output_bytes = 16
    with pytest.raises(TransferLimitError):
        asyncio.run(k.AnyEvalK8sEnvironment._stream(env, ['true']))
    response.close.assert_called_once()
    assert trial.classify('TransferLimitError') == 'infrastructure'


def test_b5_06_upload_is_disk_backed_and_bounded(env, tmp_path):
    source = tmp_path/'file';source.write_bytes(b'x'*100)
    async def upload(data, target):
        assert hasattr(data, 'read') and not isinstance(data, io.BytesIO)
    env._upload = upload
    asyncio.run(env.upload_file(source,'/file'))
    env.max_transfer_bytes = 32
    with pytest.raises(TransferLimitError):
        asyncio.run(env.upload_file(source,'/file'))


def test_b5_06_member_limit(env, tmp_path):
    source = tmp_path/'files';source.mkdir()
    for i in range(3): (source/str(i)).write_bytes(b'x')
    env.max_archive_members = 2
    with pytest.raises(TransferLimitError):
        asyncio.run(env.upload_dir(source,'/files'))


def test_b5_07_unpinned_tmux_refused(env, tmp_path, monkeypatch):
    binary = tmp_path/'tmux';binary.write_bytes(b'unapproved executable')
    monkeypatch.setenv('ANYEVAL_TB_TMUX_STATIC',str(binary))
    env.exec = AsyncMock(side_effect=[NS(return_code=1,stdout='',stderr=''), NS(return_code=0,stdout='synthetic',stderr='')])
    env.upload_file = AsyncMock()
    with pytest.raises(k.AnyEvalInfrastructureError, match='SHA256'):
        asyncio.run(k.AnyEvalK8sEnvironment._provision_tmux(env))
    env.upload_file.assert_not_called()


def test_b5_07_tmux_upload_uses_hashed_bytes(env, tmp_path, monkeypatch):
    binary = tmp_path/'tmux';original = b'approved synthetic';binary.write_bytes(original)
    monkeypatch.setenv('ANYEVAL_TB_TMUX_STATIC',str(binary))
    monkeypatch.setattr(k,'TMUX_SHA256',hashlib.sha256(original).hexdigest())
    async def command(cmd, **kwargs):
        binary.write_bytes(b'changed after hashing')
        return NS(return_code=1 if cmd.startswith('command') else 0,stdout='',stderr='')
    env.exec = command
    async def upload(source, target): assert Path(source).read_bytes() == original
    env.upload_file = upload
    asyncio.run(k.AnyEvalK8sEnvironment._provision_tmux(env))


def test_b5_08_manifest_uses_approved_digest(env):
    pins = json.loads((Path(k.__file__).parent/'data/image-digests.json').read_text())
    assert len(pins) == 89
    reference, digest = next(iter(pins.items()))
    env.task_env_config.docker_image = reference
    assert env._manifests()[0]['spec']['containers'][0]['image'] == reference+'@'+digest
    env.task_env_config.docker_image = 'unknown/unpinned:latest'
    with pytest.raises(k.AnyEvalInfrastructureError): env._manifests()


def test_b5_08_observed_digest_mismatch(env):
    async def run():
        await env.start()
        pod = env._core.read_namespaced_pod(env.pod_name,env.namespace)
        pod.status.container_statuses[0].image_id = 'synthetic@sha256:'+'f'*64
        env._core.read_namespaced_pod.side_effect = None
        env._core.read_namespaced_pod.return_value = pod
        with pytest.raises(k.AnyEvalInfrastructureError, match='digest'):
            await env._wait_running()
    asyncio.run(run())


@pytest.mark.parametrize('constant',['NaN','Infinity','-Infinity','1e9999'])
def test_b5_09_nonfinite_spec_writes_error(tmp_path, constant):
    spec = tmp_path/'spec.json';result = tmp_path/'result.json'
    spec.write_text('{"version":1,"attempt":'+constant+'}')
    assert trial.main(['--spec',str(spec),'--result',str(result)]) == 0
    data = json.loads(result.read_text())
    assert data['outcome'] == 'error' and data['attempt'] is None
    assert 'Non-finite JSON' in data['exception']['message']


@pytest.mark.parametrize('value',[[],{},True,1.5,float('nan')])
def test_b5_09_invalid_bindings_never_echo(tmp_path, value):
    result = trial.empty_result()
    trial.echo_bindings(result,dict(run_id=value,task=value,trial_id=value,attempt=value))
    assert all(result[key] is None for key in ['run_id','sample_id','trial_id','attempt'])
    result['agent']['input_tokens'] = float('inf')
    trial.atomic_write(tmp_path/'result.json',result)
    assert json.loads((tmp_path/'result.json').read_text())['agent']['input_tokens'] is None


def test_b5_10_companion_unset_is_optional(monkeypatch):
    monkeypatch.delenv('ANYEVAL_APP_CHECKOUT',raising=False)
    module = runpy.run_path(str(Path(__file__).with_name('test_app_contract.py')))
    assert module['APP'] is None
    with pytest.raises(pytest.skip.Exception, match='ANYEVAL_APP_CHECKOUT'):
        module['test_app_contract_literal_bytes'](None,None,monkeypatch)


def test_b5_02_terminal_phase_observes_grace(env, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(k.time, 'monotonic', lambda: clock[0])
    async def sleep(seconds): clock[0] += seconds
    monkeypatch.setattr(k.asyncio, 'sleep', sleep)
    env._core.read_namespaced_pod.side_effect = None
    env._core.read_namespaced_pod.return_value = NS(status=NS(phase='Failed'),spec=NS(termination_grace_period_seconds=3))
    asyncio.run(env._wait_terminated())
    assert clock[0] >= 13


def test_b5_02_unconfirmed_termination_keeps_policy(env, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(k.time, 'monotonic', lambda: clock[0])
    async def sleep(seconds): clock[0] += seconds
    monkeypatch.setattr(k.asyncio, 'sleep', sleep)
    env.cleanup_timeout_sec = 2
    env._pod_attempted = env._policy_attempted = env._final_captured = True
    env._core.delete_namespaced_pod.side_effect = None
    env._core.read_namespaced_pod.side_effect = None
    env._core.read_namespaced_pod.return_value = NS(status=NS(phase='Running'))
    with pytest.raises(k.AnyEvalInfrastructureError, match='policy retained'):
        asyncio.run(env.stop())
    env._network.delete_namespaced_network_policy.assert_not_called()


def test_b5_06_download_limits_and_filtered_extraction(env, tmp_path):
    import tarfile
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w') as archive:
        for name in ['keep.txt','drop.txt','reward.txt']:
            member = tarfile.TarInfo(name);member.size=4
            archive.addfile(member,io.BytesIO(b'data'))
    data = buffer.getvalue()
    env._download = AsyncMock(side_effect=lambda *args: data)
    asyncio.run(env.download_dir_filtered(source_dir='/remote',target_dir=tmp_path/'out',include=['keep.txt'],protect=['reward.txt']))
    assert {p.name for p in (tmp_path/'out').iterdir()} == {'keep.txt','reward.txt'}
    env.max_archive_members = 2
    with pytest.raises(TransferLimitError):
        asyncio.run(env.download_dir_filtered(source_dir='/remote',target_dir=tmp_path/'limited'))
    env.max_archive_members = 20
    env.max_transfer_bytes = len(data)-1
    with pytest.raises(TransferLimitError):
        asyncio.run(env.download_dir('/remote',tmp_path/'limited'))


def test_b5_06_stream_hashes_without_unbounded_reads(env):
    import tarfile
    original = tarfile.ExFileObject.read
    def checked(self, size=-1):
        assert 0 < size <= 65536
        return original(self,size)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(tarfile.ExFileObject,'read',checked)
        assert inventory(archive_bytes(b'x'*100000),env.max_transfer_bytes,env.max_archive_members)


def test_b5_08_all_dataset_images_are_pinned():
    import tomllib
    from terminal_bench_anyeval.eligibility import dataset_root, eligibility
    pins = json.loads((Path(k.__file__).parent/'data/image-digests.json').read_text())
    for dataset in ['terminal-bench-2-1','terminal-bench@4.0.0']:
        for task_id in eligibility(dataset)['included']:
            config = tomllib.loads((dataset_root(dataset)/task_id/'task.toml').read_text())
            environments = [config['environment']]
            if config.get('verifier',{}).get('environment_mode') == 'separate':
                environments.append(config['verifier']['environment'])
            for environment in environments:
                reference = environment['docker_image']
                if dataset == 'terminal-bench-2-1': assert reference in pins
                else: assert k.re.fullmatch(r'.+@sha256:[0-9a-f]{64}',reference)


def test_b5_03_preflight_rejects_directory_entrypoint(env):
    async def run():
        env._require_running = AsyncMock()
        env._verifier_guard = {'checked': False, 'pending_uploads': set(), 'script': '/tests/directory', 'source': 'prebuilt-image'}
        async def stream(command, **kwargs):
            return b'', b'', 1 if 'test -f' in command[-1] else 0
        env._transfer_stream = stream
        with pytest.raises(k.VerifierPreflightError):
            await env._check_verifier_guard()
    asyncio.run(run())
