"""Strict access registry and Caddy configuration; no storage or HTTP writer."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}")
LOGIN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
PASSWORD_HASH = re.compile(r"\$2[aby]\$1[2-6]\$[./A-Za-z0-9]{53}")
ENVIRONMENT_KEY = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")


class AccessConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Database:
    id: str
    name: str
    url: str
    token_env: str | None
    ca_file: Path | None
    token_file: Path | None = None


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    login: str
    password_hash: str
    databases: tuple[str, ...]


@dataclass(frozen=True)
class Registry:
    databases: tuple[Database, ...]
    groups: tuple[Group, ...]


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise AccessConfigError("invalid or unknown fields in access configuration")


def _text(value, maximum=128):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise AccessConfigError("invalid text in access configuration")
    return value


def _unique(values, kind):
    if len(values) != len(set(values)):
        raise AccessConfigError(f"duplicate {kind} in access configuration")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AccessConfigError("duplicate JSON key in access configuration")
        result[key] = value
    return result


def load_registry(path: Path, environment) -> Registry:
    try:
        with path.open("rb") as source:
            raw = source.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise AccessConfigError("access configuration exceeds 1 MiB")
        data = json.loads(raw, object_pairs_hook=_pairs)
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, AccessConfigError):
            raise
        raise AccessConfigError("access configuration must be a readable UTF-8 JSON file") from None
    return parse_registry(data, environment)


def database_token(database, environment):
    if database.token_file:
        try:
            if not database.token_file.is_file():
                raise OSError("credential source is not a regular file")
            with database.token_file.open("rb") as source:
                raw = source.read(4098)
            token = raw.decode("ascii").rstrip("\r\n")
        except (OSError, UnicodeError):
            raise AccessConfigError("unreadable database credential file in access configuration") from None
    else:
        token = environment.get(database.token_env, "")
    if not token or len(token) > 4096 or any(ord(c) < 33 or ord(c) > 126 or c in "{}" for c in token):
        raise AccessConfigError("missing or invalid database credential for access configuration")
    return token


def parse_registry(data, environment, *, allow_empty=False, resolve_credentials=True) -> Registry:
    _object(data, ("version", "databases", "groups"))
    if type(data["version"]) is not int or data["version"] != 1:
        raise AccessConfigError("unsupported access configuration version")
    for key in ("databases", "groups"):
        if not isinstance(data[key], list) or len(data[key]) > 128:
            raise AccessConfigError("access configuration lists must have at most 128 entries")
    if not data["groups"] and not allow_empty:
        raise AccessConfigError("access configuration requires at least one group")
    databases = []
    for item in data["databases"]:
        _object(item, ("id", "url"), ("name", "ca_file", "token_env", "token_file"))
        if ("token_env" in item) == ("token_file" in item):
            raise AccessConfigError("exactly one database credential source is required in access configuration")
        identifier = _text(item["id"], 64)
        if not IDENTIFIER.fullmatch(identifier):
            raise AccessConfigError("invalid database identifier in access configuration")
        value = _text(item["url"], 2048)
        try:
            url = urlsplit(value)
            host, port = url.hostname, url.port
            if not host or any(ord(c) < 33 or ord(c) > 126 for c in value):
                raise ValueError()
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host == "localhost"
                if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
                    raise ValueError()
            if (url.scheme not in ("http", "https") or (url.scheme == "http" and not loopback)
                    or url.username or url.password or url.query or url.fragment or url.path not in ("", "/")
                    or (port is not None and not 1 <= port <= 65535)):
                raise ValueError()
        except ValueError:
            raise AccessConfigError("invalid or insecure database URL in access configuration") from None
        key = None
        credential_file = None
        if "token_env" in item:
            key = _text(item["token_env"], 128)
            if not ENVIRONMENT_KEY.fullmatch(key):
                raise AccessConfigError("invalid credential environment name in access configuration")
        else:
            credential_file = Path(_text(item["token_file"], 4096)).resolve()
        ca = None
        if "ca_file" in item:
            ca = Path(_text(item["ca_file"], 4096)).resolve()
            if url.scheme != "https" or (resolve_credentials and not ca.is_file()):
                raise AccessConfigError("access configuration CA requires HTTPS and an existing file")
        database = Database(identifier, _text(item.get("name", identifier)), value.rstrip("/"), key, ca, credential_file)
        if resolve_credentials:
            database_token(database, environment)
        databases.append(database)
    _unique([item.id for item in databases], "database identifier")
    # Distinct logical databases must not accidentally route to the same origin.
    _unique([(urlsplit(d.url).scheme, urlsplit(d.url).hostname,
              urlsplit(d.url).port or (443 if d.url.startswith("https:") else 80)) for d in databases],
            "database origin")
    known = {database.id for database in databases}
    groups = []
    grants = 0
    for item in data["groups"]:
        _object(item, ("id", "login", "password_hash", "databases"), ("name",))
        identifier, login, password = (_text(item[key], maximum) for key, maximum in
                                       (("id", 64), ("login", 64), ("password_hash", 60)))
        if not IDENTIFIER.fullmatch(identifier) or not LOGIN.fullmatch(login) or not PASSWORD_HASH.fullmatch(password):
            raise AccessConfigError("invalid group identity or bcrypt hash in access configuration")
        allowed = item["databases"]
        if not isinstance(allowed, list) or any(not isinstance(value, str) or value not in known for value in allowed):
            raise AccessConfigError("unknown database grant in access configuration")
        _unique(allowed, "database grant")
        grants += len(allowed)
        groups.append(Group(identifier, _text(item.get("name", identifier)), login, password, tuple(allowed)))
    _unique([item.id for item in groups], "group identifier")
    _unique([item.login for item in groups], "group login")
    if grants > 1024:
        raise AccessConfigError("access configuration exceeds 1024 database grants")
    return Registry(tuple(databases), tuple(groups))


def json_response(status, code, message):
    return {"handler": "static_response", "status_code": status,
            "headers": {"Content-Type": ["application/json"]},
            "body": json.dumps({"error": {"code": code, "message": message}})}


def caddy_config(registry, *, host, port, http_port, bind, site, sessions, theme, certificate=None, key=None,
                 revision=None, metrics_listen=None, gateway_id=None):
    headers = {"handler": "headers", "response": {"set": {
        "Cache-Control": ["private, no-store"], "X-Content-Type-Options": ["nosniff"],
        "Referrer-Policy": ["no-referrer"], "Strict-Transport-Security": ["max-age=31536000"],
        "X-Frame-Options": ["DENY"]}}}
    authentication = {"handler": "authentication", "providers": {"http_basic": {
        "accounts": [{"username": group.login, "password": f"{{env.GLAMDB_ACCESS_GROUP_HASH_{i}}}"}
                     for i, group in enumerate(registry.groups)],
        "hash": {"algorithm": "bcrypt"}, "hash_cache": {}}}}
    if not registry.groups:
        authentication = json_response(401, "UNAUTHENTICATED", "Authentication required")
        authentication["headers"]["Www-Authenticate"] = ['Basic realm="GlamDB"']
    if revision is not None:
        headers["response"]["set"]["X-GlamDB-Access-Revision"] = [str(revision)]
    if gateway_id is not None:
        headers["response"]["set"]["X-GlamDB-Gateway-ID"] = [gateway_id]
    not_found = json_response(404, "NOT_FOUND", "Resource not found")
    routes = [{"handle": [headers, {"handler": "headers", "response": {
        "deferred": True, "set": headers["response"]["set"], "delete": ["Server"]}}, authentication]},
        {"match": [{"path_regexp": {"pattern": r"(^|/)\."}}], "handle": [not_found]}]
    database_index = {database.id: (i, database) for i, database in enumerate(registry.databases)}
    for group in registry.groups:
        identity = {"match": [{"path": ["/workbench/session"]}], "handle": [
            {"handler": "rewrite", "uri": f"/{group.id}.json"},
            {"handler": "file_server", "root": str(sessions)}], "terminal": True}
        group_routes = [identity]
        for database_id in group.databases:
            index, database = database_index[database_id]
            prefix = f"/databases/{database_id}"
            url = urlsplit(database.url)
            target_host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
            target = f"{target_host}:{url.port or (443 if url.scheme == 'https' else 80)}"
            transport = {"protocol": "http", "dial_timeout": 5_000_000_000,
                         "response_header_timeout": 30_000_000_000}
            if url.scheme == "https":
                transport["tls"] = {"server_name": url.hostname}
                if database.ca_file:
                    transport["tls"]["ca"] = {"provider": "file", "pem_files": [str(database.ca_file)]}
            proxy = {"handler": "reverse_proxy", "upstreams": [{"dial": target}],
                     "transport": transport, "flush_interval": -1,
                     "headers": {"request": {
                         "delete": ["Cookie", "Proxy-Authorization", "X-GlamDB-Group", "X-GlamDB-Database"],
                         "set": {"Authorization": [f"Bearer {{env.GLAMDB_ACCESS_BACKEND_TOKEN_{index}}}"]}}}}
            group_routes.append({"match": [{"path": [prefix + "/v0", prefix + "/v0/*"]}],
                                 "handle": [{"handler": "rewrite", "strip_path_prefix": prefix}, proxy],
                                 "terminal": True})
        routes.append({"match": [{"expression": f'{{http.auth.user.id}} == "{group.login}"'}],
                       "handle": [{"handler": "subroute", "routes": group_routes}]})
    routes.extend([
        {"match": [{"path": ["/workbench/config"]}], "handle": [
            {"handler": "rewrite", "uri": "/" + theme.name},
            {"handler": "file_server", "root": str(theme.parent)}], "terminal": True},
        {"match": [{"path": ["/workbench/*", "/workbench", "/databases/*", "/databases", "/v0", "/v0/*"]}],
         "handle": [not_found], "terminal": True},
        {"handle": [{"handler": "file_server", "root": str(site)}]}])
    errors = {"routes": [
        {"match": [{"expression": "{http.error.status_code} == 401"}],
         "handle": [headers, json_response(401, "UNAUTHENTICATED", "Authentication required")], "terminal": True},
        {"match": [{"expression": "{http.error.status_code} >= 500"}],
         "handle": [headers, json_response(502, "UNAVAILABLE", "Database server unavailable")], "terminal": True},
        {"handle": [headers, not_found]}]}
    server = {"listen": [f"{bind}:{port}"], "routes": [{"match": [{"host": [host]}],
              "handle": [{"handler": "subroute", "routes": routes}], "terminal": True}],
              "tls_connection_policies": [{}], "errors": errors}
    apps = {"http": {"http_port": http_port, "servers": {"workbench": server}},
            "pki": {"certificate_authorities": {"local": {"install_trust": False}}}}
    if metrics_listen:
        server["metrics"] = {}
        apps["http"]["servers"]["telemetry"] = {
            "listen": [metrics_listen], "routes": [{"match": [{"path": ["/metrics"]}],
            "handle": [{"handler": "metrics"}]}], "automatic_https": {"disable": True}}
    if certificate:
        apps["tls"] = {"certificates": {"load_files": [{"certificate": str(certificate), "key": str(key)}]}}
    return {"admin": {"disabled": True, "config": {"persist": False}}, "apps": apps}
