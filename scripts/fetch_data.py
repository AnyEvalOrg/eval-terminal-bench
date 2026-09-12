"""Checkout entry point for verified Harbor retrieval."""
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from terminal_bench_anyeval.fetch_data import main


if __name__ == "__main__":
    raise SystemExit(main())
