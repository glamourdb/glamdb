"""Reconciliation must fence stale routes before opening authoritative state."""

from types import SimpleNamespace

import pytest

from apps.workbench_access.catalog import CatalogError, initialize
from apps.workbench_access.supervisor import OrganizationSupervisor


class Runtime:
    def __init__(self, _args):
        self.calls = []
        self.last_error = None

    def validate(self, _registry):
        self.calls.append("validate")

    def start(self, _registry, revision):
        self.calls.append(("start", revision))

    def stop(self):
        self.calls.append("stop")

    def status(self):
        return {"gateway_running": bool(self.calls and isinstance(self.calls[-1], tuple))}


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    path = tmp_path / "access.sqlite"
    initialize(path)
    monkeypatch.setattr("apps.workbench_access.supervisor.GatewayRuntime", Runtime)
    instance = OrganizationSupervisor(SimpleNamespace(catalog=path))
    try:
        yield instance
    finally:
        path.chmod(0o600)
        instance.close()


def test_reconcile_reopens_catalog_after_fencing_previous_routes(supervisor):
    previous = supervisor.catalog
    result = supervisor.dispatch({"action": "reconcile"}, 0)
    assert supervisor.catalog is not previous
    assert supervisor.runtime.calls[0] == "stop"
    assert result["gateway_running"] is True


def test_reconcile_with_invalid_authoritative_file_keeps_gateway_stopped(supervisor):
    supervisor.catalog.path.chmod(0o644)
    with pytest.raises(CatalogError):
        supervisor.dispatch({"action": "reconcile"}, 0)
    assert supervisor.runtime.calls == ["stop"]
    assert supervisor.runtime.last_error == "GATEWAY_UNAVAILABLE"


def test_listener_leases_exclude_different_catalog_and_state_directories(tmp_path, monkeypatch):
    from apps.workbench_access.supervisor import listener_leases
    monkeypatch.setattr("apps.workbench_access.supervisor.listener_directory", lambda: tmp_path / "listeners")
    first = SimpleNamespace(host="127.0.0.1:19201", http_port=19202, metrics_listen="127.0.0.1:19203")
    # Binding any IPv4 address on an already-owned port is conservatively blocked.
    second = SimpleNamespace(host="other.example:19203", http_port=19204, metrics_listen=None)
    with listener_leases(first):
        with pytest.raises(CatalogError) as failure:
            with listener_leases(second):
                pass
        assert failure.value.code == "STATE_IN_USE"
    with listener_leases(second):
        pass


def test_packaging_failure_is_a_reportable_configuration_error(tmp_path, monkeypatch):
    import subprocess
    from apps.workbench_access.config import AccessConfigError, parse_registry
    from apps.workbench_access.runtime import GatewayRuntime
    args = SimpleNamespace(output=tmp_path / "site", state=tmp_path / "state", host="127.0.0.1:19201", caddy="caddy")
    monkeypatch.setattr("apps.workbench_access.runtime.shutil.which", lambda _: "caddy")
    runtime = GatewayRuntime(args)
    monkeypatch.setattr(runtime, "_check_listeners", lambda: None)
    def failed(*_args, **_options):
        raise subprocess.CalledProcessError(1, ["build-public-demo"])
    monkeypatch.setattr("apps.workbench_access.runtime.subprocess.run", failed)
    registry = parse_registry({"version": 1, "groups": [], "databases": []}, {}, allow_empty=True)
    with pytest.raises(AccessConfigError):
        runtime.start(registry, 0)
    assert runtime.process is None
    assert runtime.last_error == "GATEWAY_UNAVAILABLE"
