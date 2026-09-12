import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

fetch = importlib.import_module('terminal_bench_anyeval.fetch_data')
integrity = importlib.import_module('terminal_bench_anyeval.eligibility')


def test_fetch_prunes_verifies_drops_and_is_idempotent(mini_registry, capsys):
    registry, metadata, calls = mini_registry
    before = (metadata / 'manifest.json').read_bytes()
    assert fetch.main([]) == 0
    assert calls == list(integrity.DATASETS)
    for dataset in integrity.DATASETS:
        root = integrity.dataset_root(dataset)
        assert fetch.verify_dataset(root, dataset) == (2, 8)
        assert {p.name for p in root.iterdir()} == {'synthetic-a', 'synthetic-b'}
        for task in root.iterdir():
            assert {p.name for p in task.iterdir()} == {'task.toml', 'instruction.md', 'tests'}
            assert os.access(task / 'tests/test.sh', os.X_OK)
            assert (task / 'tests/nested/.fixture').read_bytes() == b'synthetic verifier fixture\x00'
    assert fetch.main([]) == 0
    assert fetch.main(['--verify-only']) == 0
    assert calls == list(integrity.DATASETS)
    assert (metadata / 'manifest.json').read_bytes() == before
    assert not list(integrity.data_root().glob('.fetch-*'))
    output = capsys.readouterr().out
    assert 'installed 2 tasks, 8 files' in output
    assert 'excluded 1 tasks' in output
    assert 'already installed' in output
    assert 'Synthetic catalogue input' not in output


@pytest.mark.parametrize('change,filename,message', [
    ('drift', 'tests/test.sh', 'SHA256 mismatch'),
    ('extra', 'tests/unexpected.txt', 'Extra file'),
    ('extra', 'tests/nested/.extra', 'Extra file'),
    ('missing', 'tests/test.sh', 'Missing file'),
    ('missing', 'instruction.md', 'Missing file'),
    ('drift', 'README.md', 'SHA256 mismatch'),
    ('missing', 'README.md', 'Missing file'),
])
def test_registry_mismatch_is_hard_error_without_install(mini_registry, change, filename, message, capsys):
    # Fail the second dataset: neither download may be installed on mismatch.
    path = mini_registry[0] / 'terminal-bench-4-0/synthetic-a' / filename
    if change == 'missing':
        path.unlink()
    else:
        path.write_text('synthetic drift')
    assert fetch.main([]) == 1
    error = capsys.readouterr().err
    assert message in error and f'synthetic-a/{filename}' in error
    assert not any((integrity.data_root() / v[2]).exists() for v in integrity.DATASETS.values())
    assert not list(integrity.data_root().glob('.fetch-*'))


@pytest.mark.parametrize('change', ['extra', 'missing', 'drift'])
def test_existing_install_is_reverified(synthetic_data, change, capsys):
    root = integrity.dataset_root('terminal-bench-2-1')
    path = root / 'synthetic-a/tests/test.sh'
    if change == 'extra':
        path = root / 'synthetic-a/extra.txt'
        path.touch()
    elif change == 'missing':
        path.unlink()
    else:
        path.write_text('synthetic change')
    assert fetch.main([]) == 1
    assert fetch.main(['--verify-only']) == 1
    assert path.name in capsys.readouterr().err
    assert len(synthetic_data[2]) == 2


def test_unexpected_task_is_rejected(mini_registry, capsys):
    (mini_registry[0] / 'terminal-bench-4-0/unexpected-task').mkdir()
    assert fetch.main([]) == 1
    assert 'Extra task' in capsys.readouterr().err


def test_retained_symlink_is_rejected(mini_registry, capsys):
    task = mini_registry[0] / 'terminal-bench-2-1/synthetic-a'
    (task / 'tests/test.sh').unlink()
    (task / 'tests/test.sh').symlink_to(task / 'instruction.md')
    assert fetch.main([]) == 1
    assert 'Unsafe file' in capsys.readouterr().err


def test_discarded_context_is_not_traversed(mini_registry):
    task = mini_registry[0] / 'terminal-bench-2-1/synthetic-a'
    (task / 'environment/broken-link').symlink_to(task / 'absent')
    (task / 'solution/broken-link').symlink_to(task / 'absent')
    assert fetch.main([]) == 0


