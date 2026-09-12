import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from harbor.environments.base import ExecResult
from kubernetes import client
from terminal_bench_anyeval.k8s_env import ACTIVE_ENVIRONMENTS
import test_anyeval_k8s as existing


@pytest.fixture
def environment():
    case = existing.ContractTests()
    case.setUp()
    env = case.env()
    env._call = AsyncMock(side_effect=lambda fn, *args, **kw: fn(*args, **kw))
    env._client = client.ApiClient()
    env._core = MagicMock()
    env._network = MagicMock()
    node = NS(status=NS(node_info=NS(kubelet_version='v1.35.1')),
              metadata=NS(labels={'sandbox.gke.io/runtime': 'gvisor', 'unrelated': 'not-exported'}))
    env._core.read_node.return_value = node
    policy = env._manifests()[1]
    env._network.read_namespaced_network_policy.return_value = client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(name=env.pod_name, uid='policy-uid', resource_version='9'),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(match_labels=policy['spec']['podSelector']['matchLabels']),
            policy_types=policy['spec']['policyTypes'], ingress=[], egress=[]))
    env._network.list_namespaced_network_policy.return_value = NS(items=[env._network.read_namespaced_network_policy.return_value])
    env._node_api = MagicMock()
    env._node_api.read_runtime_class.return_value = NS(handler='runsc', metadata=NS(uid='runtime-uid'))
    env.exec = AsyncMock(side_effect=[ExecResult(stdout='synthetic-kernel', stderr='', return_code=0),
                                      ExecResult(stdout='synthetic gVisor boot', stderr='', return_code=0)])
    yield env
    ACTIVE_ENVIRONMENTS.discard(env)
    if env._client:
        env._client.close()
    case.doCleanups()


def facts(env):
    return json.loads((env.trial_paths.trial_dir / 'anyeval' / (env.pod_name + '.json')).read_text())


def test_live_api_provenance_capture(environment):
    env = environment
    asyncio.run(env._capture_runtime_facts(NS(spec=NS(node_name='node-1', runtime_class_name='gvisor'), metadata=NS(uid='pod-uid', labels=environment._labels))))
    saved = facts(env)
    assert {k: v for k, v in saved['network_policy'].items() if k in ('name', 'uid', 'resource_version', 'egress_to_proxy_only')} == {'name': env.pod_name, 'uid': 'policy-uid',
                                        'resource_version': '9', 'egress_to_proxy_only': False}
    assert saved['kubelet_version'] == 'v1.35.1'
    assert saved['node_labels'] == {'sandbox.gke.io/runtime': 'gvisor'}
    assert saved['kernel_release'] == 'synthetic-kernel'
    assert saved['dmesg_gvisor_boot'] == 'synthetic gVisor boot'


def test_missing_node_permissions_are_recorded_as_missing(environment):
    environment._core.read_node.side_effect = PermissionError('synthetic')
    asyncio.run(environment._capture_runtime_facts(NS(spec=NS(node_name='node-1', runtime_class_name='gvisor'), metadata=NS(uid='pod-uid', labels=environment._labels))))
    saved = facts(environment)
    assert saved['node_evidence_error'] == 'PermissionError'
    assert 'kubelet_version' not in saved


def test_network_policy_drift_fails_closed(environment):
    environment._network.read_namespaced_network_policy.return_value.spec.egress = [client.V1NetworkPolicyEgressRule()]
    with pytest.raises(RuntimeError, match='attest admitted network policy'):
        asyncio.run(environment._capture_runtime_facts(NS(spec=NS(node_name='node-1', runtime_class_name='gvisor'), metadata=NS(uid='pod-uid', labels=environment._labels))))
    assert 'network_policy' not in facts(environment)


def test_saved_facts_merge_atomically(environment):
    environment._save_facts({'first': 1})
    environment._save_facts({'second': 2})
    assert facts(environment) == {'first': 1, 'second': 2}
    assert not list((environment.trial_paths.trial_dir / 'anyeval').glob('*.tmp'))


def test_proxy_log_keeps_only_this_pods_structured_addresses(environment):
    env = environment
    env._proxy_facts = {'pod': 'proxy'}
    env._save_facts({'pod_ip': '10.2.0.1', 'started_at': datetime.now(timezone.utc).isoformat()})
    env._core.read_namespaced_pod_log.return_value = '\n'.join([
        json.dumps({'client_ip': '10.2.0.1', 'synthetic': 'own'}),
        json.dumps({'remote_addr': '10.2.0.1:443', 'synthetic': 'own'}),
        json.dumps({'client_ip': '10.2.0.10', 'synthetic': 'other'}),
        json.dumps({'url': 'https://example/10.2.0.1', 'synthetic': 'other'}),
    ])
    asyncio.run(env._collect_proxy_log())
    output = (env.trial_paths.trial_dir / 'anyeval/proxy.log').read_text()
    assert 'other' not in output
    assert len(output.splitlines()) == 2
    assert env._core.read_namespaced_pod_log.call_args.kwargs['limit_bytes'] == 4 * 1024 * 1024


def test_cleanup_records_lifecycle_end(environment):
    environment._capture_final_facts = AsyncMock()
    env = environment
    env._pod_attempted = env._policy_attempted = True
    asyncio.run(env.stop(delete=False))
    assert facts(env)['ended_at']
    assert env not in ACTIVE_ENVIRONMENTS


@pytest.mark.parametrize('cpu,rejected', [('500m', True), ('1', False), ('2', False)])
def test_admission_never_reduces_requested_resources(environment, cpu, rejected):
    env = environment
    requested = env._manifests()[0]['spec']['containers'][0]['resources']
    resources = deepcopy(requested)
    resources['requests']['cpu'] = resources['limits']['cpu'] = cpu
    resource_model = client.V1ResourceRequirements(**resources)
    pod = NS(status=NS(phase='Running', pod_ip='10.2.0.1', container_statuses=[
        NS(name='main', image='synthetic', image_id='synthetic@sha256:'+'a'*64, container_id='containerd://main', restart_count=0, state=NS(running=True))]),
        metadata=NS(uid='pod-uid', resource_version='1', labels={}, annotations={}),
        spec=NS(runtime_class_name='gvisor', node_name='node-1', node_selector={},
                automount_service_account_token=False, containers=[NS(resources=resource_model)],
                dns_policy=None, dns_config=None))
    env._core.read_namespaced_pod.return_value = pod
    env._events = AsyncMock(return_value=[])
    if rejected:
        with pytest.raises(RuntimeError, match='reduced'):
            asyncio.run(env._wait_running())
    else:
        asyncio.run(env._wait_running())
        assert facts(env)['uid'] == 'pod-uid'
        assert ('resources_raised_by_admission' in facts(env)) == (cpu == '2')
