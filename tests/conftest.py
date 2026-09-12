import json
import importlib
from pathlib import Path
import shutil
import pytest

from terminal_bench_anyeval.agent_settings import agent_kwargs
from terminal_bench_anyeval.eligibility import dataset_root, eligibility


@pytest.fixture
def spec(synthetic_data):
    dataset = "terminal-bench-2-1"
    name = eligibility(dataset)["included"][0]
    return {"version": 1, "dataset": dataset, "task": name,
            "task_dir": str(dataset_root(dataset) / name),
            "api_base": "http://127.0.0.1:12345/v1", "api_key": "synthetic-shim-token",
            "model": "openai/gpt-5-mini", "agent": "terminus-2",
            "agent_kwargs": agent_kwargs({"only": ["openai"], "order": ["openai"], "allow_fallbacks": False}),
            "env": {"ANYEVAL_TB_EGRESS_PROXY": "1"}, "namespace": "anyeval-sandbox",
            "kubeconfig": "/synthetic/kubeconfig", "timeouts": {"agent_sec": 120, "verifier_sec": 30},
            "attempt": 1, "run_id": "synthetic-run", "trial_id": "authorized-trial"}


@pytest.fixture
def spec_file(tmp_path, spec):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    return path


@pytest.fixture
def mini_registry(tmp_path, monkeypatch):
    """Independent synthetic bytes and pins; no upstream material is read."""
    integrity = importlib.import_module('terminal_bench_anyeval.eligibility')
    fetch = importlib.import_module('terminal_bench_anyeval.fetch_data')
    metadata = tmp_path / 'metadata'
    metadata.mkdir()
    registry = tmp_path / 'registry'
    records = {'version': 2, 'datasets': {}}
    for dataset, (version, short, folder) in integrity.DATASETS.items():
        included = ['synthetic-a', 'synthetic-b']
        excluded = [{'id': 'synthetic-excluded', 'reasons': ['storage>10GiB: synthetic']}] if short == '4.0' else []
        report = {'version': 1, 'dataset': dataset, 'dataset_version': version,
                  'included': included, 'excluded': excluded, 'total': len(included) + len(excluded)}
        tasks = {}
        for name in included + [x['id'] for x in excluded]:
            task = registry / folder / name
            (task / 'tests/nested').mkdir(parents=True)
            mode = 'separate' if short == '4.0' else 'shared'
            (task / 'task.toml').write_text(
                'version = "1.4"\n[metadata]\ncategory = "synthetic"\n'
                '[agent]\ntimeout_sec = 120\n'
                '[environment]\ndocker_image = "example/synthetic:1"\n'
                'cpus = 1\nmemory_mb = 1024\nstorage_mb = 10240\n'
                f'[verifier]\ntimeout_sec = 30\nenvironment_mode = "{mode}"\n'
                + ('[verifier.environment]\ndocker_image = "example/synthetic-verifier:1"\n'
                   'storage_mb = 10240\n' if mode == 'separate' else ''))
            (task / 'instruction.md').write_text('Synthetic catalogue input.\n')
            (task / 'tests/test.sh').write_text('#!/bin/sh\nexit 0\n')
            (task / 'tests/test.sh').chmod(0o755)
            (task / 'tests/nested/.fixture').write_bytes(b'synthetic verifier fixture\x00')
            (task / 'README.md').write_text('Synthetic legacy support file.\n')
            if name in included:
                tasks[name] = integrity.task_hashes(task)
            for omitted in ('environment', 'solution'):
                (task / omitted).mkdir()
                (task / omitted / 'omit.txt').write_text('Synthetic omitted bytes.\n')
            (task / 'untracked-support.txt').write_text('Prune this.\n')
        records['datasets'][dataset] = {'registry_version': version, 'tasks': tasks,
            'registry_digest': integrity.canonical_digest(tasks),
            'registry_digest_kind': 'synthetic-test-inventory', 'excluded_tasks': {}}
        (metadata / f'eligibility-{short}.json').write_text(json.dumps(report))
    (metadata / 'manifest.json').write_text(json.dumps(records))
    monkeypatch.setattr(integrity, 'DATA', metadata)
    monkeypatch.setenv('ANYEVAL_TB_DATA_DIR', str(tmp_path / 'runtime'))
    calls = []

    def download(dataset, destination):
        calls.append(dataset)
        source = registry / integrity.DATASETS[dataset][2]
        shutil.copytree(source, destination, symlinks=True)
        return destination

    monkeypatch.setattr(fetch, 'download_dataset', download)
    return registry, metadata, calls


@pytest.fixture
def synthetic_data(mini_registry):
    fetch = importlib.import_module('terminal_bench_anyeval.fetch_data')
    fetch.fetch_data()
    return mini_registry


@pytest.fixture
def pinned_registry_identity(monkeypatch):
    """Allow CLI tests to reach Harbor after a valid registry preflight."""
    from terminal_bench_anyeval import fetch_data as fetch

    def rows(table, query):
        assert table == "dataset_version"
        for dataset, reference in fetch.REGISTRY_VERSIONS.items():
            digest = reference.removeprefix("sha256:")
            if query["content_hash"] == "eq." + digest:
                return [{"id": fetch.REGISTRY_VERSION_IDS[dataset], "content_hash": digest}]
        raise AssertionError("Unpinned content-hash lookup")

    monkeypatch.setattr(fetch, "registry_rows", rows)
