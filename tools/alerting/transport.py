#!/usr/bin/env python3
"""Bounded, persistent Alertmanager webhook transport for one Telegram chat."""

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import random
import re
import signal
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


STATES = {"OK": 0, "WARN": 1, "ALARM": 2, "NODATA": 3}
IDENTITY = ("check", "cluster", "service", "namespace", "pod", "node")
MAX_BODY = 65536


class QueueFull(Exception):
    pass


def clean(value, limit):
    return " ".join(re.sub(r"<[^>]*>", "", str(value)).split())[:limit]


def event(raw):
    if raw.get("status") == "resolved":
        return None
    if raw.get("status") != "firing":
        raise ValueError("status")
    labels = {k: clean(v, 128) for k, v in raw["labels"].items() if k in IDENTITY}
    if not all(labels.get(k) for k in ("check", "cluster", "service")):
        raise ValueError("identity")
    state = raw["labels"]["state"]
    if state not in STATES:
        raise ValueError("state")
    started = datetime.fromisoformat(raw["startsAt"].replace("Z", "+00:00"))
    if started.tzinfo is None:
        raise ValueError("timezone")
    identity = json.dumps(labels, sort_keys=True, separators=(",", ":"))
    key = hashlib.sha256((identity + state + str(started.timestamp())).encode()).hexdigest()
    annotations = raw.get("annotations", {})
    return {"identity": identity, "key": key, "started": started.timestamp(), "state": state,
            "labels": labels, "summary": clean(annotations.get("summary", labels["check"]), 500),
            "observed": clean(annotations.get("observed_at", raw["startsAt"]), 64)}


