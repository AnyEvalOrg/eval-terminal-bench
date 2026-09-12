"""Complete Kubernetes fixtures for the adapter's admitted-state contract."""
from copy import deepcopy
from types import SimpleNamespace as NS
from kubernetes import client
from terminal_bench_anyeval.k8s_env import PROXY_IMAGE


def upgrade_synthetic_environment(env, monkeypatch):
    # Preserve the fixture's lifecycle and transport behavior, adding real pod specs.
    original_create = env._core.create_namespaced_pod.side_effect
    original_read = env._core.read_namespaced_pod.side_effect
    requested = {}
    def create(namespace, body, **kwargs):
        requested.update(deepcopy(body))
        if callable(original_create):
            return original_create(namespace, body, **kwargs)
    env._core.create_namespaced_pod.side_effect = create
    def read(*args, **kwargs):
        pod = original_read(*args, **kwargs) if callable(original_read) else env._core.read_namespaced_pod.return_value
        if requested:
            pod.spec = env._client._ApiClient__deserialize(requested, 'V1Pod').spec
            pod.spec.node_name = 'synthetic-node'
            pod.metadata.namespace = env.namespace
            for status in pod.status.container_statuses:
                if status.name == 'main':
                    status.image_id = 'docker.io/synthetic@' + requested['spec']['containers'][0]['image'].rsplit('@',1)[1]
        return pod
    env._core.read_namespaced_pod.side_effect = read
    def deleted(*args, **kwargs):
        def absent(*a, **k):
            raise client.ApiException(status=404)
        env._core.read_namespaced_pod.side_effect = absent
    env._core.delete_namespaced_pod.side_effect = deleted
    proxy_spec = client.V1PodSpec(containers=[client.V1Container(
        name='iron-proxy', image=PROXY_IMAGE, args=['-config','/etc/iron-proxy/proxy.yaml'],
        volume_mounts=[client.V1VolumeMount(name='config', mount_path='/etc/iron-proxy', read_only=True)])])
    for pod in env._core.list_namespaced_pod.return_value.items:
        proxy_spec.volumes = pod.spec.volumes
        pod.spec = deepcopy(proxy_spec)
        for status in pod.status.container_statuses:
            status.name = 'iron-proxy'
            status.image_id = PROXY_IMAGE
    deployment = client.AppsV1Api(env._client).read_namespaced_deployment.return_value
    deployment.spec.template.spec.containers = deepcopy(proxy_spec.containers)
    original_stream = env._stream
    async def stream(command, **kwargs):
        output = kwargs.pop('output', None)
        data, err, code = await original_stream(command, **kwargs)
        if output is not None:
            output.write(data)
            return b'', err, code
        return data, err, code
    env._stream = stream


def complete_admission_pod(env, pod):
    requested = env._manifests()[0]
    spec = env._client._ApiClient__deserialize(requested, 'V1Pod').spec
    spec.containers[0].resources = pod.spec.containers[0].resources
    spec.node_name = 'synthetic-node'
    pod.spec = spec
    pod.metadata.namespace = env.namespace
    pod.metadata.name = env.pod_name
    pod.metadata.labels = dict(env._labels)
    for status in pod.status.container_statuses:
        if status.name == 'main':
            status.image_id = 'synthetic@' + requested['spec']['containers'][0]['image'].rsplit('@',1)[1]
