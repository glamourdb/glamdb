"""Operator-only organization and database authorization administration."""

import argparse
import getpass
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
from uuid import uuid4

from .catalog import AuthorizationCatalog, CatalogError, catalog_lease, initialize, private_output


def socket_request(path, request, timeout=45):
    payload = json.dumps(request).encode() + b"\n"
    if len(payload) > 65536:
        raise CatalogError("RESOURCE_EXHAUSTED", "administrative request exceeds 64 KiB")
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall(payload)
        chunks = bytearray()
        while b"\n" not in chunks:
            piece = connection.recv(65536)
            if not piece:
                raise CatalogError("UNAVAILABLE", "administrative response was interrupted; look up the operation receipt")
            chunks.extend(piece)
            if len(chunks) > 1024 * 1024:
                raise CatalogError("RESOURCE_EXHAUSTED", "administrative response exceeds 1 MiB")
    response = json.loads(chunks.split(b"\n", 1)[0])
    if not response.get("ok"):
        error = response.get("error", {})
        raise CatalogError(error.get("code", "UNAVAILABLE"), error.get("message", "administrative command failed"))
    return response["result"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--catalog", type=Path, help="offline catalog; no gateway may own it")
    destination.add_argument("--socket", type=Path, help="running gateway's local administrative socket")
    parser.add_argument("--operation-id", default=None, help="retained UUID for safe retry of identical command content")
    parser.add_argument("--expected-revision", type=int)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("init")
    create.add_argument("--catalog", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--catalog", type=Path, required=True, help="new catalog path; existing state is never overwritten")
    backup = commands.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    password = commands.add_parser("hash-password")
    password.add_argument("--output", required=True, type=Path)
    password.add_argument("--stdin", action="store_true")
    password.add_argument("--caddy", default="caddy")
    for name in ("status", "list", "reconcile"):
        commands.add_parser(name)
    audit = commands.add_parser("audit")
    audit.add_argument("--limit", type=int, default=100)
    receipt = commands.add_parser("receipt")
    receipt.add_argument("id")
    org = commands.add_parser("organization", aliases=["org"])
    org_actions = org.add_subparsers(dest="verb", required=True)
    add = org_actions.add_parser("create")
    add.add_argument("id")
    add.add_argument("--name")
    add.add_argument("--login", required=True)
    add.add_argument("--password-hash-file", required=True, type=Path)
    update = org_actions.add_parser("update")
    update.add_argument("id")
    update.add_argument("--name")
    update.add_argument("--login")
    for name in ("enable", "disable"):
        operation = org_actions.add_parser(name)
        operation.add_argument("id")
    rotate = org_actions.add_parser("password")
    rotate.add_argument("id")
    rotate.add_argument("--password-hash-file", required=True, type=Path)
    delete = org_actions.add_parser("delete")
    delete.add_argument("id")
    deletion = delete.add_mutually_exclusive_group()
    deletion.add_argument("--detach-databases", action="store_true")
    deletion.add_argument("--transfer-to")
    database = commands.add_parser("database", aliases=["db"])
    db_actions = database.add_subparsers(dest="verb", required=True)
    for name in ("create", "update"):
        operation = db_actions.add_parser(name)
        operation.add_argument("id")
        operation.add_argument("--name")
        operation.add_argument("--url", required=name == "create")
        credential = operation.add_mutually_exclusive_group(required=name == "create")
        credential.add_argument("--token-env")
        credential.add_argument("--token-file", type=Path)
        operation.add_argument("--ca-file", type=Path)
        if name == "create":
            operation.add_argument("--organization")
        enabled = operation.add_mutually_exclusive_group()
        enabled.add_argument("--disable", action="store_true")
        enabled.add_argument("--enable", action="store_true")
    assign = db_actions.add_parser("assign")
    assign.add_argument("id")
    assign.add_argument("--organization", required=True)
    detach = db_actions.add_parser("detach")
    detach.add_argument("id")
    detach.add_argument("--expected-owner", required=True)
    move = db_actions.add_parser("transfer")
    move.add_argument("id")
    move.add_argument("--from", dest="from_owner", required=True)
    move.add_argument("--to", required=True)
    move.add_argument("--name")
    remove = db_actions.add_parser("delete")
    remove.add_argument("id")
    remove.add_argument("--expected-owner")
    batch = commands.add_parser("batch")
    batch.add_argument("--file", required=True, type=Path)
    args = parser.parse_args()
    store = None
    try:
        if args.command == "init":
            result = initialize(args.catalog)
        elif args.command == "restore":
            store = AuthorizationCatalog(args.backup)
            with catalog_lease(args.catalog):
                result = store.backup(args.catalog)
        elif args.command == "hash-password":
            private_output(args.output)
            executable = shutil.which(args.caddy)
            if executable is None:
                raise CatalogError("NOT_FOUND", "Caddy is required to hash a password")
            value = sys.stdin.readline().rstrip("\r\n") if args.stdin else getpass.getpass("Organization password: ")
            if not value.strip() or not 12 <= len(value.encode("utf-8")) <= 72 or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
                raise CatalogError("INVALID_ARGUMENT", "password must contain 12–72 UTF-8 bytes without control characters")
            process = subprocess.run([executable, "hash-password", "--algorithm", "bcrypt", "--bcrypt-cost", "14"],
                                     input=value + "\n", capture_output=True, text=True, check=True, timeout=30)
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(descriptor, "w") as output:
                output.write(process.stdout.strip() + "\n")
                output.flush()
                os.fsync(output.fileno())
            result = {"password_hash_file_created": True}
        else:
            if not args.socket and not args.catalog:
                parser.error("select --socket for a running gateway or --catalog for offline administration")
            read = args.command in ("status", "list", "audit", "receipt", "reconcile", "backup")
            request = {"action": args.command}
            if args.command == "audit":
                request["limit"] = args.limit
            elif args.command == "receipt":
                request["operation_id"] = args.id
            elif args.command == "backup":
                request["output"] = str(args.output)
            elif not read:
                action = "organization" if args.command in ("organization", "org") else "database"
                verb = args.verb if args.command != "batch" else "batch"
                params = {"id": args.id} if args.command != "batch" else {}
                if args.command == "batch":
                    action, verb, params = "batch", None, {"commands": json.loads(args.file.read_text())}
                else:
                    for name in ("name", "login", "url", "token_env", "ca_file", "token_file", "expected_owner"):
                        value = getattr(args, name, None)
                        if value is not None:
                            params[name] = str(value) if isinstance(value, Path) else value
                    if getattr(args, "organization", None) is not None:
                        params["organization_id"] = args.organization
                    if getattr(args, "password_hash_file", None):
                        params["password_hash"] = args.password_hash_file.read_text().strip()
                    if verb in ("enable", "disable"):
                        params["enabled"], verb = verb == "enable", "update"
                    if getattr(args, "disable", False) or getattr(args, "enable", False):
                        params["enabled"] = not args.disable
                    if getattr(args, "detach_databases", False):
                        params["detach_databases"] = True
                    if getattr(args, "transfer_to", None):
                        params["transfer_to"] = args.transfer_to
                    if getattr(args, "from_owner", None):
                        params["from"], params["to"] = args.from_owner, args.to
                request = {"operation_id": args.operation_id or str(uuid4()),
                           "action": action + "." + verb if verb else action, "params": params}
                if args.expected_revision is not None:
                    request["expected_revision"] = args.expected_revision
            if args.socket:
                result = socket_request(args.socket, request)
            else:
                store = AuthorizationCatalog(args.catalog)
                if read:
                    if args.command in ("status", "list"):
                        result = store.state()
                    elif args.command == "audit":
                        result = store.audit(args.limit)
                    elif args.command == "receipt":
                        result = store.receipt(args.id)
                    elif args.command == "backup":
                        with catalog_lease(args.catalog):
                            result = store.backup(args.output)
                    else:
                        raise CatalogError("INVALID_ARGUMENT", "reconcile requires a running gateway socket")
                else:
                    with catalog_lease(args.catalog):
                        result = store.apply(request)
        print(json.dumps(result))
        return 2 if isinstance(result, dict) and result.get("applied") is False else 0
    except CatalogError as error:
        print(json.dumps({"error": {"code": error.code, "message": str(error)}}), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error, ValueError, subprocess.SubprocessError):
        print(json.dumps({"error": {"code": "ADMIN_ERROR", "message": "administrative command failed; verify configuration and receipt"}}), file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
