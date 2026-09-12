"""Registry identity must be fixed before any download side effects."""
import hashlib
import subprocess

import pytest

from terminal_bench_anyeval import fetch_data as fetch
from terminal_bench_anyeval.eligibility import REGISTRY_VERSIONS, eligibility, manifest


@pytest.mark.parametrize("downloader", [fetch.download_dataset, fetch.download_registry_dataset])
@pytest.mark.parametrize("reference", [
    "terminal-bench-2-1@latest", "terminal-bench-2-1@2.1.0",
    "terminal-bench-2-1@wrong", "terminal-bench-2-1@",
    "terminal-bench-2-1@sha256:" + "0" * 64,
    "terminal-bench@latest", "terminal-bench@4.1.0",
])
def test_requested_version_must_be_pinned(monkeypatch, tmp_path, downloader, reference):
    def unexpected(*args, **kwargs):
        pytest.fail("Unpinned request reached a download side effect")
    monkeypatch.setattr(fetch.subprocess, "run", unexpected)
    monkeypatch.setattr(fetch, "registry_rows", unexpected)
    destination = tmp_path / "download"
    with pytest.raises(ValueError, match="not pinned"):
        downloader(reference, destination)
    assert not destination.exists()


def test_cli_accepts_explicit_content_reference(pinned_registry_identity, monkeypatch, tmp_path):
    version = REGISTRY_VERSIONS["terminal-bench-2-1"]
    calls = []
    monkeypatch.setattr(fetch.subprocess, "run", lambda args, **kwargs:
                        calls.append(args) or subprocess.CompletedProcess(args, 0))
    monkeypatch.setattr(fetch.shutil, "which", lambda name: "/fake/harbor")
    assert fetch.download_dataset("terminal-bench-2-1@" + version, tmp_path) == tmp_path / "terminal-bench-2-1"
    assert calls[0][7] == "terminal-bench/terminal-bench-2-1@" + version


def test_http_binds_content_reference_and_resolved_id(monkeypatch, tmp_path):
    dataset = "terminal-bench-2-1"
    digest = REGISTRY_VERSIONS[dataset].removeprefix("sha256:")
    calls = []
    def rows(table, query):
        calls.append(table)
        if table == "dataset_version":
            assert query["content_hash"] == "eq." + digest
            assert query["package.name"] == "eq." + dataset
            assert query["package.org.name"] == "eq.terminal-bench"
            assert query["package.type"] == "eq.dataset"
            assert "tag" not in query
            return [{"id": "f92eea12-ff70-4d30-ace0-003abf294998", "content_hash": digest}]
        assert table == "dataset_version_task"
        assert query["dataset_version_id"] == "eq.f92eea12-ff70-4d30-ace0-003abf294998"
        return []
    monkeypatch.setattr(fetch, "registry_rows", rows)
    monkeypatch.setattr(fetch, "eligibility", lambda key: {"included": [], "excluded": []})
    monkeypatch.setattr(fetch, "manifest", lambda: {"datasets": {dataset: {"tasks": {}}}})
    assert fetch.download_registry_dataset(dataset + "@sha256:" + digest, tmp_path).is_dir()
    assert calls == ["dataset_version", "dataset_version_task"]


@pytest.mark.parametrize("response", [[], [{"id": "wrong", "content_hash": "0" * 64}],
                                      [{"content_hash": REGISTRY_VERSIONS["terminal-bench-2-1"][7:]}]])
def test_http_refuses_unavailable_or_mismatched_identity(monkeypatch, tmp_path, response):
    def rows(table, query):
        assert table == "dataset_version"
        return response
    monkeypatch.setattr(fetch, "registry_rows", rows)
    destination = tmp_path / "download"
    with pytest.raises(ValueError):
        fetch.download_registry_dataset("terminal-bench-2-1", destination)
    assert not destination.exists()


def test_manifest_and_eligibility_record_pinned_identity():
    records = manifest()["datasets"]
    for dataset, version in REGISTRY_VERSIONS.items():
        assert version != "latest"
        assert records[dataset]["registry_version"] == version
        assert eligibility(dataset)["registry_version"] == version
    record = records["terminal-bench-2-1"]
    hashes = record["registry_version_provenance"]["task_content_hashes"]
    assert len(hashes) == eligibility("terminal-bench-2-1")["total"] == 89
    # Registry content hashes include more than the local task inventory.
    assert record["registry_version"] == "sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a"
