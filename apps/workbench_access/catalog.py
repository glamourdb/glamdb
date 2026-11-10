"""Durable organization authorization metadata, separate from database storage."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
from urllib.parse import quote
from uuid import UUID

from .config import AccessConfigError, IDENTIFIER, LOGIN, PASSWORD_HASH, _text, parse_registry


APPLICATION_ID = 0x47444241
SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[2]


class CatalogError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def private_output(path):
    path = Path(path)
    if path.absolute().is_relative_to(ROOT / "ui") or path.resolve().is_relative_to(ROOT / "ui"):
        raise CatalogError("INVALID_ARGUMENT", "authorization output must remain outside the UI tree")
    return path


@contextmanager
def catalog_lease(path):
    lock = Path(str(Path(path).resolve()) + ".owner.lock")
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CatalogError("STATE_IN_USE", "catalog already has an owning process") from None
        yield descriptor
    finally:
        os.close(descriptor)


SCHEMA = """
CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL);
INSERT INTO metadata VALUES(1,0);
CREATE TABLE organizations (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, login TEXT NOT NULL UNIQUE,
 password_hash TEXT NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN(0,1)));
CREATE TABLE databases (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
 token_env TEXT, token_file TEXT, ca_file TEXT,
 organization_id TEXT REFERENCES organizations(id) ON DELETE RESTRICT,
 enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),
 CHECK((token_env IS NULL) != (token_file IS NULL)), UNIQUE(organization_id,name));
CREATE TABLE operations (id TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT NOT NULL);
CREATE TABLE audit (
 sequence INTEGER PRIMARY KEY, at TEXT NOT NULL, revision INTEGER NOT NULL,
 operation_id TEXT NOT NULL, action TEXT NOT NULL, target TEXT, actor_uid INTEGER NOT NULL,
 details TEXT NOT NULL);
