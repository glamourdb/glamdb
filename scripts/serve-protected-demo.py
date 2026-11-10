#!/usr/bin/env python3
"""Serve the browser-only demo behind mandatory Caddy HTTPS authentication."""

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--caddy", default=os.environ.get("GLAMDB_DEMO_CADDY", "caddy"))
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/protected-demo/site")
    parser.add_argument("--state", type=Path, default=ROOT / "artifacts/protected-demo/state")
    parser.add_argument("--bind", choices=("0.0.0.0", "127.0.0.1"), default="0.0.0.0")
    parser.add_argument("--http-port", type=int, default=80)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    args = parser.parse_args()

    host = os.environ.get("GLAMDB_DEMO_HOST", "")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?", host):
        parser.error("GLAMDB_DEMO_HOST must be a hostname with optional HTTPS port")
    if ":" in host and not 1 <= int(host.rsplit(":", 1)[1]) <= 65535:
        parser.error("GLAMDB_DEMO_HOST port must be between 1 and 65535")
    password_hash = os.environ.get("GLAMDB_DEMO_PASSWORD_HASH", "")
    if not re.fullmatch(r"\$2[aby]\$1[2-6]\$[./A-Za-z0-9]{53}", password_hash):
        parser.error("GLAMDB_DEMO_PASSWORD_HASH must be a bcrypt hash with cost 12–16")
    user = os.environ.get("GLAMDB_DEMO_USER", "glamdb")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", user):
        parser.error("GLAMDB_DEMO_USER must contain 1–64 ASCII letters, digits, _, . or -")
    if not 1 <= args.http_port <= 65535:
        parser.error("--http-port must be between 1 and 65535")
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key must be supplied together")
    tls_directive = ""
    if args.tls_cert:
        files = [path.resolve() for path in (args.tls_cert, args.tls_key)]
        if any(not path.is_file() or any(char in str(path) for char in '\"\n\r{}') for path in files):
            parser.error("TLS files must exist and have paths without quotes, braces or newlines")
        tls_directive = f'tls "{files[0]}" "{files[1]}"'
    output, state = args.output.resolve(), args.state.resolve()
    artifact_root = (ROOT / "artifacts").resolve()
    if state == artifact_root or not state.is_relative_to(artifact_root):
        parser.error("--state must be a dedicated artifacts subdirectory")
    if output == state or state.is_relative_to(output) or output.is_relative_to(state):
        parser.error("--state and --output must be separate directories")
    if args.tls_cert and any(path.is_relative_to(output) for path in files):
        parser.error("TLS keys and certificates must stay outside the served directory")
    executable = shutil.which(args.caddy)
    if executable is None:
        parser.error("Caddy is required; run python3 scripts/fetch-demo-caddy.py")

    # The packager enforces a dedicated artifacts subdirectory and an allowlist.
    subprocess.run([sys.executable, str(ROOT / "scripts/build-public-demo.py"),
                    "--output", str(output)], check=True)
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    state.chmod(0o700)
    environment = os.environ.copy()
    environment.update({
        "GLAMDB_DEMO_HOST": host,
        "GLAMDB_DEMO_USER": user,
        "GLAMDB_DEMO_PASSWORD_HASH": password_hash,
        "GLAMDB_DEMO_ROOT": str(output),
        "GLAMDB_DEMO_BIND": args.bind,
        "GLAMDB_DEMO_HTTP_PORT": str(args.http_port),
        "GLAMDB_DEMO_TLS": tls_directive,
        "XDG_DATA_HOME": str(state / "data"),
        "XDG_CONFIG_HOME": str(state / "config"),
    })
    # No config API, credential-bearing autosave or request/access logging.
    os.execve(executable, [executable, "run", "--config",
                          str(ROOT / "deploy/demo-access/Caddyfile"),
                          "--adapter", "caddyfile"], environment)


if __name__ == "__main__":
    main()
