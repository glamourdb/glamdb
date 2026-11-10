# Organization access validation

Verified on macOS arm64 with Python 3.13, Caddy 2.11.9, OpenSSL and
Playwright 1.56.1 / Chromium 1194. Test databases are separate explicitly
disposable in-memory adapters with distinct bearer credentials. The production
launcher does not start them and requires real server configuration.

## Checks

- `bash scripts/check-workbench.sh`: 485 Python tests, 45 Node tests and 31
  Chromium scenarios passed. Includes independent codecs/oracles, real SDK
  HTTP/HTTPS, Arrow, packaging, session isolation and responsive UI/theme/logo
  checks. UI formatting passed.
- `python -m pytest sdk/python/tests -q`: 276 passed; includes optional Basic
  credentials, UTF-8 passwords, TLS/loopback validation and every SDK route
  with both database-prefix slash forms. Bearer behavior remains covered.
- `bash scripts/check-protected-demo.sh`: 178 passed with the optional logging
  component enabled and real HTTPS/Caddy and
  browser checks, covering legacy shared-password mode, static group compatibility
  and persistent organizations. Chromium runs 3 legacy, 6 static-group and 3
  organization scenarios. The final 37-case catalog/supervisor gate also passed
  independently. The actual supervisor-to-JSONL check validates lifecycle,
  administrative receipts and secret absence; it explicitly skips when the
  independent optional logging component is not installed. Use
  `GLAMDB_LOGGING_SOURCE_ROOT=/path/to/component-checkout` for an explicit
  component source during integration, or install it in the combined checkout.
- `git diff --check` and Python compilation passed.

After merging current main's split CI and CLI11 parsing, the workbench suites
were rerun: 485 Python, 45 Node and 31 Chromium tests passed. The standalone
HTTPS suite remains a dedicated browser-job gate rather than running twice in
repository-wide Python discovery. Workflow syntax/security lint passed.

The organization browser tests use fresh Pink/Blue/Empty contexts. They check
only owned databases, no token/server prompt or browser storage, no HTTP
administration, transfer while a scan response is held and an append dialog is
open, stale-result rejection, automatic old-view clearing, disable/re-enable
without reload, and no page errors. No production credential or chat is used.

## Regression and failure evidence

Tests failed before the corresponding fixes and passed afterward for malformed
text coerced by SQLite, catalog placement in generated runtime output, empty
organization wording, reconciliation that reused an old connection, packaging
failure reporting and consistent private backup creation. A delayed-response
test harness was corrected to capture identity before disconnect; removing the
actual stale-session guard still made that regression fail.

Actual Caddy allowed two independent runtimes to reuse listeners. A retained
connection proved revision 7 remained reachable after a second runtime reported
revision 8. Shared persistent listener leases, inherited child descriptors,
occupied-listener probes and process-specific readiness were added. Repeating
that actual-process probe rejected the second start, created no second child
and kept the original connection on its sole valid runtime. An orphan-process
test also proves catalog ownership remains locked until the child exits.

Additional checks cover atomic rollback/batches, conflicting revisions,
32 concurrent retries of one operation with exactly one commit, restart and
receipt replay, safe audit transfer direction, no-overwrite/symlink backup
outputs, CLI restore into a new file, online backup and actual CLI-generated
UTF-8 password authentication through the SDK. Metrics use a dedicated listener;
the actual Caddy histogram labels are server=workbench, handler=subroute and
unknown methods become OTHER rather than unbounded label values.

## Limits

No public host, DNS or production TLS certificate was provisioned. The supplied
systemd unit was reviewed as an example, not started on a Linux production host.
No live organization/database was removed or moved. No user warehouse was
opened, no power-loss/faulting-filesystem qualification was performed and no
native HTTP/scanner integration or engine durability claim follows from these
gateway checks. Browser polling cannot retract data already downloaded, and
requests already admitted by a backend can complete after an access change.
