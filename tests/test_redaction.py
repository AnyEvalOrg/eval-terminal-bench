import importlib.util
from importlib.resources import files
import json
import os
from pathlib import Path
import sys

import pytest
import yaml
from terminal_bench_anyeval.publication import partition_artifacts, public_artifact_paths


@pytest.fixture(scope='module')
def consumer():
    root = Path(os.environ.get('ANYEVAL_APP_ROOT', '/Users/jperla/josh/repos/anyeval-app'))
    source = root / 'app/redaction.py'
    if not source.is_file():
        pytest.skip('AnyEval production consumer checkout not available')
    spec = importlib.util.spec_from_file_location('anyeval_redaction_contract', source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_actual_consumer_parses_policy_and_redacts_content(consumer):
    declaration = files('terminal_bench_anyeval').joinpath('redaction.yaml').read_text()
    policy = consumer._parse(declaration, 'Terminal-Bench package')
    raw = yaml.safe_load(declaration)
    # Exercise each exact key recursively and in mixed case. No blanket sandbox
    # rule: the agent trajectory must survive publication unchanged.
    fixture = {key: 'SYNTHETIC_PRIVATE_TEST_MATERIAL' for key in raw['redact'] if key != 'error'}
    fixture['error'] = {'message': 'SYNTHETIC_PRIVATE_TEST_MATERIAL', 'traceback': 'SYNTHETIC_PRIVATE_TEST_MATERIAL'}
    tree = {'nested': [{key.upper(): value for key, value in fixture.items()}],
            'trajectory': {'messages': [{'role': 'assistant', 'content': 'PUBLIC_TRANSCRIPT'}]},
            'artifacts': {'trajectory_path': 'agent/trajectory.json', 'verifier_stdout_path': 'SYNTHETIC_PRIVATE_TEST_MATERIAL'},
            'scores': {'binary': {'value': 1}}, 'pods': [{'uid': 'public-uid'}]}
    clean = consumer.redact_declared(tree, policy)
    assert 'SYNTHETIC_PRIVATE_TEST_MATERIAL' not in json.dumps(clean)
    assert clean['trajectory'] == tree['trajectory']
    assert clean['artifacts']['trajectory_path'] == 'agent/trajectory.json'
    assert clean['scores'] == tree['scores']
    assert clean['pods'] == tree['pods']


@pytest.mark.parametrize('bad', ['patterns: ["**/tests/**"]', 'sandbox: {unknown: [input]}', 'version: 2'])
def test_actual_consumer_rejects_unsupported_schema(consumer, bad):
    with pytest.raises(consumer.RedactionError):
        consumer._parse('version: 1\nredact: []\n' + bad, 'synthetic')


def test_file_publication_is_allowlisted(tmp_path):
    public = ['agent/trajectory.json', 'agent/trajectory.cont-1.json',
              'agent/trajectory.summarization-1-summary.json']
    private = ['tests/test.py', 'verifier/test-stdout.txt', 'artifacts/test-copy.txt',
               'agent/terminal.cast', 'agent/trajectory.extra.json', 'config.json',
               'result.json', 'anyeval/proxy.log']
    for name in public + private:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('synthetic')
    (tmp_path / 'agent/trajectory.cont-2.json').symlink_to(tmp_path / 'tests/test.py')
    assert {p.relative_to(tmp_path).as_posix() for p in public_artifact_paths(tmp_path)} == set(public)
    partition = partition_artifacts(tmp_path)
    assert set(partition['trajectory']).isdisjoint(partition['private_artifacts'])
    assert str(tmp_path / 'tests/test.py') in partition['private_artifacts']


def test_root_and_packaged_policy_match():
    assert Path('redaction.yaml').read_bytes() == files('terminal_bench_anyeval').joinpath('redaction.yaml').read_bytes()
