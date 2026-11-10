# Monitoring

The stack collects real Linux host, container, Kubernetes and native coordinator
metrics. Three provisioned Grafana dashboards cover hardware, pods and the
API/coordinator. Missing targets and unsupported kernel fields stay **No data**.
Neither an exporter scrape nor a recent metrics snapshot establishes database
readiness, successful recovery or durable storage integration.

Production HTTP/core integration remains pending. Caddy metrics describe the
backend actually configured behind that gateway; a gateway pointing at the
disposable adapter does not measure a native storage writer.

## Components and versions

`deploy/monitoring/versions.lock.json` pins multi-architecture OCI manifests by
digest, not only mutable tags. It also locks checksums for optional native CLI
downloads. The reviewed releases are Prometheus 3.13.5 (supported LTS), Grafana
13.3.0, node-exporter 1.12.1, cAdvisor 0.60.6 and kube-state-metrics 2.20.0.
Python runs only the small supplemental cgroup collector; no Python package
installation is needed. Update image digests, matching versions, checksum pins
and validation evidence together.

Prometheus pulls counters/gauges every 15 seconds and evaluates recording rules
every 30 seconds. Seven days / 5 GB block retention is the local starting point;
WAL and head data need additional disk. Grafana queries the recordings through
the `prometheus` datasource UID. The optional `loki` datasource points at
`http://loki:3100`; without that service it remains unavailable.

The standard collectors supply most measurements. The supplemental collector
reads pod-parent **cgroup v2** files for fields that standard Kubernetes
collection often omits: exact anonymous/unevictable memory, CFS burst, tasks,
PSI and block I/O. It performs no per-request scraping and writes nothing to
`/proc` or cgroups. Its network endpoint publishes an immutable cached snapshot.

## Start locally with Compose

Prerequisites: Docker Engine with Compose v2+; use a Linux host for the Linux
exporter file. A Docker Desktop/Podman VM reports the VM's Linux resources,
not macOS hardware. macOS native node-exporter has a different memory metric
schema, so Linux-specific memory panels remain unavailable there.

From the repository root:

```sh
mkdir -p artifacts/monitoring/textfile
python3 - <<'PY'
import os, pathlib, secrets
p = pathlib.Path('artifacts/monitoring/grafana-password')
if not p.exists():
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(secrets.token_urlsafe(32))
PY
# Linux Docker: install a separate secret readable by the image's UID472.
# Run from your normal shell; only install needs sudo to set numeric ownership.
sudo install -m 0400 -o 472 -g 0 \
  artifacts/monitoring/grafana-password artifacts/monitoring/grafana-password.container
export GLAMDB_GRAFANA_PASSWORD_FILE="$PWD/artifacts/monitoring/grafana-password.container"
docker network create glamdb-observability
docker compose -f deploy/monitoring/compose.yml config --quiet
docker compose -f deploy/monitoring/compose.yml up -d
```

