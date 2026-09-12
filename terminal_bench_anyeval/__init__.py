"""Terminal-Bench catalogue and one-trial AnyEval execution protocol."""
__version__ = "1.0.0"
try:
    from .task import terminal_bench_2_1, terminal_bench_4_0
except ModuleNotFoundError as exc:
    if exc.name != "inspect_ai":
        raise
    def _catalogue(name):
        try:
            from . import task as catalogue
        except ModuleNotFoundError as missing:
            if missing.name == "inspect_ai":
                raise RuntimeError(
                    "Task construction requires Inspect; install eval-terminal-bench[inspect] "
                    "or use the AnyEval app interpreter."
                ) from missing
            raise
        return getattr(catalogue, name)()

    def terminal_bench_2_1():
        return _catalogue("terminal_bench_2_1")

    def terminal_bench_4_0():
        return _catalogue("terminal_bench_4_0")

__all__ = ["terminal_bench_2_1", "terminal_bench_4_0"]
