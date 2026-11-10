"""Persistent authorization metadata is distinct from database user data."""

import os
import copy
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from apps.workbench_access.catalog import AuthorizationCatalog, CatalogError, catalog_lease, initialize


ROOT = Path(__file__).resolve().parents[2]


def test_catalog_initialization_is_explicit_and_refuses_overwrite(tmp_path):
    path = tmp_path / "authorization.sqlite"
    command = [sys.executable, "-m", "apps.workbench_access.admin", "init", "--catalog", str(path)]
    first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=5)
    assert first.returncode == 0, first.stderr
    assert path.is_file()
    assert path.stat().st_mode & 0o077 == 0
    original = path.read_bytes()
    second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=5)
    assert second.returncode != 0
    assert path.read_bytes() == original


HASH = "$2a$14$" + "." * 53


def command(action, **params):
    return {"operation_id": str(uuid4()), "action": action, "params": params}


def create_org(store, identifier="acme", **extra):
    return store.apply(command("organization.create", id=identifier, login=identifier,
                               password_hash=HASH, **extra))


def create_db(store, identifier="orders", organization_id="acme", **extra):
    return store.apply(command("database.create", id=identifier, name=identifier, url="http://127.0.0.1:19001",
                               token_env="TEST_DATABASE_TOKEN", organization_id=organization_id, **extra))


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "auth.sqlite"
    initialize(path)
    store = AuthorizationCatalog(path)
    try:
        yield store
    finally:
        store.close()


def test_organization_and_database_lifecycle_preserves_data_definition(catalog):
    create_org(catalog)
    create_org(catalog, "other")
    create_db(catalog)
    catalog.apply(command("database.transfer", id="orders", **{"from": "acme", "to": "other"}))
    state = catalog.state()
    assert state["databases"][0]["organization_id"] == "other"
    assert state["databases"][0]["url"] == "http://127.0.0.1:19001"
    assert catalog.registry_data()["groups"][0]["databases"] == []
    catalog.apply(command("database.detach", id="orders", expected_owner="other"))
    assert catalog.state()["databases"][0]["organization_id"] is None
    assert catalog.registry_data()["databases"] == []
    catalog.apply(command("database.assign", id="orders", organization_id="acme"))
    catalog.apply(command("database.delete", id="orders", expected_owner="acme"))
    assert catalog.state()["databases"] == []
    catalog.apply(command("organization.delete", id="acme"))
    assert [item["id"] for item in catalog.state()["organizations"]] == ["other"]


def test_disabled_organizations_and_databases_are_not_routed(catalog):
    create_org(catalog)
    create_db(catalog)
    catalog.apply(command("organization.update", id="acme", enabled=False))
    assert catalog.registry_data() == {"version": 1, "groups": [], "databases": []}
    assert catalog.state()["databases"][0]["organization_id"] == "acme"
    catalog.apply(command("organization.update", id="acme", enabled=True, login="new-login", name="New name"))
    assert catalog.registry_data()["groups"][0]["login"] == "new-login"
    catalog.apply(command("database.update", id="orders", enabled=False))
    assert catalog.registry_data()["groups"][0]["databases"] == []
    assert catalog.registry_data()["databases"] == []


@pytest.mark.parametrize("policy", ["detach", "transfer"])
def test_deleting_nonempty_organization_requires_explicit_policy(catalog, policy):
    create_org(catalog)
    create_org(catalog, "other")
    create_db(catalog)
    before = catalog.state()
    with pytest.raises(CatalogError) as failure:
        catalog.apply(command("organization.delete", id="acme"))
    assert failure.value.code == "NOT_EMPTY"
    assert catalog.state() == before
    params = {"detach_databases": True} if policy == "detach" else {"transfer_to": "other"}
    catalog.apply(command("organization.delete", id="acme", **params))
    assert catalog.state()["databases"][0]["organization_id"] == (None if policy == "detach" else "other")


def test_idempotent_retry_survives_reopen_and_never_reapplies_old_ownership(catalog):
    create_org(catalog)
    create_org(catalog, "other")
    request = command("database.create", id="orders", url="http://127.0.0.1:19001",
                      token_env="TEST_DATABASE_TOKEN", organization_id="acme")
    first = catalog.apply(request)
    catalog.apply(command("database.transfer", id="orders", **{"from": "acme", "to": "other"}))
    request["expected_revision"] = 0
    assert catalog.apply(request) == first
    assert catalog.state()["databases"][0]["organization_id"] == "other"
    reopened = AuthorizationCatalog(catalog.path)
    try:
        assert reopened.receipt(request["operation_id"]) == first
        assert reopened.apply(request) == first
    finally:
        reopened.close()
    changed = copy.deepcopy(request)
    changed["params"]["organization_id"] = "other"
    with pytest.raises(CatalogError) as failure:
        catalog.apply(changed)
    assert failure.value.code == "CONFLICT"


