# AnyEval worker and local installation

Build the wheel from this checkout using Python 3.12 or later, then put it in
the AnyEval Docker build context:

```sh
python -m pip install 'setuptools>=68' wheel
python -m pip wheel --no-deps --no-build-isolation -w dist .
```

Add these exact lines to `Dockerfile.worker` after installing the app's
requirements (including its Inspect and `openai==3.3.1` pins), before switching
to the runtime user. Here `python` is the app interpreter:

```dockerfile
COPY dist/eval_terminal_bench-1.0.0-py3-none-any.whl /tmp/eval_terminal_bench-1.0.0-py3-none-any.whl
ENV ANYEVAL_TB_DATA_DIR=/opt/anyeval/terminal-bench
ENV ANYEVAL_TB_TRIAL_PYTHON=/opt/anyeval/harbor-venv/bin/python
RUN python -m pip install /tmp/eval_terminal_bench-1.0.0-py3-none-any.whl
RUN python -m venv /opt/anyeval/harbor-venv
RUN /opt/anyeval/harbor-venv/bin/python -m pip install '/tmp/eval_terminal_bench-1.0.0-py3-none-any.whl[trial]'
RUN python -m pip check && /opt/anyeval/harbor-venv/bin/python -m pip check
RUN python -m terminal_bench_anyeval.fetch_data
RUN python -m terminal_bench_anyeval.fetch_data --verify-only
RUN chmod -R a+rX /opt/anyeval/terminal-bench
```

The venv is created without system site packages. The app installs only the
base wheel; `[inspect]` is a convenience for catalogue-only environments that
do not already provide Inspect. `[trial]` pins Harbor 0.22.0 and Kubernetes
36.0.3. Harbor brings LiteLLM 1.100.1 and its `openai<3` constraint into the trial
venv, independently of the app's OpenAI 3.x installation.

The app requires `ANYEVAL_TB_TRIAL_PYTHON` to select the executable and probe
trial dependency versions. Preserve the venv symlink path rather than resolving
it to the system interpreter. Each attempt runs:

```sh
/opt/anyeval/harbor-venv/bin/python -I -m terminal_bench_anyeval.trial --spec /absolute/spec.json --result /absolute/result.json
```

Both processes inherit `ANYEVAL_TB_DATA_DIR`; only the child imports Harbor and
Kubernetes. Do not add the trial venv's site-packages to the app's `PYTHONPATH`.

Retrieval runs in the base interpreter. If a Harbor CLI is available beside that
interpreter or on `PATH`, it is used as a subprocess. Otherwise the fetcher uses
plain HTTP through `urllib.request` against
`https://ofhuhcpkvzjlejydnvyd.supabase.co`:

- `GET /rest/v1/dataset_version_tag`: resolve the `terminal-bench` organization's
  `terminal-bench-2-1@latest` or `terminal-bench@4.0.0` dataset version.
- `GET /rest/v1/dataset_version_task`: enumerate its task versions and archive paths.
- `GET /storage/v1/object/packages/<archive_path>`: download gzip task archives.

The anonymous publishable API key is the public constant shipped with Harbor
0.22.0. No user credential or Harbor Python import is required. Archive paths
are checked before extracting retained regular files. Manifest SHA256 checks,
README verification before pruning, and exact retained file-set checks run
before installation. Registry metadata does not replace committed provenance.
Only retrieval needs registry access; verification is offline and fails the
build on any mismatch.

For local catalogue use and a separate trial environment:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install '.[inspect]'
python3.12 -m venv .harbor-venv
.harbor-venv/bin/python -m pip install '.[trial]'
export ANYEVAL_TB_DATA_DIR="$HOME/.cache/anyeval/terminal-bench"
export ANYEVAL_TB_TRIAL_PYTHON="$PWD/.harbor-venv/bin/python"
.venv/bin/python -m terminal_bench_anyeval.fetch_data
.venv/bin/python -m terminal_bench_anyeval.fetch_data --verify-only
```

Keep the same data environment setting in the app and trial processes, and
keep `ANYEVAL_TB_TRIAL_PYTHON` set to the absolute trial interpreter path.
