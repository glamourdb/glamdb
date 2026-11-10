#!/usr/bin/env python3
"""Render reviewed PromQL definitions into provisionable Grafana dashboards/rules."""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "deploy/monitoring"
RULES = []


def record(name, expression):
    RULES.append({"record": name, "expr": expression})


POD = "cluster,namespace,pod"
CG = "sum by (" + POD + ")"
CONTAINER = '{job="cadvisor",pod!="",container!="",container!="POD"}'
for suffix, cg, cadvisor in (
    ("cpu_usage_cores:rate5m", "rate(glamdb_cgroup_cpu_usage_seconds_total[5m])", "rate(container_cpu_usage_seconds_total" + CONTAINER + "[5m])"),
    ("cpu_throttled_seconds:rate5m", "rate(glamdb_cgroup_cpu_throttled_seconds_total[5m])", "rate(container_cpu_cfs_throttled_seconds_total" + CONTAINER + "[5m])"),
    ("memory_usage_bytes", "glamdb_cgroup_memory_usage_bytes", "container_memory_usage_bytes" + CONTAINER),
    ("threads", "glamdb_cgroup_threads", 'container_threads{job=~"cadvisor|cadvisor-detail",pod!="",container!="",container!="POD"}'),
):
    record("glamdb:pod_" + suffix, f"{CG} ({cg}) or on ({POD}) {CG} ({cadvisor})")
for suffix, raw, rate in (
    ("memory_anon_bytes", "memory_anon_bytes", False),
    ("memory_file_bytes", "memory_file_bytes", False),
    ("memory_unevictable_bytes", "memory_unevictable_bytes", False),
    ("memory_major_faults:rate5m", "memory_major_faults_total", True),
    ("cpu_burst_seconds:rate5m", "cpu_burst_seconds_total", True),
):
    expression = "glamdb_cgroup_" + raw
    if rate:
        expression = f"rate({expression}[5m])"
    record("glamdb:pod_" + suffix, f"{CG} ({expression})")
record("glamdb:pod_cpu_runqueue_wait_seconds:rate5m", CG + ' (rate(container_cpu_schedstat_runqueue_seconds_total{job=~"cadvisor|cadvisor-detail",pod!="",container!="",container!="POD"}[5m]))')
record("glamdb:pod_cpu_limit_cores", CG + " (glamdb_cgroup_cpu_limit_cores)")
record("glamdb:pod_cpu_requests_cores", 'sum by (' + POD + ') (kube_pod_container_resource_requests{resource="cpu",unit="core"})')
record("glamdb:pod_cpu_above_requests_cores:rate5m", "clamp_min(glamdb:pod_cpu_usage_cores:rate5m - on (" + POD + ") glamdb:pod_cpu_requests_cores, 0)")
record("glamdb:pod_cpu_limit_utilization:ratio5m", "glamdb:pod_cpu_usage_cores:rate5m / on (" + POD + ") (glamdb:pod_cpu_limit_cores > 0)")
record("glamdb:pod_cpu_pressure_seconds:rate5m", 'sum by (' + POD + ',scope) (rate(glamdb_cgroup_pressure_stall_seconds_total{resource="cpu"}[5m]))')
for direction, word in (("read", "reads"), ("write", "writes")):
    for quantity, cadvisor_quantity in (("bytes", "bytes"), ("operations", "")):
        raw = f"glamdb_cgroup_io_{direction}_{quantity}_total"
        cadvisor = f"container_fs_{word}_{cadvisor_quantity + '_' if cadvisor_quantity else ''}total"
        record(f"glamdb:pod_io_{direction}_{quantity}:rate5m",
               f"{CG} (rate({raw}[5m])) or on ({POD}) {CG} (rate({cadvisor}{CONTAINER}[5m]))")
for direction in ("receive", "transmit"):
    # Pod network counters can be repeated on containers sharing a network namespace.
    record(f"glamdb:pod_network_{direction}_bytes:rate5m",
           f'sum by ({POD}) (max by ({POD},interface) (rate(container_network_{direction}_bytes_total{{job="cadvisor",pod!="",interface!="lo"}}[5m])))')
