# Monitoring validation

The Monitoring checks workflow runs the reproducible component/rule/dashboard
gate and strict Kubernetes schema validation on Ubuntu, using locked upstream
validator checksums. Workflow syntax and security lint passed locally; its
hosted result is reported separately by the PR checks.

Executed on 2026-11-10. Host: Darwin arm64, 16 GiB RAM, AppleClang 21.0.0,
Python 3.14.3. Native component checks also used GCC 16.2 and LLVM 18.1.8
analysis/formatting. Runtime collection used a dedicated disposable Linux VM:
Fedora CoreOS 43, kernel 6.17.7 aarch64, cgroup v2, 2 CPUs / 1536 MiB RAM.
No database volume, existing container workload or production cluster was used.

## Executed PASS

`python3 tools/monitoring/check.py --promtool PATH_TO_VERIFIED_PROMTOOL`
with the locked Prometheus 3.13.5 executable:

- Ten Python tests: actual Caddy route-label contract, digest pins, generated
  dashboard references, Kubernetes source/privilege structure, cgroup units,
  missing-controller behavior, unlimited quotas, traversal/symlink rejection,
  bounded sample identities and pod-parent discovery.
- Both Prometheus configuration variants pass strict syntax validation.
- All 43 recording rules pass `promtool check rules`.
- Seven deterministic PromQL scenarios pass: pod-parent precedence and counter
  restart; aggregate gateway/status denominator; real PVC p100/p50/min semantics;
  shared-network namespace de-duplication / hostNetwork exclusion; actual Caddy
  2.11.9 captured label-set replay; idle versus absent traffic; bucket-weighted
  latency across unequal instance traffic. Quantile comparison uses an explicit
  1e-12 second tolerance for floating arithmetic.
- All 70 generated dashboard expressions pass PromQL parsing after replacing
  Grafana variable placeholders with valid regexes.
- The C++ native component builds with `-Wall -Wextra -Wpedantic -Werror` and
  AddressSanitizer/UndefinedBehaviorSanitizer. Eight threads add 80,000 known
  outcomes without lost increments; tests verify histogram boundaries,
  incomplete scopes, bounded error labels, admission gauges and atomic
  textfile publication. The actual exposition passes `promtool check metrics`:
  212 series / approximately 26.6 KiB for that fixture.

`cmake -S . -B build/monitoring-make -G 'Unix Makefiles'
-DGLAMDB_IO_ONLY=ON -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Debug`, then
`cmake --build build/monitoring-make --target glamdb_metrics --parallel 2`:
standalone metrics target builds without Arrow, Iceberg or SQLite.

The native component test also builds/runs with GCC 16 under strict warnings.
LLVM 18.1.8 `clang-format --dry-run --Werror` passes for changed native files
and probes. LLVM 18.1.8 `clang-tidy --warnings-as-errors=*` passes `metrics.cpp`
using compatible upstream libc++ 18 headers for analysis; the host SDK's newer
libc++ headers otherwise require builtins unavailable in Clang 18. This does
not change the installed compiler or product header configuration.

With the optional structured logger present, a separately compiled sanitized
component test opens the actual logger, submits an important event, shuts it
down and checks that process submitted/written counters advanced. The metrics
snapshot contains those actual counts; exposition passes promtool. CMake
conditionally links/exports that dependency when `glamdb_logging` exists;
the standalone monitoring build omits unavailable log series.

Both Compose files pass the official Docker Compose 5.7.0 parser:

```sh
docker-compose -f deploy/monitoring/compose.yml config --quiet
GLAMDB_NODE_LISTEN=127.0.0.1:19100 docker-compose \
  -f deploy/monitoring/compose.exporters.yml config --quiet
```

The second command validates configuration only; the loopback bind is not a
claim of reachability from a bridged Prometheus container. Real deployment
requires the private reachable address described in [MONITORING](MONITORING.md).

`python3 tools/monitoring/kubernetes.py --with-cadvisor` generates 27 resources.
Verified kubeconform 0.8.0 against Kubernetes 1.36.0 schemas:
**27 valid, 0 invalid, 0 errors, 0 skipped**. The generated Kubernetes Prometheus
configuration also passes promtool syntax validation.

## Real scrape and Grafana provisioning

