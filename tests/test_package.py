import asyncio
import importlib
from importlib.metadata import entry_points, version
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile

import pytest
from terminal_bench_anyeval.eligibility import DATA, DATASETS, canonical_digest, dataset_root, eligibility, manifest, scan, task_hashes


@pytest.mark.parametrize("dataset", DATASETS)
def test_packaged_eligibility_is_fresh(dataset, synthetic_data):
    packaged = scan(dataset_root(dataset), dataset)
    assert packaged['included'] == eligibility(dataset)['included']
    assert packaged['excluded'] == []
    assert packaged['total'] == len(eligibility(dataset)['included'])


@pytest.mark.parametrize("dataset", DATASETS)
def test_manifest_hashes_every_retained_file(dataset):
    record = manifest()["datasets"][dataset]
    assert set(record["tasks"]) == set(eligibility(dataset)['included'])
    for name, hashes in record["tasks"].items():
        assert hashes['task.toml'] == hashes['files']['task.toml']
        assert hashes['instruction.md'] == hashes['files']['instruction.md']
        assert hashes['tests'] == canonical_digest({n: h for n, h in hashes['files'].items() if n.startswith('tests/')})
        assert all(len(bytes.fromhex(h)) == 32 for h in hashes['files'].values())
    inventory = {"name": "terminal-bench", "version": DATASETS[dataset][0], "dataset": dataset,
                 "task_hashes": record["tasks"], "excluded_tasks": record['excluded_tasks'],
                 "dropped_environment_trees": record['dropped_environment_trees']}
    assert canonical_digest(inventory) == record["registry_digest"]
    exclusions = eligibility(dataset)['excluded']
    assert set(record['excluded_tasks']) == {entry['id'] for entry in exclusions}
    assert set(record['dropped_environment_trees']) == set(record['tasks']) | set(record['excluded_tasks'])
    for name, hashes in record['excluded_tasks'].items():
        assert set(hashes) == {'task.toml'}
        assert len(bytes.fromhex(hashes['task.toml'])) == 32
    for digest in record['dropped_environment_trees'].values():
        assert digest.startswith('sha256:') and len(bytes.fromhex(digest[7:])) == 32


def test_original_excluded_ids_and_reasons_are_preserved():
    report = eligibility('terminal-bench@4.0.0')
    assert report['total'] == 66
    assert {entry['id'] for entry in report['excluded']} == {
        'biped-contact-dynamics', 'ctr-optimization', 'cumulative-layout-shift',
        'distributed-dedup', 'fp8-rmsnorm-gemm', 'freight-dispatch-shift',
        'heat-pump-warranty', 'intrastat-meldung', 'jax-speedrun-gpu',
        'kv-live-surgery', 'lake-temp-glm', 'legacy-utility-triage',
        'live-database-cutover', 'math-eval-grader', 'medical-claims-processing',
        'nextjs-performance', 'payments-pipeline-fix', 'protein-autointerp-disulfide',
        'sglang-qwen-burst', 'takens-embedding-lean', 'vba-userform-port',
        'vpp-loss-divergence',
    }
    assert all(entry['reasons'] for entry in report['excluded'])
    assert eligibility('terminal-bench-2-1')['excluded'] == []


@pytest.mark.parametrize("dataset,factory", [("terminal-bench-2-1", "terminal_bench_2_1"),
                                            ("terminal-bench@4.0.0", "terminal_bench_4_0")])
def test_inspect_constructs_catalogue(dataset, factory, synthetic_data):
    module = importlib.import_module("terminal_bench_anyeval.task")
    task = getattr(module, factory)()
    assert [s.id for s in task.dataset] == eligibility(dataset)["included"]
    assert task.sandbox is None
    for sample in task.dataset:
        assert sample.sandbox is None
        raw = (dataset_root(dataset) / sample.id / "instruction.md").read_text()
        # Boolean assertion avoids dumping benchmark content on failure.
        assert bool(sample.input == raw), "Instruction bytes differ (contents suppressed)"
        assert set(sample.metadata) == {"category", "timeouts", "resources", "verifier_mode", "dataset",
                                        "dataset_version", "registry_digest", "registry_digest_kind", "task_dir", "execution"}
        assert sample.metadata["verifier_mode"] == ("separate" if "4.0" in dataset else "shared")
    with pytest.raises(RuntimeError, match="execution: harbor"):
        asyncio.run(module.harbor_only()(None, None))


def test_entrypoint_and_pins():
    assert version("inspect_ai") == "0.3.260"
    assert version("harbor") == "0.22.0"
    assert version("kubernetes") == "36.0.3"
    points = entry_points(group="inspect_ai")
    point = next(e for e in points if e.name == "terminal_bench_anyeval")
    assert point.load().terminal_bench_2_1


def test_no_solutions_in_package_tree():
    assert not any(p.name == "solution" for p in DATA.rglob("*"))


def test_no_build_contexts_or_unexpected_large_files():
    assert not list(DATA.glob('*/*/environment'))
    large = {p.relative_to(DATA).as_posix() for p in DATA.rglob('*')
             if p.is_file() and p.stat().st_size > 50_000_000}
    assert large == set()
    assert not (DATA / 'terminal-bench-2-1').exists()
    assert not (DATA / 'terminal-bench-4-0').exists()