"""


def initialize(path):
    path = private_output(path).absolute()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        raise CatalogError("ALREADY_EXISTS", "catalog initialization refuses to overwrite existing state") from None
    os.close(descriptor)
    connection = None
    try:
        connection = sqlite3.connect(path, isolation_level=None)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=EXTRA")
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
        with path.open("rb") as source:
            os.fsync(source.fileno())
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if connection is not None:
            connection.close()
    return {"schema_version": SCHEMA_VERSION, "revision": 0}


class AuthorizationCatalog:
    def __init__(self, path):
        self.path = Path(path).absolute()
        if self.path.is_symlink():
            raise CatalogError("INVALID_STATE", "catalog must be a regular file")
        try:
            info = self.path.stat()
        except OSError:
            raise CatalogError("NOT_FOUND", "catalog does not exist; initialize it explicitly") from None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise CatalogError("INVALID_STATE", "catalog must be owned by the current user with mode 0600")
        uri = "file:" + quote(str(self.path), safe="/") + "?mode=rw"
        self.connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=2, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        try:
            if (self.connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                    or self.connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION):
                raise CatalogError("INVALID_STATE", "unsupported authorization catalog format")
            self.connection.execute("PRAGMA foreign_keys=ON")
            if self.connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise CatalogError("INVALID_STATE", "authorization catalog requires DELETE journaling")
            self.connection.execute("PRAGMA synchronous=EXTRA")
            if self.connection.execute("PRAGMA synchronous").fetchone()[0] != 3:
                raise CatalogError("INVALID_STATE", "authorization catalog requires EXTRA synchronization")
            if self.connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise CatalogError("INVALID_STATE", "authorization catalog integrity check failed")
            self._validate_metadata()
        except BaseException:
            self.connection.close()
            raise

    def close(self):
        self.connection.close()

    def backup(self, output):
        """Create a consistent private catalog copy without overwriting any file."""
        output = private_output(output)
        if not output.is_absolute():
            raise CatalogError("INVALID_ARGUMENT", "backup output must be absolute")
        try:
            descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            raise CatalogError("ALREADY_EXISTS", "backup refuses to overwrite existing state") from None
        os.close(descriptor)
        destination = None
        try:
            with self.lock:
                if self.connection.in_transaction:
                    raise CatalogError("STATE_IN_USE", "backup cannot run inside an administrative transaction")
                destination = sqlite3.connect(output, isolation_level=None)
                self.connection.backup(destination)
                revision = destination.execute("SELECT revision FROM metadata WHERE singleton=1").fetchone()[0]
                destination.close()
                destination = None
            verified = AuthorizationCatalog(output)
            verified.close()
            with output.open("rb") as source:
                os.fsync(source.fileno())
            directory = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return {"backup_created": True, "revision": revision, "path": str(output)}
        except BaseException:
            if destination is not None:
                destination.close()
            output.unlink(missing_ok=True)
            raise

    @property
    def revision(self):
        return self.connection.execute("SELECT revision FROM metadata WHERE singleton=1").fetchone()[0]

    def _rows(self, table):
        return [dict(row) for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY id")]

    @contextmanager
    def _read_snapshot(self):
        owned = not self.connection.in_transaction
        if owned:
            self.connection.execute("BEGIN")
        try:
            yield
            if owned:
                self.connection.execute("COMMIT")
        except BaseException:
            if owned and self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def state(self):
        with self.lock, self._read_snapshot():
            organizations = self._rows("organizations")
            for organization in organizations:
                organization.pop("password_hash")
                organization["enabled"] = bool(organization["enabled"])
            databases = self._rows("databases")
            for database in databases:
                database["enabled"] = bool(database["enabled"])
            return {"revision": self.revision, "organizations": organizations, "databases": databases}

    def registry_data(self, *, active_only=True):
        with self.lock, self._read_snapshot():
            organizations, databases = self._rows("organizations"), self._rows("databases")
            owners = {row["id"] for row in organizations if row["enabled"] or not active_only}
            selected = [row for row in databases if not active_only or
                        (row["enabled"] and row["organization_id"] in owners)]
            definitions = []
            for row in selected:
                definition = {key: row[key] for key in ("id", "name", "url")}
                for key in ("token_env", "token_file", "ca_file"):
                    if row[key] is not None:
                        definition[key] = row[key]
                definitions.append(definition)
            groups = [{"id": row["id"], "name": row["name"], "login": row["login"],
                       "password_hash": row["password_hash"],
                       "databases": [db["id"] for db in selected if db["organization_id"] == row["id"]]}
                      for row in organizations if row["id"] in owners]
            return {"version": 1, "groups": groups, "databases": definitions}

    def _validate_metadata(self):
        try:
            parse_registry(self.registry_data(active_only=False), {}, allow_empty=True, resolve_credentials=False)
        except AccessConfigError as error:
            raise CatalogError("INVALID_ARGUMENT", str(error)) from None

    @staticmethod
    def _params(params, required, optional=()):
        if not isinstance(params, dict) or not set(required) <= params.keys() or params.keys() - set(required) - set(optional):
            raise CatalogError("INVALID_ARGUMENT", "invalid or unknown command fields")

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
            raise CatalogError("INVALID_ARGUMENT", "identifier must be a lowercase slug of at most 64 characters")
        return value

    def _organization(self, identifier):
        self._id(identifier)
        row = self.connection.execute("SELECT * FROM organizations WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise CatalogError("NOT_FOUND", "organization does not exist")
        return dict(row)

    def _database(self, identifier):
        self._id(identifier)
        row = self.connection.execute("SELECT * FROM databases WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise CatalogError("NOT_FOUND", "database mapping does not exist")
        return dict(row)

    def _mutate(self, action, params):
        if not isinstance(action, str) or not isinstance(params, dict):
            raise CatalogError("INVALID_ARGUMENT", "action must be text and params must be an object")
        for key in ("id", "name", "login", "password_hash", "url", "token_env", "token_file", "ca_file",
                    "organization_id", "from", "to", "expected_owner", "transfer_to"):
            if key not in params:
                continue
            nullable = key in ("token_env", "token_file", "ca_file", "organization_id", "expected_owner")
            if not isinstance(params[key], str) and not (nullable and params[key] is None):
                raise CatalogError("INVALID_ARGUMENT", "text command fields must be strings")
        db = self.connection
        if action == "batch":
            self._params(params, ("commands",))
            commands = params["commands"]
            if not isinstance(commands, list) or not 1 <= len(commands) <= 100:
                raise CatalogError("INVALID_ARGUMENT", "batch must contain 1–100 commands")
            for command in commands:
                self._params(command, ("action", "params"))
                if command["action"] == "batch":
                    raise CatalogError("INVALID_ARGUMENT", "nested batches are not supported")
                self._mutate(command["action"], command["params"])
        elif action == "organization.create":
            self._params(params, ("id", "login", "password_hash"), ("name",))
            identifier = self._id(params["id"])
            db.execute("INSERT INTO organizations VALUES(?,?,?,?,1)",
                       (identifier, params.get("name", identifier), params["login"], params["password_hash"]))
        elif action == "organization.update":
            self._params(params, ("id",), ("name", "login", "enabled"))
            self._organization(params["id"])
            if len(params) < 2:
                raise CatalogError("INVALID_ARGUMENT", "organization update requires a change")
            for key in ("name", "login", "enabled"):
                if key in params:
                    if key == "enabled" and type(params[key]) is not bool:
                        raise CatalogError("INVALID_ARGUMENT", "enabled must be boolean")
                    db.execute(f"UPDATE organizations SET {key}=? WHERE id=?", (params[key], params["id"]))
        elif action == "organization.password":
            self._params(params, ("id", "password_hash"))
            self._organization(params["id"])
            db.execute("UPDATE organizations SET password_hash=? WHERE id=?", (params["password_hash"], params["id"]))
        elif action == "organization.delete":
            self._params(params, ("id",), ("detach_databases", "transfer_to"))
            self._organization(params["id"])
            if "detach_databases" in params and type(params["detach_databases"]) is not bool:
                raise CatalogError("INVALID_ARGUMENT", "detach_databases must be boolean")
            target = params.get("transfer_to")
            if target is not None:
                self._organization(target)
                if target == params["id"] or params.get("detach_databases"):
                    raise CatalogError("INVALID_ARGUMENT", "invalid organization deletion policy")
            owned = db.execute("SELECT COUNT(*) FROM databases WHERE organization_id=?", (params["id"],)).fetchone()[0]
            if owned and not params.get("detach_databases") and target is None:
                raise CatalogError("NOT_EMPTY", "organization still owns databases; explicitly detach or transfer them")
            db.execute("UPDATE databases SET organization_id=? WHERE organization_id=?", (target, params["id"]))
            db.execute("DELETE FROM organizations WHERE id=?", (params["id"],))
        elif action == "database.create":
            self._params(params, ("id", "url"), ("name", "token_env", "token_file", "ca_file", "organization_id", "enabled"))
            identifier = self._id(params["id"])
            owner = params.get("organization_id")
            if owner is not None:
                self._organization(owner)
            enabled = params.get("enabled", True)
            if type(enabled) is not bool:
                raise CatalogError("INVALID_ARGUMENT", "enabled must be boolean")
            db.execute("INSERT INTO databases VALUES(?,?,?,?,?,?,?,?)", (
                identifier, params.get("name", identifier), params["url"], params.get("token_env"),
                params.get("token_file"), params.get("ca_file"), owner, enabled))
        elif action == "database.update":
            self._params(params, ("id",), ("name", "url", "token_env", "token_file", "ca_file", "enabled"))
            self._database(params["id"])
            if len(params) < 2:
                raise CatalogError("INVALID_ARGUMENT", "database update requires a change")
            if "token_env" in params and "token_file" in params:
                raise CatalogError("INVALID_ARGUMENT", "select one credential source")
            for key in ("name", "url", "token_env", "token_file", "ca_file", "enabled"):
                if key in params:
                    if key == "enabled" and type(params[key]) is not bool:
                        raise CatalogError("INVALID_ARGUMENT", "enabled must be boolean")
                    db.execute(f"UPDATE databases SET {key}=? WHERE id=?", (params[key], params["id"]))
            if "token_env" in params:
                db.execute("UPDATE databases SET token_file=NULL WHERE id=?", (params["id"],))
            if "token_file" in params:
                db.execute("UPDATE databases SET token_env=NULL WHERE id=?", (params["id"],))
        elif action in ("database.assign", "database.detach", "database.transfer"):
            required = {"database.assign": ("id", "organization_id"), "database.detach": ("id", "expected_owner"),
                        "database.transfer": ("id", "from", "to")}[action]
            self._params(params, required, ("name",) if action == "database.transfer" else ())
            current = self._database(params["id"])
            if action == "database.assign":
                target = params["organization_id"]
                if current["organization_id"] is not None:
                    raise CatalogError("CONFLICT", "database is already assigned; use transfer with the expected owner")
            else:
                expected = params["expected_owner"] if action == "database.detach" else params["from"]
                if current["organization_id"] != expected:
                    raise CatalogError("CONFLICT", "database owner changed")
                target = None if action == "database.detach" else params["to"]
                if target == expected:
                    raise CatalogError("INVALID_ARGUMENT", "transfer requires a different owner")
            if target is not None:
                self._organization(target)
            db.execute("UPDATE databases SET organization_id=?,name=? WHERE id=?",
                       (target, params.get("name", current["name"]), params["id"]))
        elif action == "database.delete":
            self._params(params, ("id",), ("expected_owner",))
            current = self._database(params["id"])
            if current["organization_id"] != params.get("expected_owner"):
                raise CatalogError("CONFLICT", "deleting an assigned mapping requires its expected owner")
            db.execute("DELETE FROM databases WHERE id=?", (params["id"],))
        else:
            raise CatalogError("INVALID_ARGUMENT", "unsupported administrative action")

    def receipt(self, operation_id):
        with self.lock:
            row = self.connection.execute("SELECT receipt FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise CatalogError("NOT_FOUND", "operation receipt does not exist")
            return json.loads(row[0])

    def audit(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise CatalogError("INVALID_ARGUMENT", "audit limit must be between 1 and 1000")
        with self.lock:
            rows = [dict(row) for row in self.connection.execute("SELECT * FROM audit ORDER BY sequence DESC LIMIT ?", (limit,))]
            for row in rows:
                row["details"] = json.loads(row["details"])
            return rows

    def apply(self, request, *, validate=None, fence=None, activate=None, actor_uid=None):
        self._params(request, ("operation_id", "action", "params"), ("expected_revision",))
        try:
            operation_id = str(UUID(request["operation_id"]))
        except (ValueError, TypeError, AttributeError):
            raise CatalogError("INVALID_ARGUMENT", "operation_id must be a retained UUID") from None
        expected = request.get("expected_revision")
        if expected is not None and (type(expected) is not int or expected < 0):
            raise CatalogError("INVALID_ARGUMENT", "expected_revision must be a nonnegative integer")
        digest = hashlib.sha256(json.dumps({key: request[key] for key in ("action", "params")},
                                          sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.lock:
            db = self.connection
            db.execute("BEGIN IMMEDIATE")
            try:
                previous = db.execute("SELECT digest,receipt FROM operations WHERE id=?", (operation_id,)).fetchone()
                if previous:
                    if previous[0] != digest:
                        raise CatalogError("CONFLICT", "operation_id is already bound to different command content")
                    db.execute("ROLLBACK")
                    return json.loads(previous[1])
                if expected is not None and expected != self.revision:
                    raise CatalogError("CONFLICT", "catalog revision changed; reread state before retrying a new command")
                self._mutate(request["action"], request["params"])
                self._validate_metadata()
                db.execute("UPDATE metadata SET revision=revision+1 WHERE singleton=1")
                revision = self.revision
                if validate:
                    validate()
                if fence:
                    fence()
                receipt = {"operation_id": operation_id, "action": request["action"], "revision": revision,
                           "target": request["params"].get("id"), "metadata_committed": True,
                           "applied": False if activate else None}
                db.execute("INSERT INTO operations VALUES(?,?,?)", (operation_id, digest, json.dumps(receipt)))
                audited = request["params"]["commands"] if request["action"] == "batch" else [request]
                for item in audited:
                    details = {key: value for key, value in item["params"].items()
                               if key in ("from", "to", "expected_owner", "organization_id", "enabled",
                                          "detach_databases", "transfer_to")}
                    if item["action"] == "organization.password":
                        details["password_rotated"] = True
                    db.execute("INSERT INTO audit(at,revision,operation_id,action,target,actor_uid,details) VALUES(?,?,?,?,?,?,?)", (
                        datetime.now(timezone.utc).isoformat(), revision, operation_id, item["action"],
                        item["params"].get("id"), os.geteuid() if actor_uid is None else actor_uid,
                        json.dumps(details, sort_keys=True)))
                db.execute("COMMIT")
            except BaseException as error:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                if isinstance(error, sqlite3.IntegrityError):
                    raise CatalogError("CONFLICT", "duplicate identity, display name, owner or invalid required field") from None
                raise
            if activate:
                try:
                    activate()
                    receipt["applied"] = True
                except Exception:
                    receipt["error"] = {"code": "APPLY_FAILED", "message": "metadata committed; gateway is unavailable; reconcile required"}
                db.execute("BEGIN IMMEDIATE")
                try:
                    db.execute("UPDATE operations SET receipt=? WHERE id=?", (json.dumps(receipt), operation_id))
                    db.execute("COMMIT")
                except BaseException:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise
            return receipt
