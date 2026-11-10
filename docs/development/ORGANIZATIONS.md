# Organizations and database access

An organization has one shared login/password and zero or more databases.
A database mapping has at most one owner. Caddy authenticates every UI, metadata,
read, write and Arrow request before proxying it. The browser receives only its
own organization and database names. No production mode substitutes test data.

The operator administers access locally through a Unix socket and CLI. Members
have no administration endpoint. A separate SQLite catalog stores authorization
metadata, bcrypt hashes, revisions, permanent operation receipts and audit.
It contains no database rows and is not a second database writer.

Implemented operations: create/list/rename/disable/enable/delete organizations;
change login; rotate/reset password; register/rename/disable/remove database
mappings; assign, detach or transfer them; atomic batches; consistent private
backups and restore to a new file. Deleting a nonempty organization requires an
explicit detach-all or transfer-all policy. Stale revisions/owners conflict.

**Registration connects an existing single-store HTTP server. Removing a
mapping never erases its underlying data.** Provisioning and physical deletion
belong to that server's operator. Native HTTP/core integration remains separate
work. Access tests use explicit disposable mock databases and do not establish
native engine durability.

Members share all organization permissions. Per-person accounts, MFA, SSO,
read-only/collection roles, password expiry and a built-in brute-force limiter
are not implemented. Rotate the shared password when someone leaves; use
hosting-edge authentication rate controls for an internet deployment. Already
downloaded data cannot be revoked.

The [static group mode](WORKBENCH_ACCESS.md) remains explicitly available for
fixed configurations with deliberate shared grants. Use the persistent
organization catalog for exclusive ownership and online administration.

## 1. Prepare hosting and backend servers

1. Obtain a continuously running Linux host with SSH access and Python 3.9+.
   Static GitHub Pages cannot execute this gateway.
2. Point a hostname's A/AAAA DNS record to the host. Allow TCP 80/443 through
   your hosting firewall while preserving SSH access. Do not expose backends,
   metrics or the operator interface publicly.
