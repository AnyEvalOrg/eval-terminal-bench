"""Inspect catalogue only: execution belongs to the AnyEval Harbor child."""
import tomllib

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.solver import solver

from .eligibility import DATASETS, eligibility, manifest, task_directory


@solver
def harbor_only():
    async def solve(state, generate):
        raise RuntimeError(
            "Terminal-Bench requires AnyEval execution: harbor. Plain Inspect cannot "
            "run this catalogue; use python -m terminal_bench_anyeval.trial --spec "
            "<spec.json> --result <result.json> through the AnyEval runner.")
    return solve


def make_task(dataset: str) -> Task:
    provenance = manifest()["datasets"][dataset]
    samples = []
    for name in eligibility(dataset)["included"]:
        directory = task_directory(dataset, name)
        config = tomllib.loads((directory / "task.toml").read_text())
        verifier = config.get("verifier", {})
        mode = verifier.get("environment_mode", "shared")
        def resources(env):
            return {key: env[key] for key in ("cpus", "memory_mb", "storage_mb", "gpus", "docker_image")
                    if key in env}
        metadata = {
            "category": config.get("metadata", {}).get("category"),
            "timeouts": {"agent_sec": config["agent"].get("timeout_sec"),
                         "verifier_sec": verifier.get("timeout_sec"),
                         "build_sec": config["environment"].get("build_timeout_sec")},
            "resources": {"agent": resources(config["environment"]),
                          "verifier": resources(verifier.get("environment", config["environment"]))},
            "verifier_mode": mode,
            "dataset": dataset, "dataset_version": DATASETS[dataset][0],
            "registry_digest": provenance["registry_digest"],
            "registry_digest_kind": provenance["registry_digest_kind"],
            "task_dir": str(directory), "execution": "harbor",
        }
        samples.append(Sample(id=name, input=(directory / "instruction.md").read_text(), metadata=metadata))
    return Task(dataset=MemoryDataset(samples=samples, name=dataset), solver=harbor_only(),
                sandbox=None, epochs=1, version="1.0.0", metadata={"execution": "harbor"})


@task
def terminal_bench_2_1() -> Task:
    return make_task("terminal-bench-2-1")


@task
def terminal_bench_4_0() -> Task:
    return make_task("terminal-bench@4.0.0")
