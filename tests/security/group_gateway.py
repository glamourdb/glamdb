"""Real Caddy routing to explicitly synthetic test databases on loopback."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from uuid import UUID, uuid4

import httpx

from glamdb_sdk import Client, Field, Schema
from tests.integration.helpers.http_server import MockHTTPServer
from .conftest import ROOT, free_port


COLLECTION_ID = UUID("66f59fe3-4874-4f6f-8e71-f8f9dc5629e1")
SCHEMA = Schema([Field("key", "string", False), Field("n", "int64", True)])


class GroupGateway:
    def __init__(self, directory, tls, caddy):
        self.directory, self.tls, self.caddy = directory, tls, caddy
        self.port, self.http_port = free_port(), free_port()
        while self.port == self.http_port:
            self.http_port = free_port()
        self.url = f"https://127.0.0.1:{self.port}"
        self.output, self.state = directory / "site", directory / "state"
        self.registry_path = directory / "access.json"
        self.log_path = directory / "gateway.log"
        self.process = self.log = None
        self.accounts = {key: (key, secrets.token_urlsafe(32)) for key in ("pink", "blue", "empty")}
        self.hashes = {login: self.hash_token(token) for login, token in self.accounts.values()}
        self.environment = os.environ.copy()
        self.backends = {}

    def hash_token(self, token):
        result = subprocess.run([self.caddy, "hash-password", "--algorithm", "bcrypt", "--bcrypt-cost", "12"],
                                input=token + "\n", text=True, capture_output=True, check=True, timeout=10)
        return result.stdout.strip()

    def start_backends(self, stack: ExitStack):
        databases = []
        for identifier, collection in (("alpha", "alpha_only"), ("beta", "beta_only"), ("shared", "common")):
            token = secrets.token_urlsafe(32)
            server = stack.enter_context(MockHTTPServer(self.directory / identifier, token=token))
            self.backends[identifier] = server
            key = f"GROUP_TEST_DATABASE_{identifier.upper()}_TOKEN"
            self.environment[key] = token
            databases.append({"id": identifier, "name": identifier.title(), "url": server.url, "token_env": key})
            with Client(server.url, token) as client:
                client.create_collection(collection, SCHEMA, collection_id=COLLECTION_ID)
                client.append(COLLECTION_ID, [{"key": identifier + "-row", "n": 1}],
                              schema=SCHEMA, operation_id=uuid4())
        self.environment["GROUP_TEST_DATABASE_OFFLINE_TOKEN"] = secrets.token_urlsafe(32)
        databases.append({"id": "offline", "name": "Offline", "url": f"http://127.0.0.1:{free_port()}",
                          "token_env": "GROUP_TEST_DATABASE_OFFLINE_TOKEN"})
        grants = {"pink": ["alpha", "shared", "offline"], "blue": ["beta", "shared"], "empty": []}
        self.registry = {"version": 1, "databases": databases, "groups": [
            {"id": login, "login": login, "name": login.title() + " team", "password_hash": self.hashes[login],
             "databases": grants[login]} for login in self.accounts]}
        self.save_registry()

    def save_registry(self):
        self.registry_path.write_text(json.dumps(self.registry), encoding="utf-8")
        self.registry_path.chmod(0o600)

    def start(self):
        command = [sys.executable, "-m", "apps.workbench_access", "--access-config", str(self.registry_path),
                   "--host", f"127.0.0.1:{self.port}", "--caddy", self.caddy, "--output", str(self.output),
                   "--state", str(self.state), "--bind", "127.0.0.1", "--http-port", str(self.http_port),
                   "--tls-cert", str(self.tls.certificate), "--tls-key", str(self.tls.key)]
        self.log = self.log_path.open("ab")
        self.process = subprocess.Popen(command, cwd=ROOT, env=self.environment, stdin=subprocess.DEVNULL,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        with self.client(timeout=0.3) as client:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("Group gateway exited before readiness: " + self.logs())
                try:
                    if client.get("/workbench/session").status_code == 401:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.025)
        raise RuntimeError("Group gateway readiness deadline exceeded: " + self.logs())

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def client(self, group=None, **options):
        return httpx.Client(base_url=self.url, verify=self.tls.trusted_context(), trust_env=False,
                            auth=self.accounts[group] if group else None, **options)

    def logs(self):
        data = self.log_path.read_text()
        for value in [*(password for _, password in self.accounts.values()), *self.hashes.values(),
                      *(value for key, value in self.environment.items() if key.startswith("GROUP_TEST_DATABASE_"))]:
            data = data.replace(value, "[redacted]")
        return data
