#!/usr/bin/env python3
"""Run one B5 regression against a pre-fix method restored only in memory.

Usage: python scripts/prove_b5_reverts.py --baseline-dir /path/to/snapshot --finding 1
The baseline directory contains the original package files and contract test.
No repository file is reverted and no benchmark material is printed.
"""
import argparse
import ast
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def restore_method(module, owner, baseline, name):
    tree = ast.parse(baseline.read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    scope = dict(vars(module))
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(baseline), 'exec'), scope)
    setattr(owner, name, scope[name])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-dir', required=True, type=Path)
    parser.add_argument('--finding', required=True, type=int, choices=range(1,11))
    args = parser.parse_args()
    import pytest
    from terminal_bench_anyeval import k8s_env as k, trial
    base = args.baseline_dir
    methods = {1: '_policy_facts', 2: 'stop', 3: 'exec', 4: '_wait_running',
               5: '_prepare_proxy', 6: '_stream', 7: '_provision_tmux', 8: '_manifests'}
    tests = {
        1: 'test_b5_01_baseline_deny_policy_is_recorded',
        2: 'test_b5_02_failed_delete_retains_policy',
        3: 'test_b5_03_launch_failure_is_infrastructure[127]',
        4: 'test_b5_04_admission_security_is_checked[host_network]',
        5: 'test_b5_05_proxy_serving_container_binding[missing]',
        6: 'test_b5_06_remote_output_is_bounded',
        7: 'test_b5_07_unpinned_tmux_refused',
        8: 'test_b5_08_manifest_uses_approved_digest',
        9: 'test_b5_09_nonfinite_spec_writes_error[NaN]',
        10: 'test_b5_10_companion_unset_is_optional',
    }
    if args.finding in methods:
        restore_method(k, k.AnyEvalK8sEnvironment, base/'terminal_bench_anyeval/k8s_env.py', methods[args.finding])
    elif args.finding == 9:
        for name in ('atomic_write', 'execute', 'main'):
            restore_method(trial, trial, base/'terminal_bench_anyeval/trial.py', name)
    else:
        # Restore only the original hardcoded APP assignment when run_path loads
        # the regression target, retaining the updated fixtures and guard checks.
        import runpy
        old_tree = ast.parse((base/'tests/test_app_contract.py').read_text())
        assignment = next(n for n in old_tree.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == 'APP' for t in n.targets))
        original = runpy.run_path
        def reverted(path, *a, **kw):
            namespace = original(path, *a, **kw)
            if Path(path).name == 'test_app_contract.py':
                exec(compile(ast.Module(body=[assignment], type_ignores=[]), '<in-memory-revert>', 'exec'), namespace)
            return namespace
        runpy.run_path = reverted
    return pytest.main(['-q', '-p', 'no:cacheprovider', '--tb=line',
                        'tests/test_review_b5.py::'+tests[args.finding]])


if __name__ == '__main__':
    raise SystemExit(main())
