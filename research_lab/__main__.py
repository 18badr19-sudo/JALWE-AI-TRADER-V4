"""Launch an isolated, network-denied child with no inherited broker settings."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    # Validate JSON before spawning; inputs are data, never Python/pickle.
    if args.bundle.stat().st_size > 100_000_000:
        parser.error('Bundle exceeds 100 MB')
    payload = args.bundle.read_text()
    json.loads(payload)
    root = str(Path(__file__).resolve().parents[1])
    env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'LANG') if key in os.environ}
    env.update(PYTHONPATH=root, PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-m', 'research_lab.worker'],
                            input=payload, text=True, capture_output=True,
                            env=env, cwd=root, timeout=600)
    if result.returncode:
        sys.stderr.write(result.stderr[-6000:])
        return result.returncode
    report = json.loads(result.stdout)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Refuse accidental replacement of an evidence bundle or an earlier result.
    with args.output.open('x') as out:
        json.dump(report, out, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"{report['state']}: {args.output}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
