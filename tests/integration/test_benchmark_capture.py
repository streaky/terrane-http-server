"""Offline report-finalization regressions; never contacts Docker or generates traffic.

Run: python -B packages/http-server/tests/integration/test_benchmark_capture.py
"""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch


BENCHMARKS = Path(__file__).resolve().parents[2] / "demo/benchmarks"
spec = importlib.util.spec_from_file_location("demo_capture", BENCHMARKS / "benchmark-running-demo.py")
capture_module = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(BENCHMARKS))
try:
    spec.loader.exec_module(capture_module)
    import summarize as reporter
finally:
    sys.path.pop(0)


class CaptureOutcomeTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="http-capture-outcome-"))
        self.capture = capture_module.Capture.__new__(capture_module.Capture)
        self.capture.stem = Path(directory) / "capture"
        self.capture.args = SimpleNamespace(container="fixture", url="http://fixture.invalid/api/health")
        self.capture.initial = {
            "id": "original-container", "image_id": "original-image",
            "state": {"StartedAt": "original-start"}, "restart_count": 0,
        }
        self.capture.stop = threading.Event()
        self.capture.observe = lambda: {}
        self.capture.data = {
            "status": "running", "configuration": {"url": self.capture.args.url},
            "container_before": self.capture.initial, "samples": [], "trials": [],
        }
        self.final = copy.deepcopy(self.capture.initial)
        self.enterContext(patch.object(capture_module, "inspect", return_value=self.final))
        self.health = self.enterContext(patch.object(capture_module, "health", return_value="ok"))
        self.enterContext(patch.object(capture_module, "command", return_value={"returncode": 0}))

    def trial(self, passed):
        counters = {"container_tcp": {}, "host_tcp": {}}
        self.capture.data["trials"].append({
            "phase": "measurement", "kind": "ab", "concurrency": 1, "mode": "fresh",
            "result": {"passed": passed, "complete": 1, "rps": 10},
            "before": copy.deepcopy(counters), "after": copy.deepcopy(counters),
        })

    def report(self, status):
        data = json.loads(self.capture.stem.with_suffix(".json").read_text())
        self.assertEqual(data["status"], status)
        self.assertIn(f"Status: **{status}**", self.capture.stem.with_suffix(".md").read_text())
        return data

    def test_success_finalizes_both_reports_without_raising(self):
        with self.capture:
            self.trial(True)
        data = self.report("passed")
        self.assertNotIn("error", data)

    def test_one_failed_trial_preserves_results_and_fails_capture(self):
        with self.assertRaises(RuntimeError):
            with self.capture:
                self.trial(True)
                self.trial(False)
        data = self.report("failed")
        self.assertEqual([trial["result"]["passed"] for trial in data["trials"]], [True, False])

    def test_empty_capture_is_not_a_success(self):
        with self.assertRaises(RuntimeError):
            with self.capture:
                pass
        self.report("failed")

    def test_interruption_is_preserved_even_when_final_health_also_fails(self):
        interrupted = KeyboardInterrupt("capture interrupted")
        self.health.side_effect = RuntimeError("final health unavailable")
        with self.assertRaises(KeyboardInterrupt) as raised:
            with self.capture:
                self.trial(True)
                raise interrupted
        self.assertIs(raised.exception, interrupted)
        self.assertEqual(self.report("failed")["error"], repr(interrupted))

    def test_failed_final_health_is_saved_and_propagated(self):
        failure = RuntimeError("final health unavailable")
        self.health.side_effect = failure
        with self.assertRaises(RuntimeError) as raised:
            with self.capture:
                self.trial(True)
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.report("failed")["error"], repr(failure))

    def test_changed_container_invalidates_successful_trials(self):
        self.final["id"] = "replacement-container"
        with self.assertRaises(RuntimeError):
            with self.capture:
                self.trial(True)
        data = self.report("failed")
        self.assertIn("error", data)

    def test_monitor_failure_invalidates_successful_trials(self):
        with self.assertRaises(RuntimeError):
            with self.capture:
                self.trial(True)
                self.capture.data["monitor_error"] = "resource sampling failed"
        data = self.report("failed")
        self.assertIn("error", data)

    def test_failed_trial_snapshot_preserves_reports_and_original_exception(self):
        self.capture.args.soak_seconds = 30
        failure = OSError("trial snapshot unavailable")
        before = {"container_tcp": {"TcpExt:ListenOverflows": 5},
                  "host_tcp": {"TcpExt:TCPSynRetrans": 7}}
        with self.assertRaises(OSError) as raised:
            with self.capture:
                # Isolate the trial snapshot failure from background sampling.
                self.capture.stop.set()
                self.capture.thread.join()
                self.trial(True)
                prior = self.capture.data["trials"][-1]
                prior.update(kind="soak", concurrency=1000)
                prior["after"] = copy.deepcopy(before)
                with patch.object(self.capture, "observe", side_effect=[before, failure]), \
                     patch.object(capture_module.time, "sleep"), \
                     patch.object(capture_module, "command", return_value={
                         "returncode": 0, "stdout": "Complete requests: 1\nFailed requests: 0\n"
                         "Requests per second: 12\nDocument Length: 4\n"}):
                    capture_module.run_soak(self.capture, self.capture.args)
        self.assertIs(raised.exception, failure)
        data = self.report("failed")
        self.assertEqual(data["error"], repr(failure))
        self.assertEqual([row["result"]["complete"] for row in data["trials"]], [1, 1])
        self.assertEqual(data["trials"][0]["tcp_counter_deltas"],
                         {"container_listen_overflows": 5, "host_syn_retransmissions": 7})
        unavailable = {"container_listen_overflows": None, "host_syn_retransmissions": None}
        self.assertEqual(data["trials"][1]["tcp_counter_deltas"], unavailable)
        self.assertEqual(reporter.summarize(data)[0]["tcp_counter_deltas"], unavailable)
        self.assertIn("| soak | 1000 | fresh | unavailable | unavailable |",
                      self.capture.stem.with_suffix(".md").read_text())

    def test_interrupted_trial_without_a_measurement_remains_reportable(self):
        failure = RuntimeError("client timed out before returning measurements")
        with self.assertRaises(RuntimeError) as raised:
            with self.capture:
                self.trial(False)
                self.capture.data["trials"][-1]["result"] = {"passed": False}
                raise failure
        self.assertIs(raised.exception, failure)
        data = self.report("failed")
        self.assertEqual(data["trials"][0]["result"]["complete"], 0)
        self.assertIsNone(data["trials"][0]["result"]["rps"])
        cell = reporter.summarize(data)[0]
        self.assertEqual((cell["median"], cell["min"], cell["max"]), (None, None, None))

    def test_sustained_results_do_not_join_repeated_matrix_estimates(self):
        with self.capture:
            self.trial(True)
            self.capture.data["trials"][-1]["concurrency"] = 1000
            self.trial(True)
            sustained = self.capture.data["trials"][-1]
            sustained.update(kind="soak", concurrency=1000)
            sustained["result"]["rps"] = 90
        cells = {cell["kind"]: cell for cell in reporter.summarize(self.report("passed"))}
        self.assertEqual((cells["ab"]["runs"], cells["ab"]["median"]), (1, 10))
        self.assertEqual((cells["soak"]["runs"], cells["soak"]["median"]), (1, 90))

    def test_sustained_timeout_saves_prior_matrix_and_partial_sustained_trial(self):
        self.capture.args.soak_seconds = 30
        self.capture.observe = lambda: {"container_tcp": {}, "host_tcp": {}}
        failure = capture_module.subprocess.TimeoutExpired(["ab"], 60)
        with self.assertRaises(capture_module.subprocess.TimeoutExpired) as raised:
            with self.capture:
                self.trial(True)
                with patch.object(capture_module.time, "sleep"), patch.object(capture_module, "command", side_effect=failure):
                    capture_module.run_soak(self.capture, self.capture.args)
        self.assertIs(raised.exception, failure)
        data = self.report("failed")
        self.assertEqual([(row["kind"], row["result"]["passed"]) for row in data["trials"]],
                         [("ab", True), ("soak", False)])
        self.assertIsNone(data["trials"][-1]["result"]["rps"])


if __name__ == "__main__":
    unittest.main()
