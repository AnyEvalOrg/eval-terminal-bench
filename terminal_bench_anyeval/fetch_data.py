"""Retrieve pinned Harbor material, or verify an existing runtime installation."""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .eligibility import (
    DATASETS, FETCH_HINT, data_root, eligibility, file_inventory, manifest,
    retained_file, verify_files,
)


def download_dataset(dataset: str, destination: Path) -> Path:
    """Harbor 0.22 export layout is <output-dir>/<short-name>/<task-name>."""
    name = dataset.split("@", 1)[0]
    # 2.1 has its own named dataset; 4.0 uses a versioned registry handle.
    # The committed file hashes pin bytes even if the named dataset drifts.
    reference = f"terminal-bench/{dataset}"
    # Prefer the CLI from this interpreter's environment over another PATH install.
    executable = Path(sys.executable).parent / "harbor"
    command = str(executable) if executable.is_file() else "harbor"
    result = subprocess.run(
        [command, "dataset", "download", reference, "--export", "--output-dir", str(destination)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if result.returncode:
        # Harbor diagnostics can contain task material: never echo them.
        raise RuntimeError(f"Harbor download failed for {reference} (exit {result.returncode})")
    return destination / name


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
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Terminal-Bench data error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
