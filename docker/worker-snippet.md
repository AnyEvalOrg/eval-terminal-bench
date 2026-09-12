# AnyEval worker and local installation

Build the wheel from this checkout, then place it at
`dist/eval_terminal_bench-1.0.0-py3-none-any.whl` in AnyEval's Docker build context:

```sh
python -m pip wheel --no-deps --no-build-isolation -w dist .
```

Add these exact lines to `Dockerfile.worker` before switching to the runtime
user (Python 3.12 or later is required):

```dockerfile
COPY dist/eval_terminal_bench-1.0.0-py3-none-any.whl /tmp/eval_terminal_bench-1.0.0-py3-none-any.whl
ENV ANYEVAL_TB_DATA_DIR=/opt/anyeval/terminal-bench
RUN python -m pip install '/tmp/eval_terminal_bench-1.0.0-py3-none-any.whl[inspect]'
RUN python -m terminal_bench_anyeval.fetch_data
RUN python -m terminal_bench_anyeval.fetch_data --verify-only
RUN chmod -R a+rX /opt/anyeval/terminal-bench
```

The explicit `ENV` keeps build and runtime processes on the same data root.
Only retrieval needs registry access. Verification is offline, checks every
retained file's hash and the exact file set, and fails the build on any mismatch.

For a local venv, run from this checkout:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
export ANYEVAL_TB_DATA_DIR="$HOME/.cache/anyeval/terminal-bench"
python -m pip install '.[inspect]'
python -m terminal_bench_anyeval.fetch_data
python -m terminal_bench_anyeval.fetch_data --verify-only
```

Keep that environment setting in the shell that runs the catalogue and trials.
The default, when unset, is the package data directory if writable, otherwise
`~/.cache/anyeval/terminal-bench`.
