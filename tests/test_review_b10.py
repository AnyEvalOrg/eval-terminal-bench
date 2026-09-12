"""Preflight registry identity before allowing Harbor to enumerate anything."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from terminal_bench_anyeval import fetch_data as fetch


@pytest.mark.parametrize("dataset", fetch.REGISTRY_VERSIONS)
@pytest.mark.parametrize("mismatch", ["id", "content_hash", "both"])
def test_b10_cli_identity_mismatch_never_invokes_harbor(dataset, mismatch, monkeypatch, tmp_path):
    reference = fetch.REGISTRY_VERSIONS[dataset]
    digest = reference.removeprefix("sha256:")
    row = {"id": fetch.REGISTRY_VERSION_IDS[dataset], "content_hash": digest}
    if mismatch in ("id", "both"):
        row["id"] = "untrusted-version-id"
    if mismatch in ("content_hash", "both"):
        row["content_hash"] = "untrusted-content-hash"
    lookups = []

    def registry_rows(table, query):
        lookups.append(table)
        assert table == "dataset_version", "Tasks must not be enumerated"
        assert query["content_hash"] == "eq." + digest
        assert query["package.name"] == "eq." + dataset.split("@", 1)[0]
        assert query["package.type"] == "eq.dataset"
        assert query["package.org.name"] == "eq.terminal-bench"
        return [row]

    harbor = Mock(return_value=type("Result", (), {"returncode": 0})())
    monkeypatch.setattr(fetch, "registry_rows", registry_rows)
    monkeypatch.setattr(fetch.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(fetch.subprocess, "run", harbor)
    destination = tmp_path / "download"
    with pytest.raises(ValueError, match="Registry version does not match pinned version"):
        fetch.download_dataset(dataset, destination)
    harbor.assert_not_called()
    assert lookups == ["dataset_version"]
    assert list(tmp_path.iterdir()) == []
