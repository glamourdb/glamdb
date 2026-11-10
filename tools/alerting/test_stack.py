"""Actual Prometheus and Alertmanager processes against loopback-only peers."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from tools.alerting.test_alerting import ROOT, module
from tools.alerting.test_http import FakeAPI, running


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(predicate, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if predicate():
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.1)
    raise AssertionError("loopback stack did not reach the expected condition")


def api(url, payload=None, method=None):
    request = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                      method=method, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=2) as response:
            data = response.read()
            return json.loads(data) if data else None
    except HTTPError as error:
        error.close()
        raise


@unittest.skipUnless(os.environ.get("PROMETHEUS_BIN") and os.environ.get("ALERTMANAGER_BIN"),
                     "set PROMETHEUS_BIN and ALERTMANAGER_BIN, or run tools/alerting/check.py")
class StackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.transport = module("transport")
        self.box = self.transport.Outbox(self.root / "outbox.sqlite")
        self.addCleanup(self.box.close)
        self.bearer = self.root / "bearer"
        self.bearer.write_text("loopback-only-bearer")
        self.token = self.root / "token"
        self.token.write_text("12345:FAKE_TEST_TOKEN")
        self.chat = self.root / "chat"
        self.chat.write_text("-10012345")
        self.webhook = self.enter(running(self.transport.server(("127.0.0.1", 0), self.box, self.bearer)))
        self.telegram = FakeAPI()
        self.telegram_url = self.enter(running(self.telegram.httpd))
        self.prom_port, self.am_port = free_port(), free_port()
        self.prom_url = f"http://127.0.0.1:{self.prom_port}"
        self.am_url = f"http://127.0.0.1:{self.am_port}"
        self.delivery = self.transport.Delivery(self.box, self.token, self.chat, self.prom_url,
                                               self.telegram_url, allow_local_test=True)
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self.deliver, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_worker)
        text = (ROOT / "deploy/alerting/alertmanager.yml").read_text()
        text = text.replace("http://alert-transport:9087/alerts", self.webhook + "/alerts")
        text = text.replace("/run/secrets/alert_webhook_bearer", str(self.bearer))
        text = text.replace("group_wait: 30s", "group_wait: 0s").replace("group_interval: 1m", "group_interval: 1s")
        self.am_config = self.root / "alertmanager.yml"
        self.am_config.write_text(text)
        self.am = self.start_am()

    def enter(self, context):
        result = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return result

    def deliver(self):
        while not self.stop.wait(0.05):
            for item in self.box.pending():
                self.delivery.send(item)

    def stop_worker(self):
        self.stop.set()
        self.worker.join(5)

    def start_process(self, args, name):
        log = (self.root / (name + ".log")).open("ab")
        self.addCleanup(log.close)
        process = subprocess.Popen(args, stdout=log, stderr=log)

        def stop():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(5)

        self.addCleanup(stop)
        return process

    def start_am(self):
        process = self.start_process([os.environ["ALERTMANAGER_BIN"],
                                      "--config.file=" + str(self.am_config),
                                      "--storage.path=" + str(self.root / "am-data"),
                                      "--web.listen-address=127.0.0.1:" + str(self.am_port),
                                      "--cluster.listen-address=", "--log.level=warn"], "alertmanager")
        wait_until(lambda: urlopen(self.am_url + "/-/ready", timeout=1).close() is None)
        return process

    def alerts(self, state, check="cpu", resolved=False):
        now = datetime.now(timezone.utc)
        return [{"labels": {"alertname": "GlamDBCheckState", "check": check, "cluster": "local",
                            "service": "glamdb", "state": state, "notify_nodata": "true"},
                 "annotations": {"summary": "Loopback integration check"},
                 "startsAt": (now - timedelta(minutes=2)).isoformat(),
                 "endsAt": (now - timedelta(seconds=1) if resolved else now + timedelta(minutes=5)).isoformat()}]

    def test_real_alertmanager_webhook_dedup_and_resolved_suppression(self):
        alert = self.alerts("ALARM")
        api(self.am_url + "/api/v2/alerts", alert)
        wait_until(lambda: len(self.telegram.calls) == 1)
        api(self.am_url + "/api/v2/alerts", alert)
        time.sleep(1.2)
        self.assertEqual(len(self.telegram.calls), 1)
        api(self.am_url + "/api/v2/alerts", self.alerts("ALARM", resolved=True))
        time.sleep(1.2)
        self.assertEqual(len(self.telegram.calls), 1)
        self.assertTrue(self.telegram.calls[0][1]["text"].startswith("ALARM GlamDB"))

    def test_silence_survives_actual_alertmanager_restart(self):
        now = datetime.now(timezone.utc)
        silence = {"matchers": [{"name": "check", "value": "silenced_cpu", "isRegex": False, "isEqual": True}],
                   "startsAt": (now - timedelta(seconds=1)).isoformat(),
                   "endsAt": (now + timedelta(minutes=5)).isoformat(),
                   "createdBy": "local-test", "comment": "loopback persistence test"}
        result = api(self.am_url + "/api/v2/silences", silence)
        self.am.terminate()
        self.am.wait(5)
        self.am = self.start_am()
        restored = api(self.am_url + "/api/v2/silence/" + result["silenceID"])
        self.assertEqual(restored["status"]["state"], "active")
        api(self.am_url + "/api/v2/alerts", self.alerts("ALARM", check="silenced_cpu"))
        time.sleep(1.2)
        self.assertEqual(self.telegram.calls, [])

    def test_real_instant_query_timestamp_cannot_hide_an_old_ok_sample(self):
        exporter = FakeAPI()
        old = int((time.time() - 120) * 1000)
        exporter.metric_payload = ('glamdb_alert_state{check="cpu",cluster="local",service="glamdb"} '
                                   f'0 {old}\n')
        url = self.enter(running(exporter.httpd))
        config = {"global": {"scrape_interval": "1s"}, "scrape_configs": [
            {"job_name": "old-state", "static_configs": [{"targets": [url.split("://", 1)[1]]}]}]}
        path = self.root / "old-state-prometheus.json"
        path.write_text(json.dumps(config))
        self.start_process([os.environ["PROMETHEUS_BIN"], "--config.file=" + str(path),
                            "--storage.tsdb.path=" + str(self.root / "old-state-data"),
                            "--web.listen-address=127.0.0.1:" + str(self.prom_port),
                            "--log.level=warn"], "old-state-prometheus")
        wait_until(lambda: len(api(self.prom_url + "/api/v1/query?query=glamdb_alert_state")["data"]["result"]) == 1)
        raw = api(self.prom_url + "/api/v1/query?query=glamdb_alert_state")["data"]["result"][0]
        self.assertGreater(float(raw["value"][0]), time.time() - 15)
        self.box.accept([{"status": "firing", "startsAt": datetime.now(timezone.utc).isoformat(),
                          "labels": {"check": "cpu", "cluster": "local", "service": "glamdb", "state": "OK"}}])
        wait_until(lambda: 'outcome="unverified_recovery"} 1' in self.box.metrics())
        self.assertEqual(self.telegram.calls, [])

    def test_real_caddy_fixture_keeps_gateway_auth_check_fresh(self):
        exporter = FakeAPI()
        exporter.metric_payload = (ROOT / "tools/alerting/fixtures/caddy-2.11.9.prom").read_text()
        url = self.enter(running(exporter.httpd))
        generate = module("generate")
        config = json.loads((ROOT / "deploy/alerting/checks.json").read_text())
        config["checks"] = [c for c in config["checks"] if c["id"] == "http_auth_failures"]
        config["hold"] = {key: "0s" for key in config["hold"]}
        config["evaluation_interval"] = "1s"
        rules = self.root / "captured-caddy-rules.json"
        rules.write_text(json.dumps(generate.rules(config)))
        prom = {"global": {"scrape_interval": "1s", "evaluation_interval": "1s"},
                "rule_files": [str(rules)], "scrape_configs": [{"job_name": "glamdb-gateway", "static_configs": [
                    {"targets": [url.split("://", 1)[1]], "labels": {"cluster": "local", "service": "glamdb"}}]}]}
        path = self.root / "captured-caddy-prometheus.json"
        path.write_text(json.dumps(prom))
        self.start_process([os.environ["PROMETHEUS_BIN"], "--config.file=" + str(path),
                            "--storage.tsdb.path=" + str(self.root / "captured-caddy-data"),
                            "--web.listen-address=127.0.0.1:" + str(self.prom_port),
                            "--log.level=warn"], "captured-caddy-prometheus")
        endpoint = self.prom_url + "/api/v1/query?query=glamdb_alert_state"
        wait_until(lambda: bool(api(self.prom_url + "/api/v1/query?query=caddy_http_request_duration_seconds_count")
                                ["data"]["result"]))
        wait_until(lambda: bool(api(endpoint)["data"]["result"]) and
                   float(api(endpoint)["data"]["result"][0]["value"][1]) == 0)
        # The captured initialized counters are idle on replay: fresh OK,
        # not NODATA from selecting an unexported nested handler.
        self.assertEqual(float(api(endpoint)["data"]["result"][0]["value"][1]), 0)

    def test_complete_stack_healthy_down_removed_target_never_false_ok(self):
        generate = module("generate")
        config = json.loads((ROOT / "deploy/alerting/checks.json").read_text())
        config["checks"] = [x for x in config["checks"] if x["id"] == "gateway_availability"]
        config["hold"] = {k: "0s" for k in config["hold"]}
        config["evaluation_interval"] = "1s"
        rules = self.root / "states.yml"
        rules.write_text(json.dumps(generate.rules(config)))
        # Use Prometheus's own /metrics as a deterministic scrape target.
        prometheus = {"global": {"scrape_interval": "1s", "evaluation_interval": "1s"},
                      "rule_files": [str(rules)],
                      "alerting": {"alertmanagers": [{"static_configs": [{"targets": [f"127.0.0.1:{self.am_port}"]}]}]},
                      "scrape_configs": [{"job_name": "glamdb-gateway", "static_configs": [{
                          "targets": [f"127.0.0.1:{self.prom_port}"], "labels": {"cluster": "local", "service": "glamdb"}}]}]}
        prom_config = self.root / "prometheus.json"
        prom_config.write_text(json.dumps(prometheus))
        self.start_process([os.environ["PROMETHEUS_BIN"], "--config.file=" + str(prom_config),
                            "--storage.tsdb.path=" + str(self.root / "prom-data"),
                            "--web.listen-address=127.0.0.1:" + str(self.prom_port), "--web.enable-lifecycle",
                            "--rules.alert.resend-delay=1s", "--log.level=warn"], "prometheus")
        wait_until(lambda: any(x[1]["text"].startswith("OK GlamDB") for x in self.telegram.calls))
        # Move the target to an unused port: up=0 is measured failure (ALARM).
        prometheus["scrape_configs"][0]["static_configs"][0]["targets"] = [f"127.0.0.1:{free_port()}"]
        prom_config.write_text(json.dumps(prometheus))
        api(self.prom_url + "/-/reload", {}, method="POST")
        wait_until(lambda: any(x[1]["text"].startswith("ALARM GlamDB") for x in self.telegram.calls))
        alarm_count = len(self.telegram.calls)
        # Remove inventory rather than setting a value to zero. This must
        # produce explicit NODATA, never the resolved notification "OK".
        prometheus["scrape_configs"] = []
        prom_config.write_text(json.dumps(prometheus))
        api(self.prom_url + "/-/reload", {}, method="POST")
        wait_until(lambda: any(x[1]["text"].startswith("NODATA GlamDB") for x in self.telegram.calls))
        self.assertFalse(any(x[1]["text"].startswith("OK GlamDB") for x in self.telegram.calls[alarm_count:]))
        self.assertEqual({x[1]["chat_id"] for x in self.telegram.calls}, {-10012345})
        for path in self.root.glob("*.log"):
            text = path.read_text()
            self.assertNotIn("FAKE_TEST_TOKEN", text)
            self.assertNotIn("loopback-only-bearer", text)


if __name__ == "__main__":
    unittest.main()
