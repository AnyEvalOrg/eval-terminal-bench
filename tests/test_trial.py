import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
from terminal_bench_anyeval import trial
from terminal_bench_anyeval.agent_settings import agent_kwargs, validate_agent_kwargs
from fake_harbor import FakeTrial


@pytest.fixture
def fake(monkeypatch):
    from harbor.trial.trial import Trial
    FakeTrial.created = FakeTrial.ran = 0
    FakeTrial.mode = "verified"
    monkeypatch.setattr(Trial, "create", FakeTrial.create)
    return FakeTrial


@pytest.mark.parametrize("mode,outcome,reward", [
    ("verified", "verified", 1.0), ("raise", "error", None), ("create_error", "error", None),
    ("AgentTimeoutError", "agent_timeout", None), ("VerifierTimeoutError", "verifier_timeout", None),
    ("EnvironmentStartTimeoutError", "infrastructure", None), ("missing_reward", "infrastructure", None),
])
def test_round_trip(fake, spec, spec_file, tmp_path, mode, outcome, reward):
    fake.mode = mode
    output = tmp_path / "result.json"
    assert trial.main(["--spec", str(spec_file), "--result", str(output)]) == 0
    result = json.loads(output.read_text())
    assert result["version"] == 1
    assert result["run_id"] == spec["run_id"] and result["attempt"] == 1
    assert result["outcome"] == outcome and result["reward"] == reward
    assert spec["api_key"] not in output.read_text()
    assert fake.created == 1 and fake.ran == (mode != "create_error")
    assert fake.config.agent.override_timeout_sec == 120
    assert fake.config.verifier.override_timeout_sec == 30
    assert fake.config.agent.model_name == spec["model"]
    assert fake.config.agent.kwargs["api_base"] == spec["api_base"]
    assert fake.config.agent.kwargs["llm_call_kwargs"] == spec["agent_kwargs"]["llm_call_kwargs"]
    assert spec["api_key"] not in fake.config.model_dump_json()
    if mode != "create_error":
        assert result["trial_id"] == spec["trial_id"]
        assert result["harbor_trial_id"] == "synthetic-harbor-trial"
        assert result["agent"] == {"episodes": 3, "input_tokens": 123, "output_tokens": 45,
                                    "cache_tokens": 67, "summarizations": 1}
        assert [p["role"] for p in result["pods"]] == ["agent", "verifier"]
        assert result["pods"][0]["uid"] == "agent-uid"
        assert result["pods"][0]["image_digest"] == "sha256:" + "a" * 64
        assert result["pods"][0]["resources_admitted"]["requests"]["cpu"] == "2"
        assert result["pods"][0]["proxy"]["pod_uid"] == "proxy-uid"
        assert result["pods"][1]["proxy"] is None
        assert result["timing"]["agent_seconds"] == 2
        assert Path(result["artifacts"]["trajectory_path"]).is_file()
    assert result["timing"]["finished_at"]
    assert not list(tmp_path.glob(".result.json.*"))


@pytest.mark.parametrize("content", ["{", "[]", "null", '{"version":999}', '{"version":true}'])
def test_invalid_spec_still_writes_result(tmp_path, content):
    source = tmp_path / "spec.json"
    source.write_text(content)
    output = tmp_path / "result.json"
    assert trial.main(["--spec", str(source), "--result", str(output)]) == 0
    assert json.loads(output.read_text())["outcome"] == "error"


def test_missing_spec_writes_result(tmp_path):
    output = tmp_path / "result.json"
    assert trial.main(["--spec", str(tmp_path / "missing"), "--result", str(output)]) == 0
    assert json.loads(output.read_text())["exception"]["type"] == "FileNotFoundError"


