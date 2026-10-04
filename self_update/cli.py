"""Trusted workflow entry points. No model output is executed as shell code."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

from self_update.contracts import run_contract
from self_update.policy import TARGET, apply_proposal, digest, load_proposal, Rejected


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def output(name, value):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as file:
            file.write(f"{name}={value}\n")


def summary(message):
    print(message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as file:
            file.write(message + "\n")


def prepare(folder, *, eligible, key_present, paused=False):
    head = git("rev-parse", "HEAD")
    source = subprocess.check_output(["git", "show", f"{head}:{TARGET}"], text=True)
    contract = run_contract(source)
    state = ("PREVIEW_ONLY" if not eligible else "PAUSED" if paused else
             "BLOCKED_MISSING_OPENAI_KEY" if not key_present else
             "NO_REPAIR_NEEDED" if not contract["failures"] else "READY_TO_GENERATE")
    context = {"base_sha": head, "target": TARGET,
               "baseline_contract": contract, "state": state}
    write_json(Path(folder) / "context.json", context)
    output("base_sha", head)
    output("ready", "true" if state == "READY_TO_GENERATE" else "false")
    summary(f"Code updater: {state}. OpenAI key configured: {str(key_present).lower()}. Numeric contract: {contract['cases']} cases; {contract['failures']} baseline failures.")
    return context


def validate(folder, raw):
    proposal = load_proposal(raw)
    head = git("rev-parse", "HEAD")
    source = subprocess.check_output(["git", "show", f"{head}:{TARGET}"], text=True)
    candidate = apply_proposal(source, proposal, head)
    write_json(Path(folder) / "proposal.json", proposal)
    if not proposal["edits"]:
        write_json(Path(folder) / "report.json", {"state": "NO_CHANGE", "base_sha": head})
        output("changed", "false")
        summary("Code updater: NO_CHANGE; no publication.")
        return
    baseline, tested = run_contract(source), run_contract(candidate)
    if tested["failures"] or not baseline["failures"] or candidate == source:
        raise Rejected("REPAIR_MUST_FIX_A_CONTRACT_FAILURE_WITH_ZERO_CANDIDATE_FAILURES")
    report = {"state": "CONTRACT_PASSED", "base_sha": head,
              "candidate_sha256": digest(candidate), "base_sha256": digest(source),
              "baseline_contract": baseline, "candidate_contract": tested, "tests_passed": False}
    write_json(Path(folder) / "report.json", report)
    Path(TARGET).write_text(candidate)
    output("changed", "true")
    summary(f"Code updater: fixed {baseline['failures']} numeric contract failures; {tested['cases']} candidate cases passed. Full PAPER tests still required.")


def attest(folder, log_path, *, candidate_path=TARGET):
    path = Path(folder) / "report.json"
    report = json.loads(path.read_text())
    if report.get("state") != "CONTRACT_PASSED" or report["base_sha"] != git("rev-parse", "HEAD"):
        raise Rejected("ATTESTATION_BASE_OR_STATE_MISMATCH")
    if git("diff", "--name-only").splitlines() != [TARGET]:
        raise Rejected("WORKTREE_CHANGE_OUTSIDE_ALLOWED_FILE")
    if digest(Path(candidate_path).read_text()) != report["candidate_sha256"]:
        raise Rejected("CANDIDATE_CHANGED_AFTER_VALIDATION")
    log = Path(log_path).read_text()
    runs = re.findall(r"^Ran (\d+) tests in .+$", log, flags=re.MULTILINE)
    if not runs or int(runs[-1]) < 190 or not re.search(r"^OK\s*$", log, flags=re.MULTILINE):
        raise Rejected("FULL_PAPER_TEST_SUITE_NOT_PASSED_WITHOUT_SKIPS")
    if re.search(r"^FAILED\b|^OK \(skipped=", log, flags=re.MULTILINE):
        raise Rejected("FULL_PAPER_TEST_FAILURE_OR_SKIPS")
    if os.environ.get("PAPER_TRADING") != "true" or os.environ.get("ALLOW_LIVE_TRADING") != "false":
        raise Rejected("PAPER_ONLY_TEST_ENVIRONMENT_REQUIRED")
    report.update(state="VALIDATED_FOR_PAPER", tests_passed=True, test_count=int(runs[-1]),
                  workflow_run_id=os.environ.get("GITHUB_RUN_ID"),
                  workflow_run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"),
                  test_log_sha256=digest(log))
    write_json(path, report)
    output("ready", "true")
    summary(f"Code updater: VALIDATED_FOR_PAPER; {report['test_count']} independent tests passed. Exact candidate hash retained for publication.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "validate", "attest", "publish"])
    parser.add_argument("--folder", default=".agent-work")
    parser.add_argument("--log", default=".agent-work/tests.log")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.folder, eligible=os.environ.get("CODE_AGENT_ELIGIBLE") == "true",
                key_present=os.environ.get("CODE_AGENT_KEY_PRESENT") == "true",
                paused=os.environ.get("CODE_AGENT_PAUSED") == "true")
    elif args.command == "validate":
        validate(args.folder, os.environ.get("CODE_AGENT_PROPOSAL", ""))
    elif args.command == "attest":
        attest(args.folder, args.log)
    else:
        from self_update.publisher import publish
        summary(json.dumps(publish(Path(args.folder))))


if __name__ == "__main__":
    main()
