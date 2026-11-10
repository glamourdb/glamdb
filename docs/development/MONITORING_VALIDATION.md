# Monitoring validation

Executed for the library/client and standard-collector configuration on
2026-11-11. Native component environment: Darwin arm64, AppleClang 21.0.0,
Python 3.14.3; formatting uses clang-format 18.1.8. These are current replacement
checks, not the earlier private collector/textfile tests. There is no claim of
full native engine durability, production HTTP/scanner integration, or hosted CI
success from these component results.

## Executed PASS

With prometheus-cpp 1.3.0 installed from its exact submodule (upstream pinned
CivetWeb, pull enabled; push/tests/compression/TLS disabled), prometheus_client
0.26.0, Prometheus/promtool 3.13.5 and Helm 3.19.1:

```sh
.venv/bin/python tools/monitoring/check.py \
  --promtool artifacts/monitoring/prometheus-3.13.5.darwin-arm64/promtool \
  --helm artifacts/monitoring/bin/helm \
  --chart artifacts/monitoring/kube-prometheus-stack-92.7.0.tgz
```

- Seven focused Python contracts: concurrent finite HTTP counters, histogram
  count/sum/boundary, fresh-registry reset, default readiness/in-flight release,
  library ASGI response, actual library HTTP socket and thread/listener teardown,
  truthful logger snapshot families, pins and absence of synthetic source values.
  These are seven test methods; several assertions share a method.
- Native component compiles with strict warnings and ASan/UBSan. Eight threads
  record 80,000 committed outcomes; exact count=80,000, sum=800 seconds and the
  inclusive 0.01-second bucket are checked. Tests also cover incomplete scopes,
  unknown-error bounding, admission ownership, phase/error pre-registration and
  scope gauge release. A second fresh process proves reset. The fixture exports
  exactly **714 domain series**, about **87.2 KiB**, before optional log metrics.
- Actual prometheus-cpp Exposer serves the domain registry over a localhost TCP
  socket. `promtool check metrics` accepts it. Destruction removes the listener;
  no publisher/snapshot file is used. It is a server-application fixture, not a
  production native database HTTP adapter.
- Both static Prometheus configurations pass `promtool check config`.
- All **13** application/PVC recording rules pass parsing. Four PromQL scenarios
  cover gateway reset/denominators, native endpoint-down suppression, absent
  sources and actual PVC median aggregation. All **33** application/upstream-node
  dashboard expressions parse after normal Grafana variable substitution.
- The exact official kube-prometheus-stack **92.7.0** archive matches locked
  SHA-256 `af35d3e93750ce46d90cbb6537483c8fa914f19fbffa19fbabcb2825181a0f1a`.
  Helm strict lint passes and rendering with Kubernetes 1.36.0 capabilities
  succeeds. No rendered `insecureSkipVerify: true` remains. This does not prove
  a real cluster's certificates, RBAC, CRDs, CSI, scheduling or runtime support.
- The standalone native metrics target builds with
  `GLAMDB_IO_ONLY=ON GLAMDB_BUILD_METRICS=ON BUILD_TESTING=OFF` and the pinned
  Prometheus prefix. IO-only also builds with no metric dependency when the
  standalone option is off. Native formatting passes LLVM 18.1.8. The metric adapter also compiles with
  strict warnings against the finalized optional logging stats header; combined
  native logging lifecycle execution remains a separate integration check.
- Changed workflow passes actionlint 1.7.12 and zizmor 1.32.0 offline pedantic
  checks with no findings.
- Fourteen mocked Docker-runner tests pass, including explicit recursive
  Prometheus/CivetWeb prerequisite failures and isolated dependency volume
  arguments. They do not start a Docker daemon or certify a real engine build.

Ignored per-run artifacts contain the exact command output; they are not
installed product files. Reproduce the component gate instead of relying on a
historical test-count claim.

## Component cost

The same one-million-call C++ fixture ran before and after replacement with
`-O2`, eight-thread comparison and no database/storage work. Original branch
head `b3f95b3ad074534f6120a372e1191965ce701e24` used the private atomic histogram:
731.477 ns/call single-thread, 0.370760 s for the eight-thread million calls,
211 observed series. The library implementation measured 1587.34 ns/call and
0.649090 s, with all 714 domain series pre-registered. This is approximately
856 ns more per instrumented fixture call; it is not a full-request or database
throughput result. No cache or revocation guarantee was changed for the result.

The Python observation fixture recorded 100,000 calls in 0.355255 s, approximately
3.55 microseconds each. Its full finite registry has **1,850 samples** including
the library's created timestamps, approximately 175.7 KiB of exposition and
515,837 retained bytes measured with tracemalloc during construction. These are
component measurements on this host, not a production memory/latency budget.
Request volume does not add metric identities.

## Standard collector runtime checks

`tools/monitoring/container_smoke.py` uses only an explicitly selected disposable
Linux VM/connection, a unique `glamdb-oss-monitor-*` network/container prefix,
CPU/memory limits and temporary in-memory Prometheus/Grafana data. It neither
starts/stops a VM nor removes any existing volume. Source comparisons run from
a separate probe container so the probe process does not inflate workload
`pids.current` or memory accounting.

