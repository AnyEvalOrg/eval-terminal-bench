"""Opt-in cache relocation for the sandbox; leaves Harbor's CLI/runner intact.

Harbor 0.22 hardcodes ~/.cache/harbor, including a first-run notification write
before job creation. This workspace cannot write there. No HOME override needed.
"""
import os
from importlib.util import find_spec
from pathlib import Path

# Kubernetes credential helpers inherit PYTHONPATH but use another interpreter.
# Apply this relocation only where Harbor is actually installed.
if (directory := os.environ.get("HARBOR_SPIKE_CACHE_DIR")) and find_spec("harbor") is not None:
    import harbor.constants as constants
    old = constants.CACHE_DIR
    new = Path(directory).resolve()
    for name, value in list(vars(constants).items()):
        if isinstance(value, Path) and value.is_relative_to(old):
            setattr(constants, name, new / value.relative_to(old))