def test_sample_construction_never_opens_or_scans_tests(synthetic_data, monkeypatch):
    original_open, original_iterdir = Path.open, Path.iterdir

    def guard_open(path, *args, **kwargs):
        assert 'tests' not in path.parts, 'Catalogue read verifier material'
        return original_open(path, *args, **kwargs)

    def guard_iterdir(path):
        assert 'tests' not in path.parts, 'Catalogue scanned verifier material'
        return original_iterdir(path)

    monkeypatch.setattr(Path, 'open', guard_open)
    monkeypatch.setattr(Path, 'iterdir', guard_iterdir)
    from terminal_bench_anyeval.task import make_task
    for dataset in integrity.DATASETS:
        samples = list(make_task(dataset).dataset)
        assert len(samples) == 2
        assert all(Path(s.metadata['task_dir']).is_relative_to(integrity.data_root()) for s in samples)


def test_absent_data_error_and_verify_only_does_not_write(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'absent'
    monkeypatch.setenv('ANYEVAL_TB_DATA_DIR', str(root))
    from terminal_bench_anyeval.task import make_task
    with pytest.raises(FileNotFoundError, match='run python -m terminal_bench_anyeval.fetch_data'):
        make_task('terminal-bench-2-1')
    assert fetch.main(['--verify-only']) == 1
    assert integrity.FETCH_HINT in capsys.readouterr().err
    assert not root.exists()


def test_missing_task_metadata_has_fetch_hint(synthetic_data):
    from terminal_bench_anyeval.task import make_task
    root = integrity.dataset_root('terminal-bench-2-1')
    (root / 'synthetic-a/task.toml').unlink()
    with pytest.raises(FileNotFoundError, match=integrity.FETCH_HINT):
        make_task('terminal-bench-2-1')


def test_trial_uses_override_and_verifies_bytes(spec):
    from terminal_bench_anyeval.trial import validate_spec
    assert validate_spec(spec) is spec
    path = Path(spec['task_dir']) / 'tests/test.sh'
    path.write_text('synthetic corruption')
    with pytest.raises(ValueError, match='SHA256 mismatch: .*tests/test.sh'):
        validate_spec(spec)


def test_trial_data_absent_has_fetch_hint(spec, monkeypatch, tmp_path):
    from terminal_bench_anyeval.trial import validate_spec
    monkeypatch.setenv('ANYEVAL_TB_DATA_DIR', str(tmp_path / 'absent'))
    with pytest.raises(FileNotFoundError, match=integrity.FETCH_HINT):
        validate_spec(spec)


def test_data_root_override_and_readonly_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv('ANYEVAL_TB_DATA_DIR', raising=False)
    monkeypatch.setattr(integrity.os, 'access', lambda *args: True)
    assert integrity.data_root() == integrity.DATA
    monkeypatch.setattr(integrity.os, 'access', lambda *args: False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    assert integrity.data_root() == tmp_path / '.cache/anyeval/terminal-bench'
    monkeypatch.setenv('ANYEVAL_TB_DATA_DIR', str(tmp_path / 'override'))
    assert integrity.data_root() == tmp_path / 'override'


@pytest.mark.parametrize('dataset,reference,folder', [
    ('terminal-bench-2-1', 'terminal-bench/terminal-bench-2-1@sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a', 'terminal-bench-2-1'),
    ('terminal-bench@4.0.0', 'terminal-bench/terminal-bench@4.0.0', 'terminal-bench'),
])
def test_download_invokes_pinned_harbor_export(monkeypatch, tmp_path, dataset, reference, folder):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(fetch.subprocess, 'run', run)
    assert fetch.download_dataset(dataset, tmp_path) == tmp_path / folder
    command, kwargs = calls[0]
    assert Path(command[0]).name == 'harbor'
    assert command[1:] == ['dataset', 'download', reference, '--export', '--output-dir', str(tmp_path)]
    assert kwargs == {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}


def test_download_failure_does_not_echo_harbor_material(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a[0], 9))
    with pytest.raises(RuntimeError, match='Harbor download failed.*exit 9'):
        fetch.download_dataset('terminal-bench-2-1', tmp_path)


@pytest.mark.parametrize('entrypoint', [
    ['-m', 'terminal_bench_anyeval.fetch_data'], ['scripts/fetch_data.py'],
])
def test_cli_missing_data_is_nonzero(tmp_path, entrypoint):
    env = dict(os.environ, ANYEVAL_TB_DATA_DIR=str(tmp_path / 'absent'))
    result = subprocess.run([sys.executable, *entrypoint, '--verify-only'], env=env, capture_output=True)
    assert result.returncode == 1
    assert integrity.FETCH_HINT.encode() in result.stderr
    assert result.stdout == b''
