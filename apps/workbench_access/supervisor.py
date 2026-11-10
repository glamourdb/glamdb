"""Local operator control; organization users never receive this interface."""

from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import pwd
import signal
import socket
import socketserver
import sqlite3
import struct
import threading

from .catalog import AuthorizationCatalog, CatalogError, catalog_lease
from .config import AccessConfigError, _pairs, parse_registry
from .runtime import GatewayRuntime


def listener_directory():
    # Use the account's persistent identity, not catalog/state/TMPDIR/HOME from
    # the launch environment. Copies of the checkout must share these leases.
    return Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".local/state/glamdb-workbench/listeners"


@contextmanager
def listener_leases(args):
    directory = listener_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if directory.is_symlink() or not directory.is_dir() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise CatalogError("INVALID_STATE", "listener lease directory must be private and owned by the service account")
    _, separator, port = args.host.partition(":")
    ports = {int(port) if separator else 443, args.http_port}
    if args.metrics_listen:
        ports.add(int(args.metrics_listen.rsplit(":", 1)[1]))
    with ExitStack() as stack:
        descriptors = [stack.enter_context(catalog_lease(directory / f"tcp4-{port}")) for port in sorted(ports)]
        yield descriptors


class OrganizationSupervisor:
    def __init__(self, args):
        self.args = args
        self.catalog = AuthorizationCatalog(args.catalog)
        self.runtime = GatewayRuntime(args)
        self.lock = threading.RLock()

    def snapshot(self):
        registry = parse_registry(self.catalog.registry_data(), os.environ, allow_empty=True)
        self.runtime.validate(registry)
        return registry

    def activate(self):
        self.runtime.start(self.snapshot(), self.catalog.revision)

    def dispatch(self, request, uid):
        with self.lock:
            action = request.get("action")
            if action in ("status", "list"):
                return {**self.catalog.state(), **self.runtime.status()}
            if action == "audit":
                return self.catalog.audit(request.get("limit", 100))
            if action == "receipt":
                return self.catalog.receipt(request.get("operation_id"))
            if action == "backup":
                self.runtime.validate_private_path(request.get("output"))
                return self.catalog.backup(request.get("output"))
            if action == "reconcile":
                self.runtime.stop()
                try:
                    self.catalog.close()
                    self.catalog = AuthorizationCatalog(self.args.catalog)
                    self.activate()
                except (CatalogError, AccessConfigError, OSError, sqlite3.Error):
                    self.runtime.last_error = "GATEWAY_UNAVAILABLE"
                    raise
                return {"revision": self.catalog.revision, **self.runtime.status()}
            return self.catalog.apply(request, validate=self.snapshot, fence=self.runtime.stop,
                                      activate=self.activate, actor_uid=uid)

    def close(self):
        self.runtime.stop()
        self.catalog.close()


class OperatorServer(socketserver.UnixStreamServer):
    request_queue_size = 16


class OperatorHandler(socketserver.StreamRequestHandler):
    timeout = 90

    def handle(self):
        try:
            if hasattr(self.request, "getpeereid"):
                uid = self.request.getpeereid()[0]
            elif hasattr(socket, "SO_PEERCRED"):
                uid = struct.unpack("3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
            else:
                uid = os.geteuid()
            if uid not in (os.geteuid(), 0):
                raise CatalogError("PERMISSION_DENIED", "operator socket belongs to a different user")
            raw = self.rfile.readline(65537)
            if len(raw) > 65536 or not raw.endswith(b"\n"):
                raise CatalogError("INVALID_ARGUMENT", "operator request must be one JSON line of at most 64 KiB")
            request = json.loads(raw, object_pairs_hook=_pairs)
            if not isinstance(request, dict):
                raise CatalogError("INVALID_ARGUMENT", "operator request must be an object")
            result = self.server.supervisor.dispatch(request, uid)
            response = {"ok": True, "result": result}
        except CatalogError as error:
            response = {"ok": False, "error": {"code": error.code, "message": str(error)}}
        except (AccessConfigError, OSError, sqlite3.Error):
            response = {"ok": False, "error": {"code": "INVALID_CONFIGURATION", "message": "candidate access configuration is invalid"}}
        except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
            response = {"ok": False, "error": {"code": "CONTROL_ERROR", "message": "operator request failed; check receipt and reconcile if required"}}
        try:
            self.wfile.write(json.dumps(response).encode() + b"\n")
        except OSError:
            pass  # The durable operation receipt remains available after disconnect.


def serve_organizations(args):
    stopping = threading.Event()
    with ExitStack() as stack:
        listener_descriptors = stack.enter_context(listener_leases(args))
        catalog_descriptor = stack.enter_context(catalog_lease(args.catalog))
        args.state.resolve().mkdir(mode=0o700, parents=True, exist_ok=True)
        state_descriptor = stack.enter_context(catalog_lease(args.state.resolve() / "gateway"))
        args.lease_fds = (catalog_descriptor, state_descriptor, *listener_descriptors)
        supervisor = OrganizationSupervisor(args)
        stack.callback(supervisor.close)
        address = args.state.resolve() / "operator.sock"
        if address.exists():
            if not address.is_socket():
                raise CatalogError("INVALID_STATE", "operator socket path contains an unrelated file")
            address.unlink()
        server = OperatorServer(str(address), OperatorHandler)
        server.supervisor = supervisor
        address.chmod(0o600)
        stack.callback(address.unlink, missing_ok=True)
        stack.callback(server.server_close)
        try:
            supervisor.activate()
        except AccessConfigError:
            supervisor.runtime.last_error = "GATEWAY_UNAVAILABLE"
        worker = threading.Thread(target=server.serve_forever, name="organization-operator", daemon=True)
        worker.start()
        stack.callback(worker.join, timeout=95)
        stack.callback(server.shutdown)
        previous = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda *_: stopping.set())
        try:
            while not stopping.wait(0.2):
                with supervisor.lock:
                    process = supervisor.runtime.process
                    if process is not None and process.poll() is not None:
                        supervisor.runtime.stop()
                        try:
                            supervisor.activate()
                        except (AccessConfigError, OSError, sqlite3.Error):
                            supervisor.runtime.last_error = "GATEWAY_UNAVAILABLE"
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