class Outbox:
    def __init__(self, path, capacity=1000, clock=time.time):
        if not 1 <= capacity <= 100000:
            raise ValueError("capacity")
        self.clock, self.capacity = clock, capacity
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (identity TEXT PRIMARY KEY, event_key TEXT, "
                        "started REAL, state TEXT, labels TEXT, summary TEXT, observed TEXT, "
                        "received REAL, expires REAL, attempts INTEGER, next_try REAL, pending INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS limits (name TEXT PRIMARY KEY, value REAL)")

    def close(self):
        with self.lock:
            self.db.close()

    def count(self, name, delta=1):
        self.db.execute("INSERT INTO counters VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=value+?",
                        (name, delta, delta))

    def accept(self, alerts):
        if not isinstance(alerts, list) or len(alerts) > 100:
            raise ValueError("alerts")
        events = [e for raw in alerts if (e := event(raw)) is not None]
        now = self.clock()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                expired = self.db.execute("SELECT count(*) FROM events WHERE pending=1 AND expires<=?", (now,)).fetchone()[0]
                self.db.execute("UPDATE events SET pending=0 WHERE pending=1 AND expires<=?", (now,))
                if expired:
                    self.count("expired", expired)
                for item in events:
                    old = self.db.execute("SELECT * FROM events WHERE identity=?", (item["identity"],)).fetchone()
                    duplicate = old and old["event_key"] == item["key"] and (old["pending"] or now - old["received"] < 14400)
                    if old and (duplicate or old["started"] > item["started"]):
                        continue
                    pending = self.db.execute("SELECT count(*) FROM events WHERE pending=1").fetchone()[0]
                    if pending >= self.capacity and not (old and old["pending"]):
                        raise QueueFull()
                    ttl = 300 if item["state"] == "OK" else 86400
                    self.db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                                    (item["identity"], item["key"], item["started"], item["state"],
                                     json.dumps(item["labels"]), item["summary"], item["observed"],
                                     now, now + ttl, 0, now))
                    self.count("accepted")
                # Keep bounded recent dedup history, including across restarts.
                self.db.execute("DELETE FROM events WHERE pending=0 AND identity NOT IN "
                                "(SELECT identity FROM events WHERE pending=0 ORDER BY received DESC LIMIT ?)",
                                (self.capacity,))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def pending(self):
        with self.lock:
            expired = self.db.execute("SELECT count(*) FROM events WHERE pending=1 AND expires<=?",
                                      (self.clock(),)).fetchone()[0]
            self.db.execute("UPDATE events SET pending=0 WHERE pending=1 AND expires<=?", (self.clock(),))
            if expired:
                self.count("expired", expired)
            if not self.delivery_allowed():
                return []
            rows = self.db.execute("SELECT * FROM events WHERE pending=1 AND next_try<=? "
                                   "ORDER BY received,identity LIMIT 100", (self.clock(),)).fetchall()
            return [{**dict(row), "labels": json.loads(row["labels"])} for row in rows]

    def delivery_allowed(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM limits WHERE name='chat_blocked_until'").fetchone()
            return not row or row[0] <= self.clock()

    def defer_chat(self, delay):
        with self.lock:
            self.db.execute("INSERT INTO limits VALUES ('chat_blocked_until',?) "
                            "ON CONFLICT(name) DO UPDATE SET value=max(value,excluded.value)",
                            (self.clock() + delay,))

    def complete(self, item, outcome="delivered"):
        with self.lock, self.db:
            cursor = self.db.execute("UPDATE events SET pending=0 WHERE identity=? AND event_key=? AND pending=1",
                                     (item["identity"], item["event_key"]))
            if cursor.rowcount:
                self.count(outcome)

    def retry(self, item, retry_after=0, max_attempts=20):
        if item["attempts"] + 1 >= max_attempts:
            self.complete(item, "exhausted")
            return
        if retry_after > 86400:
            self.complete(item, "exhausted")
            return
        delay = max(float(retry_after), min(300, 2 ** min(item["attempts"], 9)))
        delay += random.uniform(0, min(1, delay * 0.1))
        with self.lock, self.db:
            self.db.execute("UPDATE events SET attempts=attempts+1,next_try=? "
                            "WHERE identity=? AND event_key=? AND pending=1",
                            (self.clock() + delay, item["identity"], item["event_key"]))
            self.count("retries")

    def history_size(self):
        with self.lock:
            return self.db.execute("SELECT count(*) FROM events").fetchone()[0]

    def metrics(self):
        with self.lock:
            pending = self.db.execute("SELECT count(*) FROM events WHERE pending=1").fetchone()[0]
            values = self.db.execute("SELECT name,value FROM counters ORDER BY name").fetchall()
        result = [f"glamdb_alert_transport_pending {pending}", f"glamdb_alert_transport_capacity {self.capacity}"]
        result += [f'glamdb_alert_transport_events_total{{outcome="{name}"}} {value}' for name, value in values]
        return "\n".join(result) + "\n"


def valid_recovery(result, labels, now, max_age):
    """OK needs an exact current identity, finite zero, and a recent sample."""
    if len(result) != 1:
        return False
    row = result[0]
    if any(row.get("metric", {}).get(k, "") != labels.get(k, "") for k in IDENTITY):
        return False
    try:
        stamp, value = map(float, row["value"])
        return math.isfinite(value) and value == 0 and -5 <= now - stamp < max_age
    except (ValueError, TypeError, KeyError):
        return False


def message(item):
    labels = item["labels"]
    scope = " ".join(f"{key}={labels[key]}" for key in IDENTITY if labels.get(key) and key != "check")
    return (f"{item['state']} GlamDB {labels['check']}\n{scope}\n{item['summary']}\n"
            f"Observed: {item['observed']}\nEvent: {item['event_key'][:12]}")[:1800]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Delivery:
    def __init__(self, outbox, token_file, chat_file, prometheus_url, api_url="https://api.telegram.org",
                 timeout=5, allow_local_test=False, max_attempts=20):
        parsed = urlsplit(api_url)
        local_test = allow_local_test and parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
        if api_url != "https://api.telegram.org" and not local_test:
            raise ValueError("Telegram API must be official HTTPS or an explicit loopback test endpoint")
        if not 0 < timeout <= 30 or not 1 <= max_attempts <= 100:
            raise ValueError("delivery bounds")
        self.box, self.token_file, self.chat_file = outbox, Path(token_file), Path(chat_file)
        self.prometheus_url, self.api_url = prometheus_url.rstrip("/"), api_url.rstrip("/")
        self.timeout, self.max_attempts = timeout, max_attempts
        self.http = build_opener(NoRedirect)

    def recovery_is_current(self, item):
        labels = item["labels"]
        selectors = ",".join(f"{key}={json.dumps(labels[key])}" for key in IDENTITY if key in labels)
        metric = "glamdb_alert_state{" + selectors + "}"
        # An instant-query result's timestamp can be the query time. Inspect
        # the stored sample's timestamp inside PromQL, not only the JSON value.
        query = f"({metric}) and (time()-timestamp({metric}) < 90) and (time()-timestamp({metric}) >= -5)"
        request = Request(self.prometheus_url + "/api/v1/query?" + urlencode({"query": query}))
        try:
            with self.http.open(request, timeout=self.timeout) as response:
                body = json.loads(response.read(MAX_BODY))
            return body.get("status") == "success" and valid_recovery(
                body["data"]["result"], labels, time.time(), max_age=90)
        except HTTPError as error:
            error.close()
            return False
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False

    def send(self, item):
        if not self.box.delivery_allowed():
            return
        if item["state"] == "OK" and not self.recovery_is_current(item):
            self.box.complete(item, "unverified_recovery")
            return
        try:
            token = self.token_file.read_text().strip()
            if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
                raise ValueError("token")
            chat = int(self.chat_file.read_text().strip())
            if chat == 0:
                raise ValueError("chat")
            data = json.dumps({"chat_id": chat, "text": message(item),
                               "link_preview_options": {"is_disabled": True}}).encode()
            request = Request(self.api_url + "/bot" + token + "/sendMessage", data=data,
                              headers={"Content-Type": "application/json"})
            with self.http.open(request, timeout=self.timeout) as response:
                body = json.loads(response.read(MAX_BODY))
            if not isinstance(body, dict):
                raise ValueError("invalid response")
            if not body.get("ok"):
                self.box.retry(item, self.retry_after(body), self.max_attempts)
            else:
                self.box.complete(item)
                # One destination, including groups: pace to 20 messages/min.
                self.box.defer_chat(3)
        except HTTPError as error:
            # Never render exceptions: their URL can contain the bot token.
            try:
                body = json.loads(error.read(MAX_BODY))
            except (OSError, ValueError):
                body = {}
            finally:
                error.close()
            if error.code == 429 or error.code >= 500:
                if error.code == 429:
                    self.box.defer_chat(max(1, self.retry_after(body)))
                self.box.retry(item, self.retry_after(body), self.max_attempts)
            else:
                self.box.complete(item, "rejected")
        except (OSError, ValueError, URLError):
            self.box.retry(item, max_attempts=self.max_attempts)

    @staticmethod
    def retry_after(body):
        try:
            value = float(body.get("parameters", {}).get("retry_after", 0))
            return value if math.isfinite(value) and value >= 0 else 0
        except (TypeError, ValueError, AttributeError):
            return 0


def server(address, box, bearer_file):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def reply(self, code, body=b"", kind="text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/healthz":
                self.reply(200, b"ok\n")
            elif self.path == "/metrics":
                self.reply(200, box.metrics().encode(), "text/plain; version=0.0.4")
            else:
                self.reply(404)

        def do_POST(self):
            if self.path != "/alerts":
                self.reply(404)
                return
            try:
                secret = Path(bearer_file).read_text().strip()
                if not secret or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + secret):
                    self.reply(401)
                    return
                size = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= size <= MAX_BODY:
                    self.reply(413)
                    return
                body = json.loads(self.rfile.read(size))
                box.accept(body["alerts"])
                self.reply(202)
            except QueueFull:
                self.reply(503)
            except (ValueError, KeyError, TypeError, AttributeError):
                self.reply(400)
            except (OSError, sqlite3.Error):
                self.reply(503)

    return HTTPServer(address, Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="0.0.0.0:9087")
    parser.add_argument("--database", default="/data/outbox.sqlite")
    parser.add_argument("--capacity", type=int, default=1000)
    parser.add_argument("--token-file", default=os.environ.get("TELEGRAM_BOT_TOKEN_FILE", "/run/secrets/telegram_bot_token"))
    parser.add_argument("--chat-file", default=os.environ.get("TELEGRAM_CHAT_ID_FILE", "/run/secrets/telegram_chat_id"))
    parser.add_argument("--bearer-file", default=os.environ.get("ALERT_WEBHOOK_BEARER_FILE", "/run/secrets/alert_webhook_bearer"))
    parser.add_argument("--prometheus-url", default=os.environ.get("PROMETHEUS_URL", "http://prometheus:9090"))
    args = parser.parse_args()
    # Fail before opening a listener when configuration files are absent.
    for path in (args.token_file, args.chat_file, args.bearer_file):
        if not Path(path).is_file():
            parser.exit(1, "a required secret file is missing\n")
    Path(args.database).parent.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    box = Outbox(args.database, args.capacity)
    delivery = Delivery(box, args.token_file, args.chat_file, args.prometheus_url)
    host, port = args.listen.rsplit(":", 1)
    httpd = server((host, int(port)), box, args.bearer_file)
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            try:
                for item in box.pending():
                    if stop.is_set():
                        break
                    delivery.send(item)
            except Exception:
                # Health failures are visible through the listener/process;
                # never print payloads, secret paths, or Telegram URLs.
                stop.set()
                threading.Thread(target=httpd.shutdown, daemon=True).start()
            stop.wait(0.5)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    def shutdown(*_):
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        thread.join(timeout=31)
        httpd.server_close()
        box.close()


if __name__ == "__main__":
    main()