The pinned Prometheus 3.13.5, Grafana 13.3.0 and Python OCI images were run in
an isolated network with disposable task containers, bounded CPU/memory and
temporary metrics storage. The operator can reproduce the suite with:

```sh
python3 tools/monitoring/container_smoke.py \
  --connection DEDICATED_PODMAN_CONNECTION \
  --machine DEDICATED_VM --vm-root /workspace
```

`/workspace` must already be a share of this checkout in that dedicated VM.
The test never starts a VM or changes the selected default connection.

Observed results:

- A real allocator container allocated 64 MiB and started three additional
  threads. Its Linux cgroup exported **73,003,008 anonymous bytes** and **4 kernel
  tasks**; every CPU/memory/pids/I/O/PSI collector succeeded.
- Prometheus scraped the exporter and evaluated its recordings: 55 workload
  resource/recording series in the fixture.
- Actual Grafana started with an authenticated admin account, provisioned all
  three dashboard UIDs (`glamdb-nodes`, `glamdb-pods`, `glamdb-service`) and
  retained the pod dashboard's 27 panels. Its Prometheus datasource proxy query
  returned the real cgroup anonymous-memory series.
- After stopping the allocator, resource values disappeared while exporter
  `up` remained 1. The test therefore exercises missing-workload behavior without
  treating exporter reachability as workload health.
- Test containers/network were removed and the dedicated VM was stopped.
  The preexisting default VM and default connection were left unchanged.

The allocator is a collection fixture, not the database. The runtime evidence
does not claim native readiness, a production backend, or storage performance.
The ignored local artifact is `artifacts/monitoring/container-smoke.json`.

## Bounded instrumentation measurement

`tools/monitoring/tests/native_metrics_bench.cpp`, built with AppleClang `-O3
-DNDEBUG`, runs two sets of one million metrics scopes (one serial and one
across eight threads), with validation/data/catalog timing and returned outcomes.
It performs **no database or storage operations**.

Measured on this host: serial 0.254068 seconds (254.068 ns/scope); eight threads
0.193201 seconds for one million total scopes. Series count remained **211
before and after**; final snapshot approximately 26.9 KiB. These are component
cost/cardinality observations, not an engine RPS or end-to-end latency result.
Benchmark source and ignored JSON artifact permit rerunning on deployment
hardware rather than extrapolating this host's result.

## Not established by these checks

- Full Arrow/Iceberg/SQLite engine build, coordinator suite and installed-consumer
  link verification require the installed dependency prefix, absent in this
  environment. Engine hook changes were source-reviewed and formatted; the
  independent metrics and optional logger integration were compiled/tested.
- Kubernetes server-side dry-run, actual scheduling, admission/taints, kubelet
  authorization, CSI volume stats, workload discovery across reschedules and
  optional privileged cAdvisor process/sched/TCP collection require a real
  reviewed cluster. Schema validation cannot prove them.
- Production Compose startup is distinct from the isolated direct-container
  runtime test. Host network/firewall, Docker runtime paths, permissions and
  exporter privileges must be checked on the deployment host.
- Grafana provisioning, datasource queries and expression parsing passed;
  a browser visual inspection of every populated panel is not recorded.
- No physical-hardware disk saturation, network retransmission workload,
  million-RPS database load, power loss or durability qualification was run.
  Linux has no exact generic unconstrained-wait or directional wall-busy split;
  the named proxy/NoData boundaries remain as documented.

Official Grafana Darwin download returned HTTP 451 in this environment; the
locked official OCI image supplied the actual runtime verification instead.


Follow-up configuration checks: the optional private `loki` discovery job and
empty `targets/loki.json` are present in both Compose Prometheus configurations,
and the native renderer accepts `--loki`. The job name matches Loki alert
selectors. The Linux quickstart installs a separate UID472/mode0400 Grafana
file-backed secret; the original caller-owned mode0600 file remains available
for operator login and Kubernetes secret creation. This corrects ownership
that the environment-password runtime smoke did not exercise. Promtool and
Compose syntax checks were rerun for these changes; host UID mapping still
requires operator validation on the deployment engine.

The isolated CMake tooling fixture now also copies `public/`, required by the
independent metrics module's typed outcome definitions. A combined IO build
exposed the previous missing-header failure; both nested CMake cases passed
after the fixture correction.
