"""Disposable real Caddy process, TLS CA and credentials; no mock adapter."""

import os
from contextlib import ExitStack
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

from tests.integration.helpers.http_server import make_tls_material


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session", autouse=True)
def loopback_connections_stay_local():
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
        patch.setenv("no_proxy", "127.0.0.1,localhost,::1")
        yield


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class ProtectedSite:
    def __init__(self, directory, tls, caddy):
        self.directory, self.tls, self.caddy = directory, tls, caddy
        self.https_port, self.http_port = free_port(), free_port()
        while self.http_port == self.https_port:
            self.http_port = free_port()
        self.url = f"https://127.0.0.1:{self.https_port}"
        self.http_url = f"http://127.0.0.1:{self.http_port}"
        self.user = "demo-test"
        self.output = directory / "site"
        self.state = directory / "state"
        self.log_path = directory / "server.log"
        self.process = self.log = None

    def start(self):
        self.password = secrets.token_urlsafe(32)
        result = subprocess.run([self.caddy, "hash-password", "--algorithm", "bcrypt",
                                 "--bcrypt-cost", "12"], input=self.password + "\n",
                                text=True, capture_output=True, check=True, timeout=10)
        self.password_hash = result.stdout.strip()
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("GLAMDB_DEMO_")}
        environment.update(GLAMDB_DEMO_HOST=f"127.0.0.1:{self.https_port}",
                           GLAMDB_DEMO_USER=self.user,
                           GLAMDB_DEMO_PASSWORD_HASH=self.password_hash)
        command = [sys.executable, str(ROOT / "scripts/serve-protected-demo.py"),
                   "--caddy", self.caddy, "--output", str(self.output),
                   "--state", str(self.state), "--bind", "127.0.0.1",
                   "--http-port", str(self.http_port),
                   "--tls-cert", str(self.tls.certificate), "--tls-key", str(self.tls.key)]
        self.log = self.log_path.open("ab")
        self.process = subprocess.Popen(command, env=environment, cwd=ROOT,
                                        stdin=subprocess.DEVNULL, stdout=self.log,
                                        stderr=subprocess.STDOUT)
        with self.client(timeout=0.3) as client:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("Caddy exited before readiness: " + self.logs())
                try:
                    if client.get("/").status_code == 401:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.025)
        raise RuntimeError("Protected demo did not reach HTTPS authentication readiness")

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

    def client(self, **options):
        return httpx.Client(base_url=self.url, verify=self.tls.trusted_context(),
                            trust_env=False, **options)

    @property
    def auth(self):
        return self.user, self.password

    def logs(self):
        return self.log_path.read_text().replace(self.password, "[redacted]").replace(
            self.password_hash, "[redacted]")


@pytest.fixture(scope="session")
def protected_site(tmp_path_factory):
    caddy = shutil.which(os.environ.get("GLAMDB_DEMO_CADDY", "caddy"))
    if caddy is None:
        pytest.fail("Real Caddy required: python3 scripts/fetch-demo-caddy.py, then set GLAMDB_DEMO_CADDY")
    tls = make_tls_material(tmp_path_factory.mktemp("protected-demo-ca"))
    (ROOT / "artifacts").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="protected-demo-test-", dir=ROOT / "artifacts") as path:
        site = ProtectedSite(Path(path), tls, caddy)
        try:
            site.start()
            yield site
        finally:
            site.stop()


@pytest.fixture(scope="session")
def group_gateway(tmp_path_factory):
    from .group_gateway import GroupGateway

    caddy = shutil.which(os.environ.get("GLAMDB_DEMO_CADDY", "caddy"))
    if caddy is None:
        pytest.fail("Real Caddy required: python3 scripts/fetch-demo-caddy.py")
    tls = make_tls_material(tmp_path_factory.mktemp("group-gateway-ca"))
    (ROOT / "artifacts").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="group-gateway-test-", dir=ROOT / "artifacts") as path:
        with ExitStack() as stack:
            gateway = GroupGateway(Path(path), tls, caddy)
            gateway.start_backends(stack)
            stack.callback(gateway.stop)
            gateway.start()
            yield gateway


@pytest.fixture(scope="session")
def organization_backends(tmp_path_factory):
    from .group_gateway import GroupGateway

    caddy = shutil.which(os.environ.get("GLAMDB_DEMO_CADDY", "caddy"))
    if caddy is None:
        pytest.fail("Real Caddy is required")
    tls = make_tls_material(tmp_path_factory.mktemp("organization-ca"))
    with tempfile.TemporaryDirectory(prefix="organization-backends-", dir=ROOT / "artifacts") as path:
        with ExitStack() as stack:
            fleet = GroupGateway(Path(path), tls, caddy)
            fleet.start_backends(stack)
            yield fleet


@pytest.fixture
def organization_gateway(organization_backends):
    from .organization_gateway import OrganizationGateway

    with tempfile.TemporaryDirectory(prefix="org-test-", dir=ROOT / "artifacts") as path:
        gateway = OrganizationGateway(Path(path), organization_backends)
        try:
            gateway.start()
            yield gateway
        finally:
            gateway.stop()