record("glamdb:pod_tcp_established", 'sum by (' + POD + ') (max by (' + POD + ',family) (label_replace(container_network_tcp_usage_total{job=~"cadvisor|cadvisor-detail",pod!="",tcp_state="established"},"family","4","pod",".*") or label_replace(container_network_tcp6_usage_total{job=~"cadvisor|cadvisor-detail",pod!="",tcp_state="established"},"family","6","pod",".*")))')
record("glamdb:pod_tcp_retransmits:rate5m", 'max by (' + POD + ') (rate(container_network_advance_tcp_stats_total{job=~"cadvisor|cadvisor-detail",pod!="",tcp_state="retranssegs"}[5m]))')
for rule in RULES:
    if rule["record"].startswith(("glamdb:pod_network_", "glamdb:pod_tcp_")):
        rule["expr"] = '(' + rule["expr"] + ') unless on (' + POD + ') max by (' + POD + ') (kube_pod_info{host_network="true"})'
record("glamdb:node_cpu_utilization:ratio5m", '1 - avg by (cluster,instance) (rate(node_cpu_seconds_total{mode="idle"}[5m]))')
record("glamdb:node_memory_used_bytes", "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes")
record("glamdb:node_disk_busy:ratio5m", "rate(node_disk_io_time_seconds_total[5m])")
for direction in ("read", "write"):
    record(f"glamdb:node_disk_{direction}_service_time:ratio5m", f"rate(node_disk_{direction}_time_seconds_total[5m])")
record("glamdb:podset_owner", 'max by (' + POD + ',podset) (label_join(kube_pod_owner{owner_is_controller="true"},"podset","/","owner_kind","owner_name"))')
record("glamdb:pvc_available:ratio", "max by (cluster,namespace,persistentvolumeclaim) (kubelet_volume_stats_available_bytes) / max by (cluster,namespace,persistentvolumeclaim) (kubelet_volume_stats_capacity_bytes > 0)")
record("glamdb:pod_pvc_available:ratio", 'min by (' + POD + ') (glamdb:pvc_available:ratio * on (cluster,namespace,persistentvolumeclaim) group_right () (max by (' + POD + ',persistentvolumeclaim) (kube_pod_spec_volumes_persistentvolumeclaims_info)))')
for percentile, quantile in (("p00", 0), ("p50", 0.5), ("p100", 1)):
    record(f"glamdb:podset_pvc_available:{percentile}", f"quantile by (cluster,namespace,podset) ({quantile}, glamdb:pod_pvc_available:ratio * on ({POD}) group_left (podset) glamdb:podset_owner)")
HTTP = '{job="glamdb-gateway",handler="subroute",server="workbench"}'
record("glamdb:http_requests:rate5m", f"sum by (cluster,service) (rate(caddy_http_request_duration_seconds_count{HTTP}[5m]))")
for name, code in (("errors", "5.."), ("upstream_failures", "502|504")):
    record(f"glamdb:http_{name}:rate5m", 'sum by (cluster,service) (rate(caddy_http_request_duration_seconds_count{job="glamdb-gateway",handler="subroute",server="workbench",code=~"' + code + '"}[5m])) or on (cluster,service) (glamdb:http_requests:rate5m * 0)')
record("glamdb:http_error:ratio5m", "glamdb:http_errors:rate5m / on (cluster,service) (glamdb:http_requests:rate5m > 0)")
for percentile, quantile in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
    record(f"glamdb:http_request_duration_seconds:{percentile}", f"histogram_quantile({quantile}, sum by (cluster,service,le) (rate(caddy_http_request_duration_seconds_bucket{HTTP}[5m])))")
record("glamdb:native_operations:rate5m", "sum by (cluster,service,operation,outcome) (rate(glamdb_operations_total[5m]) and on (cluster,service,instance,service_instance) (time() - glamdb_metrics_snapshot_timestamp_seconds < 90))")
record("glamdb:native_snapshot_age_seconds", "time() - glamdb_metrics_snapshot_timestamp_seconds")


def panel(title, expression, unit="short", description="", *, extra=(), stat=False):
    return {"title": title, "expr": expression, "unit": unit, "description": description,
            "extra": list(extra), "stat": stat}


