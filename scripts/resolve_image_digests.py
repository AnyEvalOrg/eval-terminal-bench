#!/usr/bin/env python3
"""Re-resolve the package's 2.1 image references using crane registry access.

Requires crane on PATH and registry credentials as appropriate. Writes a candidate
mapping for review; never changes the approved package pins automatically.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / 'terminal_bench_anyeval/data/image-digests.json'
    pins = json.loads(source.read_text())
    resolved = {}
    for reference in sorted(pins):
        digest = subprocess.run(['crane', 'digest', '--platform', 'linux/amd64', reference],
                                check=True, capture_output=True, text=True, timeout=120).stdout.strip()
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError('Registry returned an invalid digest')
        resolved[reference] = digest
    args.output.write_text(json.dumps(resolved, indent=2) + '\n')


if __name__ == '__main__':
    main()
