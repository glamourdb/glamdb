"""Group authorization over real HTTPS; database engines are explicit test mocks."""

import base64
from contextlib import contextmanager
import copy
import json
from uuid import uuid4

import pytest

from glamdb_sdk import AuthenticationError, Client, HTTPError
from tests.integration.helpers.http_server import MockHTTPServer
from .group_gateway import COLLECTION_ID, SCHEMA


@pytest.mark.parametrize("group,allowed", [("pink", ["alpha", "shared", "offline"]),
                                           ("blue", ["beta", "shared"]), ("empty", [])])
def test_group_session_contains_only_its_database_allowlist(group_gateway, group, allowed):
    with group_gateway.client(group) as client:
        response = client.get("/workbench/session")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        data = response.json()
        assert data["group"]["login"] == group
        assert [database["id"] for database in data["databases"]] == allowed
        assert set(data) == {"version", "group", "databases"}
        for database in data["databases"]:
            assert set(database) == {"id", "name"}


@pytest.mark.parametrize("path", ["/", "/assets/app.js", "/workbench/session", "/workbench/config",
                                  "/databases/alpha/v0/capabilities"])
def test_anonymous_group_requests_receive_json_authentication_denial(group_gateway, path):
    with group_gateway.client() as client:
        response = client.get(path)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"
        assert response.headers["www-authenticate"].startswith("Basic ")
        assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("group,database", [("pink", "alpha"), ("blue", "beta"),
                                           ("pink", "shared"), ("blue", "shared")])
def test_group_sdk_uses_scoped_server_and_keeps_data_separate(group_gateway, group, database):
    login, token = group_gateway.accounts[group]
    with Client(group_gateway.url + "/databases/" + database, token, username=login,
                verify=group_gateway.tls.trusted_context()) as client:
        assert client.capabilities()["backend"] == "mock"
        assert client.scan_json(COLLECTION_ID).rows[0]["key"] == database + "-row"
        assert client.stats(COLLECTION_ID)["rows"] == 1


@pytest.mark.parametrize("group,foreign", [("pink", "beta"), ("blue", "alpha"),
                                          ("empty", "alpha"), ("empty", "shared")])
@pytest.mark.parametrize("suffix", ["/capabilities", "/collections", f"/collections/{COLLECTION_ID}/schema",
                                    f"/collections/{COLLECTION_ID}/stats"])
def test_foreign_database_never_discloses_metadata(group_gateway, group, foreign, suffix):
    with group_gateway.client(group) as client:
        response = client.get(f"/databases/{foreign}/v0" + suffix)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"
        assert foreign + "_only" not in response.text


@pytest.mark.parametrize("path", ["/v0/capabilities", "/databases/alpha/healthz", "/databases/alpha/workbench/config",
                                  "/databases/beta/v0/collections", "/databases/%62eta/v0/collections",
                                  "/databases/alpha/../beta/v0/collections", "/databases/alpha/%2e%2e/beta/v0/collections",
                                  "/databases/alpha/%2e%2e%2fbeta/v0/collections", "/databases/unknown/v0/collections",
                                  "/state/caddy.json", "/sessions/blue.json", "/config/demo-data.json", "/.env"])
def test_paths_and_forged_group_headers_do_not_bypass_allowlist(group_gateway, path):
    with group_gateway.client("pink") as client:
        response = client.get(path, headers={"X-GlamDB-Group": "blue", "X-GlamDB-Database": "beta",
                                            "X-Remote-User": "blue", "X-Forwarded-User": "blue"})
        assert response.status_code in (400, 404)
        assert "beta-row" not in response.text


def test_unavailable_database_reports_failure_without_demo_fallback(group_gateway):
    with group_gateway.client("pink") as client:
        response = client.get("/databases/offline/v0/capabilities")
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "UNAVAILABLE"


def test_gateway_forces_no_store_even_when_upstream_sets_cache_headers(group_gateway):
    with group_gateway.client("pink") as client:
        response = client.get("/databases/alpha/v0/capabilities")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"


