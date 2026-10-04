"""Run the fixed oracle without inheriting GitHub/OpenAI/broker credentials."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys


def run_contract(source, seed=None):
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, "-m", "self_update.contract_worker"],
        input=json.dumps({"source": source, "seed": seed if seed is not None else secrets.randbits(64)}),
        text=True, capture_output=True, timeout=10, cwd=root,
        env={"PATH": os.defpath, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"})
    if result.returncode:
        raise ValueError("NUMERIC_CONTRACT_WORKER_FAILED")
    return json.loads(result.stdout)
