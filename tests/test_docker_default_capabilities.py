"""The task container keeps Docker's default capability set, as the official protocol does."""
from terminal_bench_anyeval import k8s_env

DOCKER_DEFAULTS = {"AUDIT_WRITE", "CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "MKNOD",
                   "NET_BIND_SERVICE", "NET_RAW", "SETFCAP", "SETGID", "SETPCAP", "SETUID", "SYS_CHROOT"}


def test_default_capability_constant_is_docker_default_and_sorted():
    assert set(k8s_env.DOCKER_DEFAULT_CAPABILITIES) == DOCKER_DEFAULTS
    assert list(k8s_env.DOCKER_DEFAULT_CAPABILITIES) == sorted(k8s_env.DOCKER_DEFAULT_CAPABILITIES)
    assert "SYS_ADMIN" not in k8s_env.DOCKER_DEFAULT_CAPABILITIES


def test_manifest_requests_docker_defaults_and_no_escalation():
    source = open(k8s_env.__file__).read()
    assert '"capabilities": {"drop": ["ALL"], "add": list(DOCKER_DEFAULT_CAPABILITIES)}' in source
    assert '"allowPrivilegeEscalation": False' in source
    assert '"privileged": False' in source
