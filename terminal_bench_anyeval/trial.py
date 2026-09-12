"""AnyEval v1 child: exactly one Harbor Trial, atomic result, cancellation cleanup."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import tempfile
from urllib.parse import urlsplit
import uuid

from .agent_settings import validate_agent_kwargs
from .eligibility import DATASETS, task_directory, eligibility, verify_task

ENV_KEYS = {"ANYEVAL_TB_EGRESS_PROXY", "ANYEVAL_TB_NO_SPOT", "ANYEVAL_TB_TMUX_STATIC",
            "ANYEVAL_TB_OFFLINE_PROTOCOL"}


class TrialTerminated(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def json_safe(value):
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items() if isinstance(k, str)}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return None


def reject_constant(value):
    raise ValueError("Non-finite JSON constant is forbidden")


def finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        reject_constant(value)
    return parsed


def echo_bindings(result, spec):
    if not isinstance(spec, dict):
        return
    for dest, source in (("run_id", "run_id"), ("sample_id", "task"),
                         ("trial_id", "trial_id"), ("attempt", "attempt")):
        value = spec.get(source)
        valid = type(value) is int and value > 0 if source == "attempt" else isinstance(value, str) and bool(value)
        result[dest] = value if valid else None


def atomic_write(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(json_safe(value), stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def empty_result():
    return {"version": 1, "run_id": None, "attempt": None, "trial_id": None,
            "outcome": "error", "reward": None, "exception": None,
            "agent": {"episodes": 0, "input_tokens": 0, "output_tokens": 0,
                      "cache_tokens": 0, "summarizations": 0},
            "sample_id": None, "harbor_trial_id": None,
            "verifier_health": {"setup_completed": False, "completed": False},
            "verified_artifacts": [], "pods": [], "artifacts": {"trajectory_path": None,
                                      "verifier_stdout_path": None, "proxy_log_path": None},
            "timing": {"started_at": now(), "finished_at": None,
                       "agent_seconds": 0, "verifier_seconds": 0}}


def validate_spec(spec):
    if not isinstance(spec, dict) or type(spec.get("version")) is not int or spec["version"] != 1:
        raise ValueError("Expected trial spec version 1")
    for key in ("run_id", "trial_id", "task", "api_base", "api_key", "model", "kubeconfig", "task_dir", "dataset", "agent", "namespace"):
        if not isinstance(spec.get(key), str) or not spec[key]:
            raise ValueError(f"spec.{key} must be a nonempty string")
    if spec.get("sample_id", spec["task"]) != spec["task"]:
        raise ValueError("spec.sample_id must equal spec.task")
    if type(spec.get("attempt")) is not int or spec["attempt"] < 1:
        raise ValueError("spec.attempt must be a positive integer")
    if spec.get("dataset") not in DATASETS or spec.get("agent") != "terminus-2":
        raise ValueError("Unsupported dataset or agent")
    if spec.get("namespace") != "anyeval-sandbox":
        raise ValueError("Expected namespace anyeval-sandbox")
    url = urlsplit(spec["api_base"])
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not url.port or url.path.rstrip("/") != "/v1" or url.username or url.password
            or url.query or url.fragment):
        raise ValueError("api_base must be the worker-local HTTP model shim /v1 endpoint")
    if not spec["model"].startswith("openai/"):
        raise ValueError("model must use the OpenAI-compatible shim provider")
    validate_agent_kwargs(spec.get("agent_kwargs"))
    env = spec.get("env", {})
    if not isinstance(env, dict) or set(env) - ENV_KEYS or any(not isinstance(v, str) for v in env.values()):
        raise ValueError("Unsupported child environment setting")
    for key in ENV_KEYS - {"ANYEVAL_TB_TMUX_STATIC"}:
        if key in env and env[key] not in {"0", "1"}:
            raise ValueError("Protocol flags must be 0 or 1")
    if not isinstance(spec.get("timeouts"), dict):
        raise ValueError("spec.timeouts must be an object")
    for key in ("agent_sec", "verifier_sec"):
        value = spec.get("timeouts", {}).get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"timeouts.{key} must be positive and finite")
    task_id = spec["task"]
    if task_id not in eligibility(spec["dataset"])["included"]:
        raise ValueError("Task is not in the packaged eligibility list")
    directory = Path(spec["task_dir"])
    expected = task_directory(spec["dataset"], task_id)
    if not directory.is_absolute() or directory.resolve() != expected.resolve():
        raise ValueError("task_dir must identify the selected packaged task")
    verify_task(directory, spec["dataset"], task_id)
    return spec


@contextmanager
def child_environment(spec):
    # Credentials stay in the worker process, never in AgentConfig/trajectory or pod env.
    updates = {key: None for key in ENV_KEYS}
    updates.update(spec.get("env", {}))
    updates.update(KUBECONFIG=spec["kubeconfig"], OPENAI_API_KEY=spec["api_key"],
                   OPENAI_API_BASE=spec["api_base"], OPENAI_BASE_URL=spec["api_base"],
                   LITELLM_LOCAL_MODEL_COST_MAP="True")
    previous = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def build_config(spec, output_dir):
    from harbor.models.trial.config import TrialConfig
    kwargs = validate_agent_kwargs(spec["agent_kwargs"])
    kwargs["api_base"] = spec["api_base"]
    return TrialConfig(
        task={"path": Path(spec["task_dir"])},
        trial_name=f"tb-{uuid.uuid4().hex}", trials_dir=output_dir,
        agent={"name": "terminus-2", "model_name": spec["model"], "kwargs": kwargs,
               "override_timeout_sec": spec["timeouts"]["agent_sec"]},
        environment={"import_path": "terminal_bench_anyeval.k8s_env:AnyEvalK8sEnvironment",
                     "delete": True, "kwargs": {"namespace": spec["namespace"],
                         "binding": {"run_id": spec["run_id"], "sample_id": spec["task"],
                                     "attempt": spec["attempt"], "trial_id": spec["trial_id"]}}},
        verifier={"override_timeout_sec": spec["timeouts"]["verifier_sec"]},
    )


def get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def seconds(timing):
    start, end = get(timing, "started_at"), get(timing, "finished_at")
    if start is None or end is None:
        return 0
    if isinstance(start, str):
        start = datetime.fromisoformat(start)
    if isinstance(end, str):
        end = datetime.fromisoformat(end)
    return max(0, (end - start).total_seconds())


def classify(exception_type):
    if exception_type == "AgentTimeoutError":
        return "agent_timeout"
    if exception_type == "VerifierTimeoutError":
        return "verifier_timeout"
    if exception_type in {"TrialTerminated", "CancelledError", "KeyboardInterrupt"}:
        return "infrastructure"
    if exception_type in {"ExecStreamClosed", "VerifierPreflightError", "TransferLimitError"} or any(
        word in exception_type for word in ("Infrastructure", "Environment", "Setup", "Download", "RewardFile", "Connection", "ApiException")
    ):
        return "infrastructure"
    return "error"


def record_exception(result, exc, secret=""):
    message = str(exc)
    if isinstance(secret, str) and secret:
        message = message.replace(secret, "[redacted]")
    result.update(outcome=classify(type(exc).__name__), reward=None,
                  exception={"type": type(exc).__name__, "message": message})


def apply_harbor_result(result, harbor_result, secret):
    if harbor_result is None:
        return
    context = get(harbor_result, "agent_result")
    metadata = get(context, "metadata") or {}
    result["agent"] = {"episodes": get(metadata, "n_episodes", 0),
                       "input_tokens": get(context, "n_input_tokens") or 0,
                       "output_tokens": get(context, "n_output_tokens") or 0,
                       "cache_tokens": get(context, "n_cache_tokens") or 0,
                       "summarizations": get(metadata, "summarization_count", 0)}
    result["timing"].update(agent_seconds=seconds(get(harbor_result, "agent_execution")),
                             verifier_seconds=seconds(get(harbor_result, "verifier")))
    exception = get(harbor_result, "exception_info")
    if exception:
        kind = get(exception, "exception_type", "Error")
        message = get(exception, "exception_message", "")
        result.update(outcome=classify(kind), reward=None,
                      exception={"type": kind, "message": message.replace(secret, "[redacted]") if secret else message})
        return
    rewards = get(get(harbor_result, "verifier_result"), "rewards")
    reward = rewards.get("reward") if isinstance(rewards, dict) else None
    if type(reward) not in (int, float) or reward not in (0, 1):
        result.update(outcome="infrastructure", reward=None,
                      exception={"type": "InvalidReward", "message": "Expected binary verifier reward"})
    else:
        result.update(outcome="verified", reward=float(reward), exception=None)


def collect_pods(trial_dir):
    pods = []
    for path in sorted((trial_dir / "anyeval").glob("tb-*.json")):
        facts = json.loads(path.read_text())
        containers = facts.get("containers", [])
        image_id = next((c.get("imageID") for c in containers if c.get("name") == "main"), None)
        digest = "sha256:" + image_id.split("sha256:", 1)[1] if image_id and "sha256:" in image_id else None
        pods.append({"role": facts.get("role", "agent" if facts.get("session_id", "").endswith("__env") else "verifier"),
                     "name": facts.get("pod", path.stem), "uid": facts.get("uid"), "node": facts.get("node"),
                     "runtime_class_name": facts.get("runtimeClassName"), "image": facts.get("image"),
                     "image_digest": digest, "resources_requested": facts.get("resources_requested"),
                     "resources_admitted": facts.get("resources"), "network_policy": facts.get("network_policy"),
                     "proxy": facts.get("egress_proxy"),
                     **{key: facts.get(key) for key in ("dmesg_gvisor_boot", "kernel_release", "kubelet_version",
                                                        "node_labels", "started_at", "ended_at", "environment_context",
                                                        "run_id", "sample_id", "attempt", "trial_id", "namespace",
                                                        "container_name", "container_id", "created_uid", "finished_uid",
                                                        "resource_version", "finished_resource_version", "restart_count",
                                                        "labels", "finished_labels", "selecting_policy_uids",
                                                        "finished_selecting_policy_uids", "additional_network_policies",
                                                        "finished_additional_network_policies", "runtime_class_exists",
                                                        "runtime_class_handler", "runtime_class_uid")}})
    return sorted(pods, key=lambda pod: (pod["role"] != "agent", pod["name"]))


def collect_artifacts(trial_dir):
    def present(relative):
        path = trial_dir / relative
        return str(path.resolve()) if path.is_file() else None
    return {"trajectory_path": present("agent/trajectory.json"),
            "verifier_stdout_path": present("verifier/test-stdout.txt"),
            "proxy_log_path": present("anyeval/proxy.log")}


async def execute(spec, result, result_path, control):
    from .k8s_env import ACTIVE_ENVIRONMENTS
    echo_bindings(result, spec)
    control["task"] = asyncio.current_task()
    trial = None
    trial_dir = None
    try:
        if control["terminated"]:
            raise TrialTerminated("Child received SIGTERM")
        validate_spec(spec)
        with child_environment(spec):
            # Establish the shim and local model metadata before LiteLLM imports.
            from harbor.trial.trial import Trial
            config = build_config(spec, result_path.parent / "harbor")
            trial_dir = config.trials_dir / config.trial_name
            try:
                trial = await Trial.create(config)
                result["harbor_trial_id"] = str(trial.id)
                harbor_result = await trial.run()
                apply_harbor_result(result, harbor_result, spec["api_key"])
            except BaseException as exc:
                if trial is not None:
                    apply_harbor_result(result, getattr(trial, "_result", None), spec["api_key"])
                record_exception(result, exc, spec["api_key"])
            finally:
                # Harbor stops both roles; retry any environment retained after an
                # interrupted or failed stop, including failures during create().
                for environment in list(ACTIVE_ENVIRONMENTS):
                    try:
                        await environment.stop(delete=True)
                    except Exception as exc:
                        record_exception(result, exc, spec["api_key"])
                        result["outcome"] = "infrastructure"
    except BaseException as exc:
        record_exception(result, exc, spec.get("api_key", ""))
    finally:
        if trial_dir is not None:
            try:
                result["pods"] = collect_pods(trial_dir)
                result["artifacts"] = collect_artifacts(trial_dir)
                health = []
                for path in sorted((trial_dir / "anyeval").glob("tb-*.json")):
                    facts = json.loads(path.read_text())
                    if "verifier_health" in facts:
                        health.append(facts["verifier_health"])
                    result["verified_artifacts"].extend(facts.get("verified_artifacts", []))
                result["verifier_health"] = {
                    key: bool(health) and all(h.get(key) is True for h in health)
                    for key in ("setup_completed", "completed")}
                if result["outcome"] == "verified" and not all(result["verifier_health"].values()):
                    record_exception(result, RuntimeError("Verifier health was not proved"))
                    result["outcome"] = "infrastructure"
            except Exception as exc:
                record_exception(result, exc, spec.get("api_key", ""))
                result["outcome"] = "infrastructure"
        if control["terminated"]:
            record_exception(result, TrialTerminated("Child received SIGTERM"))
        result["timing"]["finished_at"] = now()
        atomic_write(result_path, result)
        control["task"] = None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args(argv)
    result_path = args.result.resolve()
    result = empty_result()
    control = {"task": None, "terminated": False}
    spec = {}

    def terminate(signum, frame):
        if control["terminated"]:
            return
        control["terminated"] = True
        record_exception(result, TrialTerminated("Child received SIGTERM"))
        result["timing"]["finished_at"] = now()
        # Durable fallback before potentially slow Kubernetes/Harbor cleanup.
        atomic_write(result_path, result)
        if control["task"] is not None:
            # Wake an event loop blocked in selector.select(None). Direct cancel
            # from a Python signal handler can otherwise wait forever for I/O.
            task = control["task"]
            task.get_loop().call_soon_threadsafe(task.cancel)

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        # Harbor diagnostics can include verifier material. Keep this file private.
        with (result_path.parent / (result_path.name + ".private.log")).open("a") as log:
            with redirect_stdout(log), redirect_stderr(log):
                try:
                    spec = json.loads(args.spec.read_text(), parse_constant=reject_constant, parse_float=finite_float)
                    if isinstance(spec, dict):
                        echo_bindings(result, spec)
                    else:
                        raise ValueError("Spec must be a JSON object")
                    asyncio.run(execute(spec, result, result_path, control))
                except BaseException as exc:
                    record_exception(result, exc, spec.get("api_key", "") if isinstance(spec, dict) else "")
    finally:
        result["timing"]["finished_at"] = now()
        atomic_write(result_path, result)
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
