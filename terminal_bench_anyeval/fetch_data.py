"""Retrieve pinned Harbor material, or verify an existing runtime installation."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import tarfile
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .eligibility import (
    DATASETS, REGISTRY_VERSIONS, FETCH_HINT, data_root, eligibility, file_inventory, manifest,
    retained_file, verify_files,
)


def pinned_dataset(dataset: str) -> tuple[str, str, str]:
    """Resolve local catalogue aliases, rejecting every explicit unpinned ref."""
    name, separator, requested = dataset.partition("@")
    for key, version in REGISTRY_VERSIONS.items():
        if key.split("@", 1)[0] == name:
            if not version or version == "latest" or (separator and requested != version):
                raise ValueError(f"Requested registry version is not pinned: {dataset}")
            return key, name, version
    raise ValueError(f"Unknown pinned dataset: {dataset}")


def download_dataset(dataset: str, destination: Path) -> Path:
    """Harbor 0.22 export layout is <output-dir>/<short-name>/<task-name>."""
    _, name, version = pinned_dataset(dataset)
    reference = f"terminal-bench/{name}@{version}"
    # Prefer the CLI from this interpreter's environment over another PATH install.
    executable = Path(sys.executable).parent / "harbor"
    command = str(executable) if executable.is_file() else shutil.which("harbor")
    if command is None:
        return download_registry_dataset(dataset, destination)
    result = subprocess.run(
        [command, "dataset", "download", reference, "--export", "--output-dir", str(destination)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if result.returncode:
        # Harbor diagnostics can contain task material: never echo them.
        raise RuntimeError(f"Harbor download failed for {reference} (exit {result.returncode})")
    return destination / name


# Public package registry used by Harbor 0.22.0 (harbor.auth.constants).
REGISTRY_URL = "https://ofhuhcpkvzjlejydnvyd.supabase.co"
# Published anonymous API key, not a user credential.
REGISTRY_PUBLIC_KEY = "sb_publishable_Z-vuQbpvpG-PStjbh4yE0Q_e-d3MTIH"


def registry_open(path: str):
    return urlopen(Request(REGISTRY_URL + path, headers={
        "apikey": REGISTRY_PUBLIC_KEY,
    }), timeout=120)


def registry_rows(table: str, query: dict) -> list[dict]:
    rows = []
    while True:
        with registry_open("/rest/v1/" + table + "?" + urlencode({
            **query, "limit": 1000, "offset": len(rows),
        })) as response:
            page = json.load(response)
        if not isinstance(page, list):
            raise ValueError("Invalid registry response")
        rows.extend(page)
        if len(page) < 1000:
            return rows


def download_registry_dataset(dataset: str, destination: Path) -> Path:
    """Read public PostgREST metadata and gzip task archives using only HTTP.

    The trusted local manifest, not registry metadata, authenticates task bytes.
    See docker/worker-snippet.md for endpoints and interpreter setup.
    """
    dataset, name, version = pinned_dataset(dataset)
    query = {
        "package.name": "eq." + name,
        "package.type": "eq.dataset", "package.org.name": "eq.terminal-bench",
    }
    if version.startswith("sha256:"):
        versions = registry_rows("dataset_version", {
            **query,
            "select": "id,content_hash,package:package_id!inner(name,org:org_id!inner(name))",
            "content_hash": "eq." + version.removeprefix("sha256:"), "order": "id",
        })
        if len(versions) != 1 or versions[0].get("content_hash") != version.removeprefix("sha256:"):
            raise ValueError(f"Registry version does not match pinned version: {dataset}")
        version_id = versions[0].get("id")
    else:
        versions = registry_rows("dataset_version_tag", {
            **query,
            "select": "dataset_version:dataset_version_id(id),package:package_id!inner(name,org:org_id!inner(name))",
            "tag": "eq." + version, "order": "tag",
        })
        version_id = ((versions[0].get("dataset_version") or {}).get("id")
                      if len(versions) == 1 else None)
    if not version_id:
        raise ValueError(f"Registry dataset unavailable: {dataset}")
    rows = registry_rows("dataset_version_task", {
        "select": "task_version_id,task_version:task_version_id(archive_path,package:package_id(name))",
        "dataset_version_id": "eq." + version_id,
        "order": "task_version_id",
    })
    report = eligibility(dataset)
    allowed = set(report["included"]) | {t["id"] for t in report["excluded"]}
    tasks = {}
    for row in rows:
        version = row.get("task_version")
        if not version or not version.get("package"):
            raise ValueError(f"Registry task unavailable: {dataset}")
        task = version["package"]["name"]
        if task not in allowed or task in tasks:
            raise ValueError(f"Unexpected or duplicate registry task: {dataset}")
        tasks[task] = version["archive_path"]
    if not set(report["included"]) <= tasks.keys():
        raise ValueError(f"Missing registry tasks: {dataset}")
    root = destination / name
    root.mkdir(parents=True)
    records = manifest()["datasets"][dataset]["tasks"]
    for task in report["included"]:
        target = root / task
        target.mkdir()
        with tempfile.TemporaryFile() as archive:
            with registry_open("/storage/v1/object/packages/" + quote(tasks[task], safe="/")) as response:
                shutil.copyfileobj(response, archive)
            archive.seek(0)
            with tarfile.open(fileobj=archive, mode="r:gz") as bundle:
                seen = set()
                for member in bundle:
                    path = PurePosixPath(member.name)
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError(f"Unsafe archive path: {dataset}/{task}")
                    relative = path.as_posix()
                    if not (retained_file(relative) or relative in records[task]["files"]):
                        continue
                    if member.isdir():
                        continue
                    if not member.isfile() or relative in seen:
                        raise ValueError(f"Unsafe or duplicate archive file: {dataset}/{task}")
                    seen.add(relative)
                    output = target / relative
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.extractfile(member) as source, output.open("wb") as sink:
                        shutil.copyfileobj(source, sink)
    return root


def expected_files(dataset: str) -> dict[str, str]:
    tasks = manifest()["datasets"][dataset]["tasks"]
    if set(tasks) != set(eligibility(dataset)["included"]):
        raise ValueError(f"Manifest/eligibility task set mismatch: {dataset}")
    return {f"{task}/{name}": digest for task, record in tasks.items()
            for name, digest in record["files"].items() if retained_file(name)}


def verify_dataset(root: Path, dataset: str) -> tuple[int, int]:
    if not root.exists():
        raise FileNotFoundError(f"Terminal-Bench data absent: {root}; {FETCH_HINT}")
    files = file_inventory(root)
    expected = expected_files(dataset)
    verify_files(files, expected, dataset)
    task_names = set(eligibility(dataset)["included"])
    extra = {p.name for p in root.iterdir()} - task_names
    if extra:
        raise ValueError(f"Extra task or entry: {dataset}/{sorted(extra)[0]}")
    for name in task_names:
        for forbidden in ("environment", "solution"):
            if (root / name / forbidden).exists():
                raise ValueError(f"Forbidden directory: {dataset}/{name}/{forbidden}")
    return len(task_names), len(files)


def prune_dataset(source: Path, target: Path, dataset: str) -> None:
    """Validate the download before projecting it onto the runtime file set."""
    record = manifest()["datasets"][dataset]
    report = eligibility(dataset)
    expected_files(dataset)  # Check that both committed records agree.
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"Missing or unsafe dataset directory: {dataset}")
    allowed = set(report["included"]) | {t["id"] for t in report["excluded"]}
    extra = {p.name for p in source.iterdir() if p.is_dir() or p.is_symlink()} - allowed
    if extra:
        raise ValueError(f"Extra task: {dataset}/{sorted(extra)[0]}")
    target.mkdir(parents=True)
    for task in report["included"]:
        expected = record["tasks"][task]["files"]
        # The original manifest also pins READMEs. Check them before discarding
        # them; do not rewrite the committed provenance to fit runtime storage.
        task_root = source / task
        if task_root.is_symlink() or not task_root.is_dir():
            raise ValueError(f"Missing or unsafe task directory: {dataset}/{task}")
        files = {}
        for path in task_root.iterdir():
            if path.name not in {"tests", "task.toml", "instruction.md"} and path.name not in expected:
                continue
            if path.is_symlink():
                raise ValueError(f"Unsafe file: {dataset}/{task}/{path.name}")
            if path.is_dir():
                files.update({f"{path.name}/{n}": p for n, p in file_inventory(path).items()})
            elif path.is_file():
                files[path.name] = path
            else:
                raise ValueError(f"Unsafe file: {dataset}/{task}/{path.name}")
        selected = {n: p for n, p in files.items() if retained_file(n) or n in expected}
        verify_files(selected, expected, f"{dataset}/{task}")
        for name, path in selected.items():
            if retained_file(name):
                dest = target / task / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
    verify_dataset(target, dataset)


def fetch_data(*, verify_only: bool = False) -> None:
    root = data_root()
    if verify_only:
        for dataset, (_, _, directory) in DATASETS.items():
            tasks, files = verify_dataset(root / directory, dataset)
            print(f"{dataset}: verified {tasks} tasks, {files} files")
        return
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".fetch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Stage every missing dataset before installing any of them. Existing
        # data is always checked, never silently trusted or overwritten.
        with tempfile.TemporaryDirectory(prefix=".fetch-", dir=root) as work:
            work = Path(work)
            pending = []
            for dataset, (_, _, directory) in DATASETS.items():
                destination = root / directory
                if destination.exists() or destination.is_symlink():
                    tasks, files = verify_dataset(destination, dataset)
                    print(f"{dataset}: verified {tasks} tasks, {files} files (already installed)")
                    continue
                source = download_dataset(dataset, work / "downloads" / directory)
                staged = work / "staged" / directory
                prune_dataset(source, staged, dataset)
                pending.append((dataset, staged, destination))
            for dataset, staged, destination in pending:
                os.replace(staged, destination)
                tasks, files = verify_dataset(destination, dataset)
                print(f"{dataset}: installed {tasks} tasks, {files} files; "
                      f"excluded {len(eligibility(dataset)['excluded'])} tasks")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true", help="Verify without downloading or writing")
    args = parser.parse_args(argv)
    try:
        fetch_data(verify_only=args.verify_only)
    except (OSError, ValueError, RuntimeError, tarfile.TarError) as exc:
        print(f"Terminal-Bench data error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
