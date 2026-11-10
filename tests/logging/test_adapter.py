import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("glamdb_logging", ROOT / "tools/logging/glamdb_logging.py")


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(self.module)

    def test_secret_payloads_unknown_fields_and_untrusted_ids_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = self.module.StructuredLogger(directory=directory, sample_every=1)
            request_id = logger.new_request_id()
            logger.emit("request_completed", operation="auth", outcome="denied", request_id=request_id,
                        password="sensitive-password", Authorization="Bearer secret-token", Cookie="session=secret",
                        body="private-body", query="password=secret", path="/users/private", error="secret error",
                        org_id="password=bad-id", revision=7, status_code=403)
            logger.close()
            content = (Path(directory) / "glamdb.log").read_text()
            self.assertNotIn("secret", content)
            self.assertNotIn("private", content)
            record = next(json.loads(line) for line in content.splitlines()
                          if json.loads(line)["event"] == "request_completed")
            self.assertEqual(record["request_id"], request_id)
            self.assertNotIn("org_id", record)
            self.assertEqual(record["revision"], 7)
            self.assertEqual(record["level"], "warn")

    def test_lifecycle_and_control_changes_are_not_success_sampled(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = self.module.StructuredLogger(directory=directory, sample_every=1000)
            logger.emit("org_updated", operation="organization", outcome="ok", revision=11,
                        org_id="example-team", database_id="production_1")
            logger.emit("runtime_started")
            logger.emit("runtime_stopped")
            logger.close()
            events = [json.loads(line) for line in (Path(directory) / "glamdb.log").read_text().splitlines()]
            self.assertEqual({event["event"] for event in events} & {"org_updated", "runtime_started", "runtime_stopped"},
                             {"org_updated", "runtime_started", "runtime_stopped"})
            changed = next(event for event in events if event["event"] == "org_updated")
            self.assertEqual(changed["revision"], 11)
            self.assertEqual(changed["org_id"], "example-team")
            self.assertEqual(changed["database_id"], "production_1")

    def test_invalid_enum_values_are_replaced_and_numeric_fields_are_bounded(self):
        record = self.module.sanitize_record("password=secret", operation="token=secret", outcome="secret",
                                             request_id="secret", duration_us=-1, row_count=True,
                                             status_code=2**80)
        text = json.dumps(record)
        self.assertNotIn("secret", text)
        self.assertEqual(record["operation"], "other")
        self.assertEqual(record["outcome"], "error")
        self.assertNotIn("duration_us", record)
        self.assertNotIn("row_count", record)
        self.assertNotIn("status_code", record)

    def test_duplicate_log_directory_has_one_rotation_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = self.module.StructuredLogger(directory=directory)
            try:
                with self.assertRaises(BlockingIOError):
                    self.module.StructuredLogger(directory=directory)
                logger.emit("runtime_started")
            finally:
                logger.close()
            reopened = self.module.StructuredLogger(directory=directory)
            reopened.close()

    def test_prometheus_uses_actual_admission_and_emission_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = self.module.StructuredLogger(directory=directory, sample_every=10)
            for _ in range(100):
                logger.emit("request_completed", outcome="ok")
            logger.emit("request_completed", outcome="error")
            logger.emit("request_completed", outcome="denied")
            logger.close()
            metrics = self.module.render_prometheus(logger.snapshot())
            self.assertIn("glamdb_log_submitted_total 102\n", metrics)
            self.assertIn("glamdb_log_accepted_total 12\n", metrics)
            self.assertIn("glamdb_log_sampled_total 90\n", metrics)
            self.assertIn("glamdb_log_written_total 12\n", metrics)
            self.assertIn('glamdb_log_dropped_total{reason="sink"} 0\n', metrics)
            self.assertIn("glamdb_log_backlog 0\n", metrics)


if __name__ == "__main__":
    unittest.main()
