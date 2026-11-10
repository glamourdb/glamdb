# Operational logging

The CLI now emits structured operational events to stderr. Set
`GLAMDB_LOG_DIRECTORY` to an existing absolute directory outside the warehouse
(or a new leaf directory under an existing parent) to select private, rotated
JSONL files instead. The internal logger is independent of the storage engine;
no log acknowledgment authorizes a committed storage response. Parquet field
metrics describe user-data files and are not operational logs.

`deploy/logging` provides a single-node Loki store, an Alloy file collector and
an optional standalone Grafana search UI. The Python adapter in
`tools/logging/glamdb_logging.py` supports control-plane lifecycle, request and
organization/database change events. Its caller must wire it explicitly; the
adapter does not install middleware or alter storage writers. These logs are
best effort. Authoritative administrative audit and storage outcomes belong in
their transactional journals.

## Choice and source review

Reviewed against upstream documentation on **2026-11-10**:

- **Loki + Alloy + Grafana, selected.** Loki indexes stream labels and searches
  log content with LogQL. The existing metrics UI can search logs without a
  second account/UI system. Low-cardinality streams and bounded query windows
  suit sampled application events. Wide content searches still cost work:
  adding a request ID as an indexed label is not a shortcut. See
  [Loki labels](https://grafana.com/docs/loki/latest/get-started/labels/) and
  [TSDB storage](https://grafana.com/docs/loki/latest/operations/storage/tsdb/).
  Alloy tails files and forwards batches. Its optional remote-write WAL and
  custom queue settings are experimental in this release; this configuration
  uses stable components without that WAL. See
  [Alloy file source](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.file/)
  and [write client](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.write/).
- **VictoriaLogs.** A real compact alternative with a single executable,
  built-in UI, field/full-text search using LogsQL and a Grafana plugin. It is
  attractive when starting a separate log service; adopting it here adds a
  different query language and datasource plugin. No relative throughput or
  memory claim has been benchmarked in this repository. See
  [VictoriaLogs](https://docs.victoriametrics.com/victorialogs/),
  [LogsQL](https://docs.victoriametrics.com/victorialogs/logsql/) and
  [Grafana integration](https://docs.victoriametrics.com/victorialogs/integrations/grafana/).
- **OpenSearch + Data Prepper + Dashboards.** Supports indexed document search,
  transformations and PPL analytics. The upstream log-analytics deployment uses
  a collector, Data Prepper, OpenSearch and Dashboards; those services add
  operational components for this small deployment. Prefer it when document
  indexing and its security/search ecosystem are requirements. See the
  [official ingestion walkthrough](https://docs.opensearch.org/latest/observing-your-data/log-ingestion/)
  and [Data Prepper](https://docs.opensearch.org/latest/data-prepper/).

**Promtail reached EOL on March 2, 2026**; it receives no future updates.
New deployments use Alloy. See the
[upstream EOL notice](https://grafana.com/docs/loki/latest/send-data/promtail/).
Images are versioned and digest-pinned in `versions.lock.json`; Loki 3.7.9 and
Alloy 1.21.3 were published October 20 and October 28, 2026 respectively:
[Loki release](https://github.com/grafana/loki/releases/tag/v3.7.9),
[Alloy release](https://github.com/grafana/alloy/releases/tag/v1.21.3).
Review release notes and security fixes when updating all matching pins.

## Producer contract and limits

Native API: `NGlamDB::NTelemetry::TStructuredLogger::Open`, `NewRequestID`,
`TryLog`, `GetStats`, `Shutdown`, in `core/telemetry/structured_log.h`.
Creation/opening the logger may allocate and perform IO. `TryLog` copies a fixed
record into a preallocated queue using a single mutex try-lock: it never waits
for that mutex, allocates, writes files or contacts the network. Every failure
to admit a record is counted. A single worker formats and writes events.

Default limits:

- 8,192 queued native records plus at most 64 worker records; about 0.5 MiB of
  record storage. Sixty-four queue slots are reserved from successful requests.
  Python uses 4,096 entries and small bounded critical sections; it is intended
  for the control plane, not native data-path throughput.
- Successful `request_completed` events: one candidate per 100 successes,
  **at most 20 candidates per monotonic second per logger**, before queue
  admission. This is fixed sampling, not a statistically unbiased estimator.
  An atomic admission race can conservatively reject another candidate.
- Errors, denied/unknown outcomes, lifecycle and administrative changes bypass
  success sampling/rate limiting. All remain subject to bounded admission and
  sink failures; an error storm cannot allocate an unlimited queue.
- Four files including the active `glamdb.log`, each at most 16 MiB, totaling
  **64 MiB**. Rotation renames files; there is no compression subprocess or
  copy-truncate race. Opening a smaller budget removes excess/oversized
  archives and truncates an oversized active log. Changing budgets deliberately
  reduces retained history. A nonblocking directory lock enforces one rotation
  owner per directory; concurrent owners fail setup.
- File mode 0600; directory owned by the writer and not group/other writable.
  Active files and archives must be owned, single-link regular files. Symlinks
  and special files fail setup. Parent directories must be trusted. stderr has
  no in-process file retention; configure container/journal retention there.

Schema 1 has a UTC timestamp, fixed service, finite event/operation/outcome,
level, generated 32-hex request ID, duration in microseconds, row count and
transport status. The native API has no free-text field. Python additionally
allows a nonnegative revision and explicitly validated organization/database
IDs (lowercase slugs up to 64 characters, or lowercase UUIDs). Numeric fields
are unsigned native values or Python integers bounded at `2**63-1`.

Do not copy a supplied request-ID/header into these fields: generate an ID at
the request boundary, return it to the caller and reuse it for related events.
Never put passwords, API tokens, Authorization, Cookie, bodies, raw URLs/query
values, exception messages, user data, SQL text, filesystem paths or identifiers
outside the defined schema into logs. Python projects an allowlist instead of
redacting arbitrary text. Alloy reconstructs that allowlist again, dropping
extra fields before forwarding. Records without schema 1 or a generated-format
request ID are rejected and counted as `invalid_schema`. Invalid enum values are replaced; invalid IDs
are removed/replaced. This protects the stream only when producers use the
contract; encoded secrets deliberately placed in permitted numeric/ID fields
cannot be detected reliably.

Only `service`, `environment`, `event`, `level` are configured stream labels;
Loki may add its bounded `service_name`/detected-level metadata. IDs, filenames,
queries and paths are never indexed labels. For multiple nodes, add authoritative
cluster/namespace/pod/container/node labels in deployment discovery, and review
stream budgets before doing so. Do not derive them from client input.

`logger_health` is emitted every five seconds and on clean shutdown, with
submitted/accepted/sampled/rate-limited/written counts, each drop reason,
sink errors, backlog and capacity. Synthetic health records are excluded from
application `accepted`/`written` totals. `GetProcessLogStats()` retains actual
process counters after logger destruction;
its backlog/capacity gauges describe live loggers. The monitoring publisher can
consume this internal API. Python `render_prometheus(logger.snapshot())` formats
the same bounded metric names for the supervisor endpoint; this library does not add an HTTP server.
Native backlog includes the fixed worker batch. Snapshots during concurrent
activity are approximate; terminal accounting is checked after shutdown.

On full/contended queues the producer returns false immediately. On sink failure
records are dropped, counters increase and the worker backs off for 100 ms;
there is no request-thread retry or fallback to warehouse IO. SIGPIPE on the
worker becomes a sink error. OS writes can still stall the worker and shutdown;
stop producers before destruction. Process crashes can lose queued/unflushed
logs. Operational files are not fsynced and are not a durable audit journal.
CLI stdout JSON and the storage write/publication protocol are unchanged.
Legacy human CLI diagnostics on stderr are not collector input; file mode is
recommended for central collection.

Python integration:

```python
from glamdb_logging import StructuredLogger

logger = StructuredLogger(directory="/srv/glamdb-observability/application")
request_id = logger.new_request_id()
logger.emit("request_completed", operation="auth", outcome="denied",
            request_id=request_id, status_code=403, duration_us=500)
logger.emit("org_updated", operation="organization", outcome="ok",
            revision=12, org_id="example-team", database_id="example-db")
# On process shutdown after stopping producers:
logger.close()
```

Make `tools/logging` importable in the supervisor's packaging or explicitly load
that module. Do not log raw Caddy access JSON into this file: it needs a trusted
normalizer to the schema above, with success sampling and field projection.

## Linux deployment and external setup

Assumptions: a Linux Docker host with Compose v2, a dedicated filesystem/quota
for logging, one application writer and an operator-only Grafana. No cloud
account, DNS name, certificate, secret or remote host is provisioned by Compose.

1. Install Docker Engine/Compose using the distribution's supported packages.
   Reserve log storage **separate from the warehouse**. A suggested starting
   budget is a 20 GiB dedicated filesystem for Loki and a separately bounded
   filesystem for application/collector/Grafana state. Apply a real filesystem
   or project quota; Compose bind mounts do not enforce byte quotas. Verify with
   `findmnt -T /srv/glamdb-observability/loki` and your filesystem's quota tool.
   Do not enable the stack on an unbounded shared warehouse filesystem.
2. Create directories with the actual application UID/GID; the example below
   uses 10001. Loki uses 10001, Grafana uses 472. These are new directories;
   inspect existing paths before changing ownership.

   ```sh
   sudo install -d -m 0700 -o 10001 -g 10001 /srv/glamdb-observability/application
   sudo install -d -m 0700 -o 10001 -g 10001 /srv/glamdb-observability/alloy
   sudo install -d -m 0700 -o 10001 -g 10001 /srv/glamdb-observability/loki
   export GLAMDB_LOG_UID=10001 GLAMDB_LOG_GID=10001
   export GLAMDB_LOG_DIRECTORY=/srv/glamdb-observability/application
   export GLAMDB_ALLOY_STATE_DIR=/srv/glamdb-observability/alloy
   export GLAMDB_LOKI_DATA_DIR=/srv/glamdb-observability/loki
   export GLAMDB_ENVIRONMENT=production
   docker network inspect glamdb-observability >/dev/null 2>&1 || docker network create glamdb-observability
   docker compose -f deploy/logging/compose.yaml config --quiet
   docker compose -f deploy/logging/compose.yaml up -d
   ```

3. Set the same `GLAMDB_LOG_DIRECTORY` in the application service environment.
   Under a container, mount the corresponding host directory writable into the
   application and set the variable to its **container** path. Mount only that
   log directory read-only into Alloy; never mount the warehouse or Docker socket.
   Alloy runs as the same UID as the writer to read 0600 files. Preserve its
   state directory across upgrades. Positions corruption restarts reading at
   the beginning, so duplicates are possible. Only the active filename is
   discovered; rapid rotation or long collector downtime can lose archives.
4. If the monitoring stack supplies Grafana, connect it to
   `glamdb-observability` and provision `grafana-datasource.yaml` (UID `loki`, URL
   `http://loki:3100`). The monitoring stack already uses that UID. Otherwise
   use the **optional** standalone UI below. Run one Grafana stack per port.

   ```sh
   sudo install -d -m 0700 -o 472 -g 472 /srv/glamdb-observability/grafana
   export GLAMDB_GRAFANA_DATA_DIR=/srv/glamdb-observability/grafana
   export GLAMDB_GRAFANA_PASSWORD_FILE=/srv/glamdb-secrets/grafana-admin-password
   # Provision a strong password through your secret manager into this file.
   # The container must be able to read it: owner UID472, mode0400.
   docker compose -f deploy/logging/compose.yaml -f deploy/logging/compose.grafana.yaml config --quiet
   docker compose -f deploy/logging/compose.yaml -f deploy/logging/compose.grafana.yaml up -d
   ```

   Open `http://127.0.0.1:3000`, sign in as `operator` with the provisioned
   password, open Explore and select **Loki**. Existing Grafana data keeps its
   existing administrator credentials; replacing the file does not reset them.
   Anonymous access and signup are disabled. For remote administration, use a
   secured SSH tunnel or an authenticated TLS reverse proxy with firewall rules;
   no credentials belong in datasource URLs, source files or browser storage.
5. Verify `curl --fail http://127.0.0.1:3100/ready` and
   `curl --fail http://127.0.0.1:12345/-/ready`. Run a disposable CLI command with
   file logging enabled, then use the queries below. For an automated synthetic
   fixture, run `python3 tools/logging/smoke.py --log-directory "$GLAMDB_LOG_DIRECTORY"`
   as the log owner. It writes fake secret strings to test collector projection;
   never substitute real credentials.

The Loki API has authentication disabled **inside the private network**; loopback
ports 3100/12345 and Grafana 3000 are the only host bindings. Network membership
is an operator trust boundary, not tenant authorization. Do not expose Loki or
Alloy directly on a public interface, or give application users network access.
For remote collectors, place Loki behind an authenticated TLS/mTLS gateway,
restrict push/query paths separately, provision client credentials via mounted
secret files and configure certificate validation. Grafana login alone does not
protect a separately exposed Loki API. Central logs may span organizations;
operator access must be limited independently of application data permissions.

## Retention, capacity and failure policy

Loki uses TSDB v13, 24-hour index periods, persistent chunks/WAL and Compactor
retention of **168 hours**. Deletion is asynchronous: the 10-minute compaction
cycle and two-hour delete delay mean retention is not an exact deletion deadline.
Compactor state must persist. See
[upstream retention](https://grafana.com/docs/loki/latest/operations/storage/retention/).
The filesystem store has no disk-space eviction policy; a quota and disk alerts
are mandatory. See
[filesystem storage limits](https://grafana.com/docs/loki/latest/operations/storage/filesystem/).

Loki caps ingestion at 1 MiB/s with a 2 MiB burst, 256 KiB/s per stream, 1,000
streams and 4 KiB lines; query windows are at most 24 hours/1,000 returned entries.
These are protection settings, not demonstrated capacity guarantees. In a
sizing example of 600 bytes/event, 20 successful events/s produce about 7.3 GB raw in
seven days, before errors, metadata, indexes/WAL, compression and replicas. Size
from measured event bytes and actual error rate. Retention does not guarantee
seven days of availability if admission/quota limits are exceeded.

Single-node filesystem Loki has no HA. Use an object store, authenticated
endpoints, replicated Loki topology and measured capacity for larger production
workloads. Apply object-store lifecycle only with the documented chunk-prefix
rules and a longer expiry than Compactor retention/delete delay; do not blindly
expire indexes or cluster metadata.

The shipped WAL guard rejects new writes above **90% filesystem use**, checks
the whole WAL filesystem, and resumes below the threshold. Preserve that guard:
`loki_ingester_wal_disk_usage_percent` is a **0..1 fraction** despite its name;
`loki_ingester_wal_disk_full_failures_total` records threshold/full-write failures.
See [WAL behavior](https://grafana.com/docs/loki/latest/operations/storage/wal/).
A full log volume must not fill the warehouse volume.

Alloy batches 256 KiB/one second, times out requests after five seconds and uses
six bounded retries with 0.5..5-second backoff. Exhausted batches are dropped and
counted. No collector WAL is enabled; local files and positions are not an
end-to-end durable queue, and collector restarts can lose in-flight batches.
Container memory limits are 256 MiB Alloy, 1 GiB Loki and 512 MiB optional Grafana;
CPU/PID limits and bounded Docker logs are configured too. Native stderr stalls
can delay shutdown; application record admission stays bounded.

Watch native/Python drop/sink-error/backlog counters, Alloy
`loki_write_dropped_entries_total`, `loki_write_batch_retries_total` and sent-entry
progress, Loki `loki_discarded_samples_total`, WAL usage/failures, container memory,
filesystem free space, Compactor retention operations and scrape/readiness state.
Native counters require host metrics integration; health logs alone cannot signal
a stuck sink. Do not derive exact request rates from sampled logs. During outages
restore connectivity/free capacity, verify new events are searchable and inspect
drop counters; do not replay old archives into live streams without accounting
for duplicates, timestamp limits and retention.

## Search and checks

Use a narrow time range in Grafana Explore, then expand it only when needed:

```logql
{service="glamdb", environment="production", level="error"} | json
{service="glamdb"} | json | request_id="0123456789abcdef0123456789abcdef"
{service="glamdb", event="request_completed"} | json | operation="append" | duration_us > 100000
{service="glamdb", event="logger_health"} | json | dropped_full > 0
{service="glamdb", event="org_updated"} | json | org_id="example-team"
```

Local independent verification (no Arrow/Iceberg required):

```sh
python3 -m unittest discover -s tests/logging -v
cmake -S . -B build/logging -DGLAMDB_IO_ONLY=ON -DBUILD_TESTING=ON
cmake --build build/logging --parallel 2
ctest --test-dir build/logging --output-on-failure
```

For real collector/storage checks, download Loki 3.7.9 and Alloy 1.21.3 for your
platform from the release links above, verify their published SHA256SUMS and
extract into ignored `artifacts/logging-tools`. Then run:

```sh
python3 tools/logging/run-local-e2e.py \
  --loki-binary artifacts/logging-tools/loki-darwin-arm64 \
  --alloy-binary artifacts/logging-tools/alloy-darwin-arm64 \
  --probe build/logging/tests/glamdb_logging_probe --exercise-outage
```

The harness owns only its child processes, uses unused loopback ports and a
fresh temporary data directory, terminates its children and records results,
service logs and raw metrics in `artifacts/logging-e2e`. The outage option stops
its own Loki, checks counted retry exhaustion, restarts it and checks new logs.
It never manages a pre-existing VM/service. On a host already above 90% disk use,
a **test-only** `--wal-disk-threshold 0.99` can exercise the pipeline with a tiny
fixture; it does not change deployment configuration and should not be used for
normal operation. See [recorded validation](LOGGING_VALIDATION.md) for executed
results, environment and remaining limits.
