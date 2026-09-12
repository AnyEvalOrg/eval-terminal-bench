#!/usr/bin/env python3
"""Summarize Harbor result metadata only, never trajectories or verifier output.

With several jobs, the latest result per task wins (useful for a retry sweep).
A numeric reward is authoritative even when the agent hit its time limit.
Costs cover all supplied jobs, including superseded trials, using job aggregates
when available; selected-task cost is reported separately.
"""
import argparse
from collections import Counter
import json
import math
from pathlib import Path


def classify(result):
    rewards = (result.get('verifier_result') or {}).get('rewards') or {}
    reward = rewards.get('reward')
    if isinstance(reward, (int, float)) and math.isfinite(reward):
        return ('pass' if reward == 1 else 'fail'), reward
    error = result.get('exception_info') or {}
    kind = error.get('exception_type', '')
    if kind in ('AgentTimeoutError', 'VerifierTimeoutError', 'TimeoutError', 'TimeoutExpired'):
        return 'timeout', None
    return 'infra', None


def trial_summary(result, path):
    status, reward = classify(result)
    error = result.get('exception_info') or {}
    agent = result.get('agent_result') or {}
    return {'task': result.get('task_name') or path.parent.name.rsplit('__', 1)[0],
            'trial': result.get('trial_name', path.parent.name), 'result_path': str(path.resolve()),
            'status': status, 'reward': reward,
            'exception_type': error.get('exception_type'),
            'started_at': result.get('started_at'), 'finished_at': result.get('finished_at'),
            'cost_usd': agent.get('cost_usd'),
            'n_input_tokens': agent.get('n_input_tokens'),
            'n_cache_tokens': agent.get('n_cache_tokens'),
            'n_output_tokens': agent.get('n_output_tokens')}


def summarize(jobs):
    rows, costs, missing = [], [], []
    for job in dict.fromkeys(p.resolve() for p in jobs):
        job_rows = []
        for directory in sorted(job.iterdir()):
            if not directory.is_dir() or not (directory / 'config.json').exists():
                continue
            path = directory / 'result.json'
            if not path.exists():
                missing.append(str(directory))
                continue
            job_rows.append(trial_summary(json.loads(path.read_text()), path))
        rows.extend(job_rows)
        aggregate_path = job / 'result.json'
        aggregate = json.loads(aggregate_path.read_text()) if aggregate_path.exists() else {}
        cost = (aggregate.get('stats') or {}).get('cost_usd')
        costs.append({'job': str(job),
                      'cost_usd': cost if cost is not None else sum(r['cost_usd'] or 0 for r in job_rows),
                      'source': 'job aggregate' if cost is not None else 'available trial results',
                      'missing_trial_costs': sum(r['cost_usd'] is None for r in job_rows)})
    latest = {}
    for row in sorted(rows, key=lambda r: (r['finished_at'] or r['started_at'] or '', r['result_path'])):
        latest[row['task']] = row
    counts = {key: 0 for key in ('pass', 'fail', 'timeout', 'infra')}
    counts.update(Counter(r['status'] for r in latest.values()))
    return {'counts': counts, 'n_tasks': len(latest), 'n_trial_results': len(rows),
            'cost_usd': sum(c['cost_usd'] for c in costs), 'job_costs': costs,
            'selected_task_cost_usd': sum(r['cost_usd'] or 0 for r in latest.values()),
            'missing_results': missing, 'tasks': dict(sorted(latest.items())), 'trials': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('jobs', type=Path, nargs='+')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = summarize(args.jobs)
    output = args.output or args.jobs[-1] / 'per-task-summary.json'
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(' / '.join(f'{n} {key}' for key, n in report['counts'].items()))
    print(f"Cost: ${report['cost_usd']:.6f} across supplied jobs; "
          f"${report['selected_task_cost_usd']:.6f} in selected task results")
    print(f"Missing results: {len(report['missing_results'])}; JSON: {output}")


if __name__ == '__main__':
    main()
