"""Compatibility alias for existing spike callers."""
import sys
from terminal_bench_anyeval import k8s_env as _implementation
sys.modules[__name__] = _implementation
