"""Credential-free subprocess oracle for the two permitted numeric helpers."""
from __future__ import annotations

import json
import math
import random
import resource
import sys

from self_update.policy import safe_functions


def probe(source, seed):
    helpers = safe_functions(source)
    rng = random.Random(seed)
    values = [None, True, False, 0, -1, 100, 101, -1000, 10**10000,
              -(10**10000), float("nan"), float("inf"), -float("inf"),
              "10", "nan", "bad", "", b"2.5", [], {}, (), 1e308, -1e308, 5e-324]
    values += [rng.uniform(-100000, 100000) for _ in range(4096)]
    failures, cases, examples = 0, 0, []
    def check(name, value, expected, *args):
        nonlocal cases, failures
        cases += 1
        try:
            actual = helpers[name](value, *args)
            correct = type(actual) in {int, float} and math.isfinite(actual) and actual == expected
            error = "WRONG_RETURN" if not correct else None
        except Exception as exc:
            error = type(exc).__name__
        if error:
            failures += 1
            if len(examples) < 8:
                examples.append({"method": name, "case": cases, "input_type": type(value).__name__, "error": error})
    for value in values:
        for default in (-1.0, 0.0, 2.5):
            try:
                parsed = float(value) if value is not None else default
                expected = parsed if math.isfinite(parsed) else default
            except (TypeError, ValueError, OverflowError):
                expected = default
            check("_num", value, expected, default)
        try:
            expected = max(0.0, min(100.0, float(value))) if math.isfinite(value) else 0.0
        except (TypeError, ValueError, OverflowError):
            expected = 0.0
        check("_clamp", value, expected)
    return {"cases": cases, "failures": failures, "examples": examples}


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    payload = json.load(sys.stdin)
    print(json.dumps(probe(payload["source"], payload["seed"])))


if __name__ == "__main__":
    main()
