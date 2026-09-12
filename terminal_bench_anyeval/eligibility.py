"""Deterministic eligibility and integrity checks; never render task material."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tomllib

DATA = Path(__file__).resolve().parent / "data"
DATASETS = {
    "terminal-bench-2-1": ("2.1.0", "2.1", "terminal-bench-2-1"),
    "terminal-bench@4.0.0": ("4.0.0", "4.0", "terminal-bench-4-0"),
}
# Harbor content reference reconstructed from the complete 89-task 2.1 export.
# See data/manifest.json for provenance; this is not the local inventory digest.
REGISTRY_VERSIONS = {
    "terminal-bench-2-1": "sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a",
    "terminal-bench@4.0.0": "4.0.0",
}
COMPOSE_NAMES = {"docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml"}
MAX_STORAGE_MB = 10240
FETCH_HINT = "run python -m terminal_bench_anyeval.fetch_data"


def data_root() -> Path:
    """Runtime bytes live separately from the trusted, package-local manifests."""
    override = os.environ.get("ANYEVAL_TB_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.access(DATA, os.W_OK):
        return DATA
    return Path.home() / ".cache" / "anyeval" / "terminal-bench"


def dataset_root(dataset: str) -> Path:
    root = data_root() / DATASETS[dataset][2]
    if not root.is_dir():
        raise FileNotFoundError(f"Terminal-Bench data absent: {root}; {FETCH_HINT}")
    return root


def task_directory(dataset: str, task_id: str) -> Path:
    directory = dataset_root(dataset) / task_id
    for name in ("task.toml", "instruction.md"):
        if not (directory / name).is_file():
            raise FileNotFoundError(f"Terminal-Bench data absent: {directory / name}; {FETCH_HINT}")
    return directory


def scan(root: Path, dataset: str) -> dict:
    version, _, _ = DATASETS[dataset]
    included, excluded = [], []
    paths = sorted(Path(root).glob("*/task.toml"))
    if not paths:
        raise ValueError("No task metadata found")
    for path in paths:
        config = tomllib.loads(path.read_text())
        reasons = []
        envs = [("agent", config.get("environment") or {})]
        verifier = config.get("verifier") or {}
        if verifier.get("environment_mode", "shared") == "separate":
            envs.append(("verifier", verifier.get("environment") or {}))
        for role, env in envs:
            if env.get("gpus") or env.get("gpu_types") or env.get("tpu"):
                reasons.append(f"gpu: {role} requests accelerator resources")
            size = env.get("storage_mb", MAX_STORAGE_MB)
            if isinstance(size, bool) or not isinstance(size, (int, float)) or size <= 0:
                reasons.append(f"storage>10GiB: {role} has invalid storage_mb")
            elif size > MAX_STORAGE_MB:
                reasons.append(f"storage>10GiB: {role} requests {size} MiB")
            image = env.get("docker_image")
            if not isinstance(image, str) or not image.strip():
                reasons.append(f"no prebuilt image: {role}")
        if any(p.name in COMPOSE_NAMES for p in path.parent.rglob("*")
               if "solution" not in p.relative_to(path.parent).parts):
            reasons.append("compose: Compose manifest present")
        if reasons:
            excluded.append({"id": path.parent.name, "reasons": reasons})
        else:
            included.append(path.parent.name)
    return {"version": 1, "dataset": dataset, "dataset_version": version,
            "registry_version": REGISTRY_VERSIONS[dataset],
            "total": len(paths), "included": included, "excluded": excluded}


def eligibility(dataset: str) -> dict:
    return json.loads((DATA / f"eligibility-{DATASETS[dataset][1]}.json").read_text())


def manifest() -> dict:
    return json.loads((DATA / "manifest.json").read_text())


def canonical_digest(value) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def hash_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def task_hashes(root: Path) -> dict:
    files = {p.relative_to(root).as_posix(): hash_file(p)
             for p in sorted(root.rglob("*")) if p.is_file()}
    tests = {name: digest for name, digest in files.items() if name.startswith("tests/")}
    return {"task.toml": files["task.toml"], "instruction.md": files["instruction.md"],
            "tests": canonical_digest(tests), "files": files}


def retained_file(name: str) -> bool:
    return name in {"task.toml", "instruction.md"} or name.startswith("tests/")


def file_inventory(root: Path) -> dict[str, Path]:
    """Reject links and special files instead of hashing through them."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Missing or unsafe directory: {root}")
    files = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError(f"Unsafe file: {root.name}/{name}")
        if path.is_file():
            files[name] = path
    return files


def verify_files(files: dict[str, Path], expected: dict[str, str], label: str) -> None:
    missing, extra = set(expected) - set(files), set(files) - set(expected)
    if missing:
        raise ValueError(f"Missing file: {label}/{sorted(missing)[0]}")
    if extra:
        raise ValueError(f"Extra file: {label}/{sorted(extra)[0]}")
    for name, digest in expected.items():
        if hash_file(files[name]) != digest:
            raise ValueError(f"SHA256 mismatch: {label}/{name}")


def verify_task(root: Path, dataset: str, task_id: str) -> None:
    expected = manifest()["datasets"][dataset]["tasks"][task_id]["files"]
    verify_files(file_inventory(root), {n: h for n, h in expected.items() if retained_file(n)},
                 f"{dataset}/{task_id}")