S = '{cluster=~"$cluster",namespace=~"$namespace",pod=~"$pod"}'
N = '{cluster=~"$cluster",instance=~"$instance"}'
H = '{cluster=~"$cluster",service=~"$service"}'
pods = [
    panel("Live pod CPU", "sum(glamdb:pod_cpu_usage_cores:rate5m" + S + ")", "cores", "5-minute CPU seconds/second, one core = 1.", stat=True),
    panel("Charged pod memory", "sum(glamdb:pod_memory_usage_bytes" + S + ")", "bytes", "Hierarchical charged memory; not working set or RSS.", stat=True),
    panel("Anonymous pod memory", "sum(glamdb:pod_memory_anon_bytes" + S + ")", "bytes", "Exact memory.stat anon from cgroup v2. No RSS substitution.", stat=True),
    panel("Running pods (API inventory)", 'sum(kube_pod_status_phase{cluster=~"$cluster",namespace=~"$namespace",phase="Running",pod=~"$pod"} == 1)', "short", "Running phase is neither Ready nor native DB health. Not available in Compose.", stat=True),
    panel("Top pods: kernel threads", "topk(10,glamdb:pod_threads" + S + ")", "short", "pids.current counts kernel tasks, including threads. cAdvisor process collection fallback when present."),
    panel("Top pods: CPU usage", "topk(10,glamdb:pod_cpu_usage_cores:rate5m" + S + ")", "cores"),
    panel("Top pods: local quota utilization", "topk(10,glamdb:pod_cpu_limit_utilization:ratio5m" + S + ")", "percentunit", "Usage/local CPU quota. Unlimited quotas remain absent. Ancestors/cpusets can constrain further."),
    panel("Top pods: CFS bandwidth burst time", "topk(10,glamdb:pod_cpu_burst_seconds:rate5m" + S + ")", "s", "cpu.stat burst_usec/1e6, seconds/second. Requires CPU bandwidth burst kernel support and actual field; not CPU over request."),
    panel("Top pods: CPU above container requests (proxy)", "topk(10,glamdb:pod_cpu_above_requests_cores:rate5m" + S + ")", "cores", "clamp(usage minus summed regular-container CPU requests,0). Includes sidecars; pod-level/init effective requests differ. This is not CFS burst."),
    panel("Top pods: constrained CPU (throttle proxy)", "topk(10,glamdb:pod_cpu_throttled_seconds:rate5m" + S + ")", "s", "CFS throttled seconds/second, not exact wait wall time; can exceed 1 across CPUs."),
    panel("Top pods: runqueue wait (unconstrained-wait proxy)", "topk(10,glamdb:pod_cpu_runqueue_wait_seconds:rate5m" + S + ")", "s", "Optional cAdvisor sched: aggregate runnable wait seconds/second; does not isolate unconstrained wait from throttling. No exact unconstrained-wait field exists."),
    panel("CPU PSI some/full", "glamdb:pod_cpu_pressure_seconds:rate5m" + S, "percentunit", "Wall stall time, some versus all non-idle tasks. PSI prerequisites apply; not a subtraction of throttle from runqueue wait."),
    panel("Top pods: anonymous memory", "topk(10,glamdb:pod_memory_anon_bytes" + S + ")", "bytes"),
    panel("Top pods: major faults", "topk(10,glamdb:pod_memory_major_faults:rate5m" + S + ")", "ops", "memory.stat pgmajfault/second. A fault can involve cache or swap; not disk IOPS."),
    panel("Top pods: total charged memory", "topk(10,glamdb:pod_memory_usage_bytes" + S + ")", "bytes"),
    panel("Top pods: file cache (includes tmpfs/shmem)", "topk(10,glamdb:pod_memory_file_bytes" + S + ")", "bytes"),
    panel("Top pods: unevictable memory", "topk(10,glamdb:pod_memory_unevictable_bytes" + S + ")", "bytes"),
    panel("Top pods: network receive", "topk(10,glamdb:pod_network_receive_bytes:rate5m" + S + ")", "Bps", "Per-interface max prevents shared-namespace container duplication; hostNetwork pods share node traffic and must be excluded by namespace/pod selection."),
    panel("Top pods: network transmit", "topk(10,glamdb:pod_network_transmit_bytes:rate5m" + S + ")", "Bps"),
    panel("Top pods: established TCP IPv4+IPv6", "topk(10,glamdb:pod_tcp_established" + S + ")", "short", "Optional cAdvisor tcp collector, tcp_state=established; network-namespace shared, not summed per container."),
    panel("Top pods: TCP retransmitted segments", "topk(10,glamdb:pod_tcp_retransmits:rate5m" + S + ")", "pps", "Optional cAdvisor advtcp collector tcp_advance=RetransSegs; segments/s, not bytes or packet loss percentage."),
    panel("Podset PVC free: p100 + p50 + minimum", 'glamdb:podset_pvc_available:p100{cluster=~"$cluster",namespace=~"$namespace",podset=~"$podset"}', "percentunit", "Per pod choose least-free PVC ratio; quantile across pods of immediate owning controller. p100=max/most free, p50=median, minimum=worst. Shared PVC appears once per mounting pod. CSI volume stats + KSM mounts/owners required.", extra=[('glamdb:podset_pvc_available:p50{cluster=~"$cluster",namespace=~"$namespace",podset=~"$podset"}', "p50 {{podset}}"), ('glamdb:podset_pvc_available:p00{cluster=~"$cluster",namespace=~"$namespace",podset=~"$podset"}', "minimum {{podset}}")]),
    panel("Top pods: disk read bandwidth", "topk(10,glamdb:pod_io_read_bytes:rate5m" + S + ")", "Bps", "Cgroup io.stat rbytes, cAdvisor diskIO fallback; filesystem/block-device support required. Not network/object-store traffic."),
    panel("Top pods: disk write bandwidth", "topk(10,glamdb:pod_io_write_bytes:rate5m" + S + ")", "Bps"),
    panel("Top pods: disk read IOPS", "topk(10,glamdb:pod_io_read_operations:rate5m" + S + ")", "iops"),
    panel("Top pods: disk write IOPS", "topk(10,glamdb:pod_io_write_operations:rate5m" + S + ")", "iops"),
]
hosts = [
    panel("Node CPU utilization", "glamdb:node_cpu_utilization:ratio5m" + N, "percentunit", "1 minus idle fraction; includes iowait and steal, not application CPU alone."),
    panel("Node used memory (MemTotal-MemAvailable)", "glamdb:node_memory_used_bytes" + N, "bytes"),
    panel("Node anonymous memory", "node_memory_AnonPages_bytes" + N, "bytes"),
    panel("Node total RAM", "node_memory_MemTotal_bytes" + N, "bytes"),
    panel("Node file cache", "node_memory_Cached_bytes" + N + " + node_memory_SReclaimable_bytes" + N, "bytes", "Linux Cached+SReclaimable; kernel accounting differs from cgroup file."),
    panel("Node unevictable memory", "node_memory_Unevictable_bytes" + N, "bytes"),
    panel("Node network received/transmitted", "rate(node_network_receive_bytes_total" + N + "[5m])", "Bps", "Filter virtual interfaces in Explore when diagnosing a physical link; never sum all links to infer wire traffic.", extra=[("rate(node_network_transmit_bytes_total" + N + "[5m])", "transmit {{instance}} {{device}}")]),
    panel("Node established TCP", "node_netstat_Tcp_CurrEstab" + N, "short", "Linux network namespace Tcp CurrEstab counts ESTABLISHED and CLOSE-WAIT, not a pure ESTABLISHED count."),
    panel("Node TCP retransmissions", "rate(node_netstat_Tcp_RetransSegs" + N + "[5m])", "pps"),
    panel("Node filesystem free", 'node_filesystem_avail_bytes{cluster=~"$cluster",instance=~"$instance",fstype!~"tmpfs|overlay|squashfs",mountpoint=~"$mountpoint"} / node_filesystem_size_bytes{cluster=~"$cluster",instance=~"$instance",fstype!~"tmpfs|overlay|squashfs",mountpoint=~"$mountpoint"}', "percentunit", "Real statfs available space; shared node filesystem, not per-pod ephemeral-storage quota."),
    panel("Top disks: busy time", 'topk(10,glamdb:node_disk_busy:ratio5m{cluster=~"$cluster",instance=~"$instance",device=~"$device"})', "percentunit", "Linux io_ticks wall-time busy fraction. Modern parallel devices can saturate below/above assumptions; not a universal throughput limit."),
]
for direction, plural in (("read", "reads"), ("write", "writes")):
    sel = '{cluster=~"$cluster",instance=~"$instance",device=~"$device"}'
    hosts.extend([
        panel(f"Top disks: {direction} service-time utilization (proxy)", f"topk(10,glamdb:node_disk_{direction}_service_time:ratio5m" + sel + ")", "percentunit", "Directional service time/second, can exceed 100% under concurrent I/O. Linux has no independent read/write wall busy% counters; do not clamp or call this device saturation."),
        panel(f"Top disks: {direction} bytes", f"topk(10,rate(node_disk_{'read' if direction == 'read' else 'written'}_bytes_total" + sel + "[5m]))", "Bps"),
        panel(f"Top disks: {direction} IOPS", f"topk(10,rate(node_disk_{plural}_completed_total" + sel + "[5m]))", "iops"),
    ])