def test_group_credentials_and_backend_secrets_are_not_published(group_gateway):
    secrets = [*(password for _, password in group_gateway.accounts.values()), *group_gateway.hashes.values(),
               *(value for key, value in group_gateway.environment.items() if key.startswith("GROUP_TEST_DATABASE_"))]
    secrets += [base64.b64encode(f"{user}:{password}".encode()).decode()
                for user, password in group_gateway.accounts.values()]
    for path in group_gateway.output.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert all(secret.encode() not in data for secret in secrets)
    log = group_gateway.log_path.read_text()
    assert all(secret not in log for secret in secrets)
    assert not list(group_gateway.state.rglob("autosave.json"))
    generated = (group_gateway.state / "caddy.json").read_text()
    assert all(secret not in generated for secret in secrets)


def test_wrong_group_credentials_are_sdk_authentication_errors(group_gateway):
    with Client(group_gateway.url + "/databases/alpha", "wrong-test-password", username="pink",
                verify=group_gateway.tls.trusted_context()) as client:
        with pytest.raises(AuthenticationError) as error:
            client.capabilities()
        assert error.value.status_code == 401


@contextmanager
def reconfigured(gateway, change):
    original = copy.deepcopy(gateway.registry)
    gateway.stop()
    try:
        change(gateway.registry)
        gateway.save_registry()
        gateway.start()
        yield
    finally:
        gateway.stop()
        gateway.registry = original
        gateway.save_registry()
        gateway.start()


def test_rotating_one_group_token_preserves_other_groups(group_gateway):
    new_token = "new-disposable-token-for-this-rotation-only"
    new_hash = group_gateway.hash_token(new_token)
    with reconfigured(group_gateway, lambda registry: registry["groups"][0].update(password_hash=new_hash)):
        with group_gateway.client("pink") as client:
            assert client.get("/workbench/session").status_code == 401
            assert client.get("/workbench/session", auth=("pink", new_token)).status_code == 200
        with group_gateway.client("blue") as client:
            assert client.get("/databases/beta/v0/capabilities").status_code == 200


def test_revoked_database_grant_disappears_and_direct_requests_are_denied(group_gateway):
    with reconfigured(group_gateway, lambda registry: registry["groups"][0].update(databases=["shared", "offline"])):
        with group_gateway.client("pink") as client:
            assert [db["id"] for db in client.get("/workbench/session").json()["databases"]] == ["shared", "offline"]
            assert client.get("/databases/alpha/v0/collections").status_code == 404
            assert client.get("/databases/shared/v0/collections").status_code == 200


def test_removed_group_cannot_authenticate_or_read_its_old_session_file(group_gateway):
    with reconfigured(group_gateway, lambda registry: registry["groups"].pop(0)):
        with group_gateway.client("pink") as client:
            assert client.get("/workbench/session").status_code == 401
        with group_gateway.client("blue") as client:
            assert client.get("/workbench/session").status_code == 200
            assert client.get("/sessions/pink.json").status_code == 404


def test_group_labels_are_literal_data_not_configuration_placeholders(group_gateway):
    label = '<script>"{env.GROUP_TEST_DATABASE_ALPHA_TOKEN}"</script>'
    with reconfigured(group_gateway, lambda registry: registry["groups"][0].update(name=label)):
        with group_gateway.client("pink") as client:
            data = client.get("/workbench/session?group=blue").json()
            assert data["group"]["name"] == label
            assert data["group"]["login"] == "pink"
            assert group_gateway.environment["GROUP_TEST_DATABASE_ALPHA_TOKEN"] not in json.dumps(data)


