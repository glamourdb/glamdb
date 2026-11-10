"""Exercise the actual producer, file sink, sampling and saturation in subprocesses."""
import json
import os
import select
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class NativeLoggingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = os.environ.get("GLAMDB_LOG_PROBE")
        if cls.binary:
            return
        cls.binary = str(ROOT / "artifacts/logging-probe")
        (ROOT / "artifacts").mkdir(exist_ok=True)
        subprocess.run([
            os.environ.get("CXX", "clang++"), "-std=c++23", "-O2", "-pthread",
            "-Wall", "-Wextra", "-Werror", "-I", str(ROOT),
            str(ROOT / "tests/logging/logging_probe.cpp"),
            str(ROOT / "core/telemetry/structured_log.cpp"), "-o", cls.binary,
        ], check=True, text=True)

    def run_probe(self, directory, mode="errors", count=10):
        result = subprocess.run([self.binary, mode, str(directory), str(count)],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def read_events(self, directory):
        return [json.loads(line) for path in sorted(Path(directory).glob("glamdb.log*"))
                for line in path.read_text().splitlines()]

    def test_only_typed_safe_fields_and_generated_correlation_are_written(self):
        with tempfile.TemporaryDirectory() as directory:
            stats = self.run_probe(directory)
            events = self.read_events(directory)
            requests = [event for event in events if event["event"] == "request_completed"]
            self.assertEqual(len(requests), 10)
            self.assertEqual(stats["sampled"], 0)
            self.assertEqual(stats["backlog"], 0)
            self.assertEqual(stats["process_submitted"], stats["submitted"])
            self.assertEqual(stats["process_written"], stats["written"])
            self.assertEqual(stats["process_capacity"], 0)
            self.assertEqual(stats["written"], stats["accepted"])
            self.assertEqual(len({event["request_id"] for event in requests}), 10)
            for event in requests:
                self.assertRegex(event["request_id"], r"^[a-f0-9]{32}$")
                self.assertEqual(event["operation"], "append")
                self.assertEqual(event["duration_us"], 123)
                self.assertEqual(event["row_count"], 7)
                self.assertEqual(event["level"], "error")
                self.assertFalse(set(event) & {"message", "body", "query", "path", "headers", "token"})
            for path in Path(directory).glob("glamdb.log*"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_success_sampling_leaves_errors_and_lifecycle_unsampled(self):
        with tempfile.TemporaryDirectory() as directory:
            stats = self.run_probe(directory, "sample", 100)
            self.assertEqual(stats["sampled"], 90)
            events = self.read_events(directory)
            requests = sum(e["event"] == "request_completed" for e in events)
            self.assertGreater(requests, 0)
            self.assertLessEqual(requests, 10)
            self.assertEqual(requests + 2, stats["accepted"])
            self.assertEqual({e["event"] for e in events} & {"runtime_started", "runtime_stopped"},
                             {"runtime_started", "runtime_stopped"})

    def test_success_rate_is_bounded_independently_of_sampling(self):
        with tempfile.TemporaryDirectory() as directory:
            stats = self.run_probe(directory, "rate", 100)
            self.assertGreaterEqual(stats["rate_limited"], 96)
            requests = [event for event in self.read_events(directory)
                        if event["event"] == "request_completed"]
            self.assertLessEqual(len(requests), 4)  # possible monotonic second boundary

    def test_overload_accounts_every_event_and_finishes_with_bounded_backlog(self):
        with tempfile.TemporaryDirectory() as directory:
            stats = self.run_probe(directory, "stress", 100000)
            self.assertEqual(stats["submitted"], 800002)
            self.assertEqual(stats["submitted"], stats["accepted"] + stats["dropped_full"] +
                             stats["dropped_contended"])
            self.assertGreater(stats["dropped_full"] + stats["dropped_contended"], 0)
            self.assertEqual(stats["accepted"], stats["written"] + stats["dropped_sink"])
            self.assertEqual(stats["backlog"], 0)
            self.assertLess(stats["elapsed_us"], 10000000)

    def test_rotation_retains_only_complete_lines_in_fixed_file_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_probe(directory, "rotate", 1000)
            files = list(Path(directory).glob("glamdb.log*"))
            self.assertLessEqual(len(files), 3)
            self.assertGreater(len(files), 1)
            self.assertLessEqual(sum(path.stat().st_size for path in files), 3 * 4096)
            self.assertTrue(self.read_events(directory))
            for path in files:
                self.assertTrue(path.read_bytes().endswith(b"\n"))


    def test_reopening_with_smaller_budget_prunes_previous_oversized_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "glamdb.log").write_bytes(b"x" * 5000)
            (root / "glamdb.log.1").write_bytes(b"y" * 5000)
            (root / "glamdb.log.10").write_text("old archive")
            self.run_probe(directory, "rotate", 1)
            files = list(root.glob("glamdb.log*"))
            self.assertLessEqual(len(files), 3)
            self.assertTrue(all(path.stat().st_size <= 4096 for path in files))
            self.assertTrue(self.read_events(directory))


    def test_two_processes_cannot_rotate_the_same_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            holder = subprocess.Popen([self.binary, "hold", directory, "1"],
                                      text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                ready, _, _ = select.select([holder.stdout], [], [], 5)
                self.assertTrue(ready, "First logger did not open its sink")
                self.assertEqual(holder.stdout.readline(), "ready\n")
                other = subprocess.run([self.binary, "errors", directory, "1"],
                                       text=True, capture_output=True, timeout=5)
                self.assertEqual(other.returncode, 3)
            finally:
                holder.terminate()
                holder.wait(timeout=5)
                holder.stdout.close()
                holder.stderr.close()

    def test_active_file_symlinks_and_special_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            victim = root / "private"
            victim.write_text("must remain unchanged")
            (root / "glamdb.log").symlink_to(victim)
            result = subprocess.run([self.binary, "errors", directory, "1"],
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(victim.read_text(), "must remain unchanged")
            (root / "glamdb.log").unlink()
            os.mkfifo(root / "glamdb.log")
            result = subprocess.run([self.binary, "errors", directory, "1"],
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 3)


    def test_blocked_stderr_cannot_block_producers_or_grow_the_queue(self):
        process = subprocess.Popen([self.binary, "blocked", "unused", "500000"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            process.wait(timeout=10)  # Deliberately do not drain the full stderr pipe.
            stats = json.loads(process.stdout.read())
            self.assertEqual(process.returncode, 0)
            self.assertLessEqual(stats["backlog"], 96)  # queue plus the worker's fixed batch
            self.assertGreater(stats["dropped_full"], 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def test_sink_failure_is_counted_without_changing_application_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            stats = self.run_probe(directory, "sink-error", 1000)
            self.assertGreater(stats["sink_errors"], 0)
            self.assertGreater(stats["dropped_sink"], 0)
            self.assertEqual(stats["accepted"], stats["written"] + stats["dropped_sink"])
            self.assertEqual(stats["backlog"], 0)


if __name__ == "__main__":
    unittest.main()