Create the network only if it does not already exist. Grafana is at
`http://127.0.0.1:3000`, username `admin`, with the password in that file.
Prometheus is at `http://127.0.0.1:9090`. Open the `GlamDB` dashboard folder.
The original password stays caller-owned with mode0600 for operator login and
Kubernetes secret creation. The container copy is UID472/mode0400. Compose
file-backed secrets are bind mounts: `uid`, `gid` and `mode` in Compose do not
remap the host file. Do not make the file world-readable; rootless engines need
the corresponding mapped UID. Keep the containing directory trusted. Existing
Grafana data keeps its existing administrator password after file replacement.
See [Compose secret ownership](https://docs.docker.com/reference/compose-file/services/#secrets).
Both published listeners bind loopback; anonymous login and sign-up are disabled.
Use an SSH tunnel for a remote machine. Do not expose these endpoints directly
to an untrusted network.

Target files initially contain `[]`; no database or exporter is presumed to
exist. Edit the matching `deploy/monitoring/prometheus/targets/*.json` file with
actual reachable endpoints. For example, a gateway on the shared private
container network, after enabling its metrics listener:

```json
[
  {"targets": ["gateway:9464"], "labels": {"cluster": "local", "service": "glamdb"}}
]
```

Caddy must enable HTTP server `metrics: {}` and a **separate private** metrics
handler listener. The gateway metrics opt-in port is 9464. Keep Caddy's admin
API disabled. A container cannot scrape a host listener bound only to
`127.0.0.1`; use a private reachable interface with a firewall or the shared
container network. Do not change the public gateway routing just to scrape it.
File discovery refreshes automatically; send `SIGHUP` to Prometheus after
changing its main configuration. No HTTP lifecycle/admin API is enabled.

The separate logging stack joins the same `glamdb-observability` network. If
using its optional standalone Grafana override, choose that Grafana or this
one: both use local port 3000. To scrape Alloy, place its private endpoint
(`alloy:12345`) in `targets/alloy.json`, labels `cluster` and `service` as above.
To enable Loki WAL/disk checks, put `loki:3100` in `targets/loki.json`, with
`cluster: local` and `service: logging`. This is optional private discovery;
`[]` leaves it disabled. The scrape job is **`loki`**, matching the alert
selectors, and uses Loki's `/metrics` on the same private listener. Do not
expose its unauthenticated API publicly merely to scrape it. For native
Prometheus use `local_config.py --loki 127.0.0.1:3100` (or an explicitly private
reachable address). The WAL usage metric is a 0..1 fraction despite its
`_percent` suffix; no missing target is treated as healthy.
`prometheus-alerting.yml` adds the optional Alertmanager endpoint and alert
transport scrape; it does not require them for the base stack.

### Linux host and container collectors

Review the host access before starting exporters. node-exporter uses host
network/PID namespaces and a read-only host root. cAdvisor requires privileged
host access and the runtime socket to inspect container/cgroup data; the
supplied file targets Docker's Linux layout. Adjust mounts for another runtime;
do not assume rootless Podman has Docker's filesystem/socket paths.

Choose a **private host IP** reachable by Prometheus and restrict port 9100
to its source address at the host firewall. It is deliberately a required
configuration value, not an internet-facing default:

```sh
export GLAMDB_NODE_LISTEN='PRIVATE_HOST_IP:9100'
docker compose -f deploy/monitoring/compose.exporters.yml config --quiet
docker compose -f deploy/monitoring/compose.exporters.yml up -d
```

Register `PRIVATE_HOST_IP:9100` in `targets/node.json`, `cadvisor:8080` in
`targets/cadvisor.json`, and `resources:9409` in `targets/resources.json`.
Use labels `cluster`, `service` and the actual `node` for the node target.
The resource collector has no targets until you supply cgroup paths.

For an explicitly owned cgroup, inspect the workload's host PID:

```sh
cat /proc/WORKLOAD_HOST_PID/cgroup
```

On cgroup v2 the `0::/PATH` entry identifies its path under `/sys/fs/cgroup`.
Put `PATH`, without the leading slash, in `resource-targets.json`:

```json
[
  {"path": "WORKLOAD_CGROUP_PATH", "cluster": "local", "namespace": "local",
   "pod": "writer-0", "node": "ACTUAL_NODE", "service": "glamdb"}
]
```

`pod` here is an operator-assigned stable workload identity for a non-Kubernetes
deployment. Do not label a host/root cgroup as a database pod. Configure exactly
one parent per workload; duplicate identities/paths, overlapping parents and
children, absolute paths, traversal and symlink escapes are rejected.
Missing controllers emit `glamdb_cgroup_collector_success{collector=...}=0`
and omit unavailable measurements. A running exporter is not a running writer.

### Native binaries without containers

Python 3.13+, a C++23 compiler for component checks, and the explicitly listed
native platform pins are supported by the download helper:

```sh
python3 tools/monitoring/fetch_tool.py prometheus
python3 tools/monitoring/fetch_tool.py node-exporter
python3 tools/monitoring/local_config.py --node 127.0.0.1:9100 \
  --gateway 127.0.0.1:9464 --resources 127.0.0.1:9409 \
  > artifacts/monitoring/prometheus-local.json
artifacts/monitoring/bin/promtool check config artifacts/monitoring/prometheus-local.json
artifacts/monitoring/bin/prometheus \
  --config.file=artifacts/monitoring/prometheus-local.json \
  --storage.tsdb.path=artifacts/monitoring/tsdb \
  --storage.tsdb.retention.time=7d --storage.tsdb.retention.size=5GB \
  --web.listen-address=127.0.0.1:9090
```

Enable only targets actually running on that host. In separate terminals on
Linux, run `node_exporter --web.listen-address=127.0.0.1:9100
--collector.textfile.directory=ABSOLUTE_METRICS_DIRECTORY`, and
`python3 tools/monitoring/resource_exporter.py` with the configured targets.
The helper verifies archive SHA-256 and extracts only named regular binaries;
it never modifies system PATH or installs services.

## Native coordinator telemetry

The internal `glamdb_metrics` module has fixed-size atomic storage, fixed enum
labels and histograms. `CreateCollection` and `AppendDurable` instrument every
terminal return; the returned engine outcomes remain authoritative. There is
no metrics HTTP server in the core, no per-request file I/O and no transport
dependency in the installed C++ API.

The CLI owns an optional `TMetricsPublisher`. To export snapshots:

```sh
export GLAMDB_METRICS_TEXTFILE='/ABSOLUTE_METRICS_DIRECTORY/writer-0.prom'
export GLAMDB_METRICS_INSTANCE='writer-0'
```

Use a local metrics directory outside the warehouse; publication and shutdown
can wait for OS filesystem I/O, so do not place it on an unreliable remote mount.
Create that directory with write permission for the database process and read
permission for node-exporter. Use a different file and a stable, unique
`GLAMDB_METRICS_INSTANCE` for every process sharing a collector. Instance names
accept 1..64 ASCII letters, digits, dots, dashes and underscores; never use
request/operation IDs. Each snapshot carries `service_instance` (default
`core`). Multiple processes using the same instance value on one node cause
duplicate series; distinguish them before enabling the collector.

The publisher writes once at start, every 15 seconds and at graceful exit,
then renames a `.tmp` file over `.prom` in the same directory. Atomic rename
prevents partial scrapes; this telemetry is not a durable database record.
Publication errors are diagnostic and do not change commit outcomes. The
embedding application must own a publisher for its complete serving lifetime.
The short-lived store-management CLI usually exports zero append/create calls
because those commands do not invoke those operations.

`glamdb_metrics_snapshot_timestamp_seconds` is essential: files survive process
exit and node-exporter remains reachable. Gate liveness-sensitive queries on
snapshot age under 90 seconds. The dashboard and canonical operation rate apply
that gate. A recent snapshot indicates recent publication; process exit can
remain fresh until the threshold. This is not an exact live-process count or
native readiness signal.

Native metrics are:

- `glamdb_operations_total{operation,outcome}` and
  `glamdb_operation_duration_seconds` count completed **API calls**, including
  retries. `operation` is `append|create_collection`; outcomes are
  `committed|definitely_not_committed|commit_unknown|incomplete`. A committed
  retry is not a new durable publication. `incomplete` means a scope did not
  return a tracked outcome, not an invented commit disposition.
- `glamdb_phase_duration_seconds{operation,phase}` times observed coordinator
  regions. Append observes validation, binding, data, catalog_transaction and
  complete; create observes validation, data and catalog_transaction when
  reached. Validation includes admission/writer wait, data includes backend
  metadata work, and catalog_transaction includes publication/outer COMMIT.
  Fine-grained metadata/catalog_commit timings are unavailable at this seam.
- `glamdb_operation_errors_total{operation,phase,error}` maps 12 known codes
  plus `other`; no diagnostic text or identifiers are labels.
- `glamdb_operations_in_flight`, `glamdb_admission_pending_calls` and
  `glamdb_admission_owned_input_bytes` report actual scopes/reservations.
  Admission bytes exclude other Arrow/backend/control allocations. These are
  process aggregates across stores, not a per-store limit utilization ratio.
- When the structured logger is linked, `glamdb_log_*` publishes its actual
  process counters, drops by bounded reason, backlog and queue capacity through
  `GetProcessLogStats()`. Without that optional module these series are absent.

## Kubernetes

The generator emits ordinary Deployments/DaemonSets, ConfigMaps, Services,
RBAC, NetworkPolicy and PVCs; no operator or CRDs are required. It does not
apply them. Review the generated manifest and the upstream kube-state-metrics
compatibility matrix against the real cluster. The manifest passes Kubernetes
1.36 schema validation; this does not establish a working cluster deployment.

Prerequisites: an existing workload namespace, a default StorageClass (or edit
both PVCs), Linux nodes, a CNI implementing NetworkPolicy, and admission policy
allowing the explicitly reviewed host access. node-exporter uses hostNetwork
and hostPID; the optional detailed cAdvisor requires privileged access.
The ordinary resources collector only mounts host cgroups read-only. The
monitoring namespace therefore cannot be admitted under the restricted Pod
Security profile without a reviewed exception. Never weaken an unrelated
namespace's admission policy.

```sh
python3 tools/monitoring/kubernetes.py --cluster production \
  --workload-namespace glamdb > artifacts/monitoring/kubernetes.json
python3 tools/monitoring/fetch_tool.py kubeconform
artifacts/monitoring/bin/kubeconform -strict -summary \
  -kubernetes-version 1.36.0 artifacts/monitoring/kubernetes.json
kubectl create namespace glamdb-monitoring
kubectl -n glamdb-monitoring create secret generic grafana-admin \
  --from-file=password=artifacts/monitoring/grafana-password
kubectl apply --dry-run=server -f artifacts/monitoring/kubernetes.json
kubectl apply -f artifacts/monitoring/kubernetes.json
kubectl -n glamdb-monitoring rollout status deployment/prometheus
kubectl -n glamdb-monitoring rollout status deployment/grafana
kubectl -n glamdb-monitoring port-forward service/grafana 3000:3000
```

Generate the password file using the earlier command first. `--namespace`
changes the monitoring namespace; use it consistently for the secret and
port-forward commands. Services remain ClusterIP. ConfigMap changes mounted
with `subPath` need a deployment restart to load new config; recreate/restart
the affected metrics/Grafana pods after applying reviewed updates.

Label workloads `app.kubernetes.io/part-of=glamdb`. The resources DaemonSet
lists only selected pods in the workload namespace on its own node, refreshes
discovery every 60 seconds, and matches their UID to cgroupfs/systemd pod-parent
directories. It never sums parent cgroups with containers. It reports unmatched
pod inventory separately. Only selected namespaces/labels are workload targets;
the standard kubelet/node scrapes cover the actual cluster nodes.

Prometheus uses the authenticated API-server node proxy for kubelet cAdvisor
and volume stats. Its `nodes/proxy` permission is broad and sensitive; review
that service account as infrastructure privilege. TLS verification stays on.
If the platform supplies a supported restricted scrape service, replace this
discovery/proxy configuration with that service. No cluster-admin binding is
created. The resource/state readers have only namespace-scoped pod/PVC reads.

The gateway scrape discovers pods named by
`app.kubernetes.io/name=glamdb-gateway` with a declared metrics container port
9464. Bind that listener on the private pod interface. Add a metrics ingress
rule allowing only monitoring-namespace Prometheus pods on TCP 9464 to the
gateway's existing NetworkPolicy; preserve its intended application ingress
rules. The generator does not install a policy that silently blocks existing
gateway traffic. hostNetwork node-exporter also needs a node firewall rule
allowing Prometheus to port 9100; CNI enforcement for hostNetwork varies.

Add `--with-cadvisor` to render the optional detailed cAdvisor DaemonSet and
scrape job `cadvisor-detail` for schedstat/TCP/process measurements omitted by
many kubelet builds. Runtime compatibility and privileged admission must be
verified on the actual cluster. Do not run two detailed cAdvisor scrapes for
the same node under the same job labels.

## Panel definitions and prerequisites

All expressions/units are maintained in `tools/monitoring/generate.py` and
generated into three dashboard JSON files and `recording.yml`. Pod recordings
carry exactly `cluster,namespace,pod`; HTTP recordings carry `cluster,service`.
Raw exporter series retain authoritative instance/node/container/device labels.
Always give each scrape target a real `cluster` label: Prometheus
`external_labels` alone does not add a label to locally stored samples.

- **CPU / utilization:** cgroup CPU seconds/second are cores. Pod-parent
  cgroup data takes precedence over summed regular cAdvisor containers;
  sandbox/empty containers are excluded from container sums. Quota utilization
  divides by the local `cpu.max` quota/period. Unlimited parents stay absent;
  ancestors, cpusets and child quotas can constrain further. CPU requests are
  scheduling reservations, not caps. CPU-above-request is an explicitly named
  proxy using regular-container requests, not effective init/pod-level requests.
- **Burst / constrained / unconstrained wait:** burst is the real optional
  `cpu.stat burst_usec`. Constrained-wait panel shows CFS throttle time, which
  can exceed wall time across CPUs. Runqueue wait is optional cAdvisor `sched`
  `container_cpu_schedstat_runqueue_seconds_total`; it does not separate
  unconstrained wait from throttling. There is no exact generic Linux field
  for that split, and subtracting these counters would be invalid. PSI some/full
  is a separate wall-stall view, requiring `CONFIG_PSI` and enabled PSI support.
  schedstat additionally requires `CONFIG_SCHEDSTATS`, enabled
  `kernel.sched_schedstats`, and exporter permission to read process schedstats;
  enabling it changes system-wide collection overhead and is an operator choice.
- **Threads / instances:** `pids.current` counts kernel tasks (threads), with
  cAdvisor process collector as fallback. Running pods come from KSM phase;
  fresh native publishers come from actual snapshot age. Neither is DB readiness.
- **Memory / anonymous / cache / unevictable / faults:** `memory.current` and
  `memory.stat anon,file,unevictable,pgmajfault` are hierarchical cgroup v2
  values. `file` includes tmpfs/shmem. `pgmajfault` is faults/second, not disk
  IOPS. No cAdvisor RSS-to-anonymous substitution is made. On cgroup v1, the
  supplemental fields stay absent; cAdvisor total memory/CPU still provides its
  standard fallback. Working set, charged memory and process RSS differ.
- **Network I/O / TCP established / retransmits:** kubelet/cAdvisor namespace
  counters are de-duplicated with max per pod/interface, then interfaces sum.
  Known hostNetwork pods are excluded using KSM; without KSM, exclude them in
  the selection. Optional `tcp` supplies IPv4/IPv6 established gauges, and
  `advtcp` supplies `container_network_advance_tcp_stats_total`
  `tcp_state="retranssegs"`, a monotonically increasing field despite its
  upstream gauge type. Retransmits are segments/second, not loss percentage.
  Node `Tcp_CurrEstab` includes CLOSE-WAIT, unlike the exact pod established field.
- **Podset free space p100/p50:** use actual CSI/kubelet
  `kubelet_volume_stats_available_bytes/capacity_bytes`, joined through KSM pod
  mounts and immediate controller ownership. Choose each pod's least-free PVC,
  then compute p100=max (most free), p50=median and minimum=worst across pods of
  that controller (`podset=Kind/name`). ReplicaSet rollout names remain distinct.
  RWX PVCs appear for each mounting pod. Missing CSI volume stats/owners means
  No data. A rootfs limit minus container writable-layer usage is not free space
  and is deliberately not used. The node filesystem panel shows real statfs
  available capacity shared by pods; it is not a per-pod ephemeral quota.
- **Disk bandwidth / top read-write bytes and IOPS:** cgroup `io.stat`
  `rbytes,wbytes,rios,wios` (cAdvisor diskIO fallback) measures block I/O.
  Device/filesystem/writeback attribution support is required. Cached reads,
  network filesystems and object storage need their own telemetry.
- **Read/write utilization percent:** Linux exposes total device busy time,
  but no separate directional wall busy fractions. The clearly named directional
  **service-time utilization proxy** uses node disk read/write service
  seconds/second; concurrent I/O can exceed 100%. The total busy panel uses
  `node_disk_io_time_seconds_total`; modern parallel disks require latency,
  queue-depth and throughput context. Do not clamp these values or interpret a
  directional proxy as a hard throughput ceiling.
- **RPS / statuses / latency / timeouts:** gateway rates use only
  `caddy_http_request_duration_seconds_count{server="workbench",handler="subroute"}`.
  Caddy 2.11.9 observes only the outer route and labels its first handler; nested
  subroutes have no separate metrics. This includes UI, authentication failures,
  helpers and API. DB-only HTTP RPS is unavailable at this gateway seam. The
  metrics listener and any other server are excluded from the denominator. 5xx is the error ratio; 502/504 is an
  upstream-failure/timeout proxy, not an exact timeout reason. Histogram buckets
  aggregate before quantiles. Caddy's final finite duration bucket is 10 seconds;
  slower tails cannot be resolved accurately. Native deadline-exceeded errors
  are distinct actual engine codes, not HTTP timeout equivalence.

## Cardinality, sampling and operation

No URL, query, token, request ID, operation ID, collection/store ID, diagnostic
message, image tag or user-supplied label is generated per request. Native
metric storage is fixed: at most 715 base series per process before optional
logger series, independent of request volume. Zero-operation outcomes are
actual initialized counters; unobserved phases/errors remain absent. Histograms
have 15 finite buckets plus infinity, count and sum. Operation histograms
include admission and retries; process-wide aggregation must not be mistaken
for distinct commit counts.

The resource collector defaults to 100 targets (configurable 1..1000), at most
256 I/O devices per target, a 15-second snapshot and 60-second discovery.
Collection cost scales with workloads/devices, not application RPS. It rejects
excess inventory rather than truncating silently. cAdvisor process/sched/TCP
collection scans kernel data and is optional because it can be expensive.
Intrusive referenced-memory, perf and eBPF collection is not enabled.
Watch `glamdb_resource_sample_duration_seconds`, sample age, discovery failures,
unmatched pods and per-controller success; a hung sampler makes HTTP readiness
fail after three sample intervals.

Prometheus sample/label limits are guardrails, not cardinality guarantees.
Exceeding a limit fails that scrape; examine `up`, `scrape_samples_post_metric_relabeling`,
`prometheus_tsdb_head_series`, ingestion rate and disk/WAL metrics. Raw cgroup IDs
are retained where needed to distinguish real containers; restarts create
series churn. Keep service-instance names stable and retention sized for the
actual pod/container churn. Increase scrape intervals or disable detailed
collectors before increasing unmeasured overhead at high RPS.

Review Prometheus `/targets` and `/rules` first when a panel has no data. Check
the raw metric in Explore before changing an expression. A missing kernel
field cannot be repaired by substituting zero. For native data also inspect
snapshot age and node-exporter's textfile scrape errors. Do not delete or alter
database files to repair monitoring.

Stop the local stack with `docker compose -f deploy/monitoring/compose.yml down`;
named data volumes remain. Exporter teardown is its separate compose file.
Deleting monitoring volumes is an explicit operator retention decision and is
not needed to stop or upgrade the stack.

## Verification and sources

Run component, generated-artifact, PromQL and native sanitizer checks:

```sh
python3 tools/monitoring/check.py --promtool artifacts/monitoring/bin/promtool
python3 tools/monitoring/generate.py --check
cmake -S . -B build/monitoring -DGLAMDB_IO_ONLY=ON -DBUILD_TESTING=OFF
cmake --build build/monitoring --target glamdb_metrics
```

`check.py --kubeconform PATH` also validates the generated Kubernetes manifests.
`tools/monitoring/container_smoke.py` uses an explicitly selected **dedicated**
Podman connection/VM and checkout share; it creates only disposable task
containers/network, observes a real 64 MiB allocator's Linux cgroup memory and
threads, scrapes through Prometheus, provisions the actual Grafana dashboards,
queries its datasource and verifies that stopping the workload removes its
resource values while exporter `up` remains 1. The allocator is a collection
fixture and does not establish native database health. The test removes only
the resources it created; VM lifecycle is an operator responsibility.

Executed checks and environment-specific limits are recorded in
[MONITORING_VALIDATION.md](MONITORING_VALIDATION.md). Kubernetes schema tests do
not substitute for server dry-run, real scheduling, CSI/runtime compatibility
or workload tests. Native component tests do not qualify full engine durability.

Primary references used for collector and query contracts:

- [Prometheus node-exporter guide](https://prometheus.io/docs/guides/node-exporter/),
  [node-exporter host/container deployment and collectors](https://github.com/prometheus/node_exporter/tree/v1.12.1),
  [LTS policy](https://prometheus.io/docs/introduction/release-cycle/),
  [configuration and scrape limits](https://prometheus.io/docs/prometheus/latest/configuration/configuration/).
- [cAdvisor metric inventory](https://github.com/google/cadvisor/blob/v0.60.6/docs/storage/prometheus.md)
  and [actual Prometheus label/unit implementation](https://github.com/google/cadvisor/blob/v0.60.6/lib/metrics/prometheus.go).
- [Linux cgroup v2 CPU, memory, pids and I/O interfaces](https://docs.kernel.org/admin-guide/cgroup-v2.html),
  [PSI](https://docs.kernel.org/accounting/psi.html),
  [scheduler statistics](https://docs.kernel.org/scheduler/sched-stats.html),
  [disk statistics](https://docs.kernel.org/admin-guide/iostats.html).
- [kube-state-metrics compatibility](https://github.com/kubernetes/kube-state-metrics/tree/v2.20.0#compatibility-matrix),
  [pod metric definitions](https://github.com/kubernetes/kube-state-metrics/blob/v2.20.0/docs/metrics/workload/pod-metrics.md),
  [Kubernetes system metrics](https://kubernetes.io/docs/reference/instrumentation/metrics/),
  [NetworkPolicy and hostNetwork behavior](https://kubernetes.io/docs/concepts/services-networking/network-policies/).
- [Caddy metrics](https://caddyserver.com/docs/metrics),
  [Caddy 2.11.9 route-level instrumentation](https://github.com/caddyserver/caddy/blob/v2.11.9/modules/caddyhttp/routes.go),
  [nested subroute provisioning](https://github.com/caddyserver/caddy/blob/v2.11.9/modules/caddyhttp/subroute.go),
  [Grafana datasource/dashboard provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/),
  [Grafana 13.3 release](https://github.com/grafana/grafana/releases/tag/v13.3.0).

OpenTelemetry Collector can normalize/route multiple telemetry protocols, but
does not create absent kernel fields or native operation observers; adding it
here would add another queue/configuration layer without replacing these
collectors. See the [Collector architecture](https://opentelemetry.io/docs/collector/).
The present direct-scrape layout keeps each measurement's producer visible and
permits a future remote-write/collector integration without changing metric
meanings.
