"""Start an HTTPS workbench with group credentials and database allowlists."""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .config import AccessConfigError, caddy_config, database_token, load_registry
from .catalog import AuthorizationCatalog, CatalogError


ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--access-config", type=Path, help="static group configuration")
    source_group.add_argument("--catalog", type=Path, help="existing organization authorization catalog")
    parser.add_argument("--host", default=os.environ.get("GLAMDB_WORKBENCH_HOST", ""))
    parser.add_argument("--caddy", default=os.environ.get("GLAMDB_DEMO_CADDY", "caddy"))
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/workbench-access/site")
    parser.add_argument("--state", type=Path, default=ROOT / "artifacts/workbench-access/state")
    parser.add_argument("--bind", choices=("127.0.0.1", "0.0.0.0"), default="0.0.0.0")
    parser.add_argument("--http-port", type=int, default=80)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--ready-timeout", type=float, default=30)
    parser.add_argument("--drain-timeout", type=float, default=5)
    parser.add_argument("--metrics-listen", help="dedicated metrics listener, e.g. 127.0.0.1:9464")
    parser.add_argument("--check", action="store_true", help="validate registry and settings without starting services")
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?", args.host):
            raise AccessConfigError("GLAMDB_WORKBENCH_HOST must be a hostname with optional HTTPS port")
        host, separator, port_text = args.host.partition(":")
        port = int(port_text) if separator else 443
        if not 1 <= port <= 65535 or not 1 <= args.http_port <= 65535 or port == args.http_port:
            raise AccessConfigError("HTTP and HTTPS ports must be distinct and between 1 and 65535")
        site, state = args.output.resolve(), args.state.resolve()
        source = (args.access_config or args.catalog).resolve()
        artifacts = (ROOT / "artifacts").resolve()
        if any(path == artifacts or not path.is_relative_to(artifacts) for path in (site, state)):
            raise AccessConfigError("output and state must be dedicated artifacts subdirectories")
        if site.is_relative_to(state) or state.is_relative_to(site):
            raise AccessConfigError("output and state must be separate directories")
        sensitive = [source]
        if bool(args.tls_cert) != bool(args.tls_key):
            raise AccessConfigError("--tls-cert and --tls-key must be supplied together")
        certificate = args.tls_cert.resolve() if args.tls_cert else None
        key = args.tls_key.resolve() if args.tls_key else None
        if certificate:
            sensitive += [certificate, key]
        if not 0 < args.ready_timeout <= 300 or not 0 < args.drain_timeout <= 60:
            raise AccessConfigError("startup and drain timeouts must be bounded positive seconds")
        if args.metrics_listen:
            match = re.fullmatch(r"(127\.0\.0\.1|0\.0\.0\.0):([0-9]{1,5})", args.metrics_listen)
            if not match or not 1 <= int(match[2]) <= 65535 or int(match[2]) in (port, args.http_port):
                raise AccessConfigError("metrics listener must use a distinct valid IPv4 port")
        if args.catalog:
            from .config import parse_registry
            catalog = AuthorizationCatalog(source)
            try:
                registry = parse_registry(catalog.registry_data(), os.environ, allow_empty=True)
            finally:
                catalog.close()
        else:
            registry = load_registry(source, os.environ)
        sensitive += [database.ca_file for database in registry.databases if database.ca_file]
        sensitive += [database.token_file for database in registry.databases if database.token_file]
        if any(not path.is_file() or path.is_relative_to(site) or path.is_relative_to(state) or path.is_relative_to(ROOT / "ui")
               for path in sensitive):
            raise AccessConfigError("access configuration and TLS files must exist outside served, runtime-state and UI trees")
    except (AccessConfigError, CatalogError) as error:
        parser.error("Access configuration: " + str(error))
    if args.check:
        print("Access configuration is valid; no service started")
        return
    executable = shutil.which(args.caddy)
    if executable is None:
        parser.error("Caddy is required; run python3 scripts/fetch-demo-caddy.py")
    if args.catalog:
        from .supervisor import serve_organizations
        try:
            serve_organizations(args)
        except (AccessConfigError, CatalogError, OSError) as error:
            parser.error(str(error))
        return
    from .catalog import catalog_lease
    from .runtime import check_listeners
    from .supervisor import listener_leases
    with ExitStack() as stack:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptors = stack.enter_context(listener_leases(args))
        descriptors.append(stack.enter_context(catalog_lease(state / "gateway")))
        for descriptor in descriptors:
            os.set_inheritable(descriptor, True)
        check_listeners(args.bind, port, args.http_port, args.metrics_listen)
        subprocess.run([sys.executable, str(ROOT / "scripts/build-public-demo.py"),
                        "--mode", "gateway", "--output", str(site)], check=True)
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        state.chmod(0o700)
        sessions = state / "sessions"
        sessions.mkdir(mode=0o700, exist_ok=True)
        database_index = {database.id: database for database in registry.databases}
        backend_tokens = [database_token(database, os.environ) for database in registry.databases]
        environment = os.environ.copy()
        for index, group in enumerate(registry.groups):
            environment[f"GLAMDB_ACCESS_GROUP_HASH_{index}"] = group.password_hash
            identity = {"version": 1, "group": {"id": group.id, "name": group.name, "login": group.login},
                        "databases": [{"id": identifier, "name": database_index[identifier].name}
                                      for identifier in group.databases]}
            path = sessions / f"{group.id}.json"
            path.write_text(json.dumps(identity), encoding="utf-8")
            path.chmod(0o600)
        for index, token in enumerate(backend_tokens):
            environment[f"GLAMDB_ACCESS_BACKEND_TOKEN_{index}"] = token
        environment.update(XDG_DATA_HOME=str(state / "data"), XDG_CONFIG_HOME=str(state / "config"))
        config = caddy_config(registry, host=host, port=port, http_port=args.http_port, bind=args.bind,
                              site=site, sessions=sessions, theme=ROOT / "ui/config/theme.json",
                              certificate=certificate, key=key, metrics_listen=args.metrics_listen)
        config_path = state / "caddy.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        config_path.chmod(0o600)
        os.execve(executable, [executable, "run", "--config", str(config_path)], environment)


if __name__ == "__main__":
    main()
