"""Separate write job: verify exact tested bytes, open PR, respect GitHub rules."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.request

from self_update.contracts import run_contract
from self_update.policy import TARGET, Rejected, apply_proposal, digest, load_proposal


class APIError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"GITHUB_HTTP_{status}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise APIError(code)


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not token:
            raise Rejected("GITHUB_CONFIGURATION_UNAVAILABLE")
        self.prefix = "https://api.github.com/repos/" + repository
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, data=None):
        if not path.startswith("/") or ".." in path or "?" in path:
            raise Rejected("UNSUPPORTED_GITHUB_ENDPOINT")
        req = urllib.request.Request(self.prefix + path,
            data=json.dumps(data).encode() if data is not None else None, method=method,
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "User-Agent": "jalwe-bounded-code-updater",
                     "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with self.opener.open(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise APIError(exc.code) from None

    def publish_child(self, repository, base_sha, head):
        # Use the Git protocol's expected-old-ID check. The lease cannot rewrite
        # history: head must have exactly base_sha as its only parent.
        with tempfile.TemporaryDirectory(prefix="jalwe-code-publisher-") as directory:
            askpass = Path(directory) / "askpass.py"
            askpass.write_text("#!/usr/bin/env python3\nimport os, sys\nprint('x-access-token' if 'Username' in sys.argv[1] else os.environ['CODE_UPDATE_GITHUB_TOKEN'])\n")
            askpass.chmod(0o700)
            environment = {"PATH": os.defpath, "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0",
                           "CODE_UPDATE_GITHUB_TOKEN": self.token}
            remote = f"https://github.com/{repository}.git"
            subprocess.run(["git", "fetch", "--no-tags", remote, head], env=environment,
                           check=True, capture_output=True, timeout=60)
            parents = subprocess.check_output(["git", "show", "-s", "--format=%P", head], text=True).strip()
            if parents != base_sha:
                raise Rejected("PUBLISHED_COMMIT_NOT_EXACT_CHILD_OF_TESTED_BASE")
            result = subprocess.run(["git", "push", f"--force-with-lease=refs/heads/main:{base_sha}",
                                     remote, f"{head}:refs/heads/main"],
                env=environment, capture_output=True, timeout=60)
        return result.returncode == 0


def verified_candidate(folder, base_sha, source, run_id, attempt):
    report = json.loads((folder / "report.json").read_text())
    proposal = load_proposal((folder / "proposal.json").read_text())
    candidate = apply_proposal(source, proposal, base_sha)
    log = (folder / "tests.log").read_text()
    runs = re.findall(r"^Ran (\d+) tests in .+$", log, flags=re.MULTILINE)
    if (report.get("state") != "VALIDATED_FOR_PAPER" or report.get("tests_passed") is not True
        or report.get("base_sha") != base_sha or type(report.get("test_count")) is not int or report["test_count"] < 190
        or report.get("workflow_run_id") != run_id or report.get("workflow_run_attempt") != attempt
        or report.get("candidate_sha256") != digest(candidate) or report.get("base_sha256") != digest(source)
        or not proposal["edits"] or report.get("test_log_sha256") != digest(log)
        or not runs or int(runs[-1]) != report["test_count"]
        or not re.search(r"^OK\s*$", log, flags=re.MULTILINE)
        or re.search(r"^FAILED\b|^OK \(skipped=", log, flags=re.MULTILINE)):
        raise Rejected("PUBLICATION_ATTESTATION_MISMATCH")
    if run_contract(candidate)["failures"]:
        raise Rejected("PUBLICATION_CONTRACT_RECHECK_FAILED")
    return candidate, proposal, report


def publish(folder, api=None, *, repository=None, base_sha=None, source=None, run_id=None, attempt=None):
    repository = repository or os.environ.get("GITHUB_REPOSITORY", "")
    run_id = run_id or os.environ.get("GITHUB_RUN_ID", "")
    attempt = attempt or os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if repository != "18badr19-sudo/JALWE-AI-TRADER-V4" or not all(re.fullmatch(r"\d{1,20}", x) for x in (run_id, attempt)):
        raise Rejected("PUBLICATION_CONTEXT_INVALID")
    base_sha = base_sha or subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    source = source if source is not None else subprocess.check_output(["git", "show", f"{base_sha}:{TARGET}"], text=True)
    candidate, proposal, report = verified_candidate(folder, base_sha, source, run_id, attempt)
    api = api or GitHub(repository, os.environ.get("CODE_UPDATE_GITHUB_TOKEN", ""))
    if api.request("GET", "/git/ref/heads/main")["object"]["sha"] != base_sha:
        return {"state": "STALE_BASE_NO_PUBLICATION"}
    parent = api.request("GET", "/git/commits/" + base_sha)
    blob = api.request("POST", "/git/blobs", {"content": candidate, "encoding": "utf-8"})
    tree = api.request("POST", "/git/trees", {"base_tree": parent["tree"]["sha"],
        "tree": [{"path": TARGET, "mode": "100644", "type": "blob", "sha": blob["sha"]}]})
    commit = api.request("POST", "/git/commits", {"message": "Repair numeric analysis helpers after independent PAPER tests",
        "tree": tree["sha"], "parents": [base_sha]})
    head = commit["sha"]
    branch = f"code-agent/numeric-repair-{run_id}-{attempt}"
    api.request("POST", "/git/refs", {"ref": "refs/heads/" + branch, "sha": head})
    evidence_url = f"https://github.com/{repository}/actions/runs/{run_id}"
    pr = api.request("POST", "/pulls", {"base": "main", "head": branch,
        "title": "Repair numeric analysis helpers after independent validation",
        "body": "The bounded coding agent proposed a numeric-helper repair. The trusted validator permits only _num/_clamp bodies; signatures, scoring, risk, broker execution, tests and workflows remain unchanged.\n\n"
                + f"Independent validation: {report['candidate_contract']['cases']} numeric cases and {report['test_count']} PAPER tests passed. Exact tested SHA256: {report['candidate_sha256']}.\n\n"
                + f"Evidence: {evidence_url}\n\nGenerator rationale (untrusted text):\n" + proposal["rationale"]})
    api.request("POST", "/statuses/" + head, {"state": "success", "context": "JALWE bounded code validation",
        "description": "Exact candidate passed independent numeric and PAPER tests", "target_url": evidence_url})
    if api.request("GET", "/git/ref/heads/main")["object"]["sha"] != base_sha:
        return {"state": "STALE_BASE_PR_NOT_MERGED", "pr": pr["number"]}
    info = api.request("GET", "/pulls/" + str(pr["number"]))
    if info["head"]["sha"] != head or info["base"]["sha"] != base_sha:
        raise Rejected("PR_CHANGED_BEFORE_MERGE")
    # An exact child plus an explicit expected-old-ID lease yields a guarded
    # fast-forward. It fails if main advances OR is reset during publication.
    # No branch protections or required checks are modified or bypassed.
    if not api.publish_child(repository, base_sha, head):
        return {"state": "PR_AWAITING_GITHUB_REQUIREMENTS", "pr": pr["number"]}
    if api.request("GET", "/git/ref/heads/main")["object"]["sha"] != head:
        raise Rejected("PUBLICATION_REF_MISMATCH")
    return {"state": "PUBLISHED_FOR_PAPER", "pr": pr["number"], "sha": head, "previous_sha": base_sha}
