"""Actionable diagnostics for the isolated trial interpreter."""
from contextlib import contextmanager


@contextmanager
def trial_imports():
    try:
        yield
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] not in {"harbor", "kubernetes", "litellm"}:
            raise
        raise RuntimeError(
            "Trial execution requires eval-terminal-bench[trial]; install it in "
            "/opt/anyeval/harbor-venv and use that interpreter for the trial child."
        ) from exc
