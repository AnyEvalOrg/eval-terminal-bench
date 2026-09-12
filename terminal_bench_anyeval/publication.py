"""File publication boundary in addition to AnyEval's key-based declaration.

The YAML consumer does not interpret filesystem globs. Never export a Harbor
trial directory recursively: only explicitly selected trajectory JSON files
are public; all other files (tests, verifier, recordings, logs, artifacts,
configs and exceptions) are private. The transcript itself remains public.
"""
from pathlib import Path
import re

_TRAJECTORY = re.compile(r"trajectory(?:\.cont-\d+|\.summarization-\d+-(?:summary|questions|answers))?\.json")


def public_artifact_paths(trial_dir: Path) -> list[Path]:
    root = Path(trial_dir).resolve()
    agent = root / "agent"
    return sorted(p for p in agent.glob("trajectory*.json")
                  if _TRAJECTORY.fullmatch(p.name) and p.is_file() and not p.is_symlink()
                  and p.resolve().parent == agent)


def partition_artifacts(trial_dir: Path) -> dict:
    root = Path(trial_dir).resolve()
    public = set(public_artifact_paths(root))
    return {"trajectory": [str(p) for p in sorted(public)],
            "private_artifacts": [str(p) for p in sorted(root.rglob("*"))
                                  if p.is_file() and p not in public]}
