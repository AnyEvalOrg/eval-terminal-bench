"""Compatibility alias for existing spike callers."""
import sys
from terminal_bench_anyeval import verifier as _implementation
sys.modules[__name__] = _implementation
