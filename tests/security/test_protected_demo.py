"""Fail-closed configuration for the real HTTPS website gate."""

import base64
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts/serve-protected-demo.py"


def launch_with(arguments=(), **settings):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GLAMDB_DEMO_")}
    environment.update(settings)
    return subprocess.run([sys.executable, str(LAUNCHER), *arguments], env=environment,
                          capture_output=True, text=True, timeout=10)


def test_missing_password_hash_refuses_startup():
    result = launch_with(GLAMDB_DEMO_HOST="demo.example.com")
    assert result.returncode != 0
    assert "GLAMDB_DEMO_PASSWORD_HASH" in result.stderr


@pytest.mark.parametrize("password", ["", "plaintext-test-only", "$2a$04$" + "." * 53])
def test_plaintext_or_weak_hash_refuses_startup_without_echo(password):
    result = launch_with(GLAMDB_DEMO_HOST="demo.example.com",
                         GLAMDB_DEMO_PASSWORD_HASH=password)
    assert result.returncode != 0
    assert "GLAMDB_DEMO_PASSWORD_HASH" in result.stderr
    if password:
        assert password not in result.stdout + result.stderr


@pytest.mark.parametrize("host", ["", "http://demo.example.com", "demo.example.com/path",
                                  "example.com\n:80", "example.com:0", "example.com:65536"])
def test_invalid_host_refuses_startup(host):
    result = launch_with(GLAMDB_DEMO_HOST=host,
                         GLAMDB_DEMO_PASSWORD_HASH="$2a$14$" + "." * 53)
    assert result.returncode != 0
    assert "GLAMDB_DEMO_HOST" in result.stderr


@pytest.mark.parametrize("user", ["", "demo:password", "demo\nuser", "*", "x" * 65])
def test_invalid_username_refuses_startup_without_echo(user):
    result = launch_with(GLAMDB_DEMO_HOST="demo.example.com", GLAMDB_DEMO_USER=user,
                         GLAMDB_DEMO_PASSWORD_HASH="$2a$14$" + "." * 53)
    assert result.returncode != 0
    assert "GLAMDB_DEMO_USER" in result.stderr


def test_state_outside_artifacts_is_rejected_before_mutation(tmp_path):
    old_mode = tmp_path.stat().st_mode
    result = launch_with(arguments=("--state", str(tmp_path)),
                         GLAMDB_DEMO_HOST="demo.example.com",
                         GLAMDB_DEMO_PASSWORD_HASH="$2a$14$" + "." * 53)
    assert result.returncode != 0
    assert "--state must be a dedicated artifacts subdirectory" in result.stderr
    assert tmp_path.stat().st_mode == old_mode


def test_incomplete_tls_pair_refuses_startup():
    result = launch_with(arguments=("--tls-cert", "missing.pem"),
                         GLAMDB_DEMO_HOST="demo.example.com",
                         GLAMDB_DEMO_PASSWORD_HASH="$2a$14$" + "." * 53)
    assert result.returncode != 0
    assert "--tls-cert and --tls-key" in result.stderr


@pytest.mark.parametrize("path", ["/", "/index.html", "/assets/app.js", "/assets/styles.css",
                                  "/assets/brand/wordmark.png", "/assets/brand/mark.png",
                                  "/config/theme.json", "/config/demo-data.json"])
def test_anonymous_requests_cannot_read_html_assets_or_data(protected_site, path):
    with protected_site.client() as client:
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Basic ")
        assert response.headers["cache-control"] == "private, no-store"
        assert "data-public-demo" not in response.text


@pytest.mark.parametrize("kind", ["wrong-password", "wrong-user", "bearer", "malformed-basic"])
def test_invalid_credentials_are_rejected(protected_site, kind):
    with protected_site.client() as client:
        if kind == "wrong-password":
            response = client.get("/", auth=(protected_site.user, "wrong-disposable-password"))
        elif kind == "wrong-user":
            response = client.get("/", auth=("other-user", protected_site.password))
        else:
            response = client.get("/", headers={"Authorization": "Bearer wrong" if kind == "bearer"
                                               else "Basic invalid"})
        assert response.status_code == 401


def test_correct_credentials_serve_demo_and_security_headers(protected_site):
    with protected_site.client(auth=protected_site.auth) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert 'data-public-demo="true"' in response.text
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["strict-transport-security"] == "max-age=31536000"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "server" not in response.headers
        assert client.get("/config/theme.json").json()["version"] == 1
        assert client.head("/assets/styles.css").status_code == 200
    with protected_site.client() as client:
        assert client.head("/assets/styles.css").status_code == 401


def test_http_only_redirects_to_https_without_serving_the_ui(protected_site):
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        response = client.get(protected_site.http_url + "/config/demo-data.json?preview=1")
        assert response.status_code == 308
        assert response.headers["location"] == protected_site.url + "/config/demo-data.json?preview=1"
        assert 'data-public-demo="true"' not in response.text


def test_tls_rejects_untrusted_ca(protected_site):
    with httpx.Client(verify=protected_site.tls.wrong_context(), trust_env=False) as client:
        with pytest.raises(httpx.ConnectError):
            client.get(protected_site.url, timeout=3)


def test_range_and_cache_validation_still_require_authentication(protected_site):
    with protected_site.client(auth=protected_site.auth) as client:
        etag = client.get("/assets/app.js").headers["etag"]
    with protected_site.client() as client:
        for headers in ({"Range": "bytes=0-128"}, {"If-None-Match": etag}):
            response = client.get("/assets/app.js", headers=headers)
            assert response.status_code == 401
            assert "data-public-demo" not in response.text


@pytest.mark.parametrize("path", ["/.env", "/.git/config", "/.glamdb-public-demo", "/.nojekyll",
                                  "/../state/server.key", "/%2e%2e/%2e%2e/deploy/demo-access/Caddyfile",
                                  "/scripts/serve-protected-demo.py", "/state/server.key"])
def test_private_paths_are_not_published(protected_site, path):
    with protected_site.client(auth=protected_site.auth) as client:
        assert client.get(path).status_code in (400, 404)


def test_credentials_are_absent_from_public_artifact_and_logs(protected_site):
    encoded = base64.b64encode(f"{protected_site.user}:{protected_site.password}".encode())
    for path in protected_site.output.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert protected_site.password.encode() not in data
            assert protected_site.password_hash.encode() not in data
            assert encoded not in data
    logs = protected_site.log_path.read_text()
    assert protected_site.password not in logs
    assert protected_site.password_hash not in logs
    assert encoded.decode() not in logs
    assert not list(protected_site.state.rglob("autosave.json"))


def test_rotation_revokes_old_password_on_the_server(protected_site):
    old_auth = protected_site.auth
    protected_site.stop()
    protected_site.start()
    with protected_site.client() as client:
        assert client.get("/", auth=old_auth).status_code == 401
        assert client.get("/", auth=protected_site.auth).status_code == 200