The runtime fixture uses Fedora CoreOS 43, Linux 6.17.7 aarch64, cgroup v2,
4 CPUs / 4096 MiB, and an existing checkout share. Podman Docker-compatible API
detection in cAdvisor 0.60.6 expects Docker layerdb paths that Podman does not
provide; the fixture disables that Docker adapter while keeping the upstream
Podman factory and raw cgroups available. It selects the actual Podman child
scope (including its trailing-slash exporter ID) and compares the matching
Linux cgroup path. A real Docker or Kubernetes/containerd deployment still needs its own runtime
qualification. macOS-to-VM published-loopback forwarding reset connections in
this environment, so collector correctness is queried via actual HTTP over the
isolated Linux bridge. That does not qualify external browser/host forwarding.

Executed result: **PASS with documented source gaps** using:

```sh
.venv/bin/python tools/monitoring/container_smoke.py \
  --connection DEDICATED_ROOTFUL_CONNECTION --machine DEDICATED_VM \
  --vm-root /workspace
```

- Charged memory matched `memory.current` exactly: **73,666,560 bytes**. CPU
  usage matched **0.105229 seconds**, and cAdvisor reported **4 threads** against
  actual `pids.current=4`. Host total memory matched **4,079,595,520 bytes**;
  AnonPages/Cached/Unevictable comparisons use explicit live-gauge tolerances.
- The pinned unified-v2 handler's source-proven mappings were exercised with a
  64 MiB anonymous allocator and an 8 MiB file/cache control: RSS-named series
  **73,003,008 bytes** equals actual `memory.stat anon`; cache **8,556,544 bytes**
  equals actual `file`. This is a qualified version/hierarchy mapping, not a
  generic RSS rename. A targeted file fadvise/mmap control produced **one major
  fault**, matching the real counter; it did not evict host-wide cache.
- A bounded CPU loop under 0.5-core quota produced **0.983276 seconds** of CFS
  throttling, exactly matching `throttled_usec / 1e6`. The optional burst field
  existed and matched zero; nonzero burst behavior remains unqualified.
- Actual Grafana **13.3.0** provisioned the upstream node dashboard UID
  `7d57716318ee0dddbac5a7f451fb7753` and `glamdb-service`. Its authenticated
  datasource proxy queried the real Prometheus/node producer. Startup measured
  **2.268 seconds**, with a 512 MiB / 1 CPU test cap. Bundled plugins were used;
  unpinned preinstallation and automatic updates were disabled.
- Removing the workload removed its raw/fresh resource series while cAdvisor
  stayed `up=1`. The actual observation-age query used a 30-second bound.
  Before enabling standard `track_timestamps_staleness`, the real test exposed
  cached explicitly timestamped values lasting over 90 seconds. `container_last_seen`
  in the pinned source uses scrape time and is not the observation-age guard.
- Stopping node_exporter yielded `up=0` and removed old node memory values;
  restarting it restored `up=1`. Every uniquely named test container and its
  network were independently confirmed absent after cleanup. No preexisting
  volume or unrelated VM was removed.

The ignored result is `artifacts/monitoring/standard-collectors.json`.
[MONITORING_COVERAGE](MONITORING_COVERAGE.md) states remaining scope/kernel
requirements. These Linux checks do not certify Kubernetes or a database.

## NOTRUN / operator prerequisites

- Complete engine build and installed `glamdb::core` consumer: this checkout's
  current dependency prefix has no SQLite3 package (configure stops at
  `find_package(SQLite3 CONFIG)`) and no installed Arrow/Iceberg stack. The
  package config now discovers exact prometheus-cpp 1.3.0 so static consumers can
  resolve the private metric library. Build the complete locked prefix, install
  the engine and run the existing installed-consumer CTest before release.
- Combined optional native Quill/log metric lifecycle, Linux/GCC/TSan/MSan and
  full clang-tidy execution were not run. The compile-only stats compatibility
  check does not establish runtime teardown or sanitizer coverage.
- Production native HTTP/backend/scanner requests, authoritative native
  readiness/current-unknown state and full gateway/native comparative load.
  The current native transport integration is absent; a mock is not evidence.
- Actual Kubernetes server dry-run/deployment, CSI PVC fill/free quantiles,
  kubelet certificate/RBAC/runtime checks and Kubernetes controller outages.
  Kubernetes is optional and was not introduced solely for a test.
- Controlled network-namespace byte/TCP-established comparisons and block-device
  byte/IOPS/service-time comparisons were not executed. Their table entries are
  producer/unit contracts, not runtime PASS claims.
- Nonzero burst, scheduler unconstrained-wait separation, mlock unevictable
  memory, packet-loss retransmits and directional
  wall-busy percentages are not certified by the allocator fixture. Exact
  unsupported fields remain No data; directional service time is only a proxy.
- arianvp/cgroup-exporter 0.3.3 production adoption/load/path/churn qualification
  and parent-scope support: intentionally not enabled. Its source/license/release
  review and explicit gap decision are in the coverage matrix.
- The Docker Compose CLI was unavailable on this host, so its official config
  parser was not run. Helm and promtool parsing do not substitute for it.
- Hosted workflow, full environment load and operator retention/firewall/secret
  provisioning remain deployment checks. Helm render/component smoke results
  must not be reported as those checks.
