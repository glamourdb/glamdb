# Static group access to database servers

For mutable organizations with exclusive database ownership, local operator
commands, receipts and backups, use [ORGANIZATIONS.md](ORGANIZATIONS.md).
This compatibility mode uses a static registry and restarts for changes.

The HTTPS workbench supports one login and password-token per group and an
explicit database allowlist. The selector shows only those databases. Caddy
checks every request before contacting a database, including direct API calls,
metadata, writes and Arrow downloads. Guessing an ID or sending another group
name in a request header does not grant access.

A database here is a configured HTTP origin exposing exactly one store and its
collections. Each origin represents one database; different IDs cannot alias
the same origin. Groups may share a database through explicit grants.
Permissions cover the whole database; read-only roles and collection-level
permissions are not implemented. Members of a group share its credentials.

## Deployment boundary

The standard-library Python launcher validates configuration and packages UI
assets. Caddy provides HTTPS authentication, authorization and routing. Reads
and writes belong to the configured database servers: the gateway has no storage
writer, does not create databases, and never starts a mock server. Each backend
must implement the `/v0` contract used by the [SDK](../../sdk/python/README.md)
and [workbench](WORKBENCH.md), scoped to its single database. The native C++
HTTP adapter remains separate work.

GitHub Pages cannot run this gateway. Existing anonymous Pages content stays
accessible until a protected replacement is deployed, verified and the old
publication is retired. Merging these changes does not protect that URL.

Use a continuously running host with Python 3.9+, Caddy 2.11.9, DNS for the
workbench hostname and TCP 80/443 available. Run under a dedicated service
account using the host's normal port-binding and service-management mechanism.

## Configure databases and groups

Copy [the example registry](../../deploy/workbench-access.example.json) to an
operator-managed file outside the served directory and `ui/` tree. Restrict
file access, for example mode 0600. Its hash placeholders deliberately fail
validation until replaced.

Each database has `id`, `name`, `url` and `token_env`. IDs begin with a lowercase
letter and contain lowercase letters, digits, `_` or `-`, up to 64 characters.
URLs have no credentials, path prefix, query or fragment. HTTP is allowed only
on loopback; other endpoints require HTTPS with certificate verification.
Optional `ca_file` supplies a trusted CA for that backend; verification cannot
be disabled. Backend ports should be restricted to the gateway's network or
loopback and must not expose a separate global administration API.

Provision each backend's bearer credential in its named environment variable
through the hosting secret configuration. These credentials stay on the server;
the browser never receives them and generated Caddy configuration contains only
environment references. Do not reuse a group's token as a backend credential.

Each group has unique `id` and `login`, a display `name`, `password_hash` and
`databases` containing only defined IDs. A login contains 1–64 ASCII letters,
digits, `_`, `.` or `-`. An empty database list grants no data access.
In the example, Analytics sees Sales; Research sees Research lab and Sales.
Sharing Sales is deliberate: both groups access the same database.

Choose each token with a password manager: at least 32 random bytes, for example
43 URL-safe characters, at most bcrypt's 72-byte limit. Install the pinned Caddy
and hash the token interactively without putting plaintext into shell history
or command arguments:

```sh
python3 scripts/fetch-demo-caddy.py
artifacts/tools/caddy-2.11.9/caddy hash-password --algorithm bcrypt --bcrypt-cost 14
```

Put the hash in that group's `password_hash`; retain and share the token through
the password manager. Accepted bcrypt costs are 12–16. Supply real credentials
and TLS keys through hosting configuration, outside version control/web roots.

## Validate and start

From the source checkout, after provisioning the backend token variables:

```sh
export GLAMDB_WORKBENCH_HOST=workbench.example.com
python3 -m apps.workbench_access --access-config /etc/glamdb/access.json --check
python3 -m apps.workbench_access --access-config /etc/glamdb/access.json \
  --caddy "$PWD/artifacts/tools/caddy-2.11.9/caddy"
```

