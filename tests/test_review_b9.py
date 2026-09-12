"""Registry identities must bind both acquisition paths before data writes."""
import importlib
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from terminal_bench_anyeval import _harbor_fetch
from terminal_bench_anyeval.eligibility import (
    DATASETS, REGISTRY_VERSIONS, REGISTRY_VERSION_IDS, eligibility, manifest, scan,
)

fetch = importlib.import_module('terminal_bench_anyeval.fetch_data')
PINS = [
    ('terminal-bench-2-1', 'terminal-bench-2-1',
     'f92eea12-ff70-4d30-ace0-003abf294998',
     '7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a'),
    ('terminal-bench@4.0.0', 'terminal-bench',
     '1922072f-a433-429a-8929-350d5e1bcf02',
     '39d9f44b40420cde8fdcc087579c0d72a7e14fa3656d603c3f0d22fb35e27732'),
]


@pytest.mark.parametrize('dataset,name,version_id,digest', PINS)
def test_b9_committed_and_regenerated_identity(dataset, name, version_id, digest, tmp_path):
    assert REGISTRY_VERSIONS[dataset] == 'sha256:' + digest
    assert REGISTRY_VERSION_IDS[dataset] == version_id
    task = tmp_path / 'sample'
    task.mkdir()
    (task / 'task.toml').write_text('[environment]\ndocker_image = "synthetic:1"\n')
    for record in (manifest()['datasets'][dataset], eligibility(dataset), scan(tmp_path, dataset)):
        assert record['registry_version'] == 'sha256:' + digest
        assert record['dataset_version_id'] == version_id


@pytest.mark.parametrize('dataset,name,version_id,digest', PINS)
@pytest.mark.parametrize('identity', ['match', 'id', 'hash', 'both', 'missing_id', 'missing_hash', 'empty', 'duplicate'])
def test_b9_http_identity_before_enumeration_or_writes(
    dataset, name, version_id, digest, identity, monkeypatch, tmp_path,
):
    calls = []
    row = {'id': version_id, 'content_hash': digest}
    if identity in {'id', 'both'}:
        row['id'] = '00000000-0000-0000-0000-000000000000'
    if identity in {'hash', 'both'}:
        row['content_hash'] = '0' * 64
    if identity.startswith('missing_'):
        row.pop('id' if identity == 'missing_id' else 'content_hash')

    def rows(table, query):
        calls.append(table)
        if table == 'dataset_version':
            assert query['package.name'] == 'eq.' + name
            assert query['content_hash'] == 'eq.' + digest
            assert 'tag' not in query
            return [] if identity == 'empty' else [row, row] if identity == 'duplicate' else [row]
        assert identity == 'match', 'enumerated tasks after an identity mismatch'
        assert table == 'dataset_version_task'
        assert query['dataset_version_id'] == 'eq.' + version_id
        return []

    monkeypatch.setattr(fetch, 'registry_rows', rows)
    monkeypatch.setattr(fetch, 'registry_open', lambda *a: pytest.fail('downloaded an archive'))
    monkeypatch.setattr(fetch, 'eligibility', lambda d: {'included': [], 'excluded': []})
    monkeypatch.setattr(fetch, 'manifest', lambda: {'datasets': {dataset: {'tasks': {}}}})
    destination = tmp_path / 'download'
    if identity == 'match':
        assert fetch.download_registry_dataset(dataset, destination) == destination / name
        assert calls == ['dataset_version', 'dataset_version_task']
    else:
        with pytest.raises(ValueError, match='pinned version'):
            fetch.download_registry_dataset(dataset, destination)
        assert calls == ['dataset_version']
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('dataset,name,version_id,digest', PINS)
@pytest.mark.parametrize('identity', ['match', 'id', 'hash', 'both', 'missing_id', 'missing_hash'])
def test_b9_cli_consumed_metadata_is_guarded(pinned_registry_identity, 
    dataset, name, version_id, digest, identity, monkeypatch, tmp_path,
):
    # Use Harbor's real CLI, PackageDatasetClient metadata construction, and
    # BaseRegistryClient download flow. Only registry I/O and byte transfer are fake.
    package = pytest.importorskip('harbor.registry.client.package')
    from harbor.tasks.client import TaskClient

    row = {'id': version_id, 'content_hash': digest}
    if identity in {'id', 'both'}:
        row['id'] = '00000000-0000-0000-0000-000000000000'
    if identity in {'hash', 'both'}:
        row['content_hash'] = '0' * 64
    if identity.startswith('missing_'):
        row.pop('id' if identity == 'missing_id' else 'content_hash')
    db = SimpleNamespace(
        resolve_dataset_version=AsyncMock(return_value=({}, row)),
        get_dataset_version_tasks=AsyncMock(return_value=[]),
        get_dataset_version_files=AsyncMock(return_value=[]),
        record_dataset_download=AsyncMock(),
    )
    monkeypatch.setattr(package, 'RegistryDB', lambda: db)
    transfers = []

    async def download_tasks(self, **kwargs):
        transfers.append('tasks')
        kwargs['output_dir'].mkdir(parents=True)
        return SimpleNamespace(paths=[])

    async def download_files(self, metadata, **kwargs):
        transfers.append('files')
        return {}

    monkeypatch.setattr(TaskClient, 'download_tasks', download_tasks)
    monkeypatch.setattr(package.PackageDatasetClient, 'download_dataset_files', download_files)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert Path(command[1]).name == '_harbor_fetch.py'
        assert command[2:4] == ['sha256:' + digest, version_id]
        assert command[5:8] == ['dataset', 'download', f'terminal-bench/{name}@sha256:{digest}']
        assert kwargs == {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
        with monkeypatch.context() as context:
            context.setattr(sys, 'argv', command[1:])
            try:
                _harbor_fetch.main()
            except SystemExit as exc:
                return subprocess.CompletedProcess(command, exc.code or 0)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(fetch.subprocess, 'run', run)
    destination = tmp_path / 'download'
    if identity == 'match':
        assert fetch.download_dataset(dataset, destination) == destination / name
        assert transfers == ['tasks', 'files']
    else:
        with pytest.raises(RuntimeError, match='Harbor download failed'):
            fetch.download_dataset(dataset, destination)
        assert transfers == []
        assert list(tmp_path.iterdir()) == []
    assert len(commands) == 1
    db.resolve_dataset_version.assert_awaited_once_with('terminal-bench', name, 'sha256:' + digest)