app = [
    panel("Gateway metrics scrape", 'up{job="glamdb-gateway",cluster=~"$cluster",service=~"$service"}', "short", "Exporter reachability only. Missing target remains NoData; 1 does not mean database ready.", stat=True),
    panel("Native snapshot age", 'glamdb:native_snapshot_age_seconds{cluster=~"$cluster",service=~"$service"}', "s", "Fresh process snapshot expected <90s. Stale files survive process exit; never interpret node-exporter up as native liveness.", stat=True),
    panel("Fresh native snapshots", 'sum((time() - glamdb_metrics_snapshot_timestamp_seconds{cluster=~"$cluster",service=~"$service"}) < bool 90)', "short", "Snapshots published in last90s; process exit can remain fresh until threshold. Not exact live-process count or native readiness.", stat=True),
    panel("Gateway all-HTTP RPS", "glamdb:http_requests:rate5m" + H, "reqps", "Exact outer workbench route; includes UI, authentication failures, helpers and API. Not DB-only RPS. Metrics listener excluded."),
    panel("Gateway all-HTTP responses by status/method", 'sum by (code,method) (rate(caddy_http_request_duration_seconds_count{job="glamdb-gateway",handler="subroute",server="workbench",cluster=~"$cluster",service=~"$service"}[5m]))', "reqps"),
    panel("HTTP error ratio", "glamdb:http_error:ratio5m" + H, "percentunit", "5xx fraction; idle traffic leaves ratio absent rather than healthy 0%."),
    panel("Upstream failures / timeout proxy", "glamdb:http_upstream_failures:rate5m" + H, "reqps", "502/504 rates; include connection errors and other upstream failures. Precise timeout reason is unavailable in Caddy metric labels; use structured logs."),
    panel("Gateway latency p50 / p95 / p99", "glamdb:http_request_duration_seconds:p50" + H, "s", "Histogram quantiles weighted by request count across instances; not averaged instance percentiles. Durations over final finite bucket10s cannot be resolved accurately.", extra=[("glamdb:http_request_duration_seconds:p95" + H, "p95 {{service}}"), ("glamdb:http_request_duration_seconds:p99" + H, "p99 {{service}}")]),
    panel("Native API calls by outcome", 'glamdb:native_operations:rate5m{cluster=~"$cluster",service=~"$service"}', "ops", "Actual CreateCollection/AppendDurable calls, including retries. Committed call count is not a count of distinct durable publications."),
    panel("Native operation p95", 'histogram_quantile(0.95,sum by (le,operation,outcome) (rate(glamdb_operation_duration_seconds_bucket{cluster=~"$cluster",service=~"$service"}[5m])))', "s"),
    panel("Native observed phase p95", 'histogram_quantile(0.95,sum by (le,operation,phase) (rate(glamdb_phase_duration_seconds_bucket{cluster=~"$cluster",service=~"$service"}[5m])))', "s", "Only observed coordinator regions. Data includes backend metadata work; validation includes admission. No invented Metadata/CatalogCommit timings."),
    panel("Native errors by bounded code/phase", 'sum by (operation,error,phase) (rate(glamdb_operation_errors_total{cluster=~"$cluster",service=~"$service"}[5m]))', "ops", "Unknown error strings map to other; no identifiers or diagnostics become labels."),
    panel("Native calls in flight", 'glamdb_operations_in_flight{cluster=~"$cluster",service=~"$service"}', "short"),
    panel("Native admission slots", 'glamdb_admission_pending_calls{cluster=~"$cluster",service=~"$service"}', "short", "Admitted append scopes, including active writer. Process aggregate across stores."),
    panel("Native admission-owned input", 'glamdb_admission_owned_input_bytes{cluster=~"$cluster",service=~"$service"}', "bytes", "Only coordinator input reservation; excludes Arrow/backend/control memory and is not total process RSS."),
    panel("Native logger drops", 'rate(glamdb_log_dropped_total{cluster=~"$cluster",service=~"$service"}[5m])', "ops", "Actual process logger counters when the logging module is linked. Unavailable module stays absent."),
    panel("Native logger backlog / capacity", 'glamdb_log_backlog{cluster=~"$cluster",service=~"$service"}', "short", "Bounded live queues including writer batches; not a durable replay backlog.", extra=[('glamdb_log_queue_capacity{cluster=~"$cluster",service=~"$service"}', "capacity {{service_instance}}")]),
    panel("Resource collection failures", 'glamdb_cgroup_collector_success{cluster=~"$cluster",namespace=~"$namespace",pod=~"$pod"}', "short", "0 means collector failed; absent resource values are not fabricated zeros."),
    panel("Resource sample age", "time() - glamdb_resource_sample_timestamp_seconds", "s"),
    panel("Prometheus series and ingestion", "prometheus_tsdb_head_series", "short", "Raw series/cardinality budget; use scrape_samples_post_metric_relabeling per target to identify changes."),
    panel("Scrape sample count", "scrape_samples_post_metric_relabeling", "short"),
]
for definition in app:
    if definition["title"] in ("Native calls in flight", "Native admission slots", "Native admission-owned input", "Native logger drops", "Native logger backlog / capacity"):
        freshness = ' and on (cluster,service,instance,service_instance) (time() - glamdb_metrics_snapshot_timestamp_seconds < 90)'
        definition["expr"] = '(' + definition["expr"] + ')' + freshness
        definition["extra"] = [('(' + expression + ')' + freshness, legend) for expression, legend in definition["extra"]]


