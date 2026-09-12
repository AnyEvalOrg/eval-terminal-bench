"""Offline synthetic checks for prebuilt contexts and explicit network protocols."""
import asyncio
import importlib
import json
from unittest.mock import AsyncMock

import pytest
from harbor.environments.base import ExecResult
from harbor.models.task.config import EnvironmentConfig, NetworkPolicy
from harbor.models.trial.paths import TrialPaths

from terminal_bench_anyeval import k8s_env
from terminal_bench_anyeval.trial import collect_pods


@pytest.fixture
def make_environment(tmp_path, monkeypatch):
    monkeypatch.delenv('ANYEVAL_TB_EGRESS_PROXY', raising=False)
    monkeypatch.delenv('ANYEVAL_TB_OFFLINE_PROTOCOL', raising=False)
    environments = []

    def make(module='terminal_bench_anyeval.k8s_env', role='agent', context='environment', **kwargs):
        env = importlib.import_module(module).AnyEvalK8sEnvironment(
            environment_dir=tmp_path / context, environment_name='synthetic',
            session_id='trial__env' if role == 'agent' else 'trial__verifier__grade',
            trial_paths=TrialPaths(tmp_path / 'trial'),
            task_env_config=kwargs.pop('task_env_config', EnvironmentConfig(
                docker_image='example/synthetic:1', cpus=1, memory_mb=2048, storage_mb=10240)),
            network_policy=kwargs.pop('network_policy', NetworkPolicy(network_mode='no-network')),
            **kwargs)
        environments.append(env)
        return env

    yield make
    for env in environments:
        k8s_env.ACTIVE_ENVIRONMENTS.discard(env)


@pytest.mark.parametrize('module', ['terminal_bench_anyeval.k8s_env', 'anyeval_k8s'])
@pytest.mark.parametrize('role', ['agent', 'verifier'])
@pytest.mark.parametrize('declaration', ['public', 'phase-public', 'allow_internet', 'metadata-agent', 'metadata-verifier'])
@pytest.mark.parametrize('protocol', [None, 'ANYEVAL_TB_EGRESS_PROXY', 'ANYEVAL_TB_OFFLINE_PROTOCOL'])
def test_network_declarations_require_explicit_protocol(
        tmp_path, monkeypatch, make_environment, module, role, declaration, protocol):
    if protocol:
        monkeypatch.setenv(protocol, '1')
    kwargs = {}
    if declaration == 'public':
        kwargs['network_policy'] = NetworkPolicy(network_mode='public')
    elif declaration == 'phase-public':
        kwargs['phase_network_policies'] = [NetworkPolicy(network_mode='public')]
    elif declaration == 'allow_internet':
        config = EnvironmentConfig(docker_image='example/synthetic:1', cpus=1,
                                   memory_mb=2048, storage_mb=10240)
        config.allow_internet = True
        kwargs['task_env_config'] = config
    else:
        section = 'environment' if declaration == 'metadata-agent' else 'verifier.environment'
        (tmp_path / 'task.toml').write_text(f'[{section}]\nallow_internet = true\n')
    if protocol is None:
        with pytest.raises(ValueError, match='allow_internet|deny-all'):
            make_environment(module=module, role=role, **kwargs)
        return
    env = make_environment(module=module, role=role, **kwargs)
    assert env.offline_protocol_override
    assert env.egress_proxy_requested == (protocol == 'ANYEVAL_TB_EGRESS_PROXY')
    assert env.egress_proxy_enabled == (protocol == 'ANYEVAL_TB_EGRESS_PROXY' and role == 'agent')
    if role == 'verifier':
        assert env._manifests()[1]['spec']['egress'] == []


def test_legacy_alias_shares_implementation():
    assert importlib.import_module('anyeval_k8s') is k8s_env


def test_missing_prebuilt_context_skips_upload_and_records_fact(make_environment):
    env = make_environment()
    env.exec = AsyncMock()
    env.upload_dir = AsyncMock()
    asyncio.run(env._upload_environment_dir_after_start())
    env.exec.assert_not_awaited()
    env.upload_dir.assert_not_awaited()
    saved = json.loads((env.trial_paths.trial_dir / 'anyeval' / f'{env.pod_name}.json').read_text())
    assert saved['environment_context'] == 'not packaged (prebuilt image)'
    assert collect_pods(env.trial_paths.trial_dir)[0]['environment_context'] == saved['environment_context']


@pytest.mark.parametrize('context,role', [('environment', 'agent'), ('tests', 'verifier')])
@pytest.mark.parametrize('has_dockerfile', [False, True])
def test_present_context_preserves_harbor_staging(tmp_path, make_environment, context, role, has_dockerfile):
    directory = tmp_path / context
    directory.mkdir()
    (directory / 'synthetic.txt').write_text('synthetic runtime input')
    if has_dockerfile:
        (directory / 'Dockerfile').write_text('FROM example/synthetic:1\n')
    env = make_environment(context=context, role=role)
    env.exec = AsyncMock(return_value=ExecResult(stdout='/work\n', return_code=0))
    env.upload_dir = AsyncMock()
    asyncio.run(env._upload_environment_dir_after_start())
    if has_dockerfile:
        env.upload_dir.assert_not_awaited()
    else:
        env.upload_dir.assert_awaited_once_with(directory, '/work')


def test_repackaging_removes_stale_data_and_preserves_source_hashes(tmp_path):
    from scripts.package_data import package_dataset
    from terminal_bench_anyeval.eligibility import canonical_digest, hash_file

    source, dest = tmp_path / 'download', tmp_path / 'packaged'
    for name, storage in [('included', 10240), ('excluded', 20480)]:
        task = source / name
        (task / 'environment').mkdir(parents=True)
        (task / 'environment/Dockerfile').write_text('FROM example/synthetic:1\n')
        (task / 'task.toml').write_text(f'[environment]\ndocker_image="synthetic:1"\nstorage_mb={storage}\n')
        (task / 'instruction.md').write_text('synthetic input')
        (task / 'tests').mkdir()
        (task / 'tests/test.sh').write_text('synthetic verifier')
        (task / 'support.txt').write_text('synthetic support')
        (task / 'solution').mkdir()
        (task / 'solution/solve.sh').write_text('synthetic solution')
    (dest / 'stale').mkdir(parents=True)
    report, tasks, excluded, dropped = package_dataset(source, dest, 'terminal-bench@4.0.0')
    assert {p.name for p in dest.iterdir()} == {'included'}
    assert set(tasks) == {'included'}
    assert set(excluded) == {'excluded'}
    assert excluded['excluded']['task.toml'] == hash_file(source / 'excluded/task.toml')
    assert not (dest / 'included/environment').exists()
    assert not (dest / 'included/solution').exists()
    assert (dest / 'included/tests/test.sh').is_file()
    assert (dest / 'included/support.txt').is_file()
    for name in ['included', 'excluded']:
        assert dropped[name] == canonical_digest({'Dockerfile': hash_file(source / name / 'environment/Dockerfile')})
    assert package_dataset(source, dest, 'terminal-bench@4.0.0') == (report, tasks, excluded, dropped)
    (source / 'included/environment/Dockerfile').unlink()
    (source / 'included/environment/runtime.txt').write_text('required at runtime')
    with pytest.raises(ValueError, match='Runtime environment context'):
        package_dataset(source, dest, 'terminal-bench@4.0.0')