@pytest.mark.parametrize("action,params", [
    ("organization.create", {"id": "acme", "login": "other", "password_hash": HASH}),
    ("organization.create", {"id": "other", "login": "acme", "password_hash": HASH}),
    ("organization.create", {"id": "../other", "login": "other", "password_hash": HASH}),
    ("organization.password", {"id": "acme", "password_hash": "plaintext-is-not-accepted"}),
    ("database.assign", {"id": "orders", "organization_id": "missing"}),
    ("database.transfer", {"id": "orders", "from": "wrong", "to": "acme"}),
    ("database.delete", {"id": "orders", "expected_owner": "wrong"}),
    ("database.update", {"id": "orders", "url": "http://external.example.com"}),
    ("organization.update", {"id": "acme", "enabled": "false"}),
    ("unknown", {"id": "acme"}),
])
def test_invalid_admin_operations_do_not_change_metadata(catalog, action, params):
    create_org(catalog)
    create_db(catalog)
    before = catalog.state()
    audit = catalog.audit()
    with pytest.raises(CatalogError):
        catalog.apply(command(action, **params))
    assert catalog.state() == before
    assert catalog.audit() == audit


def test_stale_revision_is_rejected_before_mutation(catalog):
    create_org(catalog)
    request = command("organization.update", id="acme", name="Changed")
    request["expected_revision"] = 0
    with pytest.raises(CatalogError):
        catalog.apply(request)
    assert catalog.state()["organizations"][0]["name"] == "acme"


def test_validation_failure_does_not_fence_or_commit(catalog):
    create_org(catalog)
    calls = []
    def reject():
        raise CatalogError("INVALID_ARGUMENT", "candidate unavailable")
    with pytest.raises(CatalogError):
        catalog.apply(command("organization.update", id="acme", name="Changed"),
                      validate=reject, fence=lambda: calls.append("fence"))
    assert not calls
    assert catalog.revision == 1


def test_activation_failure_commits_revocation_and_reports_unavailable(catalog):
    create_org(catalog)
    calls = []
    def unavailable():
        calls.append("activate")
        raise RuntimeError("injected startup failure")
    request = command("organization.update", id="acme", enabled=False)
    result = catalog.apply(request, fence=lambda: calls.append("fence"), activate=unavailable)
    assert calls == ["fence", "activate"]
    assert result["metadata_committed"] is True and result["applied"] is False
    assert result["error"]["code"] == "APPLY_FAILED"
    assert catalog.registry_data()["groups"] == []
    assert catalog.apply(request, activate=lambda: calls.append("unsafe-replay")) == result
    assert "unsafe-replay" not in calls


def test_batch_is_atomic_and_audits_each_action(catalog):
    operations = [{"action": "organization.create", "params": {"id": "acme", "login": "acme", "password_hash": HASH}},
                  {"action": "database.create", "params": {"id": "orders", "url": "http://127.0.0.1:19001",
                   "token_env": "TEST_DATABASE_TOKEN", "organization_id": "acme"}}]
    result = catalog.apply(command("batch", commands=operations))
    assert result["revision"] == 1
    assert len(catalog.audit()) == 2
    assert HASH not in json.dumps(catalog.state()) + json.dumps(catalog.audit()) + json.dumps(result)
    bad = [{"action": "organization.update", "params": {"id": "acme", "name": "Must roll back"}},
           {"action": "database.transfer", "params": {"id": "orders", "from": "wrong", "to": "acme"}}]
    with pytest.raises(CatalogError):
        catalog.apply(command("batch", commands=bad))
    assert catalog.state()["organizations"][0]["name"] == "acme"
    assert catalog.revision == 1


def test_catalog_lease_prevents_second_owner(catalog):
    with catalog_lease(catalog.path):
        with pytest.raises(CatalogError) as failure:
            with catalog_lease(catalog.path):
                pass
        assert failure.value.code == "STATE_IN_USE"