def dashboard(uid, title, definitions):
    panels = [{"id": 1, "type": "text", "title": "Scope and missing data", "gridPos": {"x": 0, "y": 0, "w": 24, "h": 3},
               "options": {"mode": "markdown", "content": "Missing collectors, unsupported kernel fields, idle histogram quantiles, and unconfigured endpoints remain **No data**. No panel substitutes zero or infers native readiness. See `docs/development/MONITORING.md` for sources, prerequisites, proxy meanings and deployment steps."}}]
    for index, definition in enumerate(definitions):
        targets = [{"expr": definition["expr"], "legendFormat": "{{pod}}{{instance}} {{operation}} {{phase}} {{outcome}} {{device}}", "refId": "A"}]
        targets.extend({"expr": expr, "legendFormat": legend, "refId": chr(66 + i)} for i, (expr, legend) in enumerate(definition["extra"]))
        panels.append({"id": index + 2, "type": "stat" if definition["stat"] else "timeseries", "title": definition["title"],
                       "description": definition["description"], "datasource": {"type": "prometheus", "uid": "prometheus"},
                       "gridPos": {"x": index % 2 * 12, "y": 3 + index // 2 * 8, "w": 12, "h": 8},
                       "targets": targets, "fieldConfig": {"defaults": {"unit": definition["unit"], "noValue": "No data", "min": 0}, "overrides": []},
                       "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull"]}, "tooltip": {"mode": "multi"}} if not definition["stat"] else {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}})
    variables = {"cluster": "label_values(up,cluster)", "namespace": 'label_values({cluster=~"$cluster",namespace!=""},namespace)',
                 "pod": 'label_values({cluster=~"$cluster",namespace=~"$namespace",pod!=""},pod)',
                 "service": 'label_values(up{cluster=~"$cluster",service!=""},service)', "instance": 'label_values(node_uname_info{cluster=~"$cluster"},instance)',
                 "device": 'label_values(node_disk_io_time_seconds_total{cluster=~"$cluster"},device)',
                 "mountpoint": 'label_values(node_filesystem_avail_bytes{cluster=~"$cluster"},mountpoint)',
                 "podset": 'label_values(glamdb:podset_owner{cluster=~"$cluster",namespace=~"$namespace"},podset)'}
    return {"uid": uid, "title": title, "schemaVersion": 39, "version": 1, "tags": ["glamdb", "provisioned"],
            "editable": False, "timezone": "browser", "refresh": "30s", "time": {"from": "now-1h", "to": "now"},
            "annotations": {"list": []}, "panels": panels,
            "templating": {"list": [{"name": name, "type": "query", "datasource": {"uid": "prometheus", "type": "prometheus"},
                                       "query": query, "refresh": 1, "multi": True, "includeAll": True, "allValue": ".*",
                                       "current": {"text": "All", "value": "$__all"}} for name, query in variables.items()]}}


def artifacts():
    return {BASE / "prometheus/rules/recording.yml": {"groups": [{"name": "glamdb-recording", "interval": "30s", "rules": RULES}]},
            BASE / "grafana/dashboards/glamdb-pods.json": dashboard("glamdb-pods", "GlamDB · Pods and containers", pods),
            BASE / "grafana/dashboards/glamdb-nodes.json": dashboard("glamdb-nodes", "GlamDB · Linux hardware", hosts),
            BASE / "grafana/dashboards/glamdb-service.json": dashboard("glamdb-service", "GlamDB · API and coordinator", app)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for path, data in artifacts().items():
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not path.exists() or path.read_text() != content:
                raise SystemExit("generated file differs: " + str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)


if __name__ == "__main__":
    main()
