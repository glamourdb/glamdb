# Logging validation — 2026-11-10

Executed on macOS arm64 with AppleClang 21, Python 3.14.3, CMake/Ninja,
clang-format/clang-tidy 18.1.8. This verifies operational logging independently
of storage durability qualification.

## Executed checks

- `python3 -m unittest discover -s tests/logging -v`: **15 PASS**. The suite
  builds the actual C++ producer when `GLAMDB_LOG_PROBE` is unset; CTest supplies
  its built probe. Coverage includes JSON/opaque correlation, error/lifecycle
  admission, sample and rate limits, eight concurrent producers, deliberately
  blocked stderr, sink errors, rotation, smaller-budget reopen, file permissions,
  symlink/FIFO rejection, exclusive directory ownership, secret-field projection
  and actual Prometheus snapshot values.
- `cmake -S . -B build/logging -DGLAMDB_IO_ONLY=ON -DBUILD_TESTING=ON`, build and
  `ctest --test-dir build/logging --output-on-failure`: **165/165 PASS**. The same
  configure/build with `GLAMDB_ENABLE_ASAN=ON` and `GLAMDB_ENABLE_UBSAN=ON` in
  `build/logging-sanitized`: **165/165 PASS**. Ninja must be on PATH for tooling
  tests that launch nested CMake. A first run lacked that PATH entry and failed
  `glamdb.tooling.cmake-configuration`; adding it resolved both nested cases.
- Strict `-std=c++23 -Wall -Wextra -Wpedantic -Werror` compilation of the CLI
  translation unit: **PASS**. CLI stdout logic is unchanged.
- LLVM 18 format check of the 68 IO-profile sources: **PASS**; focused LLVM 18
  tidy for logger, probe and CLI: **PASS**, no project diagnostics. The current
  macOS SDK's C++ library uses builtins unsupported by LLVM 18. For analysis only,
  the official libc++ 18.1.8 source headers were extracted under ignored
  `artifacts/logging-tools`, with their upstream `__config_site.in` configured
  for pthread/ABI1 and the upstream default assertion handler copied to its
  generated include name. `-nostdinc++ -isystem <those headers> -isysroot <SDK>`
  supplied a matching analysis library. No system toolchain/header was changed.
- Loki **3.7.9** config verification and Alloy **1.21.3** `validate`: **PASS**.
  Official Darwin arm64 archive SHA256s match `deploy/logging/versions.lock.json`.
- Official Compose **5.7.0** daemonless rendering of the base deployment and the
  optional Grafana variant: **PASS**. Both use mandatory path/UID settings;
  Grafana additionally requires a secret file. Resolved multi-platform registry
  manifest digests are pinned. Rendering does not start containers.
- `python3 -m py_compile tools/logging/*.py tests/logging/*.py` and
  `git diff --check`: **PASS**.

## Real ingestion, filtering and outage recovery

Executed the harness from [LOGGING.md](LOGGING.md) using the official native
Loki/Alloy binaries and the actual CMake-built logging probe, including
`--exercise-outage`. The host filesystem was approximately 93% full. Shipped
Loki correctly rejected writes at its **90%** WAL threshold, so the isolated
small-fixture harness used the explicit **test-only**
`--wal-disk-threshold 0.99` argument. Deployment configuration stays at 0.9.

Results:

1. Ten native append-error events were searchable in Loki with their literal
   `duration_us=123`, `row_count=7`, status 503 and generated request IDs.
2. A synthetic application event was ingested and queried by its generated
   correlation ID. Password, Authorization, Cookie, query, body, path, exception
   text and nested secret fields were absent from the stored JSON. Organization
   and database slugs, status, revision and duration survived projection.
3. A raw JSON record lacking schema 1 was discarded. Query results retained
   only configured stream labels plus Loki's bounded service metadata; query-time
   JSON extraction was not mistaken for indexed labels. Actual collector
   `loki_process_dropped_lines_total{reason="invalid_schema"}` increased.
4. Only the harness-owned Loki process was stopped. After finite retries,
   `loki_write_dropped_entries_total` reported **33** dropped entries. Restarting
   that Loki against its temporary data directory made new fixtures searchable.
   Both the initial search and the post-outage search returned **PASS**.
5. Raw service metrics were fetched, including actual sent/dropped/retry counts
   and `loki_ingester_wal_disk_usage_percent` of approximately **0.93**. Its units
   are 0..1, despite the name. All harness child processes were terminated.

The harness records `result.json`, service logs and raw `.prom` metrics under
ignored `artifacts/logging-e2e`. It creates fresh temporary log/Loki/collector
state and does not touch existing services, VMs, warehouses or user data.

## Admission microbenchmark

An optimized standalone probe exercised the logger only; no HTTP handling,
Arrow work, database operation, payload serialization or service-side storage
was performed. These are workload-specific admission measurements, **not**
application throughput claims or deployment capacity guarantees:

- Default policy, 10,000,000 successful request-log attempts: 9,900,000 sampled
  out, 99,980 rate-limited, 20 request events plus two lifecycle events admitted
  and written, zero terminal backlog. Elapsed about 0.162 seconds; about 6 KiB
  retained including the final health record. This demonstrates that the success
  rate cap remains finite when attempted log volume increases.
- Forced overload, eight threads × 100,000 error events with a 32-entry queue:
  800,002 total attempts including lifecycle, 1,423 written, 638,563 contended
  drops and 160,016 queue-full drops. Elapsed about 0.106 seconds; terminal
  accounting exactly balanced, zero backlog and about 363 KiB retained.
- The separate blocked-stderr test issues 500,000 events without draining the
  child's stderr pipe. The producer exits within its 10-second deadline, with
  at most 32 queued plus 64 worker records. It deliberately terminates the test
  process without waiting for a blocked OS write; normal graceful shutdown can
  still wait for an OS sink.

Timing and drop distributions vary with scheduling/hardware. Raw local output
is in ignored `artifacts/logging-native-benchmark.json`. Repeat with the probe's
`default` and `stress` modes against disposable directories when sizing a host.

## Not established by these checks

- This branch did not run the full Arrow/Iceberg engine build or full CLI
  integration suite; its dependency prefix was unavailable. The CLI translation
  unit compiled, and the independent IO/logger suites ran. No storage writer,
  publication, recovery ordering or public storage API was changed.
- Container execution of this Loki/Alloy/Grafana Compose deployment, Linux
  filesystem quotas/ownership and a remote authenticated TLS gateway were not
  exercised here. Native binaries prove ingestion/query/projection/outage
  behavior; Compose rendering proves configuration syntax. Complete the Linux
  operator setup and run `smoke.py` on the deployed stack before rollout.
- The optional Grafana service was not launched in this validation. Its secret
  and datasource mounts were rendered, and its image is pinned; verify login
  and Explore against the deployed Loki through the operator-only endpoint.
- Seven elapsed days of Compactor deletion, power-loss recovery, HA, production
  peak throughput and long-duration memory/disk behavior were not tested. The
  seven-day retention configuration was accepted; this is not proof of a
  deletion deadline. A hard storage quota and disk/readiness/drop alerts remain
  required deployment prerequisites.
