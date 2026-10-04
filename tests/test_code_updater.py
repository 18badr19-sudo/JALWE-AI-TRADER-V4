import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from self_update.cli import attest, prepare
from self_update.contracts import run_contract
from self_update.policy import TARGET, Rejected, apply_proposal, digest, load_proposal, safe_functions
from self_update.publisher import GitHub, publish, verified_candidate

BASE = "a" * 40
HEAD = "b" * 40
ROOT = Path(__file__).resolve().parent.parent
NUM = """if value is None:
    return default
try:
    number = float(value)
    return number if math.isfinite(number) else default
except (TypeError, ValueError, OverflowError):
    return default"""
CLAMP = """try:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(100.0, float(value)))
except (TypeError, ValueError, OverflowError):
    return 0.0"""


def proposal(**changes):
    value = {"schema_version": 1, "base_sha": BASE, "rationale": "Handle invalid and overflowing numeric inputs.",
             "edits": [{"method": "_num", "body": NUM}, {"method": "_clamp", "body": CLAMP}]}
    value.update(changes)
    return value


def source():
    # A deliberate isolated faulty fixture remains faulty even after the real
    # agent repairs the live repository. Tests never require a production bug.
    faulty = proposal(edits=[{"method": "_num", "body": NUM.replace(", OverflowError", "")},
                            {"method": "_clamp", "body": "return max(0.0, min(100.0, float(value)))"}])
    return apply_proposal((ROOT / TARGET).read_text(), faulty, BASE)


class FakeGitHub:
    def __init__(self, *, stale=False, move=False, block=False, change_pr=False):
        self.current = "c" * 40 if stale else BASE
        self.move, self.block, self.change_pr = move, block, change_pr
        self.calls = []
        self.child_calls = []

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        if path == "/git/ref/heads/main":
            return {"object": {"sha": self.current}}
        if path.startswith("/git/commits/"):
            return {"tree": {"sha": "d" * 40}}
        if path == "/git/blobs":
            return {"sha": "e" * 40}
        if path == "/git/trees":
            return {"sha": "f" * 40}
        if path == "/git/commits":
            return {"sha": HEAD}
        if path == "/git/refs" or path.startswith("/statuses/"):
            return {}
        if path == "/pulls":
            if self.move:
                self.current = "c" * 40
            return {"number": 41}
        if path == "/pulls/41":
            return {"head": {"sha": "c" * 40 if self.change_pr else HEAD}, "base": {"sha": BASE}}
        raise AssertionError((method, path))

    def publish_child(self, repository, base_sha, head):
        self.child_calls.append((repository, base_sha, head))
        if self.block:
            return False
        self.current = head
        return True


class CodeUpdaterTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.source = source()
        self.proposal = proposal()
        self.candidate = apply_proposal(self.source, self.proposal, BASE)

    def bundle(self):
        log = "----------------------------------------------------------------------\nRan 210 tests in 2.5s\n\nOK\n"
        report = {"state": "VALIDATED_FOR_PAPER", "tests_passed": True,
            "base_sha": BASE, "base_sha256": digest(self.source),
            "candidate_sha256": digest(self.candidate), "test_count": 210,
            "workflow_run_id": "123", "workflow_run_attempt": "1", "test_log_sha256": digest(log),
            "candidate_contract": {"cases": 16480, "failures": 0}}
        (self.folder / "proposal.json").write_text(json.dumps(self.proposal))
        (self.folder / "report.json").write_text(json.dumps(report))
        (self.folder / "tests.log").write_text(log)
        (self.folder / "candidate.py").write_text(self.candidate)
        return report

    def publication(self, api):
        return publish(self.folder, api, repository="18badr19-sudo/JALWE-AI-TRADER-V4",
                       base_sha=BASE, source=self.source, run_id="123", attempt="1")

    def test_independent_oracle_reproduces_faults_and_accepts_numeric_repair(self):
        original = run_contract(self.source, seed=11)
        fixed = run_contract(self.candidate, seed=22)
        self.assertGreater(original["failures"], 0)
        self.assertEqual(fixed["failures"], 0)
        self.assertEqual(fixed["cases"], 16480)
        self.assertEqual(safe_functions(self.candidate)["_num"](10**10000, -1), -1)

    def test_trade_gates_stop_levels_signatures_and_other_methods_are_unchanged(self):
        # The renderer's outside-body check is structural, not a list of strings
        # that an agent could sidestep by changing names or comments.
        self.assertEqual(self.candidate.split("    def _stop_below_level")[1],
                         self.source.split("    def _stop_below_level")[1])
        self.assertEqual(self.candidate.split("    @staticmethod")[0],
                         self.source.split("    @staticmethod")[0])
        self.assertEqual(apply_proposal(self.source, proposal(edits=[]), BASE), self.source)

    def test_unknown_paths_methods_headers_or_extra_fields_are_rejected(self):
        for bad in (proposal(path="core/config.py"), proposal(edits=[{"method": "evaluate", "body": "return True"}]),
                    proposal(edits=[{"method": "_num", "body": NUM, "signature": "value, default=100"}]),
                    proposal(edits=[{"method": "_num", "body": NUM}] * 2)):
            with self.assertRaises(Rejected):
                apply_proposal(self.source, bad, BASE)

    def test_io_introspection_imports_loops_calls_and_global_mutation_are_rejected(self):
        bodies = ["import os\nreturn 0", "return open('secrets').read()", "return eval(value)",
            "return math.__dict__", "return value.__class__", "global math\nreturn 0",
            "while True:\n    pass", "return [value for value in ()]", "math = value\nreturn 0",
            "math.isfinite = value\nreturn 0", "return default[0]", "return 10 ** 100000",
            "return 'x' * 10000", "def inner():\n    return 0\nreturn 0", "return __builtins__"]
        bodies += ["if value == 82.2:\n    return 100.0\nreturn float(value)",
                   "number = value + 1\nreturn number", "value = float(value)\nreturn value"]
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(Rejected):
                apply_proposal(self.source, proposal(edits=[{"method": "_num", "body": body}]), BASE)

    def test_scope_validation_rejects_invalid_json_and_stale_base(self):
        for raw in ('{"edits":[],"edits":[]}', '{"n":NaN}', 'not json', 'x' * 20001):
            with self.assertRaises(Rejected):
                load_proposal(raw)
        with self.assertRaises(Rejected):
            apply_proposal(self.source, proposal(base_sha="b" * 40), BASE)
        with self.assertRaises(Rejected):
            apply_proposal(self.source, proposal(schema_version=True), BASE)

    def test_secret_like_text_cannot_be_published(self):
        with self.assertRaises(Rejected):
            apply_proposal(self.source, proposal(rationale="sk-proj-" + "x" * 30), BASE)

    def test_score_inflation_and_missing_finite_guards_fail_the_oracle(self):
        for body in ("return 100.0", "return float(value)", "return 0.0"):
            candidate = apply_proposal(self.source, proposal(edits=[{"method": "_clamp", "body": body}]), BASE)
            self.assertGreater(run_contract(candidate, seed=99)["failures"], 0)

    def test_compiler_rejects_executable_defaults_before_execution(self):
        bad = self.source.replace("default: float = 0.0", "default: float = math.__dict__['bad']")
        with self.assertRaises(Rejected):
            safe_functions(bad)

    def test_no_credentials_are_inherited_by_contract_subprocess(self):
        with patch("self_update.contracts.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"cases":1,"failures":0}'
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret", "CODE_UPDATE_GITHUB_TOKEN": "test-token"}):
                run_contract(self.candidate)
        env = run.call_args.kwargs["env"]
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODE_UPDATE_GITHUB_TOKEN", env)
        self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_preflight_reports_missing_key_preview_pause_and_ready(self):
        for eligible, key, paused, expected in ((True, False, False, "BLOCKED_MISSING_OPENAI_KEY"),
                (False, True, False, "PREVIEW_ONLY"), (True, True, True, "PAUSED"),
                (True, True, False, "READY_TO_GENERATE")):
            with patch("self_update.cli.git", return_value=BASE), \
                 patch("self_update.cli.subprocess.check_output", return_value=self.source):
                context = prepare(self.folder, eligible=eligible, key_present=key, paused=paused)
            self.assertEqual(context["state"], expected)

    def test_passing_contract_skips_generation(self):
        with patch("self_update.cli.git", return_value=BASE), \
             patch("self_update.cli.subprocess.check_output", return_value=self.candidate):
            context = prepare(self.folder, eligible=True, key_present=True)
        self.assertEqual(context["state"], "NO_REPAIR_NEEDED")

    def test_attestation_requires_whole_suite_without_skips_and_paper_lock(self):
        report = self.bundle()
        report.update(state="CONTRACT_PASSED", tests_passed=False)
        (self.folder / "report.json").write_text(json.dumps(report))
        def git_result(*args):
            return BASE if args[0] == "rev-parse" else TARGET
        for log in ("Ran 210 tests in 1s\nOK (skipped=4)\n", "Ran 210 tests in 1s\nFAILED\n", "Ran 10 tests in 1s\nOK\n"):
            (self.folder / "tests.log").write_text(log)
            with patch("self_update.cli.git", side_effect=git_result), \
                 self.assertRaises(Rejected):
                attest(self.folder, self.folder / "tests.log", candidate_path=self.folder / "candidate.py")

    def test_complete_paper_test_evidence_attests_only_unchanged_candidate(self):
        report = self.bundle()
        report.update(state="CONTRACT_PASSED", tests_passed=False)
        (self.folder / "report.json").write_text(json.dumps(report))
        def git_result(*args):
            return BASE if args[0] == "rev-parse" else TARGET
        with patch("self_update.cli.git", side_effect=git_result), \
             patch.dict(os.environ, {"PAPER_TRADING": "true", "ALLOW_LIVE_TRADING": "false",
                                    "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}):
            attest(self.folder, self.folder / "tests.log", candidate_path=self.folder / "candidate.py")
        result = json.loads((self.folder / "report.json").read_text())
        self.assertTrue(result["tests_passed"])
        self.assertEqual(result["state"], "VALIDATED_FOR_PAPER")
        report.update(state="CONTRACT_PASSED", tests_passed=False)
        (self.folder / "report.json").write_text(json.dumps(report))
        with patch("self_update.cli.git", side_effect=git_result), \
             patch.dict(os.environ, {"PAPER_TRADING": "true", "ALLOW_LIVE_TRADING": "true"}), self.assertRaises(Rejected):
            attest(self.folder, self.folder / "tests.log", candidate_path=self.folder / "candidate.py")

    def test_wrong_worktree_or_candidate_bytes_cannot_be_attested(self):
        report = self.bundle()
        report.update(state="CONTRACT_PASSED", tests_passed=False)
        (self.folder / "report.json").write_text(json.dumps(report))
        for filenames in (TARGET + "\ncore/config.py", ""):
            with patch("self_update.cli.git", side_effect=[BASE, filenames]), self.assertRaises(Rejected):
                attest(self.folder, self.folder / "tests.log", candidate_path=self.folder / "candidate.py")
        with patch("self_update.cli.git", side_effect=[BASE, TARGET]), \
             self.assertRaises(Rejected):
            (self.folder / "candidate.py").write_text("tampered")
            attest(self.folder, self.folder / "tests.log", candidate_path=self.folder / "candidate.py")

    def test_publisher_accepts_only_exact_tested_bytes_and_bound_run(self):
        self.bundle()
        candidate, _, _ = verified_candidate(self.folder, BASE, self.source, "123", "1")
        self.assertEqual(candidate, self.candidate)
        for run_id, attempt in (("124", "1"), ("123", "2")):
            with self.assertRaises(Rejected):
                verified_candidate(self.folder, BASE, self.source, run_id, attempt)
        (self.folder / "tests.log").write_text("tampered")
        with self.assertRaises(Rejected):
            verified_candidate(self.folder, BASE, self.source, "123", "1")

    def test_publisher_rejects_proposal_tampering_before_any_write(self):
        self.bundle()
        bad = copy.deepcopy(self.proposal)
        bad["edits"][0]["body"] = "return 100.0"
        (self.folder / "proposal.json").write_text(json.dumps(bad))
        api = FakeGitHub()
        with self.assertRaises(Rejected):
            self.publication(api)
        self.assertEqual(api.calls, [])

    def test_stale_main_is_not_updated_and_no_branch_is_created(self):
        self.bundle()
        api = FakeGitHub(stale=True)
        self.assertEqual(self.publication(api)["state"], "STALE_BASE_NO_PUBLICATION")
        self.assertFalse(any(method == "POST" for method, _, _ in api.calls))

    def test_main_advancing_after_pr_creation_prevents_publication(self):
        self.bundle()
        api = FakeGitHub(move=True)
        self.assertEqual(self.publication(api)["state"], "STALE_BASE_PR_NOT_MERGED")
        self.assertEqual(api.child_calls, [])

    def test_changed_pr_head_prevents_publication(self):
        self.bundle()
        api = FakeGitHub(change_pr=True)
        with self.assertRaises(Rejected):
            self.publication(api)
        self.assertEqual(api.child_calls, [])

    def test_repository_requirements_leave_the_tested_pr_open(self):
        self.bundle()
        api = FakeGitHub(block=True)
        self.assertEqual(self.publication(api)["state"], "PR_AWAITING_GITHUB_REQUIREMENTS")
        self.assertEqual(api.current, BASE)

    def test_successful_publication_changes_only_allowed_file_and_retains_parent(self):
        self.bundle()
        api = FakeGitHub()
        result = self.publication(api)
        self.assertEqual(result["state"], "PUBLISHED_FOR_PAPER")
        self.assertEqual(result["previous_sha"], BASE)
        tree = next(data for _, path, data in api.calls if path == "/git/trees")
        self.assertEqual([entry["path"] for entry in tree["tree"]], [TARGET])
        commit = next(data for _, path, data in api.calls if path == "/git/commits")
        self.assertEqual(commit["parents"], [BASE])
        self.assertEqual(api.child_calls, [("18badr19-sudo/JALWE-AI-TRADER-V4", BASE, HEAD)])

    def test_git_publication_requires_exact_single_parent_and_explicit_lease(self):
        api = GitHub("18badr19-sudo/JALWE-AI-TRADER-V4", "test-token")
        with patch("self_update.publisher.subprocess.run") as run, \
             patch("self_update.publisher.subprocess.check_output", return_value=BASE + "\n"):
            run.return_value.returncode = 0
            self.assertTrue(api.publish_child("18badr19-sudo/JALWE-AI-TRADER-V4", BASE, HEAD))
            fetch = run.call_args_list[0]
            self.assertEqual(fetch.args[0][3], "https://github.com/18badr19-sudo/JALWE-AI-TRADER-V4.git")
            self.assertEqual(fetch.kwargs["env"]["CODE_UPDATE_GITHUB_TOKEN"], "test-token")
            command = run.call_args.args[0]
            self.assertIn(f"--force-with-lease=refs/heads/main:{BASE}", command)
            self.assertEqual(command[-1], HEAD + ":refs/heads/main")
        with patch("self_update.publisher.subprocess.run") as run, \
             patch("self_update.publisher.subprocess.check_output", return_value=BASE + " " + HEAD):
            with self.assertRaises(Rejected):
                api.publish_child("18badr19-sudo/JALWE-AI-TRADER-V4", BASE, HEAD)
            self.assertEqual(run.call_count, 1) # Fetch only; no push.


if __name__ == "__main__":
    unittest.main()
