"""Optional events must not expose operator input or replace durable audit."""

import json
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.workbench_access.config import AccessConfigError
from apps.workbench_access.operational import OperationalEvents


def test_missing_optional_module_is_explicit_when_sink_is_requested(monkeypatch):
    def missing(_name):
        raise ModuleNotFoundError(name="tools.logging")
    monkeypatch.setattr("apps.workbench_access.operational.importlib.import_module", missing)
    optional = OperationalEvents()
    optional.close()
    with pytest.raises(AccessConfigError):
        OperationalEvents("/outside/webroot/logs")


def test_administrative_events_project_only_receipt_identity(monkeypatch):
    captured = []
    class Logger:
        def __init__(self, **_options):
            pass
        def emit(self, event, **fields):
            captured.append({"event": event, **fields})
        def close(self):
            pass
    monkeypatch.setattr("apps.workbench_access.operational.importlib.import_module",
                        lambda _: SimpleNamespace(StructuredLogger=Logger))
    events = OperationalEvents()
    events.mutation({"action": "organization.password", "revision": 4, "target": "pink", "applied": True,
                     "password_hash": "never-log-this", "params": {"Authorization": "secret"}})
    events.mutation({"action": "database.transfer", "revision": 5, "target": "orders", "applied": False})
    events.close()
    assert captured[0] == {"event": "org_updated", "revision": 4, "org_id": "pink", "operation": "organization", "outcome": "ok"}
    assert captured[1]["database_id"] == "orders" and captured[1]["outcome"] == "error"
    assert captured[-1]["event"] == "runtime_stopped"
    assert "secret" not in json.dumps(captured) and "never-log-this" not in json.dumps(captured)


def test_actual_supervisor_writes_bounded_adapter_events(organization_backends, tmp_path):
    from tests.security.organization_gateway import OrganizationGateway
    from tests.security.conftest import ROOT
    from tests.security.test_organization_catalog import command
    source = os.environ.get("GLAMDB_LOGGING_SOURCE_ROOT")
    try:
        available = importlib.util.find_spec("tools.logging.glamdb_logging") is not None
    except ModuleNotFoundError:
        available = False
    if not available and not source:
        pytest.skip("optional logging component is absent; enable it to run its real sink integration")
    import tempfile
    fleet = organization_backends
    previous = fleet.environment.copy()
    directory = tmp_path / "logs"
    fleet.environment["GLAMDB_LOG_DIRECTORY"] = str(directory)
    if source:
        fleet.environment["PYTHONPATH"] = source + os.pathsep + fleet.environment.get("PYTHONPATH", "")
    try:
        with tempfile.TemporaryDirectory(prefix="logged-org-", dir=ROOT / "artifacts") as path:
            gateway = OrganizationGateway(Path(path), fleet)
            try:
                gateway.start()
                result = gateway.admin(command("organization.update", id="pink", name="Updated"))
                assert result["applied"]
            finally:
                gateway.stop()
        records = [json.loads(line) for line in (directory / "glamdb.log").read_text().splitlines()]
        assert {row["event"] for row in records} >= {"runtime_started", "org_updated", "runtime_stopped"}
        assert any(row.get("org_id") == "pink" and row.get("revision") == result["revision"] for row in records)
        for secret in [*fleet.hashes.values(), *(password for _, password in fleet.accounts.values())]:
            assert secret not in json.dumps(records)
        allowed = {"schema", "timestamp", "service", "event", "level", "operation", "outcome",
                   "request_id", "revision", "org_id", "database_id", "submitted", "accepted", "sampled",
                   "rate_limited", "dropped_full", "dropped_contended", "dropped_stopped", "dropped_sink",
                   "written", "sink_errors", "backlog", "queue_capacity"}
        assert all(set(row) <= allowed for row in records)
    finally:
        fleet.environment.clear()
        fleet.environment.update(previous)