3. Install [Caddy](https://caddyserver.com/docs/install) 2.11.9. The verified
   repository downloader supports Linux amd64 and macOS arm64:

   ~~~sh
   python3 scripts/fetch-demo-caddy.py
   ~~~

   For other architectures select the matching official release and verify its
   published checksum. Read [automatic HTTPS requirements](https://caddyserver.com/docs/automatic-https).
4. Place the checkout at a stable path, for example /opt/glamdb. Run commands
   from its root. The gateway itself uses only Python's standard library.
5. Provision actual database HTTP servers separately. Each URL must implement
   the [SDK's /v0 contract](../../sdk/python/README.md) for exactly one store.
   Distinct IDs cannot alias the same origin. HTTPS certificate verification is
   mandatory except for loopback HTTP; optional CA files support private PKI.
   URLs cannot include user info, query, fragment or path prefixes.
6. Restrict backend ports to the gateway network and require their bearer
   credentials. Do not expose an alternative global admin API through /v0.
   Backend tokens and organization passwords must be different secrets.

Run as a dedicated service account. It needs readable code/secrets and writable
authorization/artifacts directories. Members must not have its shell access.
For example, after checking that the account/path do not already exist, a Linux
operator can prepare them as follows:

~~~sh
sudo useradd --system --home-dir /var/lib/glamdb --create-home --shell /usr/sbin/nologin glamdb
sudo install -d -m 700 -o glamdb -g glamdb /var/lib/glamdb/access /var/lib/glamdb/access/backups
sudo install -d -m 700 -o glamdb -g glamdb /var/lib/glamdb/.local/state/glamdb-workbench/listeners
sudo install -d -m 700 -o glamdb -g glamdb /opt/glamdb/artifacts/workbench-access
~~~

Install the downloaded Caddy at /usr/local/bin/caddy, owned by the operator
and executable by the service account. Keep the checkout readable by that
account. The following administrative commands must run as that account
(use sudo -u glamdb from an operator shell where needed).

## 2. Initialize authorization and the first organization

Keep the catalog, backups, token files, password hashes and TLS keys outside
ui/, the served site and the generated runtime-state tree. Never commit them.

~~~sh
umask 077
python3 -m apps.workbench_access.admin init --catalog /var/lib/glamdb/access/authorization.sqlite
python3 -m apps.workbench_access.admin hash-password --caddy /usr/local/bin/caddy \
  --output /var/lib/glamdb/access/analytics-password.hash
python3 -m apps.workbench_access.admin --catalog /var/lib/glamdb/access/authorization.sqlite \
  organization create analytics --name "Analytics" --login analytics \
  --password-hash-file /var/lib/glamdb/access/analytics-password.hash
~~~

The password helper prompts without echo and writes bcrypt cost 14 to a new
mode 0600 file. Use a password manager; 32 random bytes encoded as URL-safe text
are suitable. It accepts 12–72 UTF-8 bytes without control characters. Plaintext
is never a command-line argument or catalog value. Save the password separately;
a hash cannot recover it. Initialization and hash creation refuse overwrites.

IDs are lowercase slugs starting with a letter, up to 64 characters. Logins
are unique 1–64 ASCII letters/digits/dots/underscores/hyphens. Names are display
labels. Empty organizations are valid and see an empty workspace.

## 3. Register an existing database

Provision the real backend token through your hosting secret facility into
/var/lib/glamdb/access/sales.token, mode 0600, readable only by the service.
Then register that server:

~~~sh
python3 -m apps.workbench_access.admin --catalog /var/lib/glamdb/access/authorization.sqlite \
  database create sales --name "Sales" --organization analytics \
  --url https://sales.internal.example.com --token-file /var/lib/glamdb/access/sales.token
~~~

Append --ca-file /etc/glamdb/backend-ca.pem for private PKI. Instead of
--token-file, --token-env GLAMDB_SALES_TOKEN reads a service environment secret.
Token-file replacement can be applied with reconcile. A running process cannot
read later edits to its parent shell environment.

Limits: 128 organizations, 128 database mappings, 1024 total grants, 64 KiB per
administrative request, 100 commands per batch. Disabled and unassigned
definitions must still be valid. Duplicate logins/IDs/origins and display names
within the same owner are rejected.

## 4. Start and maintain the HTTPS service

~~~sh
python3 -m apps.workbench_access --catalog /var/lib/glamdb/access/authorization.sqlite \
  --host workbench.example.com --caddy /usr/local/bin/caddy --check
python3 -m apps.workbench_access --catalog /var/lib/glamdb/access/authorization.sqlite \
  --host workbench.example.com --caddy /usr/local/bin/caddy --metrics-listen 127.0.0.1:9464
~~~

--check validates settings and available secrets, not backend readiness or
certificate issuance. Connected assets go to artifacts/workbench-access/site;
private generated routing, certificates and operator.sock stay separately in
artifacts/workbench-access/state. Both must be dedicated, separate artifacts
subdirectories. Preserve private certificate state for renewal.

Supplied certificates require --tls-cert and --tls-key together. A trusted
local reverse proxy can use --bind 127.0.0.1. HTTP redirects to HTTPS and serves
no data. The optional metrics listener is separate plain HTTP: use loopback or
a protected scrape network, never the public internet. Public /metrics is 404.
Caddy administration and configuration autosave are disabled.

Edit [workbench-access.service](../../deploy/workbench-access.service) for your
account, hostname, code/Python/Caddy paths, then install it:

~~~sh
sudo install -m 644 deploy/workbench-access.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now workbench-access
sudo systemctl status workbench-access
sudo journalctl -u workbench-access -n 100 --no-pager
~~~

The example requires the account and directories from step 1. It grants only
the privileged-port capability, restricts writes and uses KillMode=control-group
so stopping the supervisor stops its Caddy child too. It provisions no host,
DNS, backend, organization or secret.

### Optional centralized administrative logs

When the logging component is installed, the supervisor emits bounded structured
start/stop and administrative mutation events. The SQLite audit remains the
authoritative record. Events contain validated IDs/revisions and outcomes, never
passwords, hashes, request parameters or database values. Idempotent request
retries may repeat an operational event; they never repeat the audit mutation.

By default these operational events use stderr. For file collection, create a
dedicated writer directory and add a systemd override with these lines:

~~~ini
[Service]
Environment=GLAMDB_LOG_DIRECTORY=/srv/glamdb-observability/access
ReadWritePaths=/srv/glamdb-observability/access
~~~

Create that directory with mode 0700 owned by the service account before
restarting. Use a separate directory for each native/control-plane process;
concurrent rotation owners are rejected. Mount/register this source in Alloy
using the logging deployment guide. Never use a warehouse, UI, served-site or
generated runtime-state directory as the log sink. An explicitly requested sink
fails startup if its logging module/path is unavailable; without that optional
component and without a requested sink, authorization still operates normally.
The gateway does not feed raw Caddy request JSON to this structured collector.

## 5. Browser and SDK login

Open https://workbench.example.com and enter the organization's login/password
in the browser's standard HTTPS authentication dialog. Its selector lists only
its databases. Switching clears old results, forms, operations and pending
requests. Access-revision changes refresh the organization and clear stale
views without a page reload. An unavailable server stays unavailable.

GET /workbench/session returns version 2, organization {id,name,login} and
databases [{id,name}], without other organizations, URLs, tokens or hashes.
Data paths are /databases/ID/v0/…; foreign/unknown IDs both return 404.
Missing/wrong credentials return 401 JSON with a Basic challenge; backend
connection/TLS failure returns 502. Unscoped /v0 and HTTP admin are unavailable.

~~~python
import getpass
from glamdb_sdk import Client

password = getpass.getpass("Organization password: ")
with Client("https://workbench.example.com/databases/sales",
            password, username="analytics") as database:
    print(database.list_collections().items)
~~~

All SDK routes retain the prefix. Omit username for existing direct-server
Bearer authentication. Client TLS verification stays enabled.

Browsers cache Basic credentials. Disconnect clears app memory; Reload access
uses the current browser login. Close all windows of a dedicated private
session to discard it. Shared Basic authentication cannot provide an independent
per-person logout or session revocation.

Edit ui/config/theme.json to change runtime design without compilation.
Style studio previews/downloads changes; it cannot write server permissions
or publish the theme. Authenticated /workbench/config is polled live.

## 6. Online lifecycle commands

Run the CLI locally as the service account. Global --socket, --operation-id
and --expected-revision options precede the subcommand. To make the examples
shorter, define this shell function after changing to /opt/glamdb:

~~~sh
access() {
  python3 -m apps.workbench_access.admin \
    --socket /opt/glamdb/artifacts/workbench-access/state/operator.sock "$@"
}
access status
access list
~~~

The socket is mode 0600 in mode 0700 state; peer UID is checked where supported.
Never expose it through HTTP or mount it into organization containers.

~~~sh
# First generate a NEW hash file, as in step 2.
access organization create research --name "Research" --login research \
  --password-hash-file /var/lib/glamdb/access/research-password.hash
access organization update research --name "Research lab" --login research-lab
access organization disable research
access organization enable research

# Password reset needs no old password. Keep the new file stable for retries.
access organization password research --password-hash-file /var/lib/glamdb/access/research-new.hash

# Register more servers with database create, using the same options as step 3.
access database transfer sales --from analytics --to research
access database update sales --disable
access database update sales --enable --name "Sales archive"
access database detach sales --expected-owner research
access database assign sales --organization analytics
access database delete sales --expected-owner analytics

# Empty organizations need no policy; nonempty ones require one:
access organization delete research --detach-databases
# Alternative: access organization delete research --transfer-to analytics
~~~

Changing login rejects the old login. Disabling preserves ownership and hashes.
Deleting/detaching removes access without touching backend data. Updating
token_env/token_file clears the previous credential source. Transfer accepts
--name to resolve a destination display-name collision atomically.

Each successful mutation validates a candidate, stops/drains old Caddy,
commits metadata and starts the new revision. Admin changes cause a short
gateway interruption; old gateway connections cannot survive success. A backend
write admitted earlier may still finish. For strict handover, disable the
mapping, quiesce/check backend writes, transfer while disabled, then enable.
This is an access transfer, not a physical data migration.

## 7. Revisions, safe retries, failures and batches

Generate and retain a UUID before important changes:

~~~sh
python3 -c 'import uuid; print(uuid.uuid4())'
access --operation-id UUID --expected-revision N database transfer sales --from analytics --to research
access receipt UUID
access audit --limit 100
access reconcile
~~~

Read N from status. Same UUID plus exactly the same command/hash file returns
the original receipt after restart or later transfers. Different content with
that UUID conflicts. A stale revision conflicts on first execution. A lost
response is not a reason to generate a different ID. An automatically generated
ID is convenient but cannot be recovered by the caller from a lost response.

Receipts distinguish metadata_committed and applied. applied=false exits 2:
metadata committed, but gateway startup failed. Inspect status/logs, fix
ports/TLS/secrets and reconcile. Do not roll back revoked permissions to recover
availability. Receipts are historical; gateway_running/applied_revision show
current runtime. Reconcile fences old routes and reopens authoritative SQLite
before activation. Invalid state remains closed.

Offline edits require both supervisor and child to be stopped. Their shared
lease prevents concurrent catalog ownership; an orphan child inherits the
lease. Use the service manager, never delete locks or hand-edit SQLite.
Listener leases under the account's registered home directory also exclude
different catalogs, state directories and checkout copies from the same TCP
ports. The Caddy child inherits them. Do not isolate or override this directory
between services that share a host network namespace. Occupied unrelated
listeners are rejected before startup; readiness checks a process identity
as well as the catalog revision.

For a batch, write a mode 0600 JSON array and run
access --operation-id UUID batch --file /private/path/changes.json:

~~~json
[
  {"action":"organization.update","params":{"id":"research","enabled":false}},
  {"action":"database.transfer","params":{"id":"sales","from":"analytics","to":"research"}}
]
~~~

All commands validate/commit together and each produces an audit row. No nested
batches. Audit contains time, UID, operation ID, revision, action, target and
safe ownership/enable/delete details, never hashes or backend credentials.
Receipt/audit history does not expire: monitor disk usage and back it up.

## 8. Private backups and recovery

~~~sh
access backup --output /var/lib/glamdb/access/backups/access-2026-11-10.sqlite
~~~

The SQLite backup API creates a consistent verified copy with hashes, receipts
and audit, mode 0600, file/directory sync. It never overwrites output or writes
inside the runtime/UI/site trees. Encrypt off-host copies; back up database
data separately. Preserve Caddy private certificate state through host backups.

To restore, stop the ENTIRE service; retain failed files/journal for diagnosis.
Do not truncate or delete a SQLite journal. Restore to a NEW absolute path:

~~~sh
sudo systemctl stop workbench-access
python3 -m apps.workbench_access.admin restore \
  --backup /var/lib/glamdb/access/backups/access-2026-11-10.sqlite \
  --catalog /var/lib/glamdb/access/restored.sqlite
python3 -m apps.workbench_access.admin --catalog /var/lib/glamdb/access/restored.sqlite list
~~~

An old backup can reinstate old passwords/ownership. Review it, disable stale
access, rotate affected passwords/tokens and reconcile backend outcomes before
returning service. Point the stopped unit's --catalog to restored.sqlite,
run --check, then restart. Restore never replaces live state or merges histories.
There is no distributed authorization catalog or automatic failover.

## 9. Test and publish

~~~sh
bash scripts/bootstrap-workbench.sh
npm --prefix ui exec -- playwright install chromium
python3 scripts/fetch-demo-caddy.py
bash scripts/check-workbench.sh
bash scripts/check-protected-demo.sh
~~~

The access suite uses real Caddy, trusted disposable HTTPS and Chromium. It
checks direct/foreign paths, writes/Arrow, password/login changes, transfer,
backup/restore, concurrent retry, restart/orphan leases, Unicode SDK passwords
and UI revocation with delayed data and an open dialog. Missing Caddy fails,
not skips. Auth traces/HAR/video are disabled. These tests do not certify the
native DB or internet hosting.

On the real host check anonymous/wrong-password rejection; two organizations
with distinct data; direct foreign URLs; password reset; disable/enable;
transfer; backend outage; service restart; receipt and backup lookup. Use a
disposable real database for write/Arrow checks. Verify trusted TLS, secret
permissions, firewall and available disk before sharing credentials.

When replacing an anonymous Pages demo, first verify this host, then retire
the old publication in repository Settings → Pages. Merging this code cannot
put authentication in front of an existing Pages URL.
