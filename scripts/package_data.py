"""Package eligible prebuilt tasks without build contexts or solutions."""
import argparse
import json
from pathlib import Path
import shutil

from terminal_bench_anyeval.eligibility import (
    DATA, DATASETS, REGISTRY_VERSIONS, REGISTRY_VERSION_IDS, canonical_digest,
    hash_file, scan, task_hashes,
)


def environment_tree_hash(root):
    """Bind original download paths and bytes, including files we do not ship."""
    return canonical_digest({p.relative_to(root).as_posix(): hash_file(p)
                             for p in sorted(root.rglob("*")) if p.is_file()})


def package_dataset(source, dest, dataset):
    report = scan(source, dataset)
    included = set(report["included"])
    dropped, excluded = {}, {}
    for metadata in sorted(source.glob("*/task.toml")):
        task = metadata.parent
        context = task / "environment"
        if context.is_dir():
            # Harbor stages prebuilt contexts with no build spec. Refuse to
            # discard runtime inputs if a future download adds such a task.
            if (task.name in included and any(context.iterdir())
                    and not (context / "Dockerfile").exists()
                    and not (context / "docker-compose.yaml").exists()):
                raise ValueError(f"Runtime environment context requires review: {task.name}")
            dropped[task.name] = environment_tree_hash(context)
        if task.name not in included:
            excluded[task.name] = {"task.toml": hash_file(metadata)}

    # A repeated packaging run must remove stale excluded tasks/build contexts.
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name in report["included"]:
        task = source / name

        def ignore(directory, names):
            omitted = {"solution", "__pycache__", ".DS_Store", ".git"}
            if Path(directory) == task:
                omitted.add("environment")
            return omitted.intersection(names)

        shutil.copytree(task, dest / name, ignore=ignore)
    tasks = {name: task_hashes(dest / name) for name in report["included"]}
    return report, tasks, excluded, dropped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    sources = {"terminal-bench-2-1": "2.1.0-dl/terminal-bench-2-1",
               "terminal-bench@4.0.0": "4.0.0/terminal-bench"}
    manifest = {"version": 2, "license": "Apache-2.0",
                "environment_tree_digest_kind": "sha256 of canonical JSON mapping environment-relative POSIX filenames to file SHA256 hex digests",
                "datasets": {}}
    for dataset, (version, short, directory) in DATASETS.items():
        source = args.source / sources[dataset]
        dest = DATA / directory
        report, tasks, excluded, dropped = package_dataset(source, dest, dataset)
        # Download folders contain no upstream registry response. Do not invent one:
        # this digest binds the local registry inventory and the downloaded bytes.
        registry = {"name": "terminal-bench", "version": version,
                    "dataset": dataset, "task_hashes": tasks,
                    "excluded_tasks": excluded, "dropped_environment_trees": dropped}
        manifest["datasets"][dataset] = {
            "registry_version": REGISTRY_VERSIONS[dataset],
            "dataset_version_id": REGISTRY_VERSION_IDS[dataset],
            "registry_digest": canonical_digest(registry),
            "registry_digest_kind": "downloaded-task-inventory-sha256",
            "upstream_registry_digest": None,
            "upstream_registry_verified": False,
            "registry_digest_note": "Upstream registry response absent from supplied download; network unavailable at packaging.",
            "tasks": tasks,
            "excluded_tasks": excluded,
            "dropped_environment_trees": dropped,
        }
        (DATA / f"eligibility-{short}.json").write_text(json.dumps(report, indent=2) + "\n")
        print(dataset, "packaged", len(tasks), "excluded", len(excluded), "environment hashes", len(dropped))
    (DATA / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