def test_allowed_group_writes_and_arrow_streams_are_routed_to_its_database(group_gateway):
    login, password = group_gateway.accounts["pink"]
    cid, operation = uuid4(), uuid4()
    with Client(group_gateway.url + "/databases/alpha", password, username=login,
                verify=group_gateway.tls.trusted_context()) as client:
        assert client.create_collection("gateway_writes", SCHEMA, collection_id=cid).outcome == "committed"
        result = client.append(cid, [{"key": "precise", "n": 9007199254740993}],
                               operation_id=operation, schema=SCHEMA)
        assert result.outcome == "committed"
        assert client.append(cid, [{"key": "precise", "n": 9007199254740993}],
                             operation_id=operation, schema=SCHEMA).result == result.result
        with client.scan_batches(cid) as stream:
            rows = [row for batch in stream for row in batch.to_pylist()]
        assert rows == [{"key": "precise", "n": 9007199254740993}]
        assert stream.integrity == "verified" and stream.checksum_verified
        assert client.stats(cid)["rows"] == 1
    with group_gateway.client("blue") as client:
        assert client.get(f"/databases/alpha/v0/collections/{cid}").status_code == 404
        assert client.get(f"/databases/beta/v0/collections/{cid}").status_code == 404


def test_foreign_database_mutations_never_reach_its_backend(group_gateway):
    with group_gateway.client("pink") as client:
        response = client.post("/databases/beta/v0/collections", json={
            "collection_id": str(uuid4()), "name": "must_not_exist", "schema": {
                "fields": [{"id": 1, "name": "key", "type": "string", "nullable": False}]}})
        assert response.status_code == 404
    key = "GROUP_TEST_DATABASE_BETA_TOKEN"
    with Client(group_gateway.backends["beta"].url, group_gateway.environment[key]) as client:
        with pytest.raises(HTTPError) as absent:
            client.resolve_collection("must_not_exist")
        assert absent.value.status_code == 404


@pytest.mark.parametrize("trust", ["trusted", "wrong", "system"])
def test_https_upstream_verifies_its_certificate(group_gateway, tmp_path, trust):
    token = "disposable-secure-upstream-token"
    key = "GROUP_TEST_DATABASE_SECURE_TOKEN"
    group_gateway.environment[key] = token
    with MockHTTPServer(tmp_path, tls=group_gateway.tls, token=token) as backend:
        def configure(registry):
            database = {"id": "secure", "name": "Secure", "url": backend.url, "token_env": key}
            if trust != "system":
                database["ca_file"] = str(group_gateway.tls.ca if trust == "trusted" else group_gateway.tls.wrong_ca)
            registry["databases"].append(database)
            registry["groups"][0]["databases"].append("secure")
        with reconfigured(group_gateway, configure):
            with group_gateway.client("pink") as client:
                response = client.get("/databases/secure/v0/capabilities")
                assert response.status_code == (200 if trust == "trusted" else 502)
                if trust == "trusted":
                    assert response.json()["backend"] == "mock"
                else:
                    assert response.json()["error"]["code"] == "UNAVAILABLE"


def test_group_plain_http_endpoint_only_redirects_to_https(group_gateway):
    import httpx

    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        response = client.get(f"http://127.0.0.1:{group_gateway.http_port}/workbench/session")
        assert response.status_code == 308
        assert response.headers["location"] == group_gateway.url + "/workbench/session"


@pytest.mark.parametrize("database,group,alias", [
    ("alpha", "pink", "GLAMDB_ACCESS_GROUP_HASH_0"),
    ("beta", "blue", "GLAMDB_ACCESS_BACKEND_TOKEN_0"),
])
def test_credential_sources_are_resolved_before_internal_environment_updates(group_gateway, database, group, alias):
    original = group_gateway.environment[f"GROUP_TEST_DATABASE_{database.upper()}_TOKEN"]
    group_gateway.environment[alias] = original
    def configure(registry):
        next(item for item in registry["databases"] if item["id"] == database)["token_env"] = alias
    try:
        with reconfigured(group_gateway, configure):
            with group_gateway.client(group) as client:
                assert client.get(f"/databases/{database}/v0/capabilities").status_code == 200
    finally:
        group_gateway.environment.pop(alias, None)