def test_real_sigterm_delivers_atomic_result_and_cleans_up(spec_file, tmp_path, synthetic_data):
    ready = tmp_path / "ready"
    output = tmp_path / "result.json"
    script = ("import importlib; from pathlib import Path; "
              f"importlib.import_module('terminal_bench_anyeval.eligibility').DATA=Path({str(synthetic_data[1])!r}); "
              "from fake_harbor import FakeTrial; from harbor.trial.trial import Trial; "
              "FakeTrial.mode='wait'; Trial.create=FakeTrial.create; "
              "from terminal_bench_anyeval.trial import main; raise SystemExit(main())")
    env = dict(os.environ, FAKE_READY=str(ready), PYTHONPATH=os.pathsep.join(
        [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]))
    child = subprocess.Popen([sys.executable, "-c", script, "--spec", str(spec_file),
                              "--result", str(output)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "Synthetic child did not reach its ready marker"
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=30) == 0
        result = json.loads(output.read_text())
        assert result["outcome"] == "infrastructure"
        assert result["exception"]["type"] == "TrialTerminated"
        assert result["reward"] is None
        assert result["agent"]["episodes"] == 3
        assert len(result["pods"]) == 2
        assert (Path(ready.read_text()) / "cleanup-complete").is_file()
        assert not list(tmp_path.glob(".result.json.*"))
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_environment_restored_and_no_secret_in_config(spec, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "previous")
    monkeypatch.setenv("ANYEVAL_TB_OFFLINE_PROTOCOL", "1")
    with trial.child_environment(spec):
        assert os.environ["OPENAI_API_KEY"] == spec["api_key"]
        assert "ANYEVAL_TB_OFFLINE_PROTOCOL" not in os.environ
        config = trial.build_config(spec, tmp_path)
        assert spec["api_key"] not in config.model_dump_json()
        assert config.environment.env == {}
    assert os.environ["OPENAI_API_KEY"] == "previous"
    assert os.environ["ANYEVAL_TB_OFFLINE_PROTOCOL"] == "1"


@pytest.mark.parametrize("key,value", [("parser_name", "xml"), ("enable_summarize", False),
                                      ("record_terminal_session", False), ("use_responses_api", True),
                                      ("llm_kwargs", {"api_key": "secret"}), ("api_key", "secret")])
def test_reject_settings_drift(key, value):
    kwargs = agent_kwargs()
    kwargs[key] = value
    with pytest.raises(ValueError):
        validate_agent_kwargs(kwargs)


@pytest.mark.parametrize("key,value", [("api_base", "https://example.com/v1"), ("model", "anthropic/model"),
    ("namespace", "default"), ("attempt", True), ("env", {"OPENAI_API_KEY": "bad"}),
    ("task_dir", "/tmp/not-packaged"), ("task", "../elsewhere"), ("timeouts", {"agent_sec": 0})])
def test_reject_invalid_spec(spec, key, value):
    spec[key] = value
    with pytest.raises((ValueError, KeyError)):
        trial.validate_spec(spec)


def test_atomic_replace_never_truncates_existing_result(tmp_path, monkeypatch):
    path = tmp_path / "result.json"
    trial.atomic_write(path, {"old": True})
    def fail(*args):
        raise OSError("synthetic replace failure")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        trial.atomic_write(path, {"new": True})
    assert json.loads(path.read_text()) == {"old": True}
    assert not list(tmp_path.glob(".result.json.*"))


@pytest.mark.parametrize('dataset', ['terminal-bench-2-1', 'terminal-bench@4.0.0'])
@pytest.mark.parametrize('protocol', ['ANYEVAL_TB_EGRESS_PROXY', 'ANYEVAL_TB_OFFLINE_PROTOCOL'])
def test_real_harbor_constructs_packaged_task_without_cluster(spec, tmp_path, dataset, protocol, monkeypatch):
    from harbor.trial.trial import Trial
    from terminal_bench_anyeval.eligibility import dataset_root, eligibility
    from terminal_bench_anyeval.k8s_env import AnyEvalK8sEnvironment
    spec['dataset'] = dataset
    spec['task'] = eligibility(dataset)['included'][0]
    spec['task_dir'] = str(dataset_root(dataset) / spec['task'])
    monkeypatch.delenv('ANYEVAL_TB_EGRESS_PROXY', raising=False)
    monkeypatch.delenv('ANYEVAL_TB_OFFLINE_PROTOCOL', raising=False)
    spec['env'] = {protocol: '1'}
    async def construct():
        with trial.child_environment(spec):
            actual = await Trial.create(trial.build_config(spec, tmp_path))
            try:
                assert isinstance(actual.agent_environment, AnyEvalK8sEnvironment)
                assert actual._agent_timeout_sec == 120
                assert actual._verifier_timeout_sec == 30
                from harbor.trial.trial import resolve_task_verifier_mode
                assert resolve_task_verifier_mode(actual.task.config).value == ('separate' if '4.0' in dataset else 'shared')
                assert actual.agent._parser_name == 'json'
                assert actual.agent._enable_summarize is True
                assert actual.agent._record_terminal_session is True
                assert actual.agent._llm_call_kwargs == spec['agent_kwargs']['llm_call_kwargs']
                assert not actual.agent._llm_kwargs
                assert actual.agent_environment._client is None
            finally:
                await actual.agent_environment.stop(delete=True)
                actual._close_logger_handler()
    asyncio.run(construct())


def test_nonstring_secret_validation_error_is_still_serializable(spec, tmp_path):
    spec['api_key'] = {'invalid': True}
    source = tmp_path / 'spec.json'
    output = tmp_path / 'result.json'
    source.write_text(json.dumps(spec))
    assert trial.main(['--spec', str(source), '--result', str(output)]) == 0
    assert json.loads(output.read_text())['outcome'] == 'error'


def test_module_cli_writes_result_on_invalid_spec(tmp_path):
    source = tmp_path / 'spec.json'
    source.write_text('{}')
    output = tmp_path / 'result.json'
    proc = subprocess.run([sys.executable, '-m', 'terminal_bench_anyeval.trial',
                           '--spec', str(source), '--result', str(output)], capture_output=True)
    assert proc.returncode == 0
    assert proc.stdout == b'' and proc.stderr == b''
    assert json.loads(output.read_text())['exception']['type'] == 'ValueError'
