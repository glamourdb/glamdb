#!/usr/bin/env python3
"""Write a synthetic fixture, query real Loki, and prove collector field projection."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from urllib.parse import urlencode
from urllib.request import urlopen


def query(loki_url, expression):
    parameters = urlencode({"query": expression, "start": time.time_ns() - 120_000_000_000,
                            "end": time.time_ns(), "limit": 1000})
    with urlopen(loki_url.rstrip("/") + "/loki/api/v1/query_range?" + parameters, timeout=5) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError("Loki query did not succeed")
    return [(stream["stream"], json.loads(line)) for stream in payload["data"]["result"]
            for _, line in stream["values"]]


def run(loki_url, log_directory, probe=None, timeout=45):
    directory = Path(log_directory).resolve()
    if probe:
        subprocess.run([str(Path(probe).resolve()), "errors", str(directory), "10"], check=True,
                       capture_output=True, text=True, timeout=15)
    marker = secrets.token_hex(16)
    fixture = {
        "schema": 1, "timestamp": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "service": "glamdb", "level": "warn", "event": "request_completed", "operation": "auth",
        "outcome": "denied", "request_id": marker, "duration_us": 17, "status_code": 403,
        "revision": 42, "org_id": "example-team", "database_id": "example-db",
        "password": "synthetic-secret-password", "Authorization": "Bearer synthetic-secret-token",
        "Cookie": "session=synthetic-secret-cookie", "query": "token=synthetic-secret-query",
        "body": "synthetic-secret-body", "path": "/synthetic-secret-path",
        "error": "synthetic-secret-error", "nested": {"token": "synthetic-secret-nested"},
    }
    fd = os.open(directory / "glamdb.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(fixture) + "\n").encode())
        invalid = {"timestamp": fixture["timestamp"], "message": "synthetic-secret-raw", "request_id": marker}
        os.write(fd, (json.dumps(invalid) + "\n").encode())
    finally:
        os.close(fd)
    expression = '{service="glamdb"} |= "' + marker + '"'
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        records = query(loki_url, expression)
        if records:
            assert len(records) == 1, "Invalid-schema fixture was forwarded"
            for labels, record in records:
                text = json.dumps(record)
                assert "synthetic-secret" not in text, text
                assert record["status_code"] == 403, record
                assert record["duration_us"] == 17, record
                assert record["org_id"] == "example-team", record
                assert record["database_id"] == "example-db", record
                assert record["request_id"] == marker, record
                assert set(labels) <= {"service", "environment", "level", "event", "detected_level", "service_name"}, labels
            if probe:
                native = query(loki_url, '{service="glamdb"} | json | operation="append" | status_code=503')
                assert any(record["duration_us"] == 123 and record["row_count"] == 7 for _, record in native), native
            return {"status": "PASS", "projection_records": len(records),
                    "native_records": len(native) if probe else None, "correlation_id": marker}
        time.sleep(0.25)
    raise TimeoutError("Synthetic log did not become queryable before the timeout")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loki-url", default="http://127.0.0.1:3100")
    parser.add_argument("--log-directory", required=True)
    parser.add_argument("--probe")
    parser.add_argument("--timeout", type=int, default=45)
    args = parser.parse_args()
    print(json.dumps(run(args.loki_url, args.log_directory, args.probe, args.timeout), sort_keys=True))


if __name__ == "__main__":
    main()