@pytest.mark.parametrize("change,reason", [
    ('gpus = 1', 'gpu:'), ('storage_mb = 10241', 'storage>10GiB:'),
    ('docker_image = ""', 'no prebuilt image:'), ('compose', 'compose:'),
])
def test_eligibility_exclusions_are_explicit(tmp_path, change, reason):
    root = tmp_path / "task"
    root.mkdir()
    values = {'storage_mb': '10240', 'docker_image': '"synthetic:1"', 'gpus': '0'}
    if change != 'compose':
        key, value = change.split(' = ')
        values[key] = value
    text = '[environment]\n' + '\n'.join(f'{k} = {v}' for k, v in values.items())
    (root / 'task.toml').write_text(text)
    if change == 'compose':
        (root / 'compose.yaml').touch()
    report = scan(tmp_path, 'terminal-bench-2-1')
    assert not report['included']
    assert report['excluded'][0]['reasons'][0].startswith(reason)


def test_separate_verifier_is_checked_and_shared_needs_no_second_image(tmp_path):
    directory = tmp_path / 'task'
    directory.mkdir()
    metadata = directory / 'task.toml'
    metadata.write_text('[environment]\ndocker_image="synthetic"\nstorage_mb=10240\n')
    assert scan(tmp_path, 'terminal-bench-2-1')['included'] == ['task']
    with metadata.open('a') as stream:
        stream.write('\n[verifier]\nenvironment_mode="separate"\n')
    assert 'no prebuilt image: verifier' in scan(tmp_path, 'terminal-bench@4.0.0')['excluded'][0]['reasons']


@pytest.fixture(scope="session")
def distribution_source(tmp_path_factory):
    source = tmp_path_factory.mktemp('distribution-source')
    checkout = Path(__file__).resolve().parents[1]
    for name in ('pyproject.toml', 'README.md', 'MANIFEST.in'):
        shutil.copy2(checkout / name, source / name)
    shutil.copytree(DATA.parent, source / 'terminal_bench_anyeval',
                    ignore=shutil.ignore_patterns('__pycache__'))
    # Simulate a fetch into the package directory before building distributions.
    for folder in ('terminal-bench-2-1', 'terminal-bench-4-0'):
        task = source / 'terminal_bench_anyeval/data' / folder / 'synthetic'
        (task / 'tests/nested').mkdir(parents=True)
        (task / 'tests/nested/.sentinel').write_text('synthetic bytes excluded from distribution')
        (task / 'task.toml').write_text('synthetic bytes excluded from distribution')
    return source


@pytest.fixture(scope="session")
def wheel_path(tmp_path_factory, distribution_source):
    out = tmp_path_factory.mktemp('wheel')
    built = subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                            '--no-index', '-w', str(out), '.'], cwd=distribution_source, capture_output=True)
    assert built.returncode == 0, 'Wheel build failed (output suppressed; rerun pip wheel to diagnose)'
    return next(out.glob('*.whl'))


def test_sdist_excludes_fetched_task_trees(distribution_source, tmp_path):
    code = 'from setuptools.build_meta import build_sdist; import sys; build_sdist(sys.argv[1])'
    proc = subprocess.run([sys.executable, '-c', code, str(tmp_path)],
                          cwd=distribution_source, capture_output=True)
    assert proc.returncode == 0, 'Source distribution build failed (output suppressed)'
    with tarfile.open(next(tmp_path.glob('*.tar.gz'))) as archive:
        names = archive.getnames()
        assert not any('/data/terminal-bench-2-1/' in n or '/data/terminal-bench-4-0/' in n for n in names)
        assert sum(n.endswith('/data/manifest.json') for n in names) == 1


def test_wheel_contains_only_manifests_and_no_task_data(wheel_path):
    with zipfile.ZipFile(wheel_path) as wheel:
        names = set(wheel.namelist())
        assert not any('solution' in Path(n).parts for n in names)
        assert not any('__pycache__' in Path(n).parts for n in names)
        assert not any('environment' in Path(n).parts for n in names)
        wheel_data = {n for n in names if n.startswith('terminal_bench_anyeval/data/')}
        source_data = {p.relative_to(DATA.parent.parent).as_posix()
                       for p in DATA.rglob('*') if p.is_file()}
        assert wheel_data == source_data
        for p in DATA.rglob('*'):
            if p.is_file():
                assert p.relative_to(DATA.parent.parent).as_posix() in names
        for name in ('redaction.yaml', 'anyeval.json', 'k8s/allowlist.yaml'):
            assert 'terminal_bench_anyeval/' + name in names
        assert not any(n.endswith('/task.toml') or '/tests/' in n for n in names)
        assert len(wheel_data) == 4
        assert 'terminal_bench_anyeval/fetch_data.py' in names


def test_installed_wheel_works_away_from_checkout(wheel_path, tmp_path, synthetic_data):
    target = tmp_path / 'installed'
    proc = subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', '--no-index',
                           '--target', str(target), str(wheel_path)], capture_output=True)
    assert proc.returncode == 0
    code = '''
import sys, importlib
from pathlib import Path
sys.path.insert(0, sys.argv[1])
integrity = importlib.import_module('terminal_bench_anyeval.eligibility')
integrity.DATA = Path(sys.argv[2])
from terminal_bench_anyeval.task import terminal_bench_2_1, terminal_bench_4_0
from terminal_bench_anyeval.eligibility import eligibility
from terminal_bench_anyeval.iron_proxy import load_allowlist
assert [s.id for s in terminal_bench_2_1().dataset] == eligibility('terminal-bench-2-1')['included']
assert [s.id for s in terminal_bench_4_0().dataset] == eligibility('terminal-bench@4.0.0')['included']
assert load_allowlist()['version']
'''
    proc = subprocess.run([sys.executable, '-c', code, str(target), str(synthetic_data[1])], cwd=tmp_path, capture_output=True)
    assert proc.returncode == 0, 'Installed wheel smoke check failed (contents suppressed)'
