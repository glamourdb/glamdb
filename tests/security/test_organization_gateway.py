"""Organization lifecycle and strict routing through the real HTTPS gateway."""

import json
import os
import signal
import subprocess
import sys
import time

import httpx
import pytest

from apps.workbench_access.catalog import AuthorizationCatalog, CatalogError, catalog_lease
from .conftest import ROOT
from .test_organization_catalog import command


def test_organizations_get_only_their_owned_databases(organization_gateway):
    gateway = organization_gateway
    for organization, expected in (("pink", ["alpha", "offline", "shared"]), ("blue", ["beta"]), ("empty", [])):
        with gateway.client(organization) as client:
            response = client.get("/workbench/session")
            assert response.status_code == 200
            body = response.json()
            assert body["version"] == 2 and body["organization"]["id"] == organization
            assert [database["id"] for database in body["databases"]] == expected
            assert "groups" not in body and "password_hash" not in response.text
    assert gateway.socket_path.stat().st_mode & 0o077 == 0


def test_database_transfer_revokes_old_organization_and_preserves_underlying_data(organization_gateway):
    gateway = organization_gateway
    result = gateway.admin(command("database.transfer", id="alpha", **{"from": "pink", "to": "blue"}))
    assert result["metadata_committed"] and result["applied"]
    with gateway.client("pink") as old, gateway.client("blue") as new:
        assert old.get("/databases/alpha/v0/collections").status_code == 404
        assert "alpha" not in [database["id"] for database in old.get("/workbench/session").json()["databases"]]
        data = new.get("/databases/alpha/v0/collections").json()
        assert data["items"][0]["name"] == "alpha_only"
    assert gateway.admin({"action": "status"})["applied_revision"] == result["revision"]


def test_organization_disable_enable_and_password_rotation(organization_gateway):
    gateway = organization_gateway
    gateway.admin(command("organization.update", id="pink", enabled=False))
    with gateway.client("pink") as client:
        assert client.get("/workbench/session").status_code == 401
    gateway.admin(command("organization.update", id="pink", enabled=True, login="pink-new"))
    with gateway.client("pink") as client:
        assert client.get("/workbench/session").status_code == 401
        password = gateway.fleet.accounts["pink"][1]
        assert client.get("/workbench/session", auth=("pink-new", password)).status_code == 200
    new_password = "new-disposable-organization-password"
    gateway.admin(command("organization.password", id="pink", password_hash=gateway.fleet.hash_token(new_password)))
    with gateway.client("pink") as client:
        assert client.get("/workbench/session", auth=("pink-new", password)).status_code == 401
        assert client.get("/workbench/session", auth=("pink-new", new_password)).status_code == 200


def test_delete_organization_detaches_mappings_without_erasing_database(organization_gateway):
    gateway = organization_gateway
    with pytest.raises(CatalogError) as not_empty:
        gateway.admin(command("organization.delete", id="pink"))
    assert not_empty.value.code == "NOT_EMPTY"
    gateway.admin(command("organization.delete", id="pink", detach_databases=True))
    with gateway.client("pink") as client:
        assert client.get("/workbench/session").status_code == 401
    state = gateway.admin({"action": "list"})
    assert next(database for database in state["databases"] if database["id"] == "alpha")["organization_id"] is None
    gateway.admin(command("database.assign", id="alpha", organization_id="blue"))
    with gateway.client("blue") as client:
        assert client.get("/databases/alpha/v0/collections").json()["items"][0]["name"] == "alpha_only"


def test_operator_retry_is_idempotent_and_stale_revision_is_rejected(organization_gateway):
    gateway = organization_gateway
    request = command("organization.update", id="pink", name="Updated name")
    result = gateway.admin(request)
    request["expected_revision"] = 0
    assert gateway.admin(request) == result
    stale = command("organization.update", id="pink", name="Must not apply")
    stale["expected_revision"] = 0
    with pytest.raises(CatalogError) as failure:
        gateway.admin(stale)
    assert failure.value.code == "CONFLICT"
    assert gateway.admin({"action": "status"})["gateway_running"] is True


def test_catalog_is_locked_against_offline_mutation_while_gateway_runs(organization_gateway):
    with pytest.raises(CatalogError) as failure:
        with catalog_lease(organization_gateway.catalog_path):
            pass
    assert failure.value.code == "STATE_IN_USE"


def test_supervisor_restart_preserves_ownership_and_receipts(organization_gateway):
    gateway = organization_gateway
    request = command("database.transfer", id="alpha", **{"from": "pink", "to": "blue"})
    first = gateway.admin(request)
    gateway.stop()
    gateway.start()
    assert gateway.admin({"action": "receipt", "operation_id": request["operation_id"]}) == first
    assert gateway.admin(request) == first
    with gateway.client("pink") as client:
        assert client.get("/databases/alpha/v0/collections").status_code == 404


