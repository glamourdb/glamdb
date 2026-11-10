"""Loopback-only integration tests; no test calls the real Telegram API."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tools.alerting.test_alerting import module


@contextmanager
def running(httpd):
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.replies = []
        self.state = None
        self.delay = 0
        self.metric_payload = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if owner.metric_payload is not None and self.path == "/metrics":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; version=0.0.4")
                    self.end_headers()
                    self.wfile.write(owner.metric_payload.encode())
                    return
                result = [] if owner.state is None else [owner.state]
                body = json.dumps({"status": "success", "data": {"resultType": "vector", "result": result}}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                owner.calls.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                time.sleep(owner.delay)
                code, body = owner.replies.pop(0) if owner.replies else (200, {"ok": True, "result": {"message_id": 1}})
                self.send_response(code)
                if code == 302:
                    self.send_header("Location", "/redirected")
                self.end_headers()
                try:
                    self.wfile.write(json.dumps(body).encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.transport = module("transport")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.token, self.chat, self.bearer = [root / name for name in ("token", "chat", "bearer")]
        self.token.write_text("12345:FAKE_TEST_TOKEN")
        self.chat.write_text("-10012345")
        self.bearer.write_text("test-authentication-secret")
        self.now = time.time()
        self.database = root / "outbox.sqlite"
        self.box = self.transport.Outbox(self.database, 1000, clock=lambda: self.now)
        self.addCleanup(self.box.close)
        self.api = FakeAPI()
        self.api_context = running(self.api.httpd)
        self.api_url = self.api_context.__enter__()
        self.addCleanup(self.api_context.__exit__, None, None, None)
        self.delivery = self.transport.Delivery(self.box, self.token, self.chat, self.api_url,
                                               self.api_url, allow_local_test=True)

    def event(self, state="ALARM", check="cpu"):
        return {"status": "firing", "startsAt": "2026-01-01T00:00:00Z",
                "labels": {"check": check, "cluster": "local", "service": "glamdb", "state": state},
                "annotations": {"summary": "Example alert", "observed_at": str(self.now)}}

    def test_real_webhook_auth_validation_and_fixed_chat_delivery(self):
        with running(self.transport.server(("127.0.0.1", 0), self.box, self.bearer)) as url:
            event = self.event()
            event["labels"]["chat_id"] = "999999"
            body = json.dumps({"alerts": [event], "chat_id": "888888"}).encode()
            request = Request(url + "/alerts", data=body, headers={"Authorization": "Bearer wrong"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=2)
            self.assertEqual(error.exception.code, 401)
            error.exception.close()
            request.add_header("Authorization", "Bearer test-authentication-secret")
            with urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 202)
            with urlopen(url + "/healthz", timeout=2) as response:
                self.assertEqual(response.status, 200)
        self.delivery.send(self.box.pending()[0])
        self.assertEqual(self.api.calls[0][1]["chat_id"], -10012345)
        self.assertNotIn("parse_mode", self.api.calls[0][1])
        self.assertEqual(self.box.pending(), [])

    def test_429_honors_retry_after_and_eventual_delivery(self):
        self.api.replies = [(429, {"ok": False, "parameters": {"retry_after": 17}})]
        self.box.accept([self.event()])
        self.delivery.send(self.box.pending()[0])
        self.assertEqual(self.box.pending(), [])
        self.now += 16
        self.assertEqual(self.box.pending(), [])
        self.now += 2
        self.delivery.send(self.box.pending()[0])
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(self.box.pending(), [])

    def test_429_blocks_the_whole_fixed_chat_not_just_one_event(self):
        self.api.replies = [(429, {"ok": False, "parameters": {"retry_after": 17}})]
        self.box.accept([self.event(check="a"), self.event(check="b")])
        prefetched = self.box.pending()
        self.delivery.send(prefetched[0])
        self.delivery.send(prefetched[1])
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(self.box.pending(), [])
        self.now += 16
        self.assertEqual(self.box.pending(), [])
        self.now += 2
        self.delivery.send(self.box.pending()[0])
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(self.box.pending(), [])
        self.now += 4
        self.delivery.send(self.box.pending()[0])
        self.assertEqual(len(self.api.calls), 3)
        self.assertIn("glamdb_alert_transport_pending 0", self.box.metrics())

    def test_5xx_retries_but_4xx_does_not_loop(self):
        self.api.replies = [(503, {"ok": False}), (400, {"ok": False})]
        self.box.accept([self.event()])
        self.delivery.send(self.box.pending()[0])
        self.now += 2
        self.delivery.send(self.box.pending()[0])
        self.assertEqual(self.box.pending(), [])
        self.assertIn('outcome="rejected"} 1', self.box.metrics())

    def test_queued_ok_is_discarded_when_data_vanishes(self):
        self.box.accept([self.event("OK")])
        self.delivery.send(self.box.pending()[0])
        self.assertEqual(self.api.calls, [])
        self.assertIn('outcome="unverified_recovery"} 1', self.box.metrics())

    def test_ok_is_sent_only_for_current_fresh_exact_state(self):
        event = self.event("OK")
        self.api.state = {"metric": {k: v for k, v in event["labels"].items() if k != "state"},
                          "value": [time.time(), "0"]}
        self.box.accept([event])
        self.delivery.send(self.box.pending()[0])
        self.assertEqual(len(self.api.calls), 1)
        self.assertTrue(self.api.calls[0][1]["text"].startswith("OK GlamDB"))

    def test_redirect_cannot_relay_a_token_and_errors_are_silent(self):
        self.api.replies = [(302, {})]
        self.box.accept([self.event()])
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.delivery.send(self.box.pending()[0])
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(output.getvalue(), "")

    def test_load_is_bounded_and_duplicate_bursts_coalesce(self):
        for batch in range(10):
            events = [self.event(check=f"check_{batch * 100 + i}") for i in range(100)]
            self.box.accept(events)
            self.box.accept(events)
        self.assertEqual(self.box.history_size(), 1000)
        with self.assertRaises(self.transport.QueueFull):
            self.box.accept([self.event(check="overflow")])
        self.assertIn("glamdb_alert_transport_pending 1000", self.box.metrics())

    def test_invalid_and_large_webhooks_cannot_fill_the_queue(self):
        with running(self.transport.server(("127.0.0.1", 0), self.box, self.bearer)) as url:
            for data, code in ((b"not-json", 400), (b"x" * 65537, 413)):
                request = Request(url + "/alerts", data=data,
                                  headers={"Authorization": "Bearer test-authentication-secret"})
                with self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=2)
                self.assertEqual(error.exception.code, code)
                error.exception.close()
        self.assertEqual(self.box.pending(), [])

    def test_full_queue_http_503_is_retryable_and_preserves_existing_event(self):
        self.box.capacity = 1
        self.box.accept([self.event(check="existing")])
        with running(self.transport.server(("127.0.0.1", 0), self.box, self.bearer)) as url:
            request = Request(url + "/alerts", data=json.dumps({"alerts": [self.event(check="new")]}).encode(),
                              headers={"Authorization": "Bearer test-authentication-secret"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=2)
            self.assertEqual(error.exception.code, 503)
            error.exception.close()
        self.assertEqual([x["labels"]["check"] for x in self.box.pending()], ["existing"])

    def test_timeout_retries_without_printing_token_url(self):
        self.api.delay = 0.2
        self.delivery.timeout = 0.05
        self.box.accept([self.event()])
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.delivery.send(self.box.pending()[0])
        self.assertEqual(output.getvalue(), "")
        self.assertIn('outcome="retries"} 1', self.box.metrics())

    def test_pending_notification_is_delivered_after_outbox_reopen(self):
        self.box.accept([self.event()])
        self.box.close()
        self.box = self.transport.Outbox(self.database, 1000, clock=lambda: self.now)
        self.addCleanup(self.box.close)
        self.delivery.box = self.box
        self.delivery.send(self.box.pending()[0])
        self.assertEqual(len(self.api.calls), 1)
        self.assertIn('outcome="delivered"} 1', self.box.metrics())

    def test_missing_secret_and_network_failure_retry_without_secret_output(self):
        self.token.unlink()
        self.box.accept([self.event()])
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.delivery.send(self.box.pending()[0])
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.api.calls, [])
        self.assertIn('outcome="retries"} 1', self.box.metrics())


if __name__ == "__main__":
    unittest.main()
