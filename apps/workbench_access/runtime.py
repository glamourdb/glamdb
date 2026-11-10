"""One Caddy child, bounded startup and a connection fence for access changes."""

import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import time
from uuid import uuid4

from .config import AccessConfigError, caddy_config, database_token


ROOT = Path(__file__).resolve().parents[2]


def protected_json(path, data):
    temporary = path.with_name(path.name + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(data, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def check_listeners(bind, port, http_port, metrics_listen):
    addresses = [(bind, port), (bind, http_port)]
    if metrics_listen:
        host, port = metrics_listen.rsplit(":", 1)
        addresses.append((host, int(port)))
    probes = []
    try:
        for address in addresses:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probes.append(probe)
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(address)
            probe.listen(1)
    except OSError:
        raise AccessConfigError("gateway listener is unavailable or already owned by another process") from None
    finally:
        for probe in probes:
            probe.close()

class GatewayRuntime:
    def __init__(self, args):
        self.args = args
        self.site, self.state = args.output.resolve(), args.state.resolve()
        self.sessions = self.state / "sessions"
        self.host, _, port = args.host.partition(":")
        self.port = int(port) if port else 443
        self.executable = shutil.which(args.caddy)
        if self.executable is None:
            raise AccessConfigError("Caddy is required")
        self.process = None
        self.applied_revision = None
        self.last_error = None
        self.gateway_id = uuid4().hex
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state.chmod(0o700)

    def validate(self, registry):
        for database in registry.databases:
            database_token(database, os.environ)
            for file in (database.token_file, database.ca_file):
                if file and (not file.is_file() or file.is_relative_to(self.site) or file.is_relative_to(self.state)
                             or file.is_relative_to(ROOT / "ui")):
                    raise AccessConfigError("credential and CA files must exist outside UI, runtime-state and served trees")

    def validate_private_path(self, path):
        path = Path(path).resolve()
        if path.is_relative_to(self.site) or path.is_relative_to(self.state) or path.is_relative_to(ROOT / "ui"):
            raise AccessConfigError("authorization files must exist outside UI, runtime-state and served trees")

    def _prepare(self, registry, revision):
        self.validate(registry)
        try:
            subprocess.run([sys.executable, str(ROOT / "scripts/build-public-demo.py"),
                            "--mode", "gateway", "--output", str(self.site)], check=True,
                           timeout=60, stdout=subprocess.DEVNULL)
        except subprocess.SubprocessError:
            raise AccessConfigError("connected UI packaging failed; inspect the private service log") from None
        self.sessions.mkdir(mode=0o700, exist_ok=True)
        marker = self.sessions / ".glamdb-sessions"
        if not marker.exists() and any(self.sessions.iterdir()):
            raise AccessConfigError("refusing an unmarked session directory")
        marker.touch(mode=0o600)
        active = {group.id + ".json" for group in registry.groups}
        for old in self.sessions.glob("*.json"):
            if old.name not in active:
                old.unlink()
        environment = os.environ.copy()
        backend_tokens = [database_token(database, environment) for database in registry.databases]
        index = {database.id: database for database in registry.databases}
        for i, organization in enumerate(registry.groups):
            environment[f"GLAMDB_ACCESS_GROUP_HASH_{i}"] = organization.password_hash
            protected_json(self.sessions / f"{organization.id}.json", {
                "version": 2, "organization": {"id": organization.id, "name": organization.name, "login": organization.login},
                "databases": [{"id": database, "name": index[database].name} for database in organization.databases]})
        for i, token in enumerate(backend_tokens):
            environment[f"GLAMDB_ACCESS_BACKEND_TOKEN_{i}"] = token
        environment.update(XDG_DATA_HOME=str(self.state / "data"), XDG_CONFIG_HOME=str(self.state / "config"))
        config = caddy_config(registry, host=self.host, port=self.port, http_port=self.args.http_port,
                              bind=self.args.bind, site=self.site, sessions=self.sessions,
                              theme=ROOT / "ui/config/theme.json", revision=revision,
                              certificate=self.args.tls_cert.resolve() if self.args.tls_cert else None,
                              key=self.args.tls_key.resolve() if self.args.tls_key else None,
                              metrics_listen=self.args.metrics_listen, gateway_id=self.gateway_id)
        path = self.state / "caddy.json"
        protected_json(path, config)
        return path, environment

    def _ready(self, revision):
        # Local probe sends no credentials or user data. User-facing TLS is
        # independently verified by clients, not by this process-readiness check.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        address = "127.0.0.1" if self.args.bind == "0.0.0.0" else self.args.bind
        raw = socket.create_connection((address, self.port), timeout=0.4)
        connection = http.client.HTTPConnection(self.host, self.port, timeout=0.4)
        try:
            connection.sock = context.wrap_socket(raw, server_hostname=self.host)
            connection.request("GET", "/workbench/session", headers={"Host": self.args.host})
            response = connection.getresponse()
            return (response.status == 401 and response.getheader("X-GlamDB-Access-Revision") == str(revision)
                    and response.getheader("X-GlamDB-Gateway-ID") == self.gateway_id)
        finally:
            connection.close()
            raw.close()

    def _check_listeners(self):
        check_listeners(self.args.bind, self.port, self.args.http_port, self.args.metrics_listen)

    def start(self, registry, revision):
        if self.process is not None:
            raise AccessConfigError("gateway must be fenced before applying a new snapshot")
        try:
            self._check_listeners()
            path, environment = self._prepare(registry, revision)
            self.process = subprocess.Popen([self.executable, "run", "--config", str(path)],
                                            env=environment, stdin=subprocess.DEVNULL,
                                            stdout=sys.stderr, stderr=sys.stderr,
                                            pass_fds=tuple(getattr(self.args, "lease_fds", ())))
            deadline = time.monotonic() + self.args.ready_timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise AccessConfigError("gateway process exited during startup")
                try:
                    if self._ready(revision):
                        self.applied_revision, self.last_error = revision, None
                        return
                except (OSError, ssl.SSLError, http.client.HTTPException):
                    pass
                time.sleep(0.05)
            raise AccessConfigError("gateway readiness deadline exceeded")
        except BaseException:
            self.stop()
            self.last_error = "GATEWAY_UNAVAILABLE"
            raise

    def stop(self):
        self.applied_revision = None
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=self.args.drain_timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None

    def status(self):
        running = self.process is not None and self.process.poll() is None
        return {"gateway_running": running, "applied_revision": self.applied_revision if running else None,
                "gateway_pid": self.process.pid if running else None, "last_error": self.last_error}
