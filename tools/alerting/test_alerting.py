"""Behavioral tests for state generation and the durable Telegram transport."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def module(name):
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class RulesTest(unittest.TestCase):
    def test_all_checks_produce_exactly_four_states_and_never_resolved_ok(self):
        self.assertTrue(Path(__file__).with_name("generate.py").exists())
        generate = module("generate")
        config = json.loads((ROOT / "deploy/alerting/checks.json").read_text())
        rules = generate.rules(config)
        alerts = [r for g in rules["groups"] for r in g["rules"] if "alert" in r]
        for check in config["checks"]:
            selected = [r for r in alerts if r["labels"]["check"] == check["id"]]
            self.assertEqual({r["labels"]["state"] for r in selected},
                             {"OK", "WARN", "ALARM", "NODATA"})
            self.assertTrue(all("keep_firing_for" not in r for r in selected))

    def test_invalid_thresholds_are_rejected(self):
        self.assertTrue(Path(__file__).with_name("generate.py").exists())
        generate = module("generate")
        config = json.loads((ROOT / "deploy/alerting/checks.json").read_text())
        config["checks"][0]["warn"] = config["checks"][0]["alarm"] + 1
        with self.assertRaises(ValueError):
            generate.rules(config)


class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(Path(__file__).with_name("transport.py").exists())
        self.transport = module("transport")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "outbox.sqlite"
        self.now = 1000.0
        self.box = self.transport.Outbox(self.path, capacity=2, clock=lambda: self.now)
        self.addCleanup(self.box.close)

    def event(self, state="ALARM", start="2026-01-01T00:00:00Z", check="cpu"):
        return {"status": "firing", "startsAt": start,
                "labels": {"check": check, "cluster": "local", "service": "glamdb",
                           "state": state}, "annotations": {"summary": "CPU high"}}

    def test_resolved_alert_cannot_create_ok(self):
        event = self.event("OK")
        event["status"] = "resolved"
        self.box.accept([event])
        self.assertEqual(self.box.pending(), [])

    def test_coalesces_state_and_ignores_late_old_event(self):
        self.box.accept([self.event("ALARM")])
        self.box.accept([self.event("NODATA", "2026-01-01T00:01:00Z")])
        self.box.accept([self.event("ALARM")])
        self.assertEqual([x["state"] for x in self.box.pending()], ["NODATA"])

    def test_dedup_survives_restart(self):
        self.box.accept([self.event()])
        self.box.complete(self.box.pending()[0])
        self.box.close()
        self.box = self.transport.Outbox(self.path, capacity=2, clock=lambda: self.now)
        self.addCleanup(self.box.close)
        self.box.accept([self.event()])
        self.assertEqual(self.box.pending(), [])

    def test_retry_deadline_survives_restart(self):
        self.box.accept([self.event()])
        self.box.retry(self.box.pending()[0], retry_after=20)
        self.box.close()
        self.box = self.transport.Outbox(self.path, capacity=2, clock=lambda: self.now)
        self.addCleanup(self.box.close)
        self.assertEqual(self.box.pending(), [])
        self.now += 22
        self.assertEqual(self.box.pending()[0]["attempts"], 1)

    def test_chat_backoff_survives_restart(self):
        self.box.accept([self.event()])
        self.box.defer_chat(20)
        self.box.close()
        self.box = self.transport.Outbox(self.path, capacity=2, clock=lambda: self.now)
        self.addCleanup(self.box.close)
        self.assertEqual(self.box.pending(), [])
        self.now += 21
        self.assertEqual(len(self.box.pending()), 1)

    def test_identical_alarm_reminder_can_repeat_after_four_hours(self):
        self.box.accept([self.event()])
        self.box.complete(self.box.pending()[0])
        self.now += 14401
        self.box.accept([self.event()])
        self.assertEqual(len(self.box.pending()), 1)

    def test_expiry_and_retry_exhaustion_are_observable(self):
        self.box.accept([self.event("OK")])
        self.now += 301
        self.assertEqual(self.box.pending(), [])
        self.assertIn('outcome="expired"} 1', self.box.metrics())
        self.box.accept([self.event(check="other")])
        self.box.retry(self.box.pending()[0], max_attempts=1)
        self.assertEqual(self.box.pending(), [])
        self.assertIn('outcome="exhausted"} 1', self.box.metrics())

    def test_full_queue_rejects_whole_batch(self):
        self.box.accept([self.event(check="a"), self.event(check="b")])
        with self.assertRaises(self.transport.QueueFull):
            self.box.accept([self.event("WARN", check="a"), self.event(check="c")])
        self.assertEqual([x["state"] for x in self.box.pending()], ["ALARM", "ALARM"])

    def test_retry_is_bounded_and_persistent(self):
        self.box.accept([self.event()])
        event = self.box.pending()[0]
        self.box.retry(event, retry_after=17)
        self.assertEqual(self.box.pending(), [])
        self.now += 18
        event = self.box.pending()[0]
        self.assertEqual(event["attempts"], 1)
        self.box.retry(event, retry_after=5000)
        self.now += 3601
        self.assertEqual(self.box.pending(), [])
        self.now += 1401
        self.assertEqual(len(self.box.pending()), 1)

    def test_missing_or_stale_state_never_validates_recovery(self):
        transport = self.transport
        labels = self.event("OK")["labels"]
        for result in ([], [{"metric": {}, "value": [1000, "3"]}],
                       [{"metric": {}, "value": [0, "0"]}],
                       [{"metric": {}, "value": [1000, "NaN"]}]):
            with self.subTest(result=result):
                self.assertFalse(transport.valid_recovery(result, labels, now=1000, max_age=90))
        self.assertTrue(transport.valid_recovery(
            [{"metric": labels, "value": [1000, "0"]}], labels, now=1000, max_age=90))

    def test_label_mismatch_cannot_validate_recovery(self):
        labels = self.event("OK")["labels"]
        self.assertFalse(self.transport.valid_recovery(
            [{"metric": {**labels, "service": "other"}, "value": [1000, "0"]}],
            labels, now=1000, max_age=90))

    def test_untrusted_payload_cannot_choose_chat_or_inject_markup(self):
        event = self.event()
        event["labels"]["chat_id"] = "999"
        event["annotations"]["summary"] = "<b>unsafe</b>\nforged"
        self.box.accept([event])
        row = self.box.pending()[0]
        self.assertNotIn("chat_id", row["labels"])
        self.assertNotIn("<b>", self.transport.message(row))

    def test_identity_history_is_bounded(self):
        for i in range(50):
            self.box.accept([self.event(check=str(i))])
            self.box.complete(self.box.pending()[0])
        self.assertLessEqual(self.box.history_size(), 4)


if __name__ == "__main__":
    unittest.main()