`--check` validates registry/settings without contacting databases or starting
services; it does not certify backend readiness or certificate issuance.
Startup rejects malformed/duplicate fields, IDs, logins and grants, missing
credentials, unsafe URLs and credential files inside UI/web roots. Limits are
1 MiB input, 128 groups, 128 databases and 1024 total grants.

Connected UI assets are packaged into `artifacts/workbench-access/site`, without
the browser demo's sample data. Generated configuration and per-group session
descriptors stay in the separate `artifacts/workbench-access/state` directory.
Preserve this state for certificate renewal. Administration, configuration
autosave and request/access logging are disabled. No local CA is installed into
the operating system trust store. Symlinked package inputs are rejected.

Public DNS and reachable challenge ports allow automatic HTTPS certificates.
Existing certificates use both `--tls-cert` and `--tls-key`; keep these files
outside served/UI trees. `--bind 127.0.0.1` is available for local deployment
behind another trusted HTTPS endpoint. HTTP only redirects; it serves no data.

Open the HTTPS hostname and enter the group's login and token in the browser's
standard prompt. The UI shows group identity and allowed databases. Switching
databases clears old results, forms, operations and pending requests. An
unreachable backend shows an error and leaves the selector usable; it never
substitutes a simulation or another database. Tokens and results are not saved
in browser storage.

Edit `ui/config/theme.json` for colors, fonts, layout, logos and decoration.
Authenticated `/workbench/config` reads this file and the UI refreshes it without
compilation or restart. Style studio previews/downloads changes; it cannot write
the server theme or access registry.

## API and SDK

Authenticated `GET /workbench/session` returns only the current group and its
database IDs/names. No backend URL, credential, hash or other group is exposed:

```json
{
  "version": 1,
  "group": {"id": "analytics", "name": "Analytics", "login": "analytics"},
  "databases": [{"id": "sales", "name": "Sales"}]
}
```

Database paths are `/databases/<id>/v0/...`. Only `/v0` is proxied; unscoped `/v0`
and other backend/admin paths are unavailable. Unknown and forbidden IDs both
return 404. Missing/incorrect credentials return 401 JSON with a Basic challenge.
Backend connection/TLS failures return 502. A running gateway does not establish
database readiness or storage durability.

Connect the SDK with the group login and selected database prefix:

```python
import getpass
from glamdb_sdk import Client

token = getpass.getpass("Group token: ")
with Client("https://workbench.example.com/databases/sales", token,
            username="analytics") as database:
    print(database.list_collections())
```

The SDK uses HTTP Basic and preserves this prefix on all routes. Clients without
`username` retain direct-server bearer auth. The browser sends its native sign-in
credentials to the gateway, which replaces them with the configured backend
bearer credential upstream. Never publish these backend credentials or use an
unprotected alternate backend URL as the normal client entry point.

## Add, revoke and rotate access

Edit the registry and restart the gateway through its service manager:

- Add a group with a unique login, new hash and explicit database list.
- Remove a database from a group to revoke that grant, including direct URLs.
- Replace a group's hash to rotate only that group's token.
- Remove a group to reject its credentials entirely.

Revoking one member requires changing the group's shared token. There are no
per-person accounts or token expiration. Browsers cache Basic credentials:
Disconnect clears application memory; Reload access uses the browser's current
sign-in. Close all windows of a private browser session to discard credentials.
Previously downloaded data cannot be revoked.

## Verification

Install the workbench test dependencies and Chromium, then run:

```sh
bash scripts/check-workbench.sh
python3 scripts/fetch-demo-caddy.py
bash scripts/check-protected-demo.sh
```

The access suite requires real Caddy/HTTPS/Chromium and explicit disposable mock
database backends with separate stores and credentials. It checks writes, reads,
Arrow, shared/forbidden grants, direct/encoded paths and forged headers, rotation
and removal, backend TLS trust, secret publication, caching and browser switching.
These tests establish gateway behavior, not native engine durability.

Authentication traces/HAR/video are disabled. Credentials are disposable and
generated for test processes. Chromium's test-only TLS bypass is restricted to
explicit loopback URLs; HTTPX independently checks trust and rejection. Missing
Caddy is a failure, not a silent skip.
