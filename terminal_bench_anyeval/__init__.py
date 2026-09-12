"""Terminal-Bench catalogue and one-trial AnyEval execution protocol."""
__version__ = "1.0.0"
try:
    from .task import terminal_bench_2_1, terminal_bench_4_0
except ModuleNotFoundError as exc:
    if exc.name != "inspect_ai":
        raise
    __all__ = []
else:
    __all__ = ["terminal_bench_2_1", "terminal_bench_4_0"]