def test_missing_catalog_is_never_created_by_open(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(CatalogError):
        AuthorizationCatalog(path)
    assert not path.exists()


def test_backup_is_consistent_private_and_never_overwrites(catalog, tmp_path):
    create_org(catalog)
    create_db(catalog)
    backup_path = tmp_path / "backup.sqlite"
    before = catalog.state()
    result = catalog.backup(backup_path)
    assert result["revision"] == before["revision"]
    assert backup_path.stat().st_mode & 0o077 == 0
    recovered = AuthorizationCatalog(backup_path)
    try:
        assert recovered.state() == before
        assert recovered.audit() == catalog.audit()
    finally:
        recovered.close()
    create_org(catalog, "later")
    saved = backup_path.read_bytes()
    with pytest.raises(CatalogError):
        catalog.backup(backup_path)
    assert backup_path.read_bytes() == saved


def test_backup_rejects_symlink_without_reading_or_modifying_target(catalog, tmp_path):
    target = tmp_path / "important"
    target.write_bytes(b"keep")
    link = tmp_path / "backup.sqlite"
    link.symlink_to(target)
    with pytest.raises(CatalogError):
        catalog.backup(link)
    assert target.read_bytes() == b"keep"


def test_concurrent_same_operation_commits_once_and_conflicting_revisions_fail(catalog):
    from concurrent.futures import ThreadPoolExecutor
    request = command("organization.create", id="acme", login="acme", password_hash=HASH)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: catalog.apply(request), range(32)))
    assert all(result == results[0] for result in results)
    assert catalog.revision == 1 and len(catalog.audit()) == 1
    def rename(index):
        candidate = command("organization.update", id="acme", name=f"Name {index}")
        candidate["expected_revision"] = 1
        try:
            return catalog.apply(candidate)
        except CatalogError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(rename, range(16)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("CONFLICT") == 15
    assert catalog.revision == 2


def test_audit_records_transfer_direction_and_password_rotation_without_secrets(catalog):
    create_org(catalog)
    create_org(catalog, "other")
    create_db(catalog)
    catalog.apply(command("database.transfer", id="orders", **{"from": "acme", "to": "other"}))
    catalog.apply(command("organization.password", id="other", password_hash=HASH))
    assert catalog.audit()[0]["details"] == {"password_rotated": True}
    assert catalog.audit()[1]["details"] == {"from": "acme", "to": "other"}
    assert HASH not in json.dumps(catalog.audit())


def test_offline_backup_restore_cli_preserves_state_and_refuses_replacement(catalog, tmp_path):
    create_org(catalog)
    create_db(catalog)
    saved = tmp_path / "backup.sqlite"
    restored = tmp_path / "restored.sqlite"
    backup = subprocess.run([sys.executable, "-m", "apps.workbench_access.admin", "--catalog", str(catalog.path),
                             "backup", "--output", str(saved)], cwd=ROOT, capture_output=True, text=True, timeout=5)
    assert backup.returncode == 0, backup.stderr
    args = [sys.executable, "-m", "apps.workbench_access.admin", "restore", "--backup", str(saved), "--catalog", str(restored)]
    first = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=5)
    assert first.returncode == 0, first.stderr
    opened = AuthorizationCatalog(restored)
    try:
        assert opened.state() == catalog.state()
    finally:
        opened.close()
    original = restored.read_bytes()
    second = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=5)
    assert second.returncode != 0
    assert restored.read_bytes() == original


def test_check_rejects_catalog_in_generated_runtime_state(tmp_path, monkeypatch):
    from apps.workbench_access import __main__ as launcher
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    state = tmp_path / "artifacts/state"
    state.mkdir(parents=True)
    source = state / "caddy.json"
    initialize(source)
    original = source.read_bytes()
    monkeypatch.setattr(sys, "argv", ["workbench", "--catalog", str(source), "--host", "example.com",
                                     "--state", str(state), "--output", str(tmp_path / "artifacts/site"), "--check"])
    with pytest.raises(SystemExit) as failure:
        launcher.main()
    assert failure.value.code == 2
    assert source.read_bytes() == original


@pytest.mark.parametrize("field,value", [("name", 42), ("name", True), ("login", True), ("login", 12)])
def test_text_fields_are_rejected_before_sqlite_coercion(catalog, field, value):
    request = command("organization.create", id="typed", name="Typed", login="typed", password_hash=HASH)
    request["params"][field] = value
    with pytest.raises(CatalogError):
        catalog.apply(request)
    assert catalog.revision == 0 and catalog.state()["organizations"] == []


def test_private_outputs_reject_ui_tree_even_without_gateway(tmp_path, monkeypatch):
    monkeypatch.setattr("apps.workbench_access.catalog.ROOT", tmp_path)
    ui = tmp_path / "ui"
    ui.mkdir()
    with pytest.raises(CatalogError):
        initialize(ui / "access.sqlite")
    assert not (ui / "access.sqlite").exists()
