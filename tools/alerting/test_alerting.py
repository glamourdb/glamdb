"""Behavioral tests for the finite four-state rule configuration."""

import importlib.util
import json
from pathlib import Path
import unittest
import subprocess
import sys
import tempfile


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



class ConfigurationTest(unittest.TestCase):
    def test_setup_creates_only_native_receiver_secrets_and_requires_explicit_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "secrets"
            command = [sys.executable, str(ROOT / "tools/alerting/configure.py"), "--secrets-dir", str(root)]
            result = subprocess.run(command, input="12345:LOCAL_FAKE_TOKEN\n-10012345\n", text=True,
                                    capture_output=True, check=True)
            self.assertEqual({p.name for p in root.iterdir()}, {"telegram_bot_token", "telegram_chat_id"})
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((root / "telegram_bot_token").stat().st_mode & 0o777, 0o444)
            self.assertEqual((root / "telegram_chat_id").read_text().strip(), "-10012345")
            self.assertNotIn("LOCAL_FAKE_TOKEN", result.stdout + result.stderr)
            refused = subprocess.run(command, text=True, capture_output=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("--rotate", refused.stderr)
            subprocess.run(command + ["--rotate"], input="54321:NEW_LOCAL_FAKE_TOKEN\n-10054321\n",
                           text=True, capture_output=True, check=True)
            self.assertEqual((root / "telegram_chat_id").read_text().strip(), "-10054321")

    def test_external_observer_is_a_separate_deployment_with_independent_notification_path(self):
        path = ROOT / "deploy/alerting/external-observer/prometheus.yml"
        self.assertTrue(path.is_file(), "a Prometheus on the failed host cannot report its own loss")
        config = path.read_text()
        self.assertIn("independent-alertmanager.example.invalid", config)
        self.assertIn("glamdb-host.example.invalid:9090", config)
        self.assertIn("glamdb-host.example.invalid:9093", config)
        rules = ROOT / "deploy/alerting/external-observer/rules.yml"
        self.assertTrue(rules.is_file())
        self.assertIn("absent(up", rules.read_text())


if __name__ == "__main__":
    unittest.main()
