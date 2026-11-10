"""Actual organization supervisor and Caddy, with disposable test databases."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import httpx

from apps.workbench_access.admin import socket_request
from apps.workbench_access.catalog import AuthorizationCatalog, initialize
from .conftest import ROOT, free_port
from .test_organization_catalog import command


class OrganizationGateway:
    def __init__(self, directory, fleet):
        self.directory, self.fleet = directory, fleet
        self.port, self.http_port = free_port(), free_port()
        while self.port == self.http_port:
            self.http_port = free_port()
        self.url = f"https://127.0.0.1:{self.port}"
        self.metrics_port = free_port()
        self.state = directory / "state"
        self.catalog_path = directory / "authorization.sqlite"
        self.socket_path = self.state / "operator.sock"
        self.log_path = directory / "supervisor.log"
        self.process = self.log = None
        initialize(self.catalog_path)
        catalog = AuthorizationCatalog(self.catalog_path)
        try:
            commands = [{"action": "organization.create", "params": {
                "id": login, "name": login.title() + " organization", "login": login,
                "password_hash": fleet.hashes[login]}} for login in fleet.accounts]
            for database in fleet.registry["databases"]:
                owner = "blue" if database["id"] == "beta" else "pink"
                commands.append({"action": "database.create", "params": {**database, "organization_id": owner}})
            catalog.apply(command("batch", commands=commands))
        finally:
            catalog.close()

    def start(self):
        args = [sys.executable, "-m", "apps.workbench_access", "--catalog", str(self.catalog_path),
                "--host", f"127.0.0.1:{self.port}", "--caddy", self.fleet.caddy,
                "--output", str(self.directory / "site"), "--state", str(self.state),
                "--bind", "127.0.0.1", "--http-port", str(self.http_port),
                "--tls-cert", str(self.fleet.tls.certificate), "--tls-key", str(self.fleet.tls.key),
                "--ready-timeout", "8", "--drain-timeout", "1",
                "--metrics-listen", f"127.0.0.1:{self.metrics_port}"]
        self.log = self.log_path.open("ab")
        self.process = subprocess.Popen(args, cwd=ROOT, env=self.fleet.environment, stdin=subprocess.DEVNULL,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("organization supervisor exited: " + self.logs())
            if self.socket_path.exists():
                try:
                    if self.admin({"action": "status"}, timeout=0.5)["gateway_running"]:
                        return
                except (OSError, ValueError):
                    pass
            time.sleep(0.05)
        raise RuntimeError("organization supervisor failed readiness: " + self.logs())

    def admin(self, request, timeout=15):
        return socket_request(self.socket_path, request, timeout=timeout)

    def client(self, organization, **options):
        return httpx.Client(base_url=self.url, auth=self.fleet.accounts[organization],
                            verify=self.fleet.tls.trusted_context(), trust_env=False, **options)

    def logs(self):
        text = self.log_path.read_text()
        for secret in [*(password for _, password in self.fleet.accounts.values()), *self.fleet.hashes.values(),
                       *(value for key, value in self.fleet.environment.items() if key.startswith("GROUP_TEST_DATABASE_"))]:
            text = text.replace(secret, "[redacted]")
        return text

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None
