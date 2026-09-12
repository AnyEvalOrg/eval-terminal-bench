#!/usr/bin/env python3
"""Print eligible task names; read only task.toml and Compose filenames."""
import argparse
import json
from pathlib import Path
import tomllib

DEFAULT_ROOT = Path('/private/tmp/claude-501/-Users-jperla-josh/7b8624b6-7520-461a-875c-e13899ff3a86/scratchpad/tb/4.0.0/terminal-bench')
COMPOSE_NAMES = {'docker-compose.yaml', 'docker-compose.yml', 'compose.yaml', 'compose.yml'}


def eligibility(root):
    eligible, excluded = [], []
    metadata = sorted(root.glob('*/task.toml'))
    if not metadata:
        raise ValueError(f'No task.toml metadata under {root}')
    for path in metadata:
        reasons = []
        try:
            config = tomllib.loads(path.read_text())
            for role, env in [('environment', config.get('environment') or {}),
                              ('verifier.environment', config.get('verifier', {}).get('environment') or {})]:
                if env.get('gpus') or env.get('gpu_types'):
                    reasons.append(f'{role}: GPU/gpu_types requested')
                size = env.get('storage_mb')
                if isinstance(size, bool) or not isinstance(size, (int, float)) or not 0 < size <= 10240:
                    reasons.append(f'{role}: storage_mb must be positive and <= 10240')
                image = env.get('docker_image')
                if not isinstance(image, str) or not image.strip():
                    reasons.append(f'{role}: prebuilt docker_image required')
            # Inspect filenames only, including verifier and nested build contexts.
            if any(p.name in COMPOSE_NAMES for p in path.parent.rglob('*')):
                reasons.append('Compose manifest present')
        except (tomllib.TOMLDecodeError, TypeError, AttributeError):
            reasons.append('Invalid task metadata')
        if reasons:
            excluded.append({'id': path.parent.name, 'reasons': reasons})
        else:
            eligible.append(path.parent.name)
    return {'dataset_version': '4.0.0', 'root': str(root.resolve()),
            'total': len(metadata), 'eligible': eligible, 'excluded': excluded}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--output', type=Path, default=Path('eligibility-4.0.json'))
    args = parser.parse_args()
    report = eligibility(args.root)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    if not report['eligible']:
        parser.error('No eligible tasks; refusing an unfiltered run')
    print('\n'.join(report['eligible']))


if __name__ == '__main__':
    main()
