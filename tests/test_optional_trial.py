"""Exercise the dependency-free registry transport through the standard-library HTTP boundary."""
import io
import json
from pathlib import Path
import tarfile
from urllib.parse import parse_qs, urlsplit

import pytest

from terminal_bench_anyeval import fetch_data as fetch
from terminal_bench_anyeval.eligibility import hash_file


@pytest.mark.parametrize("fault", [None, "drift", "extra", "symlink", "traversal"])
def test_http_fallback_verifies_before_install(tmp_path, monkeypatch, fault):
    source = tmp_path / "source"
    source.mkdir()
    names = {"task.toml": b"version = 1", "instruction.md": b"synthetic prompt",
             "tests/check.txt": b"synthetic verifier", "README.md": b"synthetic readme"}
    hashes = {}
    for name, data in names.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        hashes[name] = hash_file(path)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        for name, data in names.items():
            if fault == "drift" and name == "task.toml":
                data += b"changed"
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            if fault == "symlink" and name == "instruction.md":
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/etc/passwd"
                entry.size = 0
            bundle.addfile(entry, io.BytesIO(data))
        if fault in {"extra", "traversal"}:
            entry = tarfile.TarInfo("tests/extra" if fault == "extra" else "../escape")
            bundle.addfile(entry, io.BytesIO())
    requests = []

    def http_response(request, timeout):
        parsed = urlsplit(request.full_url)
        requests.append(parsed.path)
        assert timeout == 120
        assert request.get_header("Apikey") == fetch.REGISTRY_PUBLIC_KEY
        query = parse_qs(parsed.query)
        if parsed.path == '/rest/v1/dataset_version':
            assert query['content_hash'] == ['eq.7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a']
            body = json.dumps([{'id': 'f92eea12-ff70-4d30-ace0-003abf294998', 'content_hash': '7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a'}]).encode()
        elif parsed.path == "/rest/v1/dataset_version_task":
            assert query["dataset_version_id"] == ["eq.f92eea12-ff70-4d30-ace0-003abf294998"]
            body = json.dumps([{"task_version": {
                "package": {"name": "sample"}, "archive_path": "sample/archive.tar.gz"
            }}]).encode()
        elif parsed.path == "/storage/v1/object/packages/sample/archive.tar.gz":
            body = archive.getvalue()
        else:
            raise AssertionError("Unexpected HTTP endpoint")
        return io.BytesIO(body)

    monkeypatch.setattr(fetch, "urlopen", http_response)
    dataset = "terminal-bench-2-1"
    root = tmp_path / "installed"
    monkeypatch.setenv("ANYEVAL_TB_DATA_DIR", str(root))
    monkeypatch.setattr(fetch.sys, "executable", str(tmp_path / "base/bin/python"))
    monkeypatch.setattr(fetch.shutil, "which", lambda name: None)
    monkeypatch.setattr(fetch, "DATASETS", {dataset: ("2.1.0", "2.1", dataset)})
    monkeypatch.setattr(fetch, "manifest", lambda: {"datasets": {
        dataset: {"tasks": {"sample": {"files": hashes}}}
    }})
    monkeypatch.setattr(fetch, "eligibility", lambda dataset: {"included": ["sample"], "excluded": []})
    if fault:
        with pytest.raises(ValueError):
            fetch.fetch_data()
        assert not (root / dataset).exists()
        assert not (tmp_path / "escape").exists()
    else:
        fetch.fetch_data()
        assert fetch.verify_dataset(root / dataset, dataset) == (1, 3)
        assert not (root / dataset / "sample/README.md").exists()
        before = len(requests)
        fetch.fetch_data(verify_only=True)
        assert len(requests) == before
    assert len(requests) == 3


def test_registry_pagination_and_content_hash(tmp_path, monkeypatch):
    pages = [[{"id": n} for n in range(1000)], [{"id": 1000}]]
    paths = []
    def response(path):
        paths.append(path)
        return io.BytesIO(json.dumps(pages.pop(0)).encode())
    monkeypatch.setattr(fetch, "registry_open", response)
    assert len(fetch.registry_rows("dataset_version_task", {"order": "task_version_id"})) == 1001
    assert parse_qs(urlsplit(paths[1]).query)["offset"] == ["1000"]

    def rows(table, query):
        assert table == "dataset_version"
        assert query["content_hash"] == "eq.39d9f44b40420cde8fdcc087579c0d72a7e14fa3656d603c3f0d22fb35e27732"
        assert query["package.name"] == "eq.terminal-bench"
        return []
    monkeypatch.setattr(fetch, "registry_rows", rows)
    with pytest.raises(ValueError, match="pinned version"):
        fetch.download_registry_dataset("terminal-bench@4.0.0", tmp_path)
