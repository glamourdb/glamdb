"""Configuration mistakes must never produce an anonymous or simulated gateway."""

import os
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_group_gateway_requires_explicit_registry():
    result = subprocess.run([sys.executable, "-m", "apps.workbench_access"], cwd=ROOT,
                            env=os.environ.copy(), text=True, capture_output=True, timeout=5)
    assert result.returncode != 0
    assert "--access-config" in result.stderr
    assert "Traceback" not in result.stderr


def registry():
    return {"version": 1,
            "databases": [{"id": "alpha", "name": "Alpha", "url": "http://127.0.0.1:9001",
                           "token_env": "TEST_ONLY_DATABASE_TOKEN"}],
            "groups": [{"id": "pink", "login": "pink", "name": "Pink team",
                        "password_hash": "$2a$14$" + "." * 53, "databases": ["alpha"]}]}


def check(path, **environment):
    env = os.environ.copy()
    env.update(GLAMDB_WORKBENCH_HOST="demo.example.com",
               TEST_ONLY_DATABASE_TOKEN="disposable-test-upstream-token")
    env.update(environment)
    return subprocess.run([sys.executable, "-m", "apps.workbench_access", "--access-config",
                           str(path), "--check"], cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=5)


def test_valid_registry_and_empty_grant_do_not_start_servers(tmp_path):
    data = registry()
    data["groups"].append({"id": "empty", "login": "empty", "password_hash": "$2a$14$" + "." * 53,
                           "databases": []})
    path = tmp_path / "access.json"
    path.write_text(json.dumps(data))
    result = check(path)
    assert result.returncode == 0, result.stderr
    assert "configuration is valid" in result.stdout
    assert data["groups"][0]["password_hash"] not in result.stdout + result.stderr


@pytest.mark.parametrize("change", [
    lambda d: d.update(version=True),
    lambda d: d.update(unknown="typo"),
    lambda d: d["databases"].append(copy.deepcopy(d["databases"][0])),
    lambda d: d["groups"].append(copy.deepcopy(d["groups"][0])),
    lambda d: d["groups"][0].update(databases=["foreign"]),
    lambda d: d["groups"][0].update(databases=["alpha", "alpha"]),
    lambda d: d["groups"][0].update(login="pink\nadmin"),
    lambda d: d["groups"][0].update(password_hash="plaintext-never-valid"),
    lambda d: d["groups"][0].update(password_hash="$2a$04$" + "." * 53),
    lambda d: d["groups"][0].update(id="../pink"),
    lambda d: d["databases"][0].update(url="http://external.example.com:9001"),
    lambda d: d["databases"][0].update(url="https://user:secret@example.com"),
    lambda d: d["databases"][0].update(url="https://example.com/private"),
    lambda d: d["databases"][0].update(url="https://example.com?token=secret"),
    lambda d: d["databases"][0].update(url="https://example.com#secret"),
    lambda d: d["databases"][0].update(url="https://{env.SECRET}"),
    lambda d: d["databases"][0].update(token_env="INVALID-NAME"),
    lambda d: d["databases"][0].update(token_env="MISSING_DATABASE_TOKEN_FOR_TEST"),
    lambda d: d["databases"][0].update(ca_file="/missing-ca.pem"),
], ids=["version", "unknown-key", "duplicate-database", "duplicate-group", "unknown-grant",
        "duplicate-grant", "login-injection", "plaintext", "weak-hash", "group-path",
        "plaintext-remote", "url-credentials", "url-path", "url-query", "url-fragment",
        "url-placeholder", "environment-name", "missing-secret", "missing-ca"])
def test_bad_registry_refuses_startup_without_dumping_values(tmp_path, change):
    data = registry()
    change(data)
    path = tmp_path / "access.json"
    path.write_text(json.dumps(data))
    result = check(path)
    assert result.returncode != 0
    assert "access configuration" in result.stderr.lower()
    assert "Traceback" not in result.stderr
    for secret in ("plaintext-never-valid", "disposable-test-upstream-token", "$2a$14$" + "." * 53):
        assert secret not in result.stdout + result.stderr


def test_duplicate_json_keys_are_not_silently_overwritten(tmp_path):
    path = tmp_path / "access.json"
    path.write_text('{"version":1,"version":1,"groups":[],"databases":[]}')
    result = check(path)
    assert result.returncode != 0
    assert "duplicate JSON" in result.stderr