def test_members_cannot_reach_operator_commands_over_http(organization_gateway):
    with organization_gateway.client("pink") as client:
        for path in ("/admin", "/operator", "/workbench/admin", "/operator.sock", "/state/authorization.sqlite"):
            assert client.post(path, json=command("organization.delete", id="blue")).status_code in (404, 405)
    assert any(row["id"] == "blue" for row in organization_gateway.admin({"action": "status"})["organizations"])


def test_empty_catalog_fails_closed_but_operator_can_create_first_organization(organization_gateway):
    gateway = organization_gateway
    commands = [{"action": "organization.delete", "params": {"id": organization, "detach_databases": True}}
                for organization in ("pink", "blue", "empty")]
    gateway.admin(command("batch", commands=commands))
    for organization in gateway.fleet.accounts:
        with gateway.client(organization) as client:
            assert client.get("/").status_code == 401
    result = gateway.admin(command("organization.create", id="pink", login="pink", password_hash=gateway.fleet.hashes["pink"]))
    assert result["applied"]
    with gateway.client("pink") as client:
        assert client.get("/workbench/session").json()["databases"] == []


def test_orphaned_gateway_keeps_metadata_lease_until_terminated(organization_gateway):
    gateway = organization_gateway
    child_pid = gateway.admin({"action": "status"})["gateway_pid"]
    gateway.process.kill()
    gateway.process.wait(timeout=5)
    try:
        with pytest.raises(CatalogError) as locked:
            with catalog_lease(gateway.catalog_path):
                pass
        assert locked.value.code == "STATE_IN_USE"
    finally:
        os.kill(child_pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with catalog_lease(gateway.catalog_path):
                    break
            except CatalogError:
                time.sleep(0.05)
        else:
            os.kill(child_pid, signal.SIGKILL)
            pytest.fail("task gateway did not release its inherited lease")


def test_metrics_are_on_a_dedicated_listener_with_bounded_request_labels(organization_gateway):
    gateway = organization_gateway
    with gateway.client("pink") as client:
        assert client.get("/metrics").status_code == 404
        client.get("/databases/alpha/v0/capabilities")
        for index in range(20):
            client.request(f"CUSTOM-METHOD-{index}", f"/databases/alpha/v0/missing-{index}")
    with httpx.Client(trust_env=False) as client:
        metrics = client.get(f"http://127.0.0.1:{gateway.metrics_port}/metrics")
        assert metrics.status_code == 200
        assert "caddy_http_request_duration_seconds_count" in metrics.text
        assert 'handler="subroute"' in metrics.text
        assert "CUSTOM-METHOD-" not in metrics.text and "missing-" not in metrics.text
        for _, password in gateway.fleet.accounts.values():
            assert password not in metrics.text


def test_online_backup_is_consistent_and_rejects_the_served_tree(organization_gateway):
    gateway = organization_gateway
    backup = gateway.directory / "backup.sqlite"
    result = gateway.admin({"action": "backup", "output": str(backup)})
    assert result["backup_created"]
    restored = AuthorizationCatalog(backup)
    try:
        assert restored.state() == {key: gateway.admin({"action": "status"})[key]
                                    for key in ("revision", "organizations", "databases")}
    finally:
        restored.close()
    with pytest.raises(CatalogError):
        gateway.admin({"action": "backup", "output": str(gateway.directory / "site" / "leak.sqlite")})
    with gateway.client("pink") as client:
        assert client.get("/leak.sqlite").status_code == 404


def test_hash_cli_and_sdk_accept_utf8_password_without_publishing_it(organization_gateway):
    from glamdb_sdk import Client
    gateway = organization_gateway
    password = "Секретный пароль 🎀"
    password_file = gateway.directory / "new-password.hash"
    hashed = subprocess.run([sys.executable, "-m", "apps.workbench_access.admin", "hash-password",
                             "--stdin", "--output", str(password_file), "--caddy", gateway.fleet.caddy],
                            input=password + "\n", capture_output=True, text=True, cwd=ROOT, timeout=30)
    assert hashed.returncode == 0, hashed.stderr
    assert password not in hashed.stdout + hashed.stderr
    gateway.admin(command("organization.password", id="pink", password_hash=password_file.read_text().strip()))
    with Client(gateway.url + "/databases/alpha", password, username="pink",
                verify=gateway.fleet.tls.trusted_context()) as database:
        assert database.list_collections().items[0]["name"] == "alpha_only"
    assert password not in gateway.logs()
